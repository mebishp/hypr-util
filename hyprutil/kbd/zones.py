"""The look of the laptop keyboard: zones, colours, brightness and effects.

The hardware is the hyprkbd driver's problem (see device.py); this module is
the arithmetic above it -- what colour each zone should be at a given moment,
and what a saved "look" is allowed to contain.

Two things follow from the firmware and are worth stating, because they look
like bugs otherwise:

* Brightness is done here, by scaling the colours before they are written.
  The level bits of the firmware's backlight byte change nothing on any board
  anyone has measured, so the vendor software writes 100 there and dims the
  colours instead. This does the same.
* Effects are frames, not firmware modes. There is no animation the firmware
  will run for you -- its own animation table is documented as taking no
  effect -- so breathe, cycle and wave are computed here and written about
  eight times a second while one is running.

Zone numbering is HP's: 0 right, 1 middle, 2 left, 3 WASD. If a board turns
out to be mirrored, DISPLAY_ORDER and ZONE_NAMES are the only things to
change -- everything else addresses zones by index.
"""
import colorsys
import math

from . import device

ZONE_RIGHT, ZONE_MIDDLE, ZONE_LEFT, ZONE_WASD = 0, 1, 2, 3
ZONE_NAMES = {ZONE_LEFT: "Left", ZONE_MIDDLE: "Middle", ZONE_RIGHT: "Right", ZONE_WASD: "WASD"}
# Left to right across the keyboard, which is the order a person reads them
# in -- not the order the firmware stores them in.
DISPLAY_ORDER = [ZONE_LEFT, ZONE_MIDDLE, ZONE_RIGHT, ZONE_WASD]

# The vendor's factory colours for zones 0..3 (right, middle, left, WASD).
FACTORY_COLORS = ["0f84fa", "710ffa", "f9350f", "faac0f"]

EFFECTS = [
    "static", "gradient", "breathe", "pulse", "cycle",
    "wave", "sweep", "aurora", "fire", "meter",
]
EFFECT_LABELS = {
    "static": "Static",
    "gradient": "Gradient",
    "breathe": "Breathe",
    "pulse": "Pulse",
    "cycle": "Cycle",
    "wave": "Wave",
    "sweep": "Sweep",
    "aurora": "Aurora",
    "fire": "Fire",
    "meter": "CPU meter",
}
# Shown under the effect picker, so someone choosing one knows what they are
# about to get without having to try all ten.
EFFECT_DESCRIPTIONS = {
    "static": "Each zone holds the colour you picked.",
    "gradient": "A smooth blend from the left zone's colour to the right zone's.",
    "breathe": "Your colours fading gently in and out together.",
    "pulse": "A sharp flash with a long dark gap between beats.",
    "cycle": "The whole board through the spectrum, in step.",
    "wave": "The spectrum, offset across the zones so it travels.",
    "sweep": "A bright band of your colour running left to right.",
    "aurora": "Slow, drifting hues that never quite repeat.",
    "fire": "A warm flicker, each zone burning on its own.",
    "meter": "Fills left to right with CPU load, green through red.",
}
# Effects that have to be redrawn over time. Static and gradient are a single
# write and then nothing, which is why the service can idle through them.
ANIMATED = {"breathe", "pulse", "cycle", "wave", "sweep", "aurora", "fire", "meter"}
# Effects that paint their own colours and ignore the zone pickers. Worth
# knowing so the UI can grey the pickers out rather than letting someone
# choose a colour that will not be used.
COLOURLESS_EFFECTS = {"cycle", "wave", "aurora", "fire", "meter"}
# Effects that read as one picture running across the board rather than as
# four independent zones. For those, WASD takes the left zone's colour and
# the keyboard behaves as three bands -- which is what it looks like, since
# the WASD keys sit inside the left third rather than beside it. Left as its
# own zone for the effects where a separately lit cluster is the point
# (static, breathe, pulse, fire) or where every zone is the same colour
# anyway (cycle).
MERGE_WASD = {"gradient", "wave", "sweep", "aurora", "meter"}

