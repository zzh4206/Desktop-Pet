"""
Pre-processing and crop preparation tool for Desktop Pet 2D Rigging.
Reads young_rig_spec.json and young_ref.jpg, extracts bounding box crops with context margins,
and generates a visual debug overlay of all 22 layers.
"""

import os
import json
from PIL import Image, ImageDraw, ImageFont


def prep_layers(spec_path: str, ref_image_path: str, out_dir: str):
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    im = Image.open(ref_image_path).convert("RGBA")
    w, h = im.size

    os.makedirs(out_dir, exist_ok=True)
    crops_dir = os.path.join(out_dir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    final_layers_dir = os.path.join(out_dir, "layers")
    os.makedirs(final_layers_dir, exist_ok=True)

    debug_img = im.copy()
    draw = ImageDraw.Draw(debug_img)

    manifest = []

    for layer in spec["layers"]:
        layer_id = layer["id"]
        bbox = layer["bbox_hint"]  # [xmin, ymin, xmax, ymax]
        xmin, ymin, xmax, ymax = bbox

        # Convert to pixel coordinates
        px_min = int(xmin * w)
        py_min = int(ymin * h)
        px_max = int(xmax * w)
        py_max = int(ymax * h)

        # Add 12% padding for context inpainting
        pad_x = int((px_max - px_min) * 0.12)
        pad_y = int((py_max - py_min) * 0.12)

        c_px_min = max(0, px_min - pad_x)
        c_py_min = max(0, py_min - pad_y)
        c_px_max = min(w, px_max + pad_x)
        c_py_max = min(h, py_max + pad_y)

        # Crop context
        crop = im.crop((c_px_min, c_py_min, c_px_max, c_py_max))
        crop_filename = f"{layer_id}_crop.png"
        crop_path = os.path.join(crops_dir, crop_filename)
        crop.save(crop_path)

        # Draw bbox on debug overlay
        draw.rectangle([px_min, py_min, px_max, py_max], outline="magenta", width=2)
        draw.text((px_min + 4, py_min + 4), f"{layer['z_order']}: {layer_id}", fill="yellow")

        manifest.append({
            "id": layer_id,
            "z_order": layer["z_order"],
            "description": layer["description"],
            "bbox_norm": bbox,
            "crop_rect_px": [c_px_min, c_py_min, c_px_max, c_py_max],
            "crop_file": crop_filename,
            "requires_inpaint": layer["requires_inpaint"],
            "inpaint_targets": layer.get("inpaint_targets", []),
            "self_completion_guide": layer.get("self_completion_guide", ""),
            "inpaint_guide": layer.get("inpaint_guide", ""),
            "target_layer_png": f"assets/rig_young/layers/{layer_id}.png"
        })

    debug_path = os.path.join(out_dir, "all_layers_bboxes.png")
    debug_img.save(debug_path)

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Prepared {len(manifest)} layers in {out_dir}")
    print(f"Debug overlay saved to {debug_path}")


if __name__ == "__main__":
    spec = "assets/reference/young_rig_spec.json"
    ref = "assets/reference/young_ref.jpg"
    out = "assets/rig_young/prep"
    prep_layers(spec, ref, out)
