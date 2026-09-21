"""
2D Delaunay Mesh Generator for Desktop Pet Skeletal Skinning.
Generates adaptive triangle meshes and calculates bone weights for all 22 layers
based on young_rig_spec.json and transparent layer PNGs.
Outputs assets/rig_young/mesh/mesh_data.json matching SkinnedMeshItem contract.
"""

from __future__ import annotations

import json
import os
import sys
import numpy as np
from PIL import Image
from scipy.spatial import Delaunay
from scipy import ndimage


def dist_point_to_segment(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance from points p (N, 2) to segment ab (2,)."""
    ab = b - a
    ab_len_sq = np.dot(ab, ab)
    if ab_len_sq < 1e-6:
        return np.linalg.norm(p - a, axis=1)
    t = np.clip(np.sum((p - a) * ab, axis=1) / ab_len_sq, 0.0, 1.0)
    projection = a + t[:, None] * ab
    return np.linalg.norm(p - projection, axis=1)


def generate_layer_mesh(
    layer_spec: dict,
    skeleton_spec: dict,
    png_path: str,
    img_size: tuple[int, int] = (1280, 1284),
    grid_step: int = 28
) -> dict | None:
    layer_id = layer_spec["id"]
    if not os.path.exists(png_path):
        print(f"Warning: {png_path} not found, skipping layer {layer_id}")
        return None

    im = Image.open(png_path).convert("RGBA")
    w_img, h_img = img_size
    if im.size != img_size:
        im = im.resize(img_size, Image.LANCZOS)

    arr = np.array(im)
    alpha = arr[:, :, 3]

    # Find active region
    mask = alpha > 15
    ys, xs = np.where(mask)
    if len(xs) == 0:
        print(f"Warning: Layer {layer_id} is completely transparent!")
        return None

    x0, x1 = xs.min(), xs.max()
    y0, y1 = ys.min(), ys.max()

    # Influence bones for skinning
    influence_bones = layer_spec.get("influence_bones", [layer_spec.get("bind_bone")])
    is_multi_bone = len(influence_bones) > 1

    # Bones dictionary for joint positions
    bones_dict = {b["bone_name"]: b for b in skeleton_spec["bones"]}

    # Rigid small parts (eyes, ahoge, eyelids, headpiece) or simple parts
    is_simple_part = layer_id in ["pupil_l", "pupil_r", "eyelid_l", "eyelid_r", "headpiece", "ahoge"]

    vertices: list[list[float]] = []
    triangles: list[int] = []

    if is_simple_part or not is_multi_bone:
        # Simple bounding quad or 2x2 grid
        step_x = max(16, (x1 - x0) // 2)
        step_y = max(16, (y1 - y0) // 2)
        gx = list(range(x0, x1 + 1, step_x))
        if gx[-1] < x1:
            gx.append(x1)
        gy = list(range(y0, y1 + 1, step_y))
        if gy[-1] < y1:
            gy.append(y1)

        pts = []
        for y in gy:
            for x in gx:
                pts.append([float(x), float(y)])
        pts = np.array(pts)

        if len(pts) >= 4:
            tri = Delaunay(pts)
            # Keep all triangles for simple parts
            for simplex in tri.simplices:
                triangles.extend([int(idx) for idx in simplex])
            vertices = [[round(p[0], 2), round(p[1], 2)] for p in pts]
    else:
        # Organic / deformable part: adaptive interior grid + boundary points
        # 1. Grid sample interior points
        gx = np.arange(x0 + grid_step // 2, x1, grid_step)
        gy = np.arange(y0 + grid_step // 2, y1, grid_step)
        grid_pts = []
        for y in gy:
            for x in gx:
                if mask[int(y), int(x)]:
                    grid_pts.append([float(x), float(y)])

        # 2. Extract contour boundary points using morphological edge
        eroded = ndimage.binary_erosion(mask, structure=np.ones((3, 3)))
        boundary_mask = mask & (~eroded)
        by, bx = np.where(boundary_mask)

        # Subsample boundary points to avoid excessive density
        stride = max(1, len(bx) // 80)
        b_pts = [[float(bx[i]), float(by[i])] for i in range(0, len(bx), stride)]

        all_pts = np.array(grid_pts + b_pts)
        if len(all_pts) < 4:
            # Fallback to bbox quad
            all_pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)

        tri = Delaunay(all_pts)

        # Filter triangles: centroid must be inside mask and edges cannot be too long
        valid_triangles = []
        for simplex in tri.simplices:
            p0, p1, p2 = all_pts[simplex[0]], all_pts[simplex[1]], all_pts[simplex[2]]
            cx = (p0[0] + p1[0] + p2[0]) / 3.0
            cy = (p0[1] + p1[1] + p2[1]) / 3.0
            
            # Check edge lengths (prevent spanning large concave gaps)
            edge_lens = [
                np.linalg.norm(p0 - p1),
                np.linalg.norm(p1 - p2),
                np.linalg.norm(p2 - p0)
            ]
            if max(edge_lens) > grid_step * 3.5:
                continue

            if 0 <= int(cy) < h_img and 0 <= int(cx) < w_img:
                if mask[int(cy), int(cx)]:
                    valid_triangles.append(simplex)

        # Compact vertices (only keep used vertices)
        used_indices = sorted(list(set(idx for simplex in valid_triangles for idx in simplex)))
        old_to_new = {old: new for new, old in enumerate(used_indices)}

        compact_pts = all_pts[used_indices]
        for simplex in valid_triangles:
            triangles.extend([old_to_new[simplex[0]], old_to_new[simplex[1]], old_to_new[simplex[2]]])

        vertices = [[round(p[0], 2), round(p[1], 2)] for p in compact_pts]

    if not triangles or len(vertices) == 0:
        # Ultimate fallback to quad
        vertices = [[float(x0), float(y0)], [float(x1), float(y0)], [float(x1), float(y1)], [float(x0), float(y1)]]
        triangles = [0, 1, 2, 0, 2, 3]

    pts_arr = np.array(vertices)

    # UV coordinates (normalized to 1280x1284)
    uvs = [[round(x / w_img, 6), round(y / h_img, 6)] for x, y in vertices]

    # Calculate bone weights for each vertex
    weight_bones: list[list[str]] = []
    weight_values: list[list[float]] = []

    if len(influence_bones) == 1:
        single_bone = influence_bones[0]
        weight_bones = [[single_bone] for _ in range(len(vertices))]
        weight_values = [[1.0] for _ in range(len(vertices))]
    else:
        # Multi-bone weighting
        # Collect bone joint positions and segments
        bone_anchors = []
        for bname in influence_bones:
            if bname in bones_dict:
                b_info = bones_dict[bname]
                j_pos = np.array([b_info["joint_pos"][0] * w_img, b_info["joint_pos"][1] * h_img])
                # Check for child in influence set to form a segment
                child = next((b for b in skeleton_spec["bones"] if b.get("parent") == bname and b["bone_name"] in influence_bones), None)
                if child:
                    c_pos = np.array([child["joint_pos"][0] * w_img, child["joint_pos"][1] * h_img])
                    bone_anchors.append((bname, j_pos, c_pos))
                else:
                    bone_anchors.append((bname, j_pos, None))
            else:
                bone_anchors.append((bname, np.array([w_img / 2, h_img / 2]), None))

        # Calculate distances for all vertices
        for pt in pts_arr:
            dists = []
            for bname, j_pos, c_pos in bone_anchors:
                if c_pos is not None:
                    d = dist_point_to_segment(pt[None, :], j_pos, c_pos)[0]
                else:
                    d = np.linalg.norm(pt - j_pos)
                dists.append(d)

            dists = np.array(dists)
            # Softmax / inverse power distance with smooth radius
            # Power = 2.0, epsilon = 25px
            weights = 1.0 / (np.maximum(dists, 1.0) + 25.0) ** 2.2
            # Normalize
            weights = weights / np.sum(weights)

            # Top 4 bones maximum for performance
            top_indices = np.argsort(weights)[::-1][:4]
            top_bones = [influence_bones[i] for i in top_indices if weights[i] > 0.02]
            top_weights = [float(weights[i]) for i in top_indices if weights[i] > 0.02]
            
            # Re-normalize top weights
            sum_w = sum(top_weights)
            if sum_w > 0:
                top_weights = [round(w / sum_w, 4) for w in top_weights]
            else:
                top_bones = [influence_bones[0]]
                top_weights = [1.0]

            weight_bones.append(top_bones)
            weight_values.append(top_weights)

    return {
        "id": layer_id,
        "texture": f"{layer_id}.png",
        "z_order": layer_spec["z_order"],
        "vertices": vertices,
        "uvs": uvs,
        "triangles": triangles,
        "weight_bones": weight_bones,
        "weight_values": weight_values
    }


def generate_all_meshes(spec_path: str, layers_dir: str, out_mesh_path: str):
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    img_w, img_h = spec["skeleton"]["source_reference"]["image_size_px"]
    layers_spec = sorted(spec["layers"], key=lambda x: x["z_order"])

    out_layers = []
    total_verts = 0
    total_tris = 0

    print(f"Generating mesh data for {len(layers_spec)} layers...")

    for l in layers_spec:
        lid = l["id"]
        png_path = os.path.join(layers_dir, f"{lid}.png")
        mesh_layer = generate_layer_mesh(l, spec["skeleton"], png_path, (img_w, img_h))
        if mesh_layer:
            out_layers.append(mesh_layer)
            nv = len(mesh_layer["vertices"])
            nt = len(mesh_layer["triangles"]) // 3
            total_verts += nv
            total_tris += nt
            print(f"  [OK] {lid:14s}: {nv:4d} vertices, {nt:4d} triangles, bones: {l['influence_bones']}")

    mesh_data = {
        "spec": 1,
        "image_size_px": [img_w, img_h],
        "layers": out_layers
    }

    os.makedirs(os.path.dirname(out_mesh_path), exist_ok=True)
    with open(out_mesh_path, "w", encoding="utf-8") as f:
        json.dump(mesh_data, f, indent=2)

    print(f"\n[DONE] Generated {len(out_layers)} layers, total {total_verts} vertices, {total_tris} triangles.")
    print(f"Mesh data saved to: {out_mesh_path}")


if __name__ == "__main__":
    spec = "assets/reference/young_rig_spec.json"
    layers = "assets/rig_young/layers"
    out = "assets/rig_young/mesh/mesh_data.json"
    generate_all_meshes(spec, layers, out)
