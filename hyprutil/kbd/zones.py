"""The laptop keyboard's own lighting: four zones, through the BIOS mailbox.

The model, and every constant in it, is the one the vendor software uses:
command 0x20009 carries the lighting calls, the colour table is a 128-byte
block whose zone `i` sits at bytes 25+3i..27+3i, and the backlight is a
single byte whose bit 7 is "lit" and whose low bits are a level the firmware
does not act on. Which keyboard is fitted comes from the *other* command id
(0x20008 / 0x2B).

Two things follow from that and are worth stating, because they look like
bugs otherwise:

* Brightness is done in software, by scaling the colours before they are
  written. The level bits of the backlight byte change nothing on any board
  anyone has measured, so the vendor app writes 100 there and dims the
  colours instead. This does the same.
* Effects are frames, not firmware modes. There is no animation the firmware
  will run for you (its own 0x06/0x07 animation table is documented as
  taking no effect), so breathe, cycle and wave are computed here and the
  colour table is rewritten ~8 times a second while one is running.

Zone numbering is HP's: 0 right, 1 middle, 2 left, 3 WASD. If a board turns
out to be mirrored, DISPLAY_ORDER and ZONE_NAMES are the only things to
change -- everything else addresses zones by index.
"""
import colorsys
import logging
import math

from . import mailbox

logger = logging.getLogger(__name__)

# Lighting opcodes, all under CMD_LIGHTING except the keyboard type.
OP_KBD_TYPE = 0x2B        # under CMD_DEFAULT, out4: [0] = keyboard type
OP_SUPPORTED = 0x01       # out128: [0] bit 0 = lighting supported (legacy boards)
OP_COLOR_GET = 0x02       # out128: the colour table
OP_COLOR_SET = 0x03       # 128 bytes in, out4
OP_LIGHT_GET = 0x04       # out128: [0] = backlight byte (a smaller read is refused)
OP_LIGHT_SET = 0x05       # {byte,0,0,0} in, out4

COLOR_OFFSET = 25         # zone i = bytes 25+3i .. 27+3i, R G B
TABLE_SIZE = 128
ON_FLAG = 0x80
FULL_LEVEL = 100          # the level byte the vendor app always writes

# Keyboard type byte (0x20008/0x2B).
TYPE_NAMES = {
    0: "standard layout",
    1: "four zones with numpad",
    2: "four zones",
    3: "per-key RGB",
    4: "one zone with numpad",
    5: "one zone",
}
KIND_NONE, KIND_ZONES, KIND_PERKEY = "none", "zones", "perkey"

ZONE_RIGHT, ZONE_MIDDLE, ZONE_LEFT, ZONE_WASD = 0, 1, 2, 3
ZONE_NAMES = {ZONE_LEFT: "Left", ZONE_MIDDLE: "Middle", ZONE_RIGHT: "Right", ZONE_WASD: "WASD"}
# Left to right across the keyboard, which is the order a person reads them
# in -- not the order the firmware stores them in.
DISPLAY_ORDER = [ZONE_LEFT, ZONE_MIDDLE, ZONE_RIGHT, ZONE_WASD]

# The vendor's factory colours for zones 0..3 (right, middle, left, WASD).
FACTORY_COLORS = ["0f84fa", "710ffa", "f9350f", "faac0f"]

EFFECTS = ["static", "breathe", "cycle", "wave"]
EFFECT_LABELS = {
    "static": "Static",
    "breathe": "Breathe",
    "cycle": "Cycle",
    "wave": "Wave",
}
ANIMATED = {"breathe", "cycle", "wave"}

SPEED_MIN, SPEED_MAX = 1, 5
DEFAULT_SPEED = 3
# What one speed step is worth; the middle one is 1.0 by definition.
SPEED_FACTORS = {1: 0.35, 2: 0.6, 3: 1.0, 4: 1.6, 5: 2.4}
FRAME_SECONDS = 0.12      # ~8 frames a second, as the vendor app animates
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


class Keyboard:
    """What the firmware says is fitted, and whether we can drive it."""

    def __init__(self, type_byte, kind, zones, numpad, declared):
        self.type_byte = type_byte
        self.kind = kind
        self.zones = zones
        self.numpad = numpad
        self.declared = declared

    @property
    def inert(self):
        """True on per-key boards: every lighting call is answered and none
        of them reaches the LEDs. 128 bytes cannot address 176 keys, and
        owners of three separate per-key boards have confirmed the interface
        does nothing. Those keyboards need their own USB HID protocol, which
        nobody has published for the pre-2025 models."""
        return self.kind == KIND_PERKEY

    @property
    def usable(self):
        return self.kind == KIND_ZONES

    @property
    def describe(self):
        if self.kind == KIND_PERKEY:
            return "per-key (not driveable through the firmware)"
        if self.kind == KIND_NONE:
            return "no controllable lighting"
        return "1 zone" if self.zones == 1 else f"{self.zones} zones"

    def as_dict(self):
        return {
            "type": self.type_byte,
            "type_name": TYPE_NAMES.get(self.type_byte, f"unknown ({self.type_byte})"),
            "kind": self.kind,
            "zones": self.zones,
            "numpad": self.numpad,
            "declared": self.declared,
            "describe": self.describe,
            "usable": self.usable,
        }


