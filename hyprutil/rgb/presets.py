"""Saved lighting looks, plus the current one.

A look is what the keyboard is showing: one of the twelve built-in effects
with a colour. See controller.DEFAULT_LOOK for the shape.

Each of the four presets is bound to one of the keyboard's own hardware
profiles (preset 1 to profile 0, and so on). Applying a preset therefore
selects that hardware profile first, so the keyboard keeps the lighting
itself -- it survives unplugging, rebooting, and this software not running
at all.
"""
import json

from . import controller
from ..util import CONFIG_DIR, atomic_write_text

RGB_DIR = CONFIG_DIR / "rgb"
ACTIVE_FILE = RGB_DIR / "active"
CURRENT_FILE = RGB_DIR / "current.json"
LEGACY_CURRENT_FILE = RGB_DIR / "editor.json"

PRESET_SLOTS = [1, 2, 3, 4]

DEFAULT_PRESETS = {
    1: {"name": "Red", "effect": "static", "color": "ff0000"},
    2: {"name": "Blue", "effect": "static", "color": "0042ff"},
    3: {"name": "Rainbow", "effect": "breathe", "rainbow": True},
    4: {"name": "Ocean", "effect": "ripple", "color": "00b4ff"},
}


def profile_for(slot):
    """The keyboard profile a preset is bound to, or None if it has none.

    There are more preset slots than the keyboard has profiles, so the last
    preset is software-only: applying it still works, it just does not get
    stored on the keyboard and so does not survive with nothing running.
    """
    index = slot - 1
    return index if index < controller.PROFILE_COUNT else None


def preset_path(slot):
    return RGB_DIR / f"preset{slot}.json"


def ensure_defaults():
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    for slot, preset in DEFAULT_PRESETS.items():
        path = preset_path(slot)
        if not path.exists():
            atomic_write_text(path, json.dumps(_with_defaults(preset, slot), indent=2))


def _migrate_palette(data):
    """Turn a pre-existing palette-style preset into a colour plus rainbow.

    Those presets stored one to seven colours and set the device's colour
    index to 7 whenever there was more than one -- which is LOOP, the
    device's own rainbow, not a cycle through the chosen colours. So a
    multi-colour preset was always showing a rainbow, and that is what it is
    honestly recorded as here.
    """
    palette = data.get("palette")
    if palette is None and "colors" in data:  # older still: a fixed 7 slots
        palette = data.get("colors") or []
        if data.get("color_idx", 7) != 7:
            palette = palette[data["color_idx"]:data["color_idx"] + 1]
        elif len(set(palette)) == 1:
            palette = palette[:1]
    if not palette:
        return None, None
    return str(palette[0]).lstrip("#"), len(palette) > 1


def _brightness_and_speed(data):
    """Recover brightness and speed from any generation of preset.

    Presets written before the two were known to be separate fields stored
    one number called "brightness" that the device was reading as animation
    speed, with real brightness pinned at maximum.
    """
    brightness = data.get("brightness", controller.DEFAULT_BRIGHTNESS)
    if "speed" in data:
        speed = data["speed"]
    else:
        speed = brightness if controller.SPEED_MIN <= brightness <= controller.SPEED_MAX \
            else controller.DEFAULT_SPEED
        brightness = controller.DEFAULT_BRIGHTNESS
    return brightness, speed


def _with_defaults(data, slot):
    data = dict(data or {})
    brightness, speed = _brightness_and_speed(data)
    data["brightness"], data["speed"] = brightness, speed
    if "color" not in data or "rainbow" not in data:
        color, rainbow = _migrate_palette(data)
        data.setdefault("color", color or controller.DEFAULT_LOOK["color"])
        data.setdefault("rainbow", bool(rainbow))
    look = controller.normalize_look(data)
    look["name"] = data.get("name") or f"Preset {slot}"
    return look


def read_preset(slot):
    path = preset_path(slot)
    if path.exists():
        try:
            return _with_defaults(json.loads(path.read_text()), slot)
        except (json.JSONDecodeError, OSError):
            pass
    return _with_defaults(DEFAULT_PRESETS.get(slot, DEFAULT_PRESETS[1]), slot)


def write_preset(slot, look, name=None):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    stored = controller.normalize_look(look)
    stored["name"] = name or read_preset(slot)["name"]
    atomic_write_text(preset_path(slot), json.dumps(stored, indent=2))


def rename_preset(slot, name):
    """Rename without touching the look."""
    write_preset(slot, read_preset(slot), name=name)


def reset_preset(slot):
    default = _with_defaults(DEFAULT_PRESETS.get(slot, DEFAULT_PRESETS[1]), slot)
    write_preset(slot, default, name=default["name"])


def active_preset():
    """The preset currently on the keyboard, or None if a hand-edited look is."""
    if ACTIVE_FILE.exists():
        try:
            return int(ACTIVE_FILE.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def set_active_preset(slot):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ACTIVE_FILE, str(slot))


def clear_active_preset():
    ACTIVE_FILE.unlink(missing_ok=True)


def apply_preset(slot):
    preset = read_preset(slot)
    controller.apply_look(preset, profile=profile_for(slot))
    write_current(preset)
    set_active_preset(slot)


def read_current():
    """The look on the keyboard now, restored across restarts."""
    for path in (CURRENT_FILE, LEGACY_CURRENT_FILE):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "color" not in data or "rainbow" not in data:
            color, rainbow = _migrate_palette(data)
            if color is None and data.get("multi") is not None:  # oldest editor.json
                color, rainbow = str(data.get("color") or "ff0000").lstrip("#"), bool(data.get("multi"))
            data.setdefault("color", color or controller.DEFAULT_LOOK["color"])
            data.setdefault("rainbow", bool(rainbow))
        brightness, speed = _brightness_and_speed(data)
        data["brightness"], data["speed"] = brightness, speed
        return controller.normalize_look(data)
    return controller.normalize_look({})


def write_current(look):
    RGB_DIR.mkdir(parents=True, exist_ok=True)
    stored = controller.normalize_look(look)
    atomic_write_text(CURRENT_FILE, json.dumps(stored, indent=2))


def apply_current(look):
    """Apply an unsaved look, without binding it to a keyboard profile."""
    controller.apply_look(look)
    write_current(look)
