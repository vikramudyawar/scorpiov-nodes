"""
Scorpiov Width Height Node
--------------------------
Sets width and height for image generation.

Features:
  - Ratio dropdown loaded from ratios.txt (add new ratios without code changes)
  - Custom width/height text fields (only used when "Custom" is selected)
  - Upscale factor (e.g. 2.0 doubles both dimensions)
  - Outputs: width (INT), height (INT), empty_latent (LATENT)
"""

import os
import torch
from server import PromptServer
from aiohttp import web

# ── Path to the ratios config file ──────────────────────────────────────────
RATIOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratios.txt")

# ── In-memory cache so we only re-read when needed ──────────────────────────
_ratio_cache: list[tuple[str, int, int]] = []   # [(label, w, h), ...]


def _load_ratios() -> list[tuple[str, int, int]]:
    """
    Read ratios.txt and return a list of (label, width, height) tuples.
    Lines starting with # are comments. Format: Label | width | height
    Always prepends ("Custom", 0, 0) as the first entry.
    """
    ratios = [("Custom", 0, 0)]
    if not os.path.isfile(RATIOS_FILE):
        print(f"[Scorpiov WH] ratios.txt not found at {RATIOS_FILE}, using defaults.")
        return ratios

    with open(RATIOS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 3:
                print(f"[Scorpiov WH] Skipping bad ratios line: {line!r}")
                continue
            try:
                label = parts[0]
                w = int(parts[1])
                h = int(parts[2])
                ratios.append((label, w, h))
            except ValueError:
                print(f"[Scorpiov WH] Could not parse width/height in: {line!r}")

    return ratios


def _get_ratios() -> list[tuple[str, int, int]]:
    """Return cached ratios, loading from file if cache is empty."""
    global _ratio_cache
    if not _ratio_cache:
        _ratio_cache = _load_ratios()
    return _ratio_cache


def _ratio_labels() -> list[str]:
    return [r[0] for r in _get_ratios()]


def _ratio_map() -> dict[str, tuple[int, int]]:
    return {r[0]: (r[1], r[2]) for r in _get_ratios()}


# ── REST endpoint: refresh ratio list without restarting ComfyUI ─────────────
@PromptServer.instance.routes.post("/scorpiov/wh/refresh")
async def scorpiov_wh_refresh(request):
    global _ratio_cache
    _ratio_cache = _load_ratios()
    labels = [r[0] for r in _ratio_cache]
    print(f"[Scorpiov WH] Ratios refreshed: {labels}")
    return web.json_response({"status": "ok", "ratios": labels})


# ── The Node ─────────────────────────────────────────────────────────────────

class ScorpiovWidthHeight:
    """
    Scorpiov Width Height Node.
    Select a ratio preset or enter custom width/height.
    Applies an upscale factor, then outputs width, height, and an empty latent.
    """

    @classmethod
    def INPUT_TYPES(cls):
        labels = _ratio_labels()
        return {
            "required": {
                "ratio": (labels, {"default": labels[0]}),
                "width":  ("INT", {
                    "default": 1024, "min": 64, "max": 8192, "step": 8,
                    "tooltip": "Used only when ratio is set to Custom"
                }),
                "height": ("INT", {
                    "default": 1024, "min": 64, "max": 8192, "step": 8,
                    "tooltip": "Used only when ratio is set to Custom"
                }),
                "upscale_factor": ("FLOAT", {
                    "default": 1.0, "min": 0.1, "max": 8.0, "step": 0.1,
                    "tooltip": "Multiplies both width and height. e.g. 2.0 = double resolution"
                }),
                "batch_size": ("INT", {
                    "default": 1, "min": 1, "max": 64,
                    "tooltip": "Number of images in the empty latent batch"
                }),
            }
        }

    RETURN_TYPES  = ("INT", "INT", "LATENT")
    RETURN_NAMES  = ("width", "height", "empty_latent")
    FUNCTION      = "process"
    CATEGORY      = "Scorpiov/Image"

    def process(self, ratio: str, width: int, height: int,
                upscale_factor: float, batch_size: int):

        rmap = _ratio_map()

        if ratio == "Custom":
            out_w = width
            out_h = height
        elif ratio in rmap:
            out_w, out_h = rmap[ratio]
        else:
            print(f"[Scorpiov WH] Unknown ratio '{ratio}', falling back to custom.")
            out_w = width
            out_h = height

        # Apply upscale factor, round to nearest multiple of 8 (required by SD)
        out_w = max(64, round(out_w * upscale_factor / 8) * 8)
        out_h = max(64, round(out_h * upscale_factor / 8) * 8)

        print(f"[Scorpiov WH] ratio={ratio!r}  →  {out_w} x {out_h}  "
              f"(upscale={upscale_factor}x, batch={batch_size})")

        # Build empty latent tensor  (SD latent space is 1/8 of pixel dimensions)
        latent = torch.zeros([batch_size, 4, out_h // 8, out_w // 8])
        empty_latent = {"samples": latent}

        return (out_w, out_h, empty_latent)

    @classmethod
    def IS_CHANGED(cls, ratio, width, height, upscale_factor, batch_size):
        # Re-run whenever any value changes
        return hash((ratio, width, height, upscale_factor, batch_size))


# ── Registration ─────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "ScorpiovWidthHeight": ScorpiovWidthHeight,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ScorpiovWidthHeight": "Scorpiov Width Height 📐",
}
