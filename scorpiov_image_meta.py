"""
Scorpiov Image Meta Reader
--------------------------
Reads metadata embedded in a PNG image and extracts:
  - Checkpoint / model name
  - VAE name
  - LoRA names and weights
  - Positive prompt
  - Negative prompt
  - Raw metadata dump (full text)

Supports two formats:
  1. ComfyUI native  — JSON stored in the PNG "prompt" tEXt chunk
  2. A1111 / AUTOMATIC1111 — plain text stored in the PNG "parameters" tEXt chunk

The node receives the image as a file path string (from Load Image's
"filename" output, or typed manually). It reads the file directly from
disk so metadata is never lost (ComfyUI strips metadata when it converts
a PNG to a tensor).
"""

import os
import json
import re
import folder_paths
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from server import PromptServer
from aiohttp import web


# Same tag pattern used by scorpiov_lora_tag_loader.py — <lora:name:weight>
# or <lora:name:weight:clip_weight>. LoRAs applied this way (typed straight
# into prompt text and loaded by Scorpiov Lora Tag Loader) never show up as
# a LoraLoader node in the graph, so we scan resolved prompt text for tags
# directly as well.
_LORA_TAG_PATTERN = re.compile(
    r'<lora:([^:>\n]+):([0-9]*\.?[0-9]+)(?::([0-9]*\.?[0-9]+))?>', re.IGNORECASE
)


def _extract_lora_tags(text: str) -> list:
    tags = []
    for m in _LORA_TAG_PATTERN.finditer(text or ""):
        name = m.group(1).strip()
        # Strip any subfolder prefix (\ or /) before display, matching the
        # subfolder-agnostic lookup logic in scorpiov_lora_tag_loader.py.
        basename = re.split(r'[\\/]+', name)[-1]
        display_name = os.path.splitext(basename)[0]
        tags.append({"name": display_name, "weight": float(m.group(2))})
    return tags


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: resolve a filename to a full path
#  ComfyUI Load Image nodes output just the filename (e.g. "photo.png").
#  The file lives in the ComfyUI input/ folder.
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_path(filename: str) -> str | None:
    """
    Try to find the image file on disk.
    Accepts:
      - An absolute path (already complete)
      - A bare filename  → looks in ComfyUI's input folder
    Returns the full path if found, or None.
    """
    if os.path.isabs(filename) and os.path.isfile(filename):
        return filename

    # ComfyUI input folder(s)
    for input_dir in folder_paths.get_input_directory():
        candidate = os.path.join(input_dir, filename)
        if os.path.isfile(candidate):
            return candidate

    # Also try the filename directly (relative path from CWD)
    if os.path.isfile(filename):
        return os.path.abspath(filename)

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  PNG metadata extraction
# ─────────────────────────────────────────────────────────────────────────────

