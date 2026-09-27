"""The laptop's own keyboard lighting: four zones, straight through sysfs.

This is the HP OMEN keyboard built into the machine, not the external
Firefly (that one is USB HID and lives in hyprutil/rgb/). The two are
deliberately separate: they share no protocol, no state and no failure mode,
and the only thing they have in common is that both are keyboards.

The privileged part -- one ACPI-WMI call into the BIOS mailbox -- belongs to
the hyprkbd kernel module in kernel/hyprkbd/, and a udev rule hands its sysfs
attributes to the desktop user at install time. So everything here is an
ordinary file read or write by an ordinary process: no root, no daemon, no
socket. The effect animation is the one thing that must outlive the app, and
it lives in effects.py as a user service.
"""
from . import (
    device, effects, indicators, presets, settings, state, sync, sysinfo, zones,
)
from .device import DeviceError, DeviceUnavailable
from .state import read_current, write_current
from .zones import (
    ANIMATED,
    COLOURLESS_EFFECTS,
    EFFECT_DESCRIPTIONS,
    BRIGHTNESS_MAX,
    DEFAULT_BRIGHTNESS,
    DEFAULT_LOOK,
    DEFAULT_SPEED,
    DISPLAY_ORDER,
    EFFECTS,
    EFFECT_LABELS,
    FACTORY_COLORS,
    MERGE_WASD,
    SPEED_MAX,
    SPEED_MIN,
    ZONE_LEFT,
    ZONE_MIDDLE,
    ZONE_NAMES,
    ZONE_RIGHT,
    ZONE_WASD,
    frame_colors,
    merges_wasd,
    normalize_look,
    to_hex,
    to_rgb,
    zone_colors,
)

__all__ = [
    "ANIMATED", "BRIGHTNESS_MAX", "COLOURLESS_EFFECTS", "DEFAULT_BRIGHTNESS",
    "DEFAULT_LOOK", "DEFAULT_SPEED", "DISPLAY_ORDER", "EFFECTS", "EFFECT_DESCRIPTIONS",
    "EFFECT_LABELS", "FACTORY_COLORS", "MERGE_WASD", "SPEED_MAX", "SPEED_MIN",
    "ZONE_LEFT", "ZONE_MIDDLE", "ZONE_NAMES", "ZONE_RIGHT", "ZONE_WASD",
    "DeviceError", "DeviceUnavailable",
    "apply", "apply_preset", "available", "device", "effects", "frame_colors",
    "indicators", "match_firefly", "merges_wasd", "normalize_look", "presets",
    "preview", "probe", "read_current", "read_settings", "reload", "save_preset",
    "settings", "state", "status", "sync", "sysinfo", "to_hex", "to_rgb",
    "update_settings",
    "write_current", "zone_colors", "zones",
]


def _keyboard_or_raise():
    keyboard = device.keyboard()
    if not keyboard:
        raise DeviceUnavailable(
            device.explain() if not device.present()
            else "this keyboard has no lighting the firmware will drive"
        )
    return keyboard


def _render_context(look, config=None, zone_count=4):
    """Everything a frame needs: the effective look, and any live alert.

    Shared by apply(), reload(), status() and the UI preview so that all of
    them agree about what the keyboard should be showing -- a preview that
    ignored the battery saver, or an apply that dropped an alert mid-flash,
    would each be a quiet lie.

    The alert comes from the process-wide monitor, so asking twice in one
    frame does not fire anything twice.
    """
    config = config if config is not None else settings.read()
    look = normalize_look(look)
    telemetry = sysinfo.telemetry()
    alert = indicators.monitor().update(
        config["indicators"], telemetry, look["on"],
    )
    effective = zones.apply_battery_saver(look, config["battery_saver"], telemetry)
    return effective, alert, telemetry, config


def status():
    """What keyboard is fitted, what look is saved, and whether it is lit."""
    keyboard = device.keyboard()
    lit = None
    if keyboard:
        try:
            lit = device.read_backlight()
        except DeviceError:
            lit = None
    look = read_current()
    zone_count = (keyboard or {}).get("zones") or 4
    effective, alert, telemetry, config = _render_context(look, zone_count=zone_count)
    return {
        "ok": True,
        "keyboard": keyboard,
        "look": look,
        "effective_look": effective,
        "saving": effective != look,
        "settings": config,
        "telemetry": telemetry,
        "alert": alert.as_dict() if alert is not None else None,
        "lit": lit,
        "error": None if device.present() else device.explain(),
    }


