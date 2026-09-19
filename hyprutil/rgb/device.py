"""Direct HID control of the Firefly keyboard, over hidraw.

No helper binary, no libusb, no elevated capability. The vendor's own Windows
app drives this keyboard through hidapi -- hid_send_feature_report, hid_write
and hid_get_feature_report -- and those are exactly the three operations
Linux's hidraw exposes, so the whole protocol is reachable from Python. The
udev rule in system/udev/ already grants the hidraw node mode 0666, and
hidraw coexists with the kernel's HID driver instead of having to detach it.

Wire format, from the device's own HID report descriptor (vendor usage page
0xff01, interface 2):

    Feature report, 8 bytes   commands, and the reply to a query
    Output report, 64 bytes   the colour payload: 7 RGB triples, 6 bits each
    Input report, 64 bytes    unused here

Every command is a 9-byte Feature report; a query's reply is read back as
another 9-byte Feature report. Confirmed against the vendor app's own code,
which builds the packet at a fixed offset as:

    byte 0   0x00        HID report id (this collection declares none)
    byte 1   opcode
    byte 2-5 parameters, zero for a plain query
    byte 6   0x55        magic
    byte 7   0xAA        magic
    byte 8   0x00

Four transaction shapes, all lifted from the vendor app:

    set          send_feature(9)
    query        send_feature(9) -> get_feature(9)
    bulk set     send_feature(9) -> write output report (64+1)
    bulk get     send_feature(9) -> get_feature(9) -> read input report (64+1)

The ack in the middle of a bulk get is not optional: without that
get_feature the device never emits the input report at all.

Command table, recovered from the vendor app's embedded symbol strings, with
the opcodes confirmed in its disassembly. GET is always SET | 0x80:

    0x02 SetKeyboardoption      0x82 GetKeyboardoption
    0x04 SetButtonSensitivity   0x84 GetButtonSensitivity
    0x08 SetLEDType             0x88 GetLEDType
    0x09 StopMacroSending
    0x0C SetKeyMatrix_1         0x8C GetKeyMatrix_1
    0x0D SetKeyMatrix_2         0x8D GetKeyMatrix_2
    0x0F SetISPBootLoader                                  ** see below **
    0x10 SetMacroKey            0x90 GetMacroKey
    0x12 SetUserPicture         0x92 GetUserPicture
    0x13 SetRecoveryData                                   ** see below **
    0x21 SetCurrentProfile      0xA1 GetCurrentProfile
    0x30 SetSevenColor          0xB0 GetSevenColor
                                0x80 GetFWVersion

Only 0x08, 0x30, 0x88 and 0x80 are ever sent from here. 0x0F puts the
keyboard into its firmware bootloader and 0x13 rewrites recovery data; both
are guarded in the vendor app by their own magic parameters rather than the
usual 0x55/0xAA, which is a good reason never to sweep this device with
opcodes that are not in the table above.
"""
import contextlib
import fcntl
import logging
import os
import select
import time
from pathlib import Path

from ..util import CONFIG_DIR

logger = logging.getLogger(__name__)

HIDRAW_CLASS = Path("/sys/class/hidraw")
# uevent's HID_ID is "bus:vendor:product", zero-padded and upper case.
HID_ID = "0003:000004D9:0000A1CD"
# The RGB interface is the one whose report descriptor opens with usage page
# 0xff01 -- this keyboard exposes four hidraw nodes and the other three are
# the ordinary keyboard/consumer/mouse collections.
VENDOR_DESCRIPTOR_PREFIX = b"\x06\x01\xff"

CMD_SET_LED = 0x08
CMD_GET_LED = 0x88
CMD_SET_COLORS = 0x30
CMD_GET_COLORS = 0xB0
CMD_SET_PROFILE = 0x21
CMD_GET_PROFILE = 0xA1

# On-board lighting profiles. Selecting one makes the keyboard remember its
# lighting across unplug and reboot with no software running.
#
# Three, not the four the vendor's HWDef.bin declares as maxProfile: this
# keyboard accepts 0, 1 and 2 and silently ignores a request for 3, staying
# on whichever profile it was already showing. Measured, not assumed.
PROFILE_COUNT = 3

