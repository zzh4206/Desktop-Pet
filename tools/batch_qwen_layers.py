"""
Automated batch layer generator using local ComfyUI + Qwen-Image-2.1.
Connects to http://127.0.0.1:8188, loads crops or full reference, sends structured
layer extraction & inpainting prompts, and saves resulting RGBA PNGs to assets/rig_young/layers/.
"""

import os
import sys
import json
import time
import urllib.request
import numpy as np
from PIL import Image

COMFY_URL = "http://127.0.0.1:8188"
COMFY_INPUT = r"D:\AI\ComfyUI\input"
SPEC_PATH = "assets/reference/young_rig_spec.json"
OUT_LAYERS_DIR = "assets/rig_young/layers"
CROPS_DIR = "assets/rig_young/prep/crops"
MANIFEST_PATH = "assets/rig_young/prep/manifest.json"

# Curated prompts for Qwen-Image-2.1 tailored to each layer
PROMPTS = {
    "ahoge": {
        "prompt": "Extract the single hair ahoge (the top curved antenna hair strand) from <image1>. Remove the white maid headpiece, background, and other hair completely. Keep only the single ahoge strand, smooth out the root connection, output as a transparent RGBA PNG image with pure transparent background.",
        "negative": "white headpiece, face, extra hair, opaque background, background, lowres"
    },
    "headpiece": {
        "prompt": "Extract the white frilled maid headdress headband from <image1>. Keep the ruffled lace edges and curved shape. Remove the blue hair, face, ahoge, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "hair, face, skin, eyes, background, lowres, blurry"
    },
    "bangs": {
        "prompt": "Extract the front bangs and short side hair flares from <image1>. Inpaint and extend the hair roots upward by 15px behind where the white headpiece was. Keep the pointed tips, hair strands, and soft blue highlights. Remove the face, eyes, eyebrows, headpiece, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "face, eyes, skin, eyebrows, headpiece, background, lowres, blurry"
    },
    "ear_fin_l": {
        "prompt": "Extract the left whale-fin ear from <image1>. Keep the dark blue fin shape and light blue lower scalloped edge. Inpaint and smoothly extend the root into where the hair was. Remove the hair, face, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "hair, face, skin, background, human ear, lowres, blurry"
    },
    "ear_fin_r": {
        "prompt": "Extract the right whale-fin ear and its dark blue ribbon bow from <image1>. Keep the distinct shapes of both the ear fin and the ribbon. Inpaint and smoothly extend the root behind the side hair. Remove the hair, face, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "hair, face, skin, background, human ear, lowres, blurry"
    },
    "pupil_l": {
        "prompt": "Extract the left eye iris, pupil, and white sparkle highlight from <image1>. Inpaint and complete the top curved arc of the iris that was hidden under the upper eyelid. Remove the sclera, eyelashes, eyelids, and skin completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "eyelash, eyelid, skin, sclera, white of eye, background, face, lowres"
    },
    "pupil_r": {
        "prompt": "Extract the right eye iris, pupil, and white sparkle highlight from <image1>. Inpaint and complete the top curved arc of the iris hidden under the upper eyelid. Keep the original tilt and highlights. Remove the sclera, eyelashes, eyelids, and skin completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "eyelash, eyelid, skin, sclera, white of eye, background, face, lowres"
    },
    "eyelid_l": {
        "prompt": "Extract the left eye upper and lower eyelid skin patches and black eyelash curve from <image1>. Keep the peach skin tone, subtle blush gradient, and crisp eyelash stroke. Remove the iris, pupil, sclera, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "iris, pupil, blue eye, eyeball, sclera, background, lowres"
    },
    "eyelid_r": {
        "prompt": "Extract the right eye upper and lower eyelid skin patches and black eyelash curve from <image1>. Keep the peach skin tone, subtle blush gradient, and crisp eyelash stroke matching the right eye angle. Remove the iris, pupil, sclera, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "iris, pupil, blue eye, eyeball, sclera, background, lowres"
    },
    "hair_back_l": {
        "prompt": "Extract the left long rear hair locks from <image1>. Keep the dark blue to light cyan gradient at the curly hair tips. Inpaint and complete the upper hair body that was covered by the ear fin, face cheek, and sleeve. Keep the see-through holes between locks transparent, do not fill into solid block. Remove the face, arm, dress, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "face, skin, arm, sleeve, dress, apron, background, solid hair block, lowres"
    },
    "hair_back_r": {
        "prompt": "Extract the right long rear hair locks from <image1>. Keep the dark blue to light cyan gradient at the curly hair tips. Inpaint and smoothly complete the hair flow behind where the whale tail and sleeve were, with continuous gradient. Remove the face, arm, dress, tail, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "face, skin, arm, sleeve, dress, tail, apron, background, lowres"
    },
    "arm_l": {
        "prompt": "Extract the left arm, puffy sleeve, and hand from <image1>. Keep the bent elbow pose. Inpaint and smoothly connect the shoulder joint into the sleeve. Keep the short round hand and cuff thickness, do not generate unfolded fingers. Remove the torso, dress, apron, and hair completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "torso, dress, apron, hair, background, face, extra fingers, lowres, blurry, stray lines"
    },
    "arm_r": {
        "prompt": "Extract the right arm, puffy sleeve, and hand from <image1>. Keep the bent elbow pose tucked in front. Inpaint and smoothly connect the right shoulder joint. Keep the short round hand and cuff decoration. Remove the torso, dress, apron, hair, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "torso, dress, apron, hair, background, face, extra fingers, lowres, blurry, stray lines"
    },
    "leg_l": {
        "prompt": "Extract the left leg, white ankle sock, and black Mary Jane shoe from <image1>. Inpaint and extend a short leg cylinder upward by 20px into where it was hidden under the skirt. Keep the shoe shape, strap, and shading. Remove the skirt, background, and other leg completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "skirt, dress, apron, background, other leg, lowres, blurry"
    },
    "leg_r": {
        "prompt": "Extract the right leg, white ankle sock, and black Mary Jane shoe from <image1>. Inpaint and extend a short leg cylinder upward by 20px into where it was hidden under the skirt. Preserve the right shoe unique perspective angle and strap. Remove the skirt, background, and other leg completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "skirt, dress, apron, background, other leg, lowres, blurry"
    },
    "apron": {
        "prompt": "Extract the white frilled bib apron from <image1>. Keep the embroidered blue whale mascot on the center and the delicate ruffled lace along all borders. Inpaint the top corners slightly behind where the sleeves were. Remove the arms, dress, legs, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "arms, sleeves, hands, navy dress, skirt, legs, background, lowres"
    },
    "tail_seg1": {
        "prompt": "Extract the root base of the whale tail from <image1> where it emerges from behind the skirt. Inpaint and smoothly extend the tail root 20px inward into where it connects behind the dress. Maintain the dark blue dorsal and light blue ventral colors with smooth shading and matching lineart. Remove the skirt, legs, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "skirt, dress, legs, background, extra branches, lowres"
    },
    "tail_seg2": {
        "prompt": "Extract the curved mid-body segment of the whale tail from <image1>. Inpaint and complete the areas covered by the right hair locks and skirt hem, keeping the curved tail width and light cyan belly stripe seamlessly continuous. Overlap 15px at both ends for skinning joint blend. Remove hair, skirt, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "hair, skirt, background, harsh seam lines, lowres"
    },
    "tail_tip": {
        "prompt": "Extract the upturned end whale tail fluke from <image1>. Preserve the pointed leaf-like fluke shape and subtle cyan gradient. Inpaint the fluke base connecting downward into the tail body. Do NOT add a second fluke leaf that is not in the original art. Remove hair, background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "hair, background, second tail fluke, lowres, blurry"
    },
    "face_base": {
        "prompt": "Extract the full face skin, forehead, cheeks, eyebrows, nose, mouth, blush, and short neck connection from <image1>. COMPLETELY CLEAR the iris, pupils, highlights, and eyelashes: inpaint the eye sockets with smooth, clean, continuous pure white sclera. Inpaint forehead skin under bangs and cheeks under side hair. Remove hair, headpiece, dress, and background completely. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "iris, pupils, blue eyes, eyelashes, headpiece, bangs, hair, background, lowres"
    },
    "torso": {
        "prompt": "Extract the upper torso maid blouse, collar, blue necktie gem, chest ruffles, and navy corset waist from <image1>. Remove the arms, hands, sleeves, and front apron completely, and inpaint the underlying navy blouse fabric and white ruffles smoothly with continuous shading. Inpaint the neck connection upward. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "arms, sleeves, hands, apron, whale embroidery, hair, background, lowres"
    },
    "skirt": {
        "prompt": "Extract the navy blue main bell skirt and white petticoat hem from <image1>. Completely remove the white apron, hands, sleeves, and whale embroidery: inpaint and complete the continuous navy skirt cloth and soft vertical folds across the entire skirt front. Preserve the gold filigree trim, white ruffled bottom hem, and side ribbon bows. Output as a clean transparent RGBA PNG image with pure transparent background.",
        "negative": "apron, whale embroidery, arms, sleeves, hands, legs, background, lowres"
    }
}