def _read_png_meta(path: str) -> dict:
    """
    Open a PNG with Pillow and return its tEXt/iTXt metadata as a plain dict.
    Keys are chunk keywords (e.g. "parameters", "prompt", "workflow").
    """
    try:
        img = Image.open(path)
        img.load()                        # force full decode so info is populated
        return dict(img.info or {})
    except Exception as e:
        print(f"[Scorpiov Meta] Could not open image: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  A1111 parser
#
#  Format:
#    <positive prompt text>
#    Negative prompt: <negative prompt text>
#    Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 123, Size: 512x512,
#    Model hash: abc123, Model: v1-5-pruned-emaonly, VAE hash: ..., VAE: vae.pt,
#    Lora hashes: "name: hash", ..., Version: ...
# ─────────────────────────────────────────────────────────────────────────────

def _parse_a1111(text: str) -> dict:
    result = {
        "format":          "A1111",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             text,
    }

    # Split on "Negative prompt:" to separate positive from the rest
    neg_split = re.split(r'\nNegative prompt\s*:', text, maxsplit=1, flags=re.IGNORECASE)
    result["positive_prompt"] = neg_split[0].strip()

    remainder = ""
    if len(neg_split) > 1:
        # Split the negative+params section on the first line that looks like
        # "Steps: ..." to isolate the negative prompt text
        steps_split = re.split(r'\nSteps\s*:', neg_split[1], maxsplit=1, flags=re.IGNORECASE)
        result["negative_prompt"] = steps_split[0].strip()
        remainder = ("Steps:" + steps_split[1]) if len(steps_split) > 1 else ""
    else:
        # No negative prompt — the whole second half after the first newline
        # block is the params line
        lines = text.split("\n")
        # Find the first line that starts with "Steps:"
        param_start = next(
            (i for i, l in enumerate(lines) if re.match(r'Steps\s*:', l, re.IGNORECASE)),
            None
        )
        if param_start is not None:
            result["positive_prompt"] = "\n".join(lines[:param_start]).strip()
            remainder = "\n".join(lines[param_start:])

    # Extract key: value pairs from the params line(s)
    def _grab(pattern):
        m = re.search(pattern, remainder, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    result["model"] = _grab(r'(?<!\w)Model\s*:\s*([^,\n]+)')
    result["vae"]   = _grab(r'(?<!\w)VAE\s*:\s*([^,\n]+)')

    # LoRAs in A1111 appear inline in the prompt as <lora:name:weight>
    lora_pattern = re.compile(r'<lora:([^:>]+):([0-9]*\.?[0-9]+)>', re.IGNORECASE)
    for m in lora_pattern.finditer(text):
        result["loras"].append({"name": m.group(1).strip(), "weight": float(m.group(2))})

    # Also check "Lora hashes" field for names not in prompt tags
    lora_hashes_raw = _grab(r'Lora hashes\s*:\s*"([^"]*)"')
    if lora_hashes_raw:
        for entry in lora_hashes_raw.split(","):
            name_part = entry.split(":")[0].strip()
            if name_part and not any(l["name"].lower() == name_part.lower()
                                     for l in result["loras"]):
                result["loras"].append({"name": name_part, "weight": None})

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  ComfyUI native parser
#
#  The "prompt" chunk is a JSON object whose keys are node IDs (strings).
#  Each value is: { "class_type": "...", "inputs": { ... } }
#
#  Node types we care about:
#    CheckpointLoaderSimple / CheckpointLoader  → ckpt_name
#    VAELoader                                  → vae_name
#    LoraLoader / LoraLoaderModelOnly           → lora_name, strength_model
#    CLIPTextEncode                             → text  (positive / negative)
#    KSampler / KSamplerAdvanced                → (links positive/negative)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_comfyui(json_text: str) -> dict:
    result = {
        "format":          "ComfyUI",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             json_text,
    }

    try:
        graph = json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"[Scorpiov Meta] Could not parse ComfyUI JSON: {e}")
        result["raw"] = json_text
        return result

    # ── Index all nodes by ID ─────────────────────────────────────────────
    nodes = {}   # id (str) -> node dict
    for node_id, node_data in graph.items():
        if isinstance(node_data, dict) and "class_type" in node_data:
            nodes[str(node_id)] = node_data

    # ── Helper: resolve an input that may be a [node_id, output_index] link ──
    def resolve(value):
        if isinstance(value, list) and len(value) == 2:
            ref_id, _output_idx = str(value[0]), value[1]
            return nodes.get(ref_id)
        return None

    # ── Checkpoint ────────────────────────────────────────────────────────
    CHECKPOINT_TYPES = {
        "CheckpointLoaderSimple", "CheckpointLoader",
        "unCLIPCheckpointLoader", "Checkpoint Loader (Simple)",
        "CheckpointLoaderSimpleWithNoiseSelect",
    }
    for node in nodes.values():
        if node.get("class_type") in CHECKPOINT_TYPES:
            ckpt = node.get("inputs", {}).get("ckpt_name", "")
            if ckpt:
                result["model"] = os.path.splitext(os.path.basename(ckpt))[0]
                break

    # Some workflows (e.g. UNet-only / Flux-style setups) load the model via
    # UNETLoader instead of a checkpoint node. Check this separately since it
    # uses a different field name ("unet_name" vs "ckpt_name").
    if not result["model"]:
        for node in nodes.values():
            if node.get("class_type") == "UNETLoader":
                unet = node.get("inputs", {}).get("unet_name", "")
                if unet:
                    result["model"] = os.path.splitext(os.path.basename(unet))[0]
                    break

    # ── VAE ───────────────────────────────────────────────────────────────
    VAE_TYPES = {"VAELoader", "VAEDecodeTiled", "VAEEncodeTiled"}
    for node in nodes.values():
        if node.get("class_type") == "VAELoader":
            vae = node.get("inputs", {}).get("vae_name", "")
            if vae:
                result["vae"] = os.path.splitext(os.path.basename(vae))[0]
                break

    # ── LoRAs ─────────────────────────────────────────────────────────────
    LORA_TYPES = {
        "LoraLoader", "LoraLoaderModelOnly",
        "Lora Loader", "LoRALoader",
    }
    for node in nodes.values():
        if node.get("class_type") in LORA_TYPES:
            inputs = node.get("inputs", {})
            name   = inputs.get("lora_name", "")
            weight = inputs.get("strength_model", inputs.get("strength", 1.0))
            if name:
                result["loras"].append({
                    "name":   os.path.splitext(os.path.basename(name))[0],
                    "weight": float(weight) if weight is not None else 1.0,
                })

    # ── Prompts ───────────────────────────────────────────────────────────
    # Strategy: find KSampler nodes and follow their positive/negative links
    # back to CLIPTextEncode nodes to get the actual text.
    KSAMPLER_TYPES = {
        "KSampler", "KSamplerAdvanced", "KSamplerSelect",
        "SamplerCustom", "KSampler (Efficient)",
    }
    CLIP_ENCODE_TYPES = {
        "CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeSDXLRefiner",
        "smZ CLIPTextEncode", "BNK_CLIPTextEncode",
    }

    MAX_HOPS = 8

    def resolve_value_text(value, depth=0):
        """
        A field's value can be a plain string, or a [node_id, output_index]
        link. If it's a link, follow it to the source node and ask that node
        for its text via resolve_node_text(). Keeps hopping until it bottoms
        out at real text or MAX_HOPS is hit.
        """
        if depth > MAX_HOPS or value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list) and len(value) == 2:
            source = nodes.get(str(value[0]))
            return resolve_node_text(source, depth + 1)
        return ""

    def resolve_node_text(node, depth=0):
        """
        Given a node, figure out "the text this node represents" — handling
        a few specific node types whose text doesn't live in a field simply
        called "text", plus a generic fallback for everything else.
        """
        if node is None or depth > MAX_HOPS:
            return ""
        ct = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # WAS Node Suite "Text Concatenate" — text is split across
        # text_a / text_b / text_c / text_d (+ a delimiter), not one "text"
        # field. Resolve each piece (which may itself be wired) and join
        # them in order using the node's own delimiter.
        if ct == "Text Concatenate":
            parts = []
            for key in sorted(k for k in inputs if k.startswith("text_")):
                parts.append(resolve_value_text(inputs[key], depth + 1))
            delimiter = inputs.get("delimiter", ", ")
            if not isinstance(delimiter, str):
                delimiter = ", "
            return delimiter.join(p for p in parts if p)

        # Scorpiov Lora Tag Loader — its own "text" input is itself the
        # upstream source text (it strips <lora:...> tags on its way OUT,
        # but the wired-in "text" input is the original prompt). Follow it
        # rather than treating this node as a dead end.
        if ct == "ScorpiovLoraTagLoader":
            return resolve_value_text(inputs.get("text"), depth + 1)

        # Generic fallback: try every commonly-used field name for text,
        # following further links if the field itself is wired.
        for field in ("text", "value", "string", "prompt"):
            if field in inputs:
                resolved = resolve_value_text(inputs[field], depth + 1)
                if resolved:
                    return resolved

        return ""  # node type/shape we don't recognise — give up

    def get_text_from_node(n):
        if n is None:
            return ""
        ct = n.get("class_type", "")
        if ct in CLIP_ENCODE_TYPES:
            raw = n.get("inputs", {}).get("text", "")
            return resolve_value_text(raw)
        # Some setups chain through conditioning nodes — follow one more level
        cond_input = n.get("inputs", {}).get("conditioning")
        if isinstance(cond_input, list):
            deeper = resolve(cond_input)
            if deeper and deeper.get("class_type") in CLIP_ENCODE_TYPES:
                raw = deeper.get("inputs", {}).get("text", "")
                return resolve_value_text(raw)
        return ""

    positive_texts = []
    negative_texts = []

    for node in nodes.values():
        if node.get("class_type") not in KSAMPLER_TYPES:
            continue
        inputs = node.get("inputs", {})

        pos_node = resolve(inputs.get("positive"))
        neg_node = resolve(inputs.get("negative"))

        pos_text = get_text_from_node(pos_node)
        neg_text = get_text_from_node(neg_node)

        if pos_text and pos_text not in positive_texts:
            positive_texts.append(pos_text)
        if neg_text and neg_text not in negative_texts:
            negative_texts.append(neg_text)

    # If no KSampler found, fall back: collect all CLIPTextEncode nodes
    if not positive_texts and not negative_texts:
        clip_texts = []
        for node in nodes.values():
            if node.get("class_type") in CLIP_ENCODE_TYPES:
                raw = node.get("inputs", {}).get("text", "")
                t = resolve_value_text(raw)
                if t:
                    clip_texts.append(t)
        # Heuristic: longer text = positive, shorter = negative
        clip_texts.sort(key=len, reverse=True)
        if clip_texts:
            positive_texts = [clip_texts[0]]
        if len(clip_texts) > 1:
            negative_texts = [clip_texts[1]]

    result["positive_prompt"] = "\n---\n".join(positive_texts)
    result["negative_prompt"] = "\n---\n".join(negative_texts)

    # Pick up LoRAs applied via <lora:name:weight> tags in prompt text
    # (Scorpiov Lora Tag Loader workflows) — these never appear as a
    # LoraLoader node, so they're invisible to the node-scan above.
    existing_names = {l["name"].lower() for l in result["loras"]}
    combined_text = "\n".join(positive_texts + negative_texts)
    for tag in _extract_lora_tags(combined_text):
        if tag["name"].lower() not in existing_names:
            result["loras"].append(tag)
            existing_names.add(tag["name"].lower())

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Main dispatch: detect format and parse
# ─────────────────────────────────────────────────────────────────────────────

