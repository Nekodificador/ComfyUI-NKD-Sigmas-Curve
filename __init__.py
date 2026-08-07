"""
NKD Sigma Curve – ComfyUI custom node package initialisation.

Registers the web assets directory so ComfyUI serves the JavaScript
extension, and exposes the legacy NODE_CLASS_MAPPINGS required for
custom_nodes/ discovery.
"""

import os
import nodes

# Register web assets so ComfyUI serves our JS extension
_web_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "web")
nodes.EXTENSION_WEB_DIRS["nkd_sigmas_curve"] = _web_dir

from .nkd_sigma_curve import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: E402
from .nkd_h3_audio_shift import NKDH3AudioShiftCurve  # noqa: E402

NODE_CLASS_MAPPINGS["NKDH3AudioShiftCurve"] = NKDH3AudioShiftCurve
NODE_DISPLAY_NAME_MAPPINGS["NKDH3AudioShiftCurve"] = "😺NKD H3 Audio Shift Curve"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
