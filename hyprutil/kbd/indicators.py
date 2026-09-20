"""Status the keyboard shows: power profile, battery, charging.

An indicator is a single zone given a colour that the look does not get a
say in. They are composited over whatever the lighting is already doing, and
-- the point of the whole design -- they still work when the lighting is
switched off: the backlight goes on, every other zone is written black, and
the board reads as dark with one status light. A zone set to 000000 on this
hardware is genuinely dark, which is what makes that honest rather than a
dim glow pretending to be off.

Colours are the ones hyprutil/rgb/notify.py already flashes on the external
Firefly for the same events, so the two keyboards say the same thing in the
same colours.

When two indicators want the same zone, the higher priority wins outright
rather than blending -- a blend of "battery critical" and "performance mode"
is a colour that means neither.
"""
import math

from . import zones

# Same vocabulary and same colours as rgb/notify.PROFILE_FLASH_COLORS.
PROFILE_COLORS = {
    "power-saver": "00ff00",
    "balanced": "ffff00",
    "performance": "ff0000",
}

BATTERY_CRITICAL_COLOR = "ff0000"
BATTERY_LOW_COLOR = "ff6a00"
CHARGING_COLOR = "00ff88"

# Higher wins the zone. Battery beats profile because a flat battery is the
# more urgent of the two things a keyboard can tell you.
PRIORITY_BATTERY_CRITICAL = 40
PRIORITY_BATTERY_LOW = 30
PRIORITY_CHARGING = 20
PRIORITY_PROFILE = 10

STYLE_SOLID = "solid"
STYLE_PULSE = "pulse"    # a slow breathe, for "this is fine but worth seeing"
STYLE_BLINK = "blink"    # a hard flash, for "do something about this"


class Overlay:
    """One zone, one colour, held above the look."""

    def __init__(self, zone, color, style=STYLE_SOLID, priority=0, label=""):
        self.zone = zone
        self.color = zones.to_rgb(color)
        self.style = style
        self.priority = priority
        self.label = label

    def render(self, phase):
        """The colour to write for this zone at this moment."""
        if self.style == STYLE_PULSE:
            return zones.scale(self.color, 0.25 + 0.75 * (0.5 + 0.5 * math.sin(phase * 1.4)))
        if self.style == STYLE_BLINK:
            # Square-ish rather than sinusoidal: a critical battery should
            # read as an alarm, not as gentle breathing.
            lit = (math.sin(phase * 2.6) > -0.3)
            return self.color if lit else (0, 0, 0)
        return self.color

    @property
    def animated(self):
        return self.style != STYLE_SOLID

    def as_dict(self):
        return {
            "zone": self.zone,
            "zone_name": zones.ZONE_NAMES.get(self.zone),
            "color": zones.to_hex(self.color),
            "style": self.style,
            "label": self.label,
        }


def build(config, telemetry, zone_count=4, lighting_on=True):
    """The indicators that should be showing, highest priority per zone.

    `config` is the "indicators" section of settings.read(); `telemetry` is
    a sysinfo.telemetry() sample.
    """
    if not config.get("enabled"):
        return []
    if not lighting_on and not config.get("when_off"):
        return []
    if zone_count <= 1 and lighting_on:
        # A one-zone board has nowhere to put a status light that is not the
        # whole keyboard. With the lighting off that is exactly what is
        # wanted; with it on it would mean the indicator eats the look.
        return []

    candidates = []

    profile_cfg = config.get("profile") or {}
    profile = telemetry.get("profile")
    if profile_cfg.get("enabled") and profile in PROFILE_COLORS:
        candidates.append(Overlay(
            profile_cfg["zone"], PROFILE_COLORS[profile],
            STYLE_SOLID, PRIORITY_PROFILE, f"profile: {profile}",
        ))

    battery_cfg = config.get("battery") or {}
    if battery_cfg.get("enabled"):
        level = telemetry.get("battery")
        charging = telemetry.get("charging")
        zone = battery_cfg["zone"]
        if level is not None and not charging and level <= battery_cfg["critical"]:
            candidates.append(Overlay(
                zone, BATTERY_CRITICAL_COLOR, STYLE_BLINK,
                PRIORITY_BATTERY_CRITICAL, f"battery {level}% (critical)",
            ))
        elif level is not None and not charging and level <= battery_cfg["low"]:
            candidates.append(Overlay(
                zone, BATTERY_LOW_COLOR, STYLE_SOLID,
                PRIORITY_BATTERY_LOW, f"battery {level}% (low)",
            ))
        elif charging and battery_cfg.get("show_charging"):
            candidates.append(Overlay(
                zone, CHARGING_COLOR, STYLE_PULSE,
                PRIORITY_CHARGING, f"charging ({level}%)" if level is not None else "charging",
            ))

    # One winner per zone, and only zones this keyboard actually has.
    best = {}
    for overlay in candidates:
        if overlay.zone >= zone_count:
            continue
        if overlay.zone not in best or overlay.priority > best[overlay.zone].priority:
            best[overlay.zone] = overlay
    return [best[zone] for zone in sorted(best)]


def wants_animation(overlays):
    """True when at least one indicator has to be redrawn over time."""
    return any(overlay.animated for overlay in overlays)