# Where each zone physically sits across the keyboard, left 0.0 to right 1.0.
# The firmware's zone numbering is not spatial, and the travelling effects
# have to be: sweep would jump about at random without this. WASD is not a
# full quarter of the board -- it sits just left of centre, over the keys it
# is named for.
ZONE_POSITION = {
    ZONE_LEFT: 0.0,
    ZONE_WASD: 0.22,
    ZONE_MIDDLE: 0.55,
    ZONE_RIGHT: 1.0,
}

SPEED_MIN, SPEED_MAX = 1, 5
DEFAULT_SPEED = 3
# What one speed step is worth; the middle one is 1.0 by definition.
SPEED_FACTORS = {1: 0.35, 2: 0.6, 3: 1.0, 4: 1.6, 5: 2.4}
FRAME_SECONDS = 0.12      # ~8 frames a second, as the vendor software animates
PHASE_STEP = 0.12

BRIGHTNESS_MAX = 100
DEFAULT_BRIGHTNESS = 100
# Never scale all the way to black: a brightness of 0 with the backlight on
# reads as "the app broke" rather than as a dim keyboard.
MIN_SCALE = 0.05

DEFAULT_LOOK = {
    "on": True,
    "effect": "static",
    "colors": list(FACTORY_COLORS),
    "brightness": DEFAULT_BRIGHTNESS,
    "speed": DEFAULT_SPEED,
}


# -- colour helpers --

def to_rgb(color):
    # A plain 3-tuple is taken as already clamped. Everything in this module
    # that produces one has clamped it, and the animation path hands those
    # tuples straight back here -- four zones, eight times a second, plus
    # once more per alert frame. Re-validating colours we just computed is
    # the kind of cost that only shows up as a warm laptop.
    if type(color) is tuple and len(color) == 3:
        return color
    if isinstance(color, (tuple, list)):
        return tuple(max(0, min(255, int(c))) for c in color[:3])
    text = str(color).lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def to_hex(color):
    r, g, b = to_rgb(color)
    return f"{r:02x}{g:02x}{b:02x}"


def scale(color, factor):
    r, g, b = to_rgb(color)
    factor = max(0.0, min(1.0, factor))
    return (round(r * factor), round(g * factor), round(b * factor))


def from_hue(hue, value=1.0):
    """Full-saturation colour at a hue in degrees."""
    r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360.0, 1.0, max(0.0, min(1.0, value)))
    return (round(r * 255), round(g * 255), round(b * 255))


def blend(first, second, amount):
    """`first` at amount 0, `second` at amount 1."""
    amount = max(0.0, min(1.0, amount))
    a, b = to_rgb(first), to_rgb(second)
    return tuple(round(a[i] + (b[i] - a[i]) * amount) for i in range(3))


def merges_wasd(effect):
    """Whether this effect wants WASD folded into the left zone.

    Asked by the preview as well as by the frame builder, so the drawing on
    screen loses the WASD cluster at exactly the moment the keyboard does.
    """
    return effect in MERGE_WASD


def _position(index, count):
    """Where a zone sits across the board, 0.0 left to 1.0 right."""
    if count == 4 and index in ZONE_POSITION:
        return ZONE_POSITION[index]
    if count <= 1:
        return 0.0
    return index / (count - 1)


def _hash01(value):
    """A deterministic pseudo-random number in [0, 1) from one integer."""
    return (math.sin(value * 12.9898) * 43758.5453) % 1.0


def _flicker(x):
    """Smooth pseudo-noise in [0, 1), for the fire effect.

    Interpolated between integer steps rather than sampled raw: raw hashes
    strobe, which looks like a fault, while an eased blend between them
    looks like something burning. Deterministic in `x`, so the UI preview
    and the keyboard show the same flame at the same moment.
    """
    lower = math.floor(x)
    fraction = x - lower
    start, end = _hash01(lower), _hash01(lower + 1)
    eased = fraction * fraction * (3.0 - 2.0 * fraction)
    return start + (end - start) * eased


# -- looks --