# Switching profile makes the keyboard load that profile's stored lighting,
# and it keeps doing so for a moment afterwards. Anything written during
# that window gets overwritten by the profile being loaded, so a send that
# follows a profile switch too closely reads back as the profile's old
# settings rather than what was just sent.
PROFILE_SETTLE_SECONDS = 0.15
# How many times to re-send before giving up on the readback agreeing.
VERIFY_ATTEMPTS = 4
MAGIC = (0x55, 0xAA)

COLOR_COUNT = 7
OUTPUT_REPORT_SIZE = 64
FEATURE_REPORT_SIZE = 8

# SetLEDType's seven parameters, named by the vendor app's own Qt signature
# string: "type,brightness,speed,direction,color,bl0,bl1".
#
# Worth spelling out because this was wrong for a long time: brightness and
# speed were transposed, so the brightness setting was driving the animation
# speed while brightness itself sat hard-coded at 0x3f (maximum). The stray
# "the device's default of 1 renders washed out" note came from the same
# mix-up -- 1 in that slot is the fastest animation, not the dimmest light.
BRIGHTNESS_MAX = 63
DEFAULT_BRIGHTNESS = BRIGHTNESS_MAX
# 1 is fastest, 7 slowest. The vendor UI's slider sits at 4 by default.
SPEED_MIN, SPEED_MAX = 1, 7
DEFAULT_SPEED = 4
DIRECTION_RIGHT, DIRECTION_LEFT = 0, 1
# bl0/bl1 are device-owned and opaque: the vendor app never computes them,
# it reads the pair back with GetLEDType and echoes them straight into the
# next SetLEDType. Different units report different pairs, so hard-coding
# one unit's values (0xc4/0x3b) was wrong in principle even though it worked
# here. These are only the fallback for a device that refuses the readback.
FALLBACK_BL = (0xC4, 0x3B)

# Each message of a send has to be given a moment to land. The colour payload
# is an Output report on an interrupt endpoint while the commands around it
# are Feature reports on the control endpoint, and USB orders transfers only
# within a single endpoint -- so back-to-back sends let the effect command
# reach the device while the colour payload is still in flight, and the
# effect gets applied against the previous colours. That is the "it applied
# the effect but not the colours" case.
SETTLE_SECONDS = 0.02
# The device still drops an occasional colour upload even when paced, so the
# sequence is sent twice. This used to be done by invoking a helper binary
# twice over, paying for two device opens each time.
REPEAT = 2
# The vendor app waits 500ms on its own bulk reads.
READ_TIMEOUT_SECONDS = 0.5

_cached_path = None


