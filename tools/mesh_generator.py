"""
Alpha-covering Grid Mesh Generator for Desktop Pet Skeletal Skinning.
Generates gap-free triangle meshes and calculates bone weights for rig layers
based on young_rig_spec.json and transparent layer PNGs.
Outputs assets/rig_young/mesh/mesh_data.json matching SkinnedMeshItem contract.
"""

from __future__ import annotations

import json
import os
import sys
import numpy as np
from PIL import Image
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

    # Mesh cells cover the alpha support, including antialiased edges. Sampling
    # only opaque contour pixels and dropping long triangles cuts holes in art.
    im = Image.open(png_path).convert("RGBA")
    tw, th = im.size
    w_img, h_img = img_size
    # P1（纹理 trim）：裁透明边后 (tw,th) 是裁后尺寸，trim_offset_px 是裁剪区
    # 在源图坐标的左上角。顶点/骨骼活在源图像素空间（scale=1.0 + 平移），
    # uv 用纹理自身归一化；瞳层 uv 需减去 trim_offset 再除以裁后尺寸。
    trim_off = layer_spec.get("trim_offset_px", [0, 0])
    trim_off_x, trim_off_y = float(trim_off[0]), float(trim_off[1])
    alpha = np.asarray(im)[:, :, 3]
    mask = alpha > 0
    if layer_spec.get("largest_component"):
        labels, count = ndimage.label(alpha > 15)
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        mask = ndimage.binary_dilation(labels == sizes.argmax(), iterations=2)
    ys, xs = np.where(mask)
    if not len(xs):
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    target = layer_spec.get("target_bbox_px")
    if target:
        # Register generated content through geometry, preserving the RGBA file.
        sy, sx = np.where(alpha > 15)
        bx0, bx1, by0, by1 = sx.min(), sx.max() + 1, sy.min(), sy.max() + 1
        scale_x = (target[2] - target[0]) / (bx1 - bx0)
        scale_y = (target[3] - target[1]) / (by1 - by0)
        off_x, off_y = target[0] - bx0 * scale_x, target[1] - by0 * scale_y
    else:
        # 裁后纹理像素 → 源图像素 = +trim_offset；scale 恒 1.0。
        scale_x, scale_y = 1.0, 1.0
        off_x, off_y = trim_off_x, trim_off_y
    off_x += layer_spec.get("offset_px", [0, 0])[0]
    off_y += layer_spec.get("offset_px", [0, 0])[1]
    step = layer_spec.get("grid_step", grid_step)
    gx = sorted(set([max(0, x0 - 2), min(tw, x1 + 2)] + list(range(x0, x1, max(2, int(step / scale_x))))))
    gy = sorted(set([max(0, y0 - 2), min(th, y1 + 2)] + list(range(y0, y1, max(2, int(step / scale_y))))))
    # Add exact eye-opening boundaries so full closure cannot leave white slivers.
    for zone in layer_spec.get("blink_zones", []):
        for value in [zone[1] - 35, zone[1], zone[2], zone[2] + 35]:
            ty = (value - off_y) / scale_y
            if gy[0] < ty < gy[-1]:
                gy.append(ty)
    gy = sorted(set(gy))
    points, lookup, triangles = [], {}, []
    def vertex(x, y):
        key = (x, y)
        if key not in lookup:
            lookup[key] = len(points)
            points.append(key)
        return lookup[key]
    for ya, yb in zip(gy, gy[1:]):
        for xa, xb in zip(gx, gx[1:]):
            if not mask[int(ya):int(np.ceil(yb)), int(xa):int(np.ceil(xb))].any():
                continue
            ids = [vertex(xa, ya), vertex(xb, ya), vertex(xb, yb), vertex(xa, yb)]
            triangles.extend([ids[0], ids[1], ids[2], ids[0], ids[2], ids[3]])
    vertices = [[round(x * scale_x + off_x, 4), round(y * scale_y + off_y, 4)] for x, y in points]
    uvs = [[x / tw, y / th] for x, y in points]
    pts_arr = np.array(vertices)
    influence_bones = layer_spec.get("influence_bones", [layer_spec["bind_bone"]])
    bones_dict = {b["bone_name"]: b for b in skeleton_spec["bones"]}

    if layer_spec.get("gaze_ellipse"):
        # The pupil is sampled through the fixed sclera silhouette. Moving UVs
        # looks around without drawing iris pixels over the surrounding skin.
        cx, cy, rx, ry = layer_spec["gaze_ellipse"]
        count = 48
        points = [[cx, cy]] + [[cx + rx * np.cos(t), cy + ry * np.sin(t)]
                              for t in np.linspace(0, 2 * np.pi, count, endpoint=False)]
        vertices = [[round(x, 4), round(y, 4)] for x, y in points]
        uvs = [[(x - trim_off_x) / tw, (y - trim_off_y) / th] for x, y in points]
        triangles = [idx for j in range(count) for idx in (0, 1 + j, 1 + (j + 1) % count)]
        pts_arr = np.asarray(vertices)

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

    lock = layer_spec.get("root_lock")
    if lock:
        root = lock["bone"]
        t = np.clip((pts_arr[:, 1] - lock["full_before_y"]) /
                    (lock["free_after_y"] - lock["full_before_y"]), 0, 1)
        t = t * t * (3 - 2 * t)
        for index, blend in enumerate(t):
            weights = {b: w * blend for b, w in zip(weight_bones[index], weight_values[index])}
            weights[root] = weights.get(root, 0) + 1 - blend
            weight_bones[index] = list(weights)
            weight_values[index] = [round(v, 6) for v in weights.values()]

    blink_delta = np.zeros_like(pts_arr)
    for cx, top, bottom, radius in layer_spec.get("blink_zones", []):
        x, y = pts_arr[:, 0], pts_arr[:, 1]
        closure = top + (bottom - top) * 0.78
        mapped = np.interp(y, [top - 16, top, bottom, bottom + 16],
                           [top - 16, closure, closure, bottom + 16])
        mapped = np.where((y < top - 16) | (y > bottom + 16), y, mapped)
        # A slight downward arc and retained stroke thickness read as a closed eye.
        curve = 6 * np.clip(1 - ((x - cx) / radius) ** 2, 0, 1)
        ramp = np.interp(y, [top - 16, top, bottom, bottom + 16], [0, 1, 1, 0])
        mapped += curve * ramp
        if layer_id.startswith("eyelid"):
            mapped = closure + curve + (y - top) * 0.25
            blend = np.ones_like(x)
        else:
            blend = np.clip((radius + 25 - abs(x - cx)) / 25, 0, 1)
        blink_delta[:, 1] += (mapped - y) * blend
    blink_delta = np.round(blink_delta, 4).tolist()

    return {
        "id": layer_id,
        "texture": layer_spec.get("texture", f"{layer_id}.png"),
        "trim_offset_px": [trim_off_x, trim_off_y],   # 便于回算；运行时不消费
        "blink_delta": blink_delta if layer_spec.get("blink_zones") else None,
        "gaze_uv": bool(layer_spec.get("gaze_ellipse")),
        "texture_size_px": [tw, th],
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
        png_path = os.path.join(layers_dir, l.get("texture", f"{lid}.png"))
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
    spec = "assets/rig_young/spec.json"
    layers = "assets/rig_young/layers"
    out = "assets/rig_young/mesh/mesh_data.json"
    generate_all_meshes(spec, layers, out)
