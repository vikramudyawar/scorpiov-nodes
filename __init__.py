"""
Scorpiov-Nodes — ComfyUI Custom Node Package
Place this folder in: ComfyUI/custom_nodes/scorpiov-nodes/

Nodes included:
  - Scorpiov Wildcard Processor  (scorpiov_wildcard.py)
  - Scorpiov Width Height        (scorpiov_width_height.py)
"""

from .scorpiov_wildcard import (
    NODE_CLASS_MAPPINGS as WILDCARD_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as WILDCARD_NAMES,
)
from .scorpiov_width_height import (
    NODE_CLASS_MAPPINGS as WH_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as WH_NAMES,
)

NODE_CLASS_MAPPINGS = {**WILDCARD_CLASSES, **WH_CLASSES}
NODE_DISPLAY_NAME_MAPPINGS = {**WILDCARD_NAMES, **WH_NAMES}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
