"""The 4 standalone RGB lighting presets -- saved to disk, selectable from
either the tray or the settings app, independent of the fan power profile."""
import json

from . import controller
from ..util import CONFIG_DIR, atomic_write_text

RGB_DIR = CONFIG_DIR / "rgb"
ACTIVE_FILE = RGB_DIR / "active"
EDITOR_STATE_FILE = RGB_DIR / "editor.json"

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
    atomic_write_text(
        preset_path(slot),
        json.dumps(
            {
                "name": name or read_preset(slot).get("name", f"Preset {slot}"),
                "effect": effect, "color_idx": color_idx, "colors": colors, "brightness": brightness,
            },
            indent=2,
        ),
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
    atomic_write_text(ACTIVE_FILE, str(slot))


def apply_preset(slot):
    preset = read_preset(slot)
    controller.apply(
        preset["effect"], preset["colors"], preset["color_idx"],
        preset.get("brightness", controller.DEFAULT_BRIGHTNESS),
    )
    _set_active_preset(slot)


def read_editor_state():
    """Last effect/colors applied from the settings app's Lighting editor
    (not one of the 4 saved preset slots) -- restored on next launch so the
    editor doesn't reset to defaults every time."""
    default = {
        "effect": controller.EFFECTS[0],
        "multi": False,
        "color": controller.DEFAULT_COLORS[0],
        "colors": list(controller.DEFAULT_COLORS),
    }
    if EDITOR_STATE_FILE.exists():
        try:
            state = json.loads(EDITOR_STATE_FILE.read_text())
            default.update(state)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def write_editor_state(effect, multi, color, colors):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        EDITOR_STATE_FILE,
        json.dumps({"effect": effect, "multi": multi, "color": color, "colors": colors}, indent=2),
    )