def extract_metadata(path: str) -> dict:
    """
    Read a PNG file and return a parsed metadata dict with keys:
      format, positive_prompt, negative_prompt, model, vae, loras, raw
    """
    meta = _read_png_meta(path)

    empty = {
        "format":          "none",
        "positive_prompt": "",
        "negative_prompt": "",
        "model":           "",
        "vae":             "",
        "loras":           [],
        "raw":             "",
    }

    # ComfyUI native: has a "prompt" key containing JSON
    if "prompt" in meta and meta["prompt"]:
        raw = meta["prompt"]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        parsed = _parse_comfyui(raw)
        # Also pick up "workflow" if present (we just stuff it in raw)
        if "workflow" in meta:
            parsed["raw"] = f"=== prompt ===\n{raw}\n\n=== workflow ===\n{meta['workflow']}"
        return parsed

    # A1111: has a "parameters" key
    if "parameters" in meta and meta["parameters"]:
        raw = meta["parameters"]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return _parse_a1111(raw)

    # Nothing found
    print(f"[Scorpiov Meta] No recognised metadata found in: {path}")
    empty["raw"] = f"No metadata found.\n\nAll PNG chunks present: {list(meta.keys())}"
    return empty


# ─────────────────────────────────────────────────────────────────────────────
#  REST endpoint — called by the JS side to read metadata for display
#  POST /scorpiov/imagemeta/read   { "path": "filename.png" }
# ─────────────────────────────────────────────────────────────────────────────