def read_keyboard_type():
    """The keyboard type byte, asked at whichever answer size works."""
    last = None
    for out_size in (4, 128):
        try:
            data = mailbox.call(mailbox.CMD_DEFAULT, OP_KBD_TYPE, out_size=out_size)
        except mailbox.MailboxUnavailable:
            raise
        except mailbox.MailboxError as e:
            last = e
            continue
        if data:
            return data[0]
    raise last or mailbox.MailboxError("no answer to the keyboard type query")


def detect():
    """Ask the firmware what keyboard this is, then prove it by reading the
    colour table. Returns a Keyboard, or None when there is nothing here to
    drive.

    Two independent questions, and answering only one of them is the mistake
    to avoid: 0x20009/0x01 says whether there is a controllable backlight,
    0x20008/0x2B says what the layout is. Type 0 means "standard layout",
    not "no lighting", so bailing out on it would drop every board that
    reports a plain layout. The colour table answering is the real proof,
    in both directions.
    """
    declared = False
    try:
        data = mailbox.call(mailbox.CMD_LIGHTING, OP_SUPPORTED, out_size=128)
        declared = bool(data and data[0] & 1)
    except mailbox.MailboxUnavailable:
        raise
    except mailbox.MailboxError as e:
        logger.debug("lighting support probe: %s", e)

    type_byte = 0
    try:
        type_byte = read_keyboard_type()
    except mailbox.MailboxUnavailable:
        raise
    except mailbox.MailboxError as e:
        # Not fatal, and not unusual: boards are known that refuse every
        # 0x20008 command while answering every lighting one. A standard
        # layout is what both reference implementations assume here, and
        # the colour table below is the real proof either way.
        logger.info("keyboard type query failed (%s); assuming standard layout", e)
    if type_byte == 0xFF:  # -1 from the firmware means the same as 0 here
        type_byte = 0

    if type_byte == 3:
        kind, zones = KIND_PERKEY, 4
    elif type_byte in (4, 5):
        kind, zones = KIND_ZONES, 1
    else:  # 0 standard, 1 numpad, 2 tenkeyless -- all four-zone boards
        kind, zones = KIND_ZONES, 4
    kbd = Keyboard(type_byte, kind, zones, type_byte in (1, 4), declared)

    try:
        read_colors(kbd.zones)
        read_backlight()
    except mailbox.MailboxUnavailable:
        raise
    except mailbox.MailboxError as e:
        logger.info("keyboard lighting: none (type %d, colour table: %s)", type_byte, e)
        return None
    if kbd.inert:
        logger.info(
            "keyboard lighting: type 3 (per-key). The firmware answers these "
            "calls and drives nothing on such boards."
        )
    logger.info(
        "keyboard lighting: type %d -> %s, firmware %s support",
        type_byte, kbd.describe, "declares" if declared else "declares no",
    )
    return kbd


# -- colours --

# The best 128-byte picture of the table we have: whatever the firmware
# last showed us, with everything we have written since laid over it. It
# serves two purposes -- an effect writes a frame eight times a second and
# should not re-read the table for each one, and the transport may not be
# able to show us the whole table at all (see mailbox.py on acpi_call's
# result buffer), so what it cannot show has to be remembered instead.
_table_cache = None
# How much of a read-back the firmware has actually shown us, for the probe.
last_read_length = 0

# Enough of the table to be worth acting on: the first 25 bytes belong to
# something else and are written back untouched, so a read that cannot even
# reach the colours is a read that proves nothing.
MIN_USEFUL_READ = COLOR_OFFSET


def read_table():
    """The lighting table as the firmware holds it, as far as we can see it.

    Short answers are normal rather than fatal: the transport, not the
    firmware, decides how much of the reply comes back, and everything this
    module needs to *read* lives in the first 34 bytes.
    """
    global _table_cache, last_read_length
    data = mailbox.call(mailbox.CMD_LIGHTING, OP_COLOR_GET, b"\x00", out_size=128)
    last_read_length = len(data)
    if len(data) < MIN_USEFUL_READ:
        raise mailbox.MailboxError(
            f"colour table came back as {len(data)} bytes, too short to use"
        )
    table = bytearray(_table_cache) if _table_cache else bytearray(TABLE_SIZE)
    table[:len(data)] = data[:TABLE_SIZE]
    _table_cache = bytearray(table)
    return table


