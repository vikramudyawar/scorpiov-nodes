"""
Scorpiov Wildcard Node for ComfyUI
----------------------------------------
A single node that:
  - Processes {a|b|c} inline wildcards
  - Loads __wildcard_file__ text files (searches recursively)
  - Parses and loads <lora:name:weight> tags (searches all lora subfolders)
  - Encodes text to CONDITIONING
  - Supports Random or Serial selection modes
  - Serial mode remembers position and loops
  - Exposes a /scorpiov/wildcard/refresh API endpoint for the JS Refresh button
  - Sends resolved text back to the node UI for preview display
"""

import os
import re
import random
import json
import folder_paths
import comfy.sd
import comfy.utils
from server import PromptServer
from aiohttp import web


# ─────────────────────────────────────────────
#  State: tracks serial positions per file/slot
#  Key: (node_id, slot_identifier) → line_index
# ─────────────────────────────────────────────
_serial_state: dict[str, int] = {}

# Stores the last resolved text per node_id so the JS preview can fetch it
_last_resolved: dict[str, str] = {}


def _state_key(node_id: str, identifier: str) -> str:
    return f"{node_id}::{identifier}"


def _get_serial_index(node_id: str, identifier: str, total: int, start_line: int) -> int:
    """Return the current serial index, then advance it (wrapping at total)."""
    key = _state_key(node_id, identifier)
    if key not in _serial_state:
        # First run — use start_line (convert from 1-based to 0-based, clamp)
        _serial_state[key] = max(0, min(start_line - 1, total - 1))
    idx = _serial_state[key]
    # Advance for next run
    _serial_state[key] = (idx + 1) % total
    return idx


def _reset_serial_state(node_id: str):
    """Clear all serial state for this node (called on Refresh)."""
    keys_to_del = [k for k in _serial_state if k.startswith(f"{node_id}::")]
    for k in keys_to_del:
        del _serial_state[k]
    print(f"[Scorpiov Wildcard] Serial state reset for node {node_id}")


# ─────────────────────────────────────────────
#  REST endpoint for the JS Refresh button
#  POST /scorpiov/wildcard/refresh  { "node_id": "123" }
#  → resets serial state without triggering generation
# ─────────────────────────────────────────────
@PromptServer.instance.routes.post("/scorpiov/wildcard/refresh")
async def scorpiov_refresh_endpoint(request):
    try:
        data = await request.json()
        node_id = str(data.get("node_id", ""))
        wildcard_folder = str(data.get("wildcard_folder", ""))
        if node_id:
            _reset_serial_state(node_id)
        files = _scan_wildcard_folder(wildcard_folder) if wildcard_folder else []
        return web.json_response({"status": "ok", "wildcards_found": files})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────
#  Wildcard file loading — searches recursively
# ─────────────────────────────────────────────

def _find_wildcard_file(name: str, wildcard_folder: str) -> str | None:
    """
    Search recursively under wildcard_folder for <name>.txt.
    Returns the full path if found, else None.
    """
    if not os.path.isdir(wildcard_folder):
        return None
    target = f"{name}.txt"
    for root, dirs, files in os.walk(wildcard_folder):
        # Sort for determinism
        dirs.sort()
        for fname in sorted(files):
            if fname.lower() == target.lower():
                return os.path.join(root, fname)
    return None


def _load_wildcard_file(name: str, wildcard_folder: str) -> list[str]:
    """Load lines from the wildcard file, stripping blanks."""
    path = _find_wildcard_file(name, wildcard_folder)
    if path is None:
        print(f"[Scorpiov Wildcard] WARNING: wildcard file not found: {name}.txt "
              f"(searched under {wildcard_folder})")
        return [f"__{name}__"]
    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    return lines if lines else [f"__{name}__"]


# ─────────────────────────────────────────────
#  Core wildcard processing
# ─────────────────────────────────────────────

def _pick(options: list[str], mode: str, node_id: str,
          identifier: str, start_line: int) -> str:
    """Pick one option from a list, either randomly or serially."""
    if not options:
        return ""
    if mode == "random":
        return random.choice(options)
    else:  # serial
        idx = _get_serial_index(node_id, identifier, len(options), start_line)
        return options[idx]