def _ioc(direction, type_char, nr, size):
    return (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr


def _hidiocsfeature(size):
    return _ioc(3, "H", 0x06, size)


def _hidiocgfeature(size):
    return _ioc(3, "H", 0x07, size)


def find_path(refresh=False):
    """Path of the keyboard's vendor hidraw node, or None if not plugged in.

    Resolved by matching the descriptor rather than by remembering a device
    number: hidraw numbering is assigned in hotplug order, so the node moves
    between reboots and replugs.
    """
    global _cached_path
    if not refresh and _cached_path is not None and _cached_path.exists():
        return _cached_path
    try:
        entries = sorted(HIDRAW_CLASS.glob("hidraw*"))
    except OSError:
        return None
    for entry in entries:
        try:
            uevent = (entry / "device" / "uevent").read_text()
            if HID_ID not in uevent:
                continue
            descriptor = (entry / "device" / "report_descriptor").read_bytes()
        except OSError:
            continue
        if descriptor.startswith(VENDOR_DESCRIPTOR_PREFIX):
            _cached_path = Path("/dev") / entry.name
            return _cached_path
    _cached_path = None
    return None


def available():
    return find_path() is not None


def _command(opcode, *payload):
    packet = [opcode, 0, 0, 0, 0, MAGIC[0], MAGIC[1], 0]
    for i, value in enumerate(payload):
        packet[1 + i] = value
    return bytes(packet)


def _set_feature(fd, packet):
    # A hidraw feature buffer is prefixed with the report id, which is 0 for
    # this device -- its vendor collection declares no Report ID.
    fcntl.ioctl(fd, _hidiocsfeature(FEATURE_REPORT_SIZE + 1), bytearray(b"\x00" + packet), True)


def _get_feature(fd):
    buf = bytearray(FEATURE_REPORT_SIZE + 1)
    fcntl.ioctl(fd, _hidiocgfeature(FEATURE_REPORT_SIZE + 1), buf, True)
    return bytes(buf[1:])


def _write_colors(fd, colors):
    # Channels are 6-bit on this device, so every byte is scaled to 0-63.
    payload = bytes(channel // 4 for color in colors for channel in color)
    report = b"\x00" + payload + bytes(OUTPUT_REPORT_SIZE - len(payload))
    written = os.write(fd, report)
    if written != len(report):
        raise OSError(f"short colour write: {written} of {len(report)} bytes")


def read_state(fd):
    """The device's live LED parameters, as the seven named fields.

    Ask with GetLEDType first rather than reading the feature report bare:
    the register holds the reply to the last command, so a bare read after
    some other command returns that command's answer instead of LED state.
    """
    _set_feature(fd, _command(CMD_GET_LED))
    time.sleep(SETTLE_SECONDS)
    reply = _get_feature(fd)
    return {
        "effect": reply[1],
        "brightness": reply[2],
        "speed": reply[3],
        "direction": reply[4],
        "color_idx": reply[5],
        "bl0": reply[6],
        "bl1": reply[7],
    }


def _send_once(fd, effect, colors, color_idx, brightness, speed, direction, bl, settle):
    _set_feature(fd, _command(CMD_SET_COLORS))
    time.sleep(settle)
    _write_colors(fd, colors)
    time.sleep(settle)
    _set_feature(fd, bytes([CMD_SET_LED, effect, brightness, speed, direction, color_idx, bl[0], bl[1]]))


def read_profile(fd):
    """Which of the keyboard's own profiles is selected (0-based)."""
    _set_feature(fd, _command(CMD_GET_PROFILE))
    time.sleep(SETTLE_SECONDS)
    return _get_feature(fd)[1]


def set_profile(fd, profile):
    """Select one of the keyboard's own profiles.

    Binding a preset to one of these is what makes lighting stick without
    this software running at all -- the keyboard keeps the profile.
    """
    if not 0 <= profile < PROFILE_COUNT:
        raise ValueError(f"profile must be 0-{PROFILE_COUNT - 1}")
    _set_feature(fd, _command(CMD_SET_PROFILE, profile))
    time.sleep(PROFILE_SETTLE_SECONDS)


def set_led(fd, effect, brightness=DEFAULT_BRIGHTNESS, speed=DEFAULT_SPEED,
            direction=DIRECTION_RIGHT, color_idx=0, bl=None):
    """Set the effect and its parameters without touching the colour palette.

    `effect` is a raw device code: 0-11 for the built-in effects.
    """
    if bl is None:
        try:
            current = read_state(fd)
            bl = (current["bl0"], current["bl1"])
        except OSError:
            bl = FALLBACK_BL
    _set_feature(fd, bytes([CMD_SET_LED, effect, brightness, speed, direction, color_idx, bl[0], bl[1]]))
    time.sleep(SETTLE_SECONDS)


def read_colors(fd):
    """The palette the device reports it is holding, as 7 (r, g, b) triples.

    Diagnostics only, and deliberately not used to verify a write: measured
    against known payloads this lags by one transaction, returning the
    previously stored palette rather than the one just sent.
    """
    _set_feature(fd, _command(CMD_GET_COLORS))
    time.sleep(SETTLE_SECONDS)
    _get_feature(fd)  # the ack; the input report is not emitted without it
    if not select.select([fd], [], [], READ_TIMEOUT_SECONDS)[0]:
        raise OSError("keyboard did not return its colour buffer")
    raw = os.read(fd, OUTPUT_REPORT_SIZE)
    return [tuple(min(255, raw[i + c] * 4) for c in range(3)) for i in range(0, COLOR_COUNT * 3, 3)]


LOCK_PATH = CONFIG_DIR / "rgb" / ".device.lock"


@contextlib.contextmanager
def _device_lock():
    """Serialize keyboard access across processes.

    The in-process lock in controller.py is not enough: the settings app,
    the tray and the automation daemon are three separate processes, and a
    send is a command plus an output report that must not be interleaved
    with anything else. One stray feature report from another process
    landing mid-send leaves the device showing one call's effect with the
    other call's colours.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def opened():
    """Open the keyboard, re-resolving its node once if it moved.

    Everything that talks to the device goes through here, so the hotplug
    retry, the cross-process lock and the close are each written once.
    """
    path = find_path()
    if path is None:
        raise RuntimeError("Firefly keyboard not found -- is it plugged in?")
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        # A node that vanished between find_path() and here means it was
        # unplugged or renumbered; re-resolve once before giving up.
        path = find_path(refresh=True)
        if path is None:
            raise RuntimeError("Firefly keyboard not found -- is it plugged in?") from e
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_NONBLOCK)
        except OSError as e2:
            raise RuntimeError(f"cannot open {path}: {e2}") from e2
    try:
        with _device_lock():
            yield fd
    finally:
        os.close(fd)


def _validate(brightness, speed):
    if not 0 <= brightness <= BRIGHTNESS_MAX:
        raise ValueError(f"brightness must be 0-{BRIGHTNESS_MAX}")
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise ValueError(f"speed must be {SPEED_MIN}-{SPEED_MAX}")


def apply(effect, colors, color_idx, brightness=DEFAULT_BRIGHTNESS, speed=DEFAULT_SPEED,
          direction=DIRECTION_RIGHT, profile=None, settle=SETTLE_SECONDS, repeat=REPEAT):
    """Send one look. `colors` is 7 (r, g, b) triples of 0-255.

    Raises RuntimeError if the keyboard is absent or refuses the command.
    """
    if len(colors) != COLOR_COUNT:
        raise ValueError(f"exactly {COLOR_COUNT} colors required, got {len(colors)}")
    _validate(brightness, speed)
    with opened() as fd:
        if profile is not None:
            # Select the keyboard's own profile first, so what follows is
            # stored into that profile rather than whichever was current.
            set_profile(fd, profile)
        # bl0/bl1 belong to the device; carry its own pair forward rather
        # than imposing one taken from another unit.
        try:
            current = read_state(fd)
            bl = (current["bl0"], current["bl1"])
        except OSError:
            bl = FALLBACK_BL
        want = {"effect": effect, "brightness": brightness, "speed": speed,
                "direction": direction, "color_idx": color_idx}
        for i in range(repeat):
            _send_once(fd, effect, colors, color_idx, brightness, speed, direction, bl, settle)
            if i + 1 < repeat:
                time.sleep(settle)
        # The device reports its own state back, so a command that did not
        # take is detectable instead of assumed. Re-send rather than fail on
        # the first disagreement: a profile the keyboard is still loading
        # keeps overwriting what was just written, for a window that varies
        # run to run, so a single fixed delay does not cover it.
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                got = read_state(fd)
            except OSError as e:
                logger.debug("could not read back keyboard state: %s", e)
                return
            if {k: got[k] for k in want} == want:
                return
            if attempt + 1 == VERIFY_ATTEMPTS:
                break
            logger.info("keyboard state mismatch after send, re-sending (attempt %d)", attempt + 2)
            time.sleep(PROFILE_SETTLE_SECONDS * (attempt + 1))
            _send_once(fd, effect, colors, color_idx, brightness, speed, direction, bl, settle)
        raise RuntimeError("keyboard did not accept the lighting command")


def firmware_version(fd):
    """Reported firmware bytes -- diagnostics only."""
    _set_feature(fd, _command(0x80))
    time.sleep(SETTLE_SECONDS)
    return _get_feature(fd)[1:]