def readable_zones():
    """How many zones the last read-back actually showed.

    Four zones need 37 bytes of the table and the stock acpi_call shows 34,
    so the WASD zone usually cannot be read back -- only written. The app's
    own saved look is what the UI displays, so this costs nothing but the
    ability to adopt a colour someone else set on that one zone.
    """
    return max(0, (min(last_read_length, TABLE_SIZE) - COLOR_OFFSET) // 3)


def read_colors(zones=4):
    """The zone colours the firmware is holding, as hex strings.

    Only the zones the transport let us see; see readable_zones().
    """
    table = read_table()
    out = []
    for i in range(min(zones, readable_zones())):
        r, g, b = table[COLOR_OFFSET + 3 * i: COLOR_OFFSET + 3 * i + 3]
        out.append(f"{r:02x}{g:02x}{b:02x}")
    return out


# Which answer size this firmware accepts for a command that only writes.
# The vendor app asks for 4 bytes; this board answers AE_AML_OPERAND_VALUE
# to at least some 4-byte requests, so the working size is found once and
# then remembered rather than guessed per call.
SET_OUT_SIZES = (4, 128)
_set_out_size = None


def _set(command, command_type, data):
    """A command that writes, at whatever answer size this firmware takes."""
    global _set_out_size
    sizes = (_set_out_size,) if _set_out_size else SET_OUT_SIZES
    last = None
    for out_size in sizes:
        try:
            result = mailbox.call(command, command_type, data, out_size=out_size)
        except mailbox.MailboxUnavailable:
            raise
        except mailbox.MailboxError as e:
            last = e
            continue
        if _set_out_size != out_size:
            logger.info("writes use a %d-byte answer size on this firmware", out_size)
            _set_out_size = out_size
        return result
    raise last


def write_colors(colors, reuse_table=False):
    """Write zone colours, given as hex strings or (r, g, b) triples.

    Read-modify-write, the way the vendor app does it: the first 25 bytes of
    the table are something else's and are handed back untouched.
    `reuse_table` writes against the last table read instead of reading it
    again, which is what an animation frame wants -- nothing else on the
    machine writes this table.
    """
    global _table_cache
    table = bytearray(_table_cache) if (reuse_table and _table_cache) else read_table()
    if len(table) < TABLE_SIZE:  # never sent short, whatever we could read
        table = table + bytearray(TABLE_SIZE - len(table))
    for i, color in enumerate(colors):
        if COLOR_OFFSET + 3 * i + 3 > TABLE_SIZE:
            break
        r, g, b = to_rgb(color)
        table[COLOR_OFFSET + 3 * i] = r
        table[COLOR_OFFSET + 3 * i + 1] = g
        table[COLOR_OFFSET + 3 * i + 2] = b
    _set(mailbox.CMD_LIGHTING, OP_COLOR_SET, bytes(table))
    _table_cache = table


def read_backlight():
    """The backlight byte: bit 7 is lit, the low bits are a level."""
    data = mailbox.call(mailbox.CMD_LIGHTING, OP_LIGHT_GET, b"\x00", out_size=128)
    if not data:
        raise mailbox.MailboxError("empty backlight reply")
    return data[0]


def write_backlight(on, level=FULL_LEVEL):
    value = (max(0, min(100, int(level))) | (ON_FLAG if on else 0)) & 0xFF
    _set(mailbox.CMD_LIGHTING, OP_LIGHT_SET, bytes([value, 0, 0, 0]))


def is_lit(backlight_byte):
    return bool(backlight_byte & ON_FLAG)


# -- colour helpers --

def to_rgb(color):
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


def effect_frame(effect, phase, colors, index):
    """The colour of one zone at one moment. Also drives the UI preview, so
    what is drawn and what is written come from the same function."""
    if effect == "breathe":
        return scale(colors[index], 0.15 + 0.85 * (0.5 + 0.5 * math.sin(phase * 1.6)))
    if effect == "cycle":
        return from_hue(phase * 25)
    if effect == "wave":
        return from_hue(phase * 25 + index * (360.0 / max(1, len(colors))))
    return to_rgb(colors[index])


def frame_colors(look, zones, phase=0.0):
    """Every zone's colour for this look at this phase, brightness applied."""
    base = zone_colors(look, zones)
    factor = max(MIN_SCALE, look["brightness"] / 100.0)
    return [
        scale(effect_frame(look["effect"], phase, base, i), factor)
        for i in range(len(base))
    ]


def apply_look(look, zones=4, phase=0.0, frame_only=False):
    """Put a look on the keyboard: colours first, then the backlight.

    Order matters when turning it on -- writing the colours while the board
    is dark and lighting it afterwards avoids a frame of whatever it held
    before.

    `frame_only` is the animation path: one colour write, no table read and
    no backlight write, because neither has changed since the frame before.
    """
    look = normalize_look(look)
    if look["on"]:
        write_colors(frame_colors(look, zones, phase), reuse_table=frame_only)
    if not frame_only:
        write_backlight(look["on"])
