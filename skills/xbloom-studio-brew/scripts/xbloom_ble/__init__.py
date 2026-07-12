"""Vendored xbloom-ble core for unofficial xBloom Studio BLE control.

This package speaks the reverse-engineered BLE protocol of the xBloom Studio
pour-over machine. There is no official API. The vendored client contains both
load-only and brew-control primitives; public Agent workflows must call them
through ``scripts/xbloom.py``, which applies the skill's stricter safety gates.
"""

__version__ = "2.3.0"

from .protocol import PATTERN_CODES, build_load_frames, crc16_kermit, xbloom_frame
from .recipe import Pour, Recipe, RecipeError
from .telemetry import STATE_NAMES, StatusEvent, parse_notification

__all__ = [
    "__version__",
    "build_load_frames",
    "PATTERN_CODES",
    "crc16_kermit",
    "xbloom_frame",
    "Recipe",
    "Pour",
    "RecipeError",
    "StatusEvent",
    "parse_notification",
    "STATE_NAMES",
]