def prepare_snapped_crop(layer_id: str, manifest_entry: dict) -> tuple[str, list[int]]:
    """Crops the layer with coordinates snapped to multiples of 32."""
    im = Image.open("assets/reference/young_ref.jpg").convert("RGBA")
    w_full, h_full = im.size
    c_min_x, c_min_y, c_max_x, c_max_y = manifest_entry["crop_rect_px"]

    crop_w = c_max_x - c_min_x
    crop_h = c_max_y - c_min_y

    pad_w = (32 - (crop_w % 32)) % 32
    pad_h = (32 - (crop_h % 32)) % 32
    c_max_x = min(w_full, c_max_x + pad_w)
    c_max_y = min(h_full, c_max_y + pad_h)

    crop_w = c_max_x - c_min_x
    crop_h = c_max_y - c_min_y
    if crop_w % 32 != 0:
        c_min_x = max(0, c_min_x - (32 - (crop_w % 32)))
    if crop_h % 32 != 0:
        c_min_y = max(0, c_min_y - (32 - (crop_h % 32)))

    crop = im.crop((c_min_x, c_min_y, c_max_x, c_max_y))
    crop_filename = f"qwen_crop_{layer_id}.png"
    crop.save(os.path.join(COMFY_INPUT, crop_filename))
    return crop_filename, [c_min_x, c_min_y, c_max_x, c_max_y]