def normalize_look(data):
    """Fill in and clamp a look read from disk or built by the UI."""
    look = dict(DEFAULT_LOOK)
    look["colors"] = list(DEFAULT_LOOK["colors"])
    for key, value in (data or {}).items():
        if key in DEFAULT_LOOK:
            look[key] = value
    look["on"] = bool(look["on"])
    if look["effect"] not in EFFECTS:
        look["effect"] = DEFAULT_LOOK["effect"]
    look["brightness"] = max(0, min(BRIGHTNESS_MAX, int(look["brightness"])))
    look["speed"] = max(SPEED_MIN, min(SPEED_MAX, int(look["speed"])))
    colors = list(look["colors"] or [])
    fixed = []
    for i in range(len(DEFAULT_LOOK["colors"])):
        try:
            fixed.append(to_hex(colors[i]))
        except (IndexError, ValueError, TypeError):
            fixed.append(DEFAULT_LOOK["colors"][i])
    look["colors"] = fixed
    return look


def zone_colors(look, zones):
    """The look's colours, one per zone this keyboard actually has.

    A one-zone board gets the left zone's colour rather than zone 0's, so
    picking a colour on the left of the editor does what it looks like it
    does on both kinds of board.
    """
    colors = look["colors"]
    if zones == 1:
        return [colors[ZONE_LEFT]]
    return colors[:zones]


def effect_frame(effect, phase, colors, index, telemetry=None):
    """The colour of one zone at one moment.

    Also drives the UI preview, so what is drawn on screen and what is
    written to the keyboard come from the same function and cannot drift
    apart. Pure: the same arguments always give the same colour.
    """
    count = len(colors)
    position = _position(index, count)

    if effect == "breathe":
        return scale(colors[index], 0.15 + 0.85 * (0.5 + 0.5 * math.sin(phase * 1.6)))
    if effect == "pulse":
        # Raised to the fourth so the beat is short and the gap is long --
        # the difference between this and breathe is the shape, not the rate.
        beat = (0.5 + 0.5 * math.sin(phase * 1.5)) ** 4
        return scale(colors[index], 0.05 + 0.95 * beat)
    if effect == "cycle":
        return from_hue(phase * 25)
    if effect == "wave":
        return from_hue(phase * 25 + index * (360.0 / max(1, count)))
    if effect == "gradient":
        left = colors[ZONE_LEFT] if count == 4 else colors[0]
        right = colors[ZONE_RIGHT] if count == 4 else colors[-1]
        return blend(left, right, position)
    if effect == "sweep":
        # The band runs past both ends before wrapping, so there is a beat
        # of darkness rather than it reappearing the instant it leaves.
        head = (phase * 0.16) % 1.5 - 0.25
        nearness = max(0.0, 1.0 - abs(position - head) / 0.4)
        return scale(colors[index], 0.06 + 0.94 * nearness)
    if effect == "aurora":
        hue = phase * 9 + position * 70 + 40 * math.sin(phase * 0.55 + index * 1.7)
        return from_hue(hue, 0.75 + 0.25 * math.sin(phase * 0.9 + index))
    if effect == "fire":
        hue = 4 + 34 * _flicker(phase * 2.2 + index * 7.3)
        return from_hue(hue, 0.35 + 0.65 * _flicker(phase * 3.7 + index * 3.1 + 40))
    if effect == "meter":
        # Zones fill left to right with CPU load, and the whole board shifts
        # green to red as it climbs -- so it reads at a glance from across
        # the desk, before you have counted which zones are lit.
        load = max(0.0, min(1.0, (telemetry or {}).get("load") or 0.0))
        order = sorted(range(count), key=lambda z: _position(z, count))
        slot = order.index(index)
        low, high = slot / count, (slot + 1) / count
        fill = max(0.0, min(1.0, (load - low) / (high - low))) if high > low else 0.0
        return from_hue(120 - 120 * load, 0.06 + 0.94 * fill)
    return to_rgb(colors[index])