@PromptServer.instance.routes.post("/scorpiov/imagemeta/read")
async def scorpiov_imagemeta_read(request):
    try:
        data     = await request.json()
        filename = data.get("path", "").strip()
        if not filename:
            return web.json_response({"status": "error", "message": "No path provided."}, status=400)

        full_path = _resolve_path(filename)
        if full_path is None:
            return web.json_response(
                {"status": "error", "message": f"File not found: {filename!r}"},
                status=404,
            )

        parsed = extract_metadata(full_path)

        # Format LoRAs
        lora_lines = []
        for l in parsed["loras"]:
            w = f"{l['weight']:.2f}" if l["weight"] is not None else "?"
            lora_lines.append(f"{l['name']} (weight: {w})")
        loras_str = "\n".join(lora_lines) if lora_lines else "(none)"
        parsed["loras_display"] = loras_str

        # Build the single combined display string for the textarea
        parsed["display_text"] = _format_ui_display(
            parsed["model"], parsed["vae"], loras_str,
            parsed["positive_prompt"], parsed["negative_prompt"], parsed["raw"],
        )

        return web.json_response({"status": "ok", "meta": parsed})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovImageMeta:
    """
    Reads PNG metadata from an image file and outputs the extracted fields
    as strings that can be wired to other nodes.

    Connect the "filename" output of a Load Image node to the "image_path"
    input here, or type a filename manually.

    Outputs:
      model           – checkpoint / model name
      vae             – VAE name
      loras           – formatted list of LoRA names and weights
      positive_prompt – full positive prompt text
      negative_prompt – full negative prompt text
      raw_metadata    – complete raw metadata dump
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_path": ("STRING", {
                    "multiline": False,
                    "default":   "",
                    "tooltip":   "Filename from a Load Image node, or a full path to a PNG file.",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES  = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES  = ("model", "vae", "loras", "positive_prompt", "negative_prompt", "raw_metadata")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Image"
    OUTPUT_NODE   = True

    def process(self, image_path: str, unique_id="0"):

        image_path = image_path.strip()

        if not image_path:
            empty = ("", "", "", "", "", "No image path provided.")
            return {"ui": {"meta_display": [_format_ui_display(*empty)]}, "result": empty}

        full_path = _resolve_path(image_path)
        if full_path is None:
            msg = f"File not found: {image_path!r}"
            print(f"[Scorpiov Meta] {msg}")
            empty = ("", "", "", "", "", msg)
            return {"ui": {"meta_display": [_format_ui_display(*empty)]}, "result": empty}

        parsed = extract_metadata(full_path)

        lora_lines = []
        for l in parsed["loras"]:
            w = f"{l['weight']:.2f}" if l["weight"] is not None else "?"
            lora_lines.append(f"{l['name']} (weight: {w})")
        loras_str = "\n".join(lora_lines) if lora_lines else "(none)"

        model    = parsed["model"]
        vae      = parsed["vae"]
        positive = parsed["positive_prompt"]
        negative = parsed["negative_prompt"]
        raw      = parsed["raw"]

        print(f"[Scorpiov Meta] Format   : {parsed['format']}")
        print(f"[Scorpiov Meta] Model    : {model}")
        print(f"[Scorpiov Meta] VAE      : {vae}")
        print(f"[Scorpiov Meta] LoRAs    : {loras_str}")
        print(f"[Scorpiov Meta] Positive : {positive[:80]}{'...' if len(positive) > 80 else ''}")
        print(f"[Scorpiov Meta] Negative : {negative[:80]}{'...' if len(negative) > 80 else ''}")

        display = _format_ui_display(model, vae, loras_str, positive, negative, raw)

        return {
            "ui":     {"meta_display": [display]},
            "result": (model, vae, loras_str, positive, negative, raw),
        }

    @classmethod
    def IS_CHANGED(cls, image_path, **kwargs):
        return image_path   # re-run whenever the path string changes


def _format_ui_display(model, vae, loras, positive, negative, raw):
    """
    Build a single formatted text block for the in-node textarea.
    Includes every piece of metadata — model, VAE, LoRAs, prompts, and
    the full raw dump so nothing (ADetailer, Hires, sampler settings…) is hidden.
    """
    sep = "─" * 60
    lines = []

    lines.append("📦 MODEL")
    lines.append(model or "(not found)")
    lines.append(sep)

    lines.append("🎨 VAE")
    lines.append(vae or "(not found)")
    lines.append(sep)

    lines.append("🔗 LoRAs")
    lines.append(loras if loras and loras != "(none)" else "(none)")
    lines.append(sep)

    lines.append("✅ POSITIVE PROMPT")
    lines.append(positive or "(empty)")
    lines.append(sep)

    lines.append("❌ NEGATIVE PROMPT")
    lines.append(negative or "(empty)")

    if raw:
        lines.append(sep)
        lines.append("📄 FULL RAW METADATA")
        lines.append(raw)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovImageMeta": ScorpiovImageMeta,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovImageMeta": "Scorpiov Image Meta Reader 🔍",
}