def available():
    """True when there is a zoned keyboard here that we can drive."""
    keyboard = device.keyboard()
    return bool(keyboard and keyboard["usable"])


def apply(look):
    """Save a look and put it on the keyboard.

    Saving first is deliberate: the file is what the effects service reads
    back, and what survives a reboot, so a look that reached the hardware but
    not the disk would quietly revert the next time the machine came up.
    Writing the hardware straight afterwards is what makes the change
    immediate -- the service notices the new file on its next frame and takes
    the animation over from there.
    """
    look = write_current(look)
    keyboard = _keyboard_or_raise()
    effective, alert, telemetry, config = _render_context(
        look, zone_count=keyboard["zones"]
    )
    zones.apply_look(
        effective, keyboard["zones"], alert=alert, telemetry=telemetry
    )
    sync.push(look, config["sync"])
    return status()


def reload():
    """Put the saved look back on the keyboard, without rewriting the file.

    What the boot-time and resume paths call: the firmware brings the
    backlight back at its own default after a resume, not at whatever was
    last set.
    """
    keyboard = _keyboard_or_raise()
    effective, alert, telemetry, _ = _render_context(
        read_current(), zone_count=keyboard["zones"]
    )
    zones.apply_look(
        effective, keyboard["zones"], alert=alert, telemetry=telemetry
    )
    return status()


def probe():
    """Everything that decides whether this works, for `hyprutil kbd probe`.

    Every attribute the driver publishes, read one at a time, so a single
    failing one is visible as itself rather than as a blanket "unavailable".
    """
    out = {
        "ok": True,
        "module": device.MODULE,
        "sysfs": str(device.SYSFS),
        "loaded": device.present(),
        "writable": device.writable(),
    }
    if not device.present():
        out["error"] = device.explain()
        return out
    attributes = {}
    for name in ("keyboard_type", "type_name", "zones", "kind", "supported",
                 "colors", "backlight", "backlight_raw"):
        try:
            attributes[name] = device.read_attribute(name)
        except DeviceError as e:
            attributes[name] = None
            out.setdefault("failures", {})[name] = str(e)
    out["attributes"] = attributes
    out["keyboard"] = device.keyboard()
    return out


# -- settings, presets, and the other keyboard --

def read_settings():
    """Indicators, battery saver and keyboard matching."""
    return settings.read()


def update_settings(patch):
    """Merge a change into the settings, save it, and show the result.

    Merged rather than replaced so a caller may send only the part it owns:
    the tray toggling one indicator does not have to know the battery
    thresholds to avoid clobbering them.
    """
    current = settings.read()

    def merge(into, changes):
        for key, value in (changes or {}).items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = value

    merge(current, patch)
    saved = settings.write(current)
    try:
        reload()
    except DeviceError:
        # The settings are saved either way; a keyboard that will not take
        # them right now is not a reason to lose the change.
        pass
    return saved


def apply_preset(slot):
    """Put a saved preset on the keyboard."""
    look = presets.read(slot)
    return apply({k: v for k, v in look.items() if k != "name"})


def save_preset(slot, look=None, name=None):
    """Store a look (the current one, by default) into a preset slot."""
    return presets.write(slot, look if look is not None else read_current(), name=name)


def match_firefly(look=None):
    """Push the laptop keyboard's colour to the external Firefly now.

    Used by the "match now" action, which works whether or not automatic
    matching is switched on -- so someone can try it once before committing
    to having it happen on every change.
    """
    config = dict(settings.read()["sync"])
    config["enabled"] = True
    return sync.push(look if look is not None else read_current(), config)


def preview(look, zone_count=4, phase=0.0, config=None):
    """What the keyboard would be showing, for the UI to draw.

    Returns (colours, lit, alert). Goes through the same composition as the
    hardware path, so the preview includes an alert and the battery saver
    rather than only the look being edited.

    One call rather than the pair this replaced: the preview redraws eight
    times a second, and each of those used to build the whole render context
    twice -- two telemetry samples, two normalizations, two passes over the
    indicator config -- to answer two questions about the same frame.

    `config` lets that caller hand in the settings it already holds rather
    than making this re-read and re-parse the file for every frame.
    """
    effective, alert, telemetry, _ = _render_context(
        look, config=config, zone_count=zone_count
    )
    colors = zones.compose(effective, zone_count, phase, alert, telemetry)
    return colors, zones.backlight_wanted(effective, alert), alert