def frame_colors(look, zones, phase=0.0, telemetry=None):
    """Every zone's colour for this look at this phase, brightness applied.

    `look` must already be normalized -- this is the inner loop of the
    animation, run four times a frame at eight frames a second in two
    processes, and re-validating a dict that the caller has already
    validated is the whole of what it used to spend its time on.
    """
    effect = look["effect"]
    base = zone_colors(look, zones)
    if effect not in COLOURLESS_EFFECTS:
        # Parsed once here rather than once per zone inside effect_frame:
        # the colours are hex on disk and tuples everywhere else, and this
        # is the single place the conversion has to happen. Skipped whole
        # for the effects that paint their own hues -- parsing four colours
        # that nothing is going to read is pure loss at eight frames a
        # second.
        base = [to_rgb(c) for c in base]
    factor = max(MIN_SCALE, look["brightness"] / 100.0)
    count = len(base)
    frame = [
        scale(effect_frame(effect, phase, base, i, telemetry), factor)
        for i in range(count)
    ]
    if count == 4 and merges_wasd(effect):
        # One picture across the board rather than four zones: WASD is
        # inside the left third, so it takes the left zone's colour and the
        # keyboard reads as three bands.
        frame[ZONE_WASD] = frame[ZONE_LEFT]
    return frame


# -- battery saver --

def apply_battery_saver(look, saver, telemetry):
    """The look as it should be on battery, or unchanged on mains.

    Returns a modified copy and never touches the saved look: unplugging
    dims the keyboard, plugging back in restores exactly what was chosen,
    and nothing had to be written to disk in between.

    An unknown power source (no mains adapter in /sys, a desktop, a VM) is
    treated as mains. Dimming someone's keyboard because we could not find
    out whether they were on battery would be the wrong way to be wrong.
    """
    if not (saver and saver.get("enabled")):
        return look
    if (telemetry or {}).get("on_ac") is not False:
        return look
    saved = dict(look)
    saved["brightness"] = min(saved["brightness"], saver.get("brightness", 40))
    if saver.get("static_only") and saved["effect"] in ANIMATED:
        # Stopped rather than slowed: the cost of an animation is the eight
        # WMI calls a second, not the brightness of any one frame.
        saved["effect"] = "static"
    return saved


# -- composition --

def compose(look, zone_count=4, phase=0.0, alert=None, telemetry=None, now=None):
    """The frame to write: the look, or the alert that has taken it over.

    An alert owns the whole keyboard for its couple of seconds -- that is
    the point of it -- and only its two fading edges need the look
    underneath, to cross into and back out of. The look's phase is not
    advanced during an alert (see effects.Animator.tick), so what comes back
    afterwards is the frame the effect was on rather than the one it would
    have reached.

    `look` must already be normalized; the callers that take one off disk or
    out of a UI do that once, at the edge.
    """
    lit = look["on"]
    if alert is not None:
        color, mix = alert.frame(now)
        if mix >= 1.0:
            return [color] * zone_count
        under = (frame_colors(look, zone_count, phase, telemetry) if lit
                 else [(0, 0, 0)] * zone_count)
        return [blend(c, color, mix) for c in under]
    if lit:
        return frame_colors(look, zone_count, phase, telemetry)
    return [(0, 0, 0)] * zone_count


def backlight_wanted(look, alert=None):
    """Whether the backlight should be lit at all.

    An alert lights a keyboard whose lighting is switched off, which is what
    lets the machine say something on a board someone has deliberately left
    dark -- and then put it back the way it was.
    """
    return bool(look["on"] or alert is not None)


# -- the hardware --

def apply_look(look, zones=4, phase=0.0, frame_only=False, alert=None,
               telemetry=None, now=None):
    """Put a look on the keyboard: colours first, then the backlight.

    Order matters when turning it on -- writing the colours while the board
    is dark and lighting it afterwards avoids a frame of whatever it held
    before.

    `frame_only` is the animation path: the colours and nothing else, because
    the backlight has not changed since the frame before. One write either
    way; the driver holds the firmware's table.
    """
    look = normalize_look(look)
    lit = backlight_wanted(look, alert)
    if lit:
        device.write_colors([
            to_hex(c) for c in compose(look, zones, phase, alert, telemetry, now)
        ])
    if not frame_only:
        device.write_backlight(lit)
