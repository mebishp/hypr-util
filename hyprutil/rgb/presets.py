"""The 4 standalone RGB lighting presets -- saved to disk, selectable from
either the tray or the settings app, independent of the fan power profile."""
import json
from pathlib import Path

from . import controller

CONFIG_DIR = Path.home() / ".config" / "hypr-util"
RGB_DIR = CONFIG_DIR / "rgb"
ACTIVE_FILE = RGB_DIR / "active"

PRESET_SLOTS = [1, 2, 3, 4]

DEFAULT_PRESETS = {
    1: {
        "name": "Preset 1", "effect": "static", "color_idx": 0,
        "colors": ["ff0000"] * 7, "brightness": controller.DEFAULT_BRIGHTNESS,
    },
    2: {
        "name": "Preset 2", "effect": "static", "color_idx": 0,
        "colors": ["0042ff"] * 7, "brightness": controller.DEFAULT_BRIGHTNESS,
    },
    3: {
        "name": "Preset 3", "effect": "breathe", "color_idx": 7,
        "colors": controller.DEFAULT_COLORS, "brightness": controller.DEFAULT_BRIGHTNESS,
    },
    4: {
        "name": "Preset 4", "effect": "wave", "color_idx": 7,
        "colors": ["ff0000", "ff4500", "ff8c00"] * 2 + ["ff0000"],
        "brightness": controller.DEFAULT_BRIGHTNESS,
    },
}


def preset_path(slot):
    return RGB_DIR / f"preset{slot}.json"


def ensure_defaults():
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    for slot, preset in DEFAULT_PRESETS.items():
        p = preset_path(slot)
        if not p.exists():
            p.write_text(json.dumps(preset, indent=2))


def read_preset(slot):
    p = preset_path(slot)
    if p.exists():
        preset = json.loads(p.read_text())
        preset.setdefault("name", f"Preset {slot}")
        return preset
    return dict(DEFAULT_PRESETS.get(slot, DEFAULT_PRESETS[1]))


def write_preset(slot, effect, colors, color_idx, brightness=controller.DEFAULT_BRIGHTNESS, name=None):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    preset_path(slot).write_text(
        json.dumps(
            {
                "name": name or read_preset(slot).get("name", f"Preset {slot}"),
                "effect": effect, "color_idx": color_idx, "colors": colors, "brightness": brightness,
            },
            indent=2,
        )
    )


def active_preset():
    """Which preset slot was applied last, or None if none have been applied yet."""
    if ACTIVE_FILE.exists():
        try:
            return int(ACTIVE_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _set_active_preset(slot):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(str(slot))


def apply_preset(slot):
    preset = read_preset(slot)
    controller.apply(
        preset["effect"], preset["colors"], preset["color_idx"],
        preset.get("brightness", controller.DEFAULT_BRIGHTNESS),
    )
    _set_active_preset(slot)