def process_text(
    text: str,
    wildcard_folder: str,
    mode: str,
    start_line: int,
    node_id: str,
    seed: int,
) -> str:
    """
    Full wildcard resolution pipeline:
      1. Resolve __file__ wildcards (reads .txt files, recursive search)
      2. Resolve {a|b|c} inline groups (innermost first, supports nesting)
    Both respect mode (random/serial).
    """
    if mode == "random":
        random.seed(seed)

    inline_counter = [0]

    # ── Step 1: __file__ wildcards ──────────────────────────────────────
    def replace_file_wildcard(m):
        name = m.group(1)
        lines = _load_wildcard_file(name, wildcard_folder)
        return _pick(lines, mode, node_id, f"file::{name}", start_line)

    for _ in range(10):
        new_text = re.sub(r'__([a-zA-Z0-9_\-]+)__', replace_file_wildcard, text)
        if new_text == text:
            break
        text = new_text

    # ── Step 2: {a|b|c} inline wildcards (innermost first) ─────────────
    def replace_inline_group(m):
        content = m.group(1)
        options = [o.strip() for o in content.split("|")]
        inline_counter[0] += 1
        return _pick(options, mode, node_id, f"inline::{inline_counter[0]}", start_line)

    for _ in range(20):
        new_text = re.sub(r'\{([^{}]*)\}', replace_inline_group, text)
        if new_text == text:
            break
        text = new_text

    return text


# ─────────────────────────────────────────────
#  LoRA parsing and loading
# ─────────────────────────────────────────────

def _build_lora_index() -> dict[str, str]:
    """
    Build a name→path index by recursively walking ALL lora folders
    registered with ComfyUI. This handles subfolders correctly.
    Returns a dict mapping lowercase stem → full path.
    e.g. "afrobullixl" → "/path/to/loras/artists/AfrobullIXL.safetensors"
    """
    index: dict[str, str] = {}
    lora_dirs = folder_paths.get_folder_paths("loras")
    for lora_dir in lora_dirs:
        if not os.path.isdir(lora_dir):
            continue
        for root, dirs, files in os.walk(lora_dir):
            dirs.sort()
            for fname in sorted(files):
                if fname.lower().endswith((".safetensors", ".pt", ".ckpt")):
                    full_path = os.path.join(root, fname)
                    # Index by stem (no extension), lowercase for case-insensitive match
                    stem = os.path.splitext(fname)[0].lower()
                    if stem not in index:  # first match wins (shallowest folder)
                        index[stem] = full_path
    return index


def _find_lora_path(name: str, index: dict[str, str]) -> str | None:
    """
    Find a lora by name using the pre-built index.
    Tries exact stem match first, then strips extension if provided.
    """
    # Strip extension from the name if user included it
    stem = os.path.splitext(name)[0].lower()
    return index.get(stem)


def _parse_loras(text: str) -> tuple[str, list[tuple[str, float]]]:
    """
    Extract <lora:name:weight> tags from text.
    Returns (cleaned_text, [(lora_name, weight), ...])
    Weight is used as both model_strength and clip_strength (A1111 behaviour).
    """
    lora_pattern = re.compile(r'<lora:([^:>\n]+):([0-9]*\.?[0-9]+)>')
    loras = []
    for match in lora_pattern.finditer(text):
        name = match.group(1).strip()
        weight = float(match.group(2))
        loras.append((name, weight))
    clean_text = lora_pattern.sub('', text).strip()
    clean_text = re.sub(r',\s*,', ',', clean_text)
    clean_text = re.sub(r'\s{2,}', ' ', clean_text)
    return clean_text, loras


def _load_loras(model, clip, loras: list[tuple[str, float]]):
    """
    Load a list of (lora_name, weight) onto model and clip.
    Builds a full recursive index of all lora subfolders first.
    Weight applies equally to model and clip strength (matches A1111).
    """
    if not loras:
        return model, clip

    print("[Scorpiov Wildcard] Building LoRA index (scanning all subfolders)...")
    lora_index = _build_lora_index()
    print(f"[Scorpiov Wildcard] LoRA index built: {len(lora_index)} files found.")

    for lora_name, weight in loras:
        lora_path = _find_lora_path(lora_name, lora_index)
        if lora_path is None:
            print(f"[Scorpiov Wildcard] ⚠ LoRA NOT FOUND: '{lora_name}' "
                  f"(searched {len(lora_index)} files in all lora subfolders)")
            continue
        print(f"[Scorpiov Wildcard] ✓ Loading LoRA: '{lora_name}' "
              f"@ strength {weight} → {lora_path}")
        lora_sd = comfy.utils.load_torch_file(lora_path, safe_load=True)
        # weight applies to both model_weight and clip_weight — same as A1111
        model, clip = comfy.sd.load_lora_for_models(model, clip, lora_sd, weight, weight)

    return model, clip


