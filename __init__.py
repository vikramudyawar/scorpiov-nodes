"""
Scorpiov Wildcard Node — ComfyUI Custom Node Package
Place this folder in: ComfyUI/custom_nodes/scorpiov-wildcard-node/
"""

from .scorpiov_wildcard import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
