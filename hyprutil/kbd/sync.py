"""Make the two keyboards agree about what colour the machine is.

They have nothing in common at the hardware level -- the laptop's is four
zones behind a BIOS mailbox, the Firefly is a USB HID device with twelve
firmware effects and a seven-colour palette -- so "matching" is a
translation, not a shared state. This module owns that translation and
nothing else.

The colour is the part that matters and the part that translates exactly:
one zone of the laptop keyboard is nominated as the source, and the Firefly
is set to it. The effect is a best-effort correspondence between two sets of
animations that were designed by different people for different hardware,
which is why matching it is a separate switch that can be turned off while
the colours still follow.

Failure here is never fatal to the caller. The Firefly is a peripheral that
may simply not be plugged in, and a laptop keyboard colour change must not
fail because of that.
"""
import logging

from . import zones

logger = logging.getLogger(__name__)

# Laptop effect -> the closest of the Firefly's twelve, and whether that
# Firefly effect should run in rainbow mode.
#
# Best-effort, and the pairs that are not obvious are the ones to argue
# with: `cycle` and `aurora` have no single-colour equivalent at all, so
# they go to a Firefly effect with its rainbow palette turned on, and the
# laptop's colour is deliberately ignored for them -- as it is on the
# laptop itself, where those effects paint their own hues.
EFFECT_MAP = {
    "static":   ("static", False),
    "gradient": ("static", False),
    "breathe":  ("breathe", False),
    "pulse":    ("fade", False),
    "cycle":    ("breathe", True),
    "wave":     ("wave", True),
    "sweep":    ("laser", False),
    "aurora":   ("swirl", True),
    "fire":     ("ripple", False),
    "meter":    ("static", False),
}

FIREFLY_BRIGHTNESS_MAX = 63
FIREFLY_SPEED_MIN, FIREFLY_SPEED_MAX = 1, 7


def _brightness(value):
    """0-100 on the laptop to 0-63 on the Firefly."""
    return max(0, min(FIREFLY_BRIGHTNESS_MAX,
                      round(value / zones.BRIGHTNESS_MAX * FIREFLY_BRIGHTNESS_MAX)))


def _speed(value):
    """Laptop speed 1-5 (5 fastest) to Firefly 1-7 (1 fastest).

    The two scales run in opposite directions, which is the kind of thing
    that silently produces a keyboard crawling when the other is sprinting.
    """
    span = (value - zones.SPEED_MIN) / max(1, zones.SPEED_MAX - zones.SPEED_MIN)
    return round(FIREFLY_SPEED_MAX - span * (FIREFLY_SPEED_MAX - FIREFLY_SPEED_MIN))


def firefly_look(look, config):
    """The Firefly look that matches this laptop look."""
    look = zones.normalize_look(look)
    source = config.get("source_zone", zones.ZONE_LEFT)
    colors = look["colors"]
    color = colors[source] if source < len(colors) else colors[0]

    effect, rainbow = "static", False
    if config.get("match_effect"):
        effect, rainbow = EFFECT_MAP.get(look["effect"], ("static", False))

    return {
        "effect": effect,
        "color": color,
        "rainbow": rainbow,
        "brightness": _brightness(look["brightness"]),
        "speed": _speed(look["speed"]),
    }


def laptop_colors(firefly_look_data, existing=None):
    """The four zone colours that match a Firefly look.

    The Firefly has one colour, so all four zones take it. A rainbow look
    has no single colour to copy, so the existing zones are kept and the
    caller is told nothing changed.
    """
    if firefly_look_data.get("rainbow"):
        return None
    color = zones.to_hex(firefly_look_data.get("color") or "ffffff")
    return [color] * len(zones.DEFAULT_LOOK["colors"])


def push(look, config):
    """Send a matching look to the Firefly. Returns what was sent, or None.

    Silent about a missing keyboard -- that is the normal state of a USB
    peripheral -- and loud about anything else, because a Firefly that is
    plugged in and refusing is worth a line in the log.
    """
    if not config.get("enabled"):
        return None
    try:
        from ..rgb import controller
    except ImportError:
        return None
    try:
        if not controller.ready():
            return None
        target = firefly_look(look, config)
        controller.apply_look(target)
        logger.info("matched the Firefly to the laptop keyboard: %s", target)
        return target
    except Exception as e:
        logger.warning("could not match the Firefly: %s", e)
        return None
