"""Keyboard RGB control: device protocol (controller), saved presets, and
event-driven connection detection (watch), under one public API."""
from .controller import (
    CTL_BIN,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLORS,
    EFFECTS,
    apply,
    available,
    is_connected,
    on_connection_change,
    ready,
)
from .presets import (
    ACTIVE_FILE,
    CONFIG_DIR,
    DEFAULT_PRESETS,
    PRESET_SLOTS,
    RGB_DIR,
    active_preset,
    apply_preset,
    ensure_defaults,
    preset_path,
    read_preset,
    write_preset,
)
from .notify import PROFILE_FLASH_COLORS, flash, flash_for_profile

__all__ = [
    "CTL_BIN", "DEFAULT_BRIGHTNESS", "DEFAULT_COLORS", "EFFECTS",
    "apply", "available", "is_connected", "on_connection_change", "ready",
    "ACTIVE_FILE", "CONFIG_DIR", "DEFAULT_PRESETS", "PRESET_SLOTS", "RGB_DIR",
    "active_preset", "apply_preset", "ensure_defaults", "preset_path",
    "read_preset", "write_preset",
    "PROFILE_FLASH_COLORS", "flash", "flash_for_profile",
]