# ─────────────────────────────────────────────
#  Wildcard folder scanning
# ─────────────────────────────────────────────

def _scan_wildcard_folder(folder: str) -> list[str]:
    """Return sorted list of .txt stems found recursively under folder."""
    if not os.path.isdir(folder):
        return []
    results = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for fname in sorted(files):
            if fname.endswith(".txt"):
                results.append(os.path.splitext(fname)[0])
    return results


# ─────────────────────────────────────────────
#  The ComfyUI Node Class
# ─────────────────────────────────────────────

class ScorpiovWildcardProcessor:
    """
    All-in-one wildcard processor node.
    Handles: inline {a|b|c}, __file__ wildcards, <lora:x:w> loading,
    CLIP text encoding, and resolved-text preview — in one node.
    """

    DEFAULT_WILDCARD_FOLDER = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "wildcards"
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model":             ("MODEL",),
                "clip":              ("CLIP",),
                "text":              ("STRING", {
                    "multiline": True,
                    "default": "masterpiece, {girl|boy}, __hairstyles__"
                }),
                "mode":              (["random", "serial"], {"default": "random"}),
                "seed":              ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff
                }),
                "wildcard_folder":   ("STRING", {
                    "default": cls.DEFAULT_WILDCARD_FOLDER,
                    "multiline": False,
                }),
                "serial_start_line": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "Line to start from in serial mode (1 = first line). "
                               "Applies to all wildcards in this node."
                }),
            },
            # No 'refresh' input here — handled entirely by the JS button
            # which calls POST /scorpiov/wildcard/refresh without triggering generation
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES  = ("CONDITIONING", "MODEL", "CLIP", "STRING")
    RETURN_NAMES  = ("conditioning",  "model",  "clip",  "processed_text")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Prompt"
    OUTPUT_NODE   = True   # allows us to send UI data back to the frontend

    def process(
        self,
        model,
        clip,
        text: str,
        mode: str,
        seed: int,
        wildcard_folder: str,
        serial_start_line: int,
        unique_id: str = "0",
    ):
        node_id = str(unique_id)

        # ── Step 1: Resolve all wildcards ───────────────────────────────
        resolved_text = process_text(
            text=text,
            wildcard_folder=wildcard_folder,
            mode=mode,
            start_line=serial_start_line,
            node_id=node_id,
            seed=seed,
        )
        _last_resolved[node_id] = resolved_text
        print(f"[Scorpiov Wildcard] ✓ Resolved prompt:\n{resolved_text}\n")

        # ── Step 2: Extract and load LoRAs ──────────────────────────────
        clean_text, loras = _parse_loras(resolved_text)
        if loras:
            print(f"[Scorpiov Wildcard] Found {len(loras)} LoRA(s): {loras}")
            model, clip = _load_loras(model, clip, loras)
        else:
            print("[Scorpiov Wildcard] No LoRAs in prompt.")

        # ── Step 3: Encode to CONDITIONING ──────────────────────────────
        tokens = clip.tokenize(clean_text)
        conditioning = clip.encode_from_tokens_scheduled(tokens)

        # ── Step 4: Send preview text back to the node UI ───────────────
        # ComfyUI reads the "ui" key from OUTPUT_NODE returns and sends it
        # to the frontend via websocket, where our JS picks it up.
        return {
            "ui":     {"preview_text": [resolved_text]},
            "result": (conditioning, model, clip, resolved_text),
        }

    @classmethod
    def IS_CHANGED(cls, text, mode, seed, wildcard_folder,
                   serial_start_line, **kwargs):
        if mode == "random":
            return float("nan")   # always re-run in random mode
        # Serial: re-run whenever any setting changes
        return float("nan")       # serial always advances, so always re-run


# ─────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovWildcardProcessor": ScorpiovWildcardProcessor,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovWildcardProcessor": "Scorpiov Wildcard Processor 🎲",
}
