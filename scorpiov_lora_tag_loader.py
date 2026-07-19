"""
Scorpiov Lora Tag Loader for ComfyUI
----------------------------------------
Reads <lora:name:weight> or <lora:name:weight:clip_weight> tags out of a text
prompt, loads each named LoRA onto the given model/clip, strips the tags from
the text, and outputs the merged MODEL, CLIP, and the cleaned STRING (ready
to wire into a standard CLIPTextEncode node).

Tag formats supported:
  <lora:name:0.8>       -> model_weight = clip_weight = 0.8
  <lora:name:0.8:0.6>   -> model_weight = 0.8, clip_weight = 0.6

Also outputs a "loras_info" string formatted as "name (weight: 0.8)" per
line -- wire this straight into Scorpiov Save Image's "loras" input so
Civitai-compatible metadata (Lora hashes line + <lora:...> badge tags) gets
embedded in the saved PNG automatically.

Same LoRA search/index logic as scorpiov_wildcard.py: searches every folder
registered under ComfyUI's "loras" folder_paths entry, matched by filename
stem (case-insensitive), so subfolders "just work".
"""

import os
import re
import folder_paths
import comfy.sd
import comfy.utils
from .scorpiov_hash_utils import get_autov2_hash


# ─────────────────────────────────────────────────────────────────────────────
#  LoRA parsing and loading
# ─────────────────────────────────────────────────────────────────────────────

_TAG_PATTERN = re.compile(
    r'<lora:([^:>\n]+):([0-9]*\.?[0-9]+)(?::([0-9]*\.?[0-9]+))?>'
)


def _build_lora_index():
    index = {}
    for lora_dir in folder_paths.get_folder_paths("loras"):
        if not os.path.isdir(lora_dir):
            continue
        for root, dirs, files in os.walk(lora_dir):
            dirs.sort()
            for fname in sorted(files):
                if fname.lower().endswith((".safetensors", ".pt", ".ckpt")):
                    stem = os.path.splitext(fname)[0].lower()
                    if stem not in index:
                        index[stem] = os.path.join(root, fname)
    return index


def _parse_loras(text):
    """
    Returns (clean_text, loras) where loras is a list of
    (name, model_weight, clip_weight) tuples.
    """
    loras = []
    for m in _TAG_PATTERN.finditer(text):
        name         = m.group(1).strip()
        model_weight = float(m.group(2))
        clip_weight  = float(m.group(3)) if m.group(3) is not None else model_weight
        loras.append((name, model_weight, clip_weight))

    clean = _TAG_PATTERN.sub('', text).strip()
    clean = re.sub(r',\s*,', ',', clean)
    clean = re.sub(r'\s{2,}', ' ', clean)
    return clean, loras


def _lora_lookup_key(name):
    """
    Returns the LoRA index lookup key for a name taken from a <lora:...> tag.
    Tags commonly include a subfolder prefix, e.g. <lora:Anima\\sshelen:0.8>
    or <lora:Anima/sshelen:0.8> -- but _build_lora_index() keys files by
    filename stem only (subfolder-agnostic), matching how the rest of this
    package finds files. Strip any subfolder prefix (either slash style)
    before taking the stem, or a tag like that would silently fail to match
    and the LoRA would be skipped without any visible error.
    """
    basename = re.split(r'[\\/]+', name)[-1]
    return os.path.splitext(basename)[0].lower()


def _resolve_loras(loras):
    """
    Given a list of (name, model_weight, clip_weight) tuples parsed from tags,
    look up each LoRA's file path and AutoV2 hash once. Returns a list of
    dicts: {name, model_weight, clip_weight, path, hash}.
    A lora that can't be found on disk gets path=None, hash="".
    """
    if not loras:
        return []
    print("[Scorpiov Lora Tag Loader] Building LoRA index...")
    lora_index = _build_lora_index()
    print(f"[Scorpiov Lora Tag Loader] {len(lora_index)} LoRA files indexed.")

    resolved = []
    for name, model_weight, clip_weight in loras:
        stem = _lora_lookup_key(name)
        path = lora_index.get(stem)
        if path is None:
            print(f"[Scorpiov Lora Tag Loader] WARNING: LoRA not found: '{name}'")
            resolved.append({
                "name": name, "model_weight": model_weight,
                "clip_weight": clip_weight, "path": None, "hash": "",
            })
            continue
        file_hash = get_autov2_hash(path)
        resolved.append({
            "name": name, "model_weight": model_weight,
            "clip_weight": clip_weight, "path": path, "hash": file_hash,
        })
    return resolved


def _load_loras(model, clip, resolved):
    for entry in resolved:
        if entry["path"] is None:
            continue
        print(f"[Scorpiov Lora Tag Loader] Loading LoRA '{entry['name']}' "
              f"@ model={entry['model_weight']}, clip={entry['clip_weight']} "
              f"(hash: {entry['hash'] or 'unknown'})")
        lora_sd = comfy.utils.load_torch_file(entry["path"], safe_load=True)
        model, clip = comfy.sd.load_lora_for_models(
            model, clip, lora_sd, entry["model_weight"], entry["clip_weight"]
        )
    return model, clip


# ─────────────────────────────────────────────────────────────────────────────
#  The Node
# ─────────────────────────────────────────────────────────────────────────────

class ScorpiovLoraTagLoader:
    """
    Extracts <lora:name:weight[:clip_weight]> tags from text, loads them onto
    model/clip, and outputs the merged model/clip plus the tag-stripped text.
    Wire the stripped text into a CLIPTextEncode node afterward.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip":  ("CLIP",),
                "text":  ("STRING", {
                    "multiline":   True,
                    "forceInput":  True,
                    "tooltip":     "Text containing <lora:name:weight> tags, "
                                   "e.g. from Wildcard Prompter.",
                }),
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING")
    RETURN_NAMES = ("model",  "clip",  "text",   "loras_info")
    FUNCTION     = "load"
    CATEGORY     = "Scorpiov/Loaders"
    OUTPUT_NODE  = True

    def load(self, model, clip, text):
        clean_text, loras = _parse_loras(text)

        resolved = _resolve_loras(loras)
        if resolved:
            model, clip = _load_loras(model, clip, resolved)
        else:
            print("[Scorpiov Lora Tag Loader] No <lora:...> tags found in text.")

        # Format for Scorpiov Save Image's "loras" field, one per line:
        #   "name (weight: 0.8) [hash: 6ce0161689]"
        # This is the exact format _parse_loras() in scorpiov_save_image.py
        # expects, so it can build the real Civitai "Lora hashes:" metadata
        # line (not just a placeholder) and re-embed the <lora:...> tag in
        # the saved prompt.
        info_lines = []
        for entry in resolved:
            line = f"{entry['name']} (weight: {entry['model_weight']})"
            if entry["hash"]:
                line += f" [hash: {entry['hash']}]"
            info_lines.append(line)
        loras_info = "\n".join(info_lines)

        return {
            "ui":     {"preview_text": [clean_text]},
            "result": (model, clip, clean_text, loras_info),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovLoraTagLoader": ScorpiovLoraTagLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovLoraTagLoader": "Scorpiov Lora Tag Loader 🏷️",
}