def queue_comfy_qwen(crop_filename: str, prompt_text: str, neg_prompt_text: str, prefix: str, seed: int = 20260922) -> str:
    prompt = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "qwen_image_2.1_int8_convrot.safetensors",
                "weight_dtype": "default"
            }
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "qwen3vl_8b_w4a8.safetensors",
                "type": "qwen_image",
                "device": "default"
            }
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": "qwen_image_2.1_vae_bf16.safetensors"
            }
        },
        "5": {
            "class_type": "LoadImage",
            "inputs": {
                "image": crop_filename
            }
        },
        "4": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": prompt_text,
                "negative_prompt": neg_prompt_text,
                "resolution": 0,
                "vae": ["3", 0],
                "images.image_1": ["5", 0]
            }
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["4", 1],
                "latent_image": ["4", 2],
                "seed": seed,
                "steps": 25,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0
            }
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["7", 0],
                "vae": ["3", 0]
            }
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": prefix,
                "images": ["8", 0]
            }
        }
    }

    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json"}
    )
    res = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return res["prompt_id"]


def wait_for_completion(prompt_id: str, timeout_s: int = 180) -> str:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            h = json.loads(urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10).read())
            if prompt_id in h:
                outputs = h[prompt_id].get("outputs", {})
                for out in outputs.values():
                    for img in out.get("images", []):
                        return img["filename"]
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"Generation timed out for prompt {prompt_id}")


def process_and_align_layer(layer_id: str, gen_filename: str, crop_rect_px: list[int]):
    """Loads generated crop, applies clean alpha and pastes to full canvas."""
    gen_path = os.path.join(r"D:\AI\ComfyUI\output", gen_filename)
    gen_im = Image.open(gen_path).convert("RGBA")

    # Composite into full 1280x1284 transparent canvas
    full_canvas = Image.new("RGBA", (1280, 1284), (0, 0, 0, 0))
    c_min_x, c_min_y, c_max_x, c_max_y = crop_rect_px

    # If size slightly deviates, resize to exact crop box
    if gen_im.size != (c_max_x - c_min_x, c_max_y - c_min_y):
        gen_im = gen_im.resize((c_max_x - c_min_x, c_max_y - c_min_y), Image.LANCZOS)

    full_canvas.paste(gen_im, (c_min_x, c_min_y), gen_im)

    os.makedirs(OUT_LAYERS_DIR, exist_ok=True)
    out_file = os.path.join(OUT_LAYERS_DIR, f"{layer_id}.png")
    full_canvas.save(out_file)

    # Also save the raw cropped version
    raw_crop_file = os.path.join(OUT_LAYERS_DIR, f"{layer_id}_crop.png")
    gen_im.save(raw_crop_file)

    print(f"[OK] Layer [{layer_id}] aligned and saved to {out_file}")


def generate_layer(layer_id: str):
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    entry = next((m for m in manifest if m["id"] == layer_id), None)
    if not entry:
        print(f"Error: layer {layer_id} not found in manifest")
        return

    if layer_id not in PROMPTS:
        print(f"Error: no prompt defined for {layer_id}")
        return

    print(f"\n--- Generating layer: {layer_id} ---")
    crop_filename, crop_rect = prepare_snapped_crop(layer_id, entry)
    p_info = PROMPTS[layer_id]

    prompt_id = queue_comfy_qwen(
        crop_filename=crop_filename,
        prompt_text=p_info["prompt"],
        neg_prompt_text=p_info["negative"],
        prefix=f"young_{layer_id}"
    )
    print(f"Queued in ComfyUI (ID: {prompt_id}), waiting...")
    out_name = wait_for_completion(prompt_id)
    print(f"ComfyUI output generated: {out_name}")
    process_and_align_layer(layer_id, out_name, crop_rect)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if target == "all":
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            for m in manifest:
                generate_layer(m["id"])
        else:
            generate_layer(target)
    else:
        print("Usage: python tools/batch_qwen_layers.py [layer_id | all]")
