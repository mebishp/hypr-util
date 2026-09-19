"""The HP BIOS WMI mailbox, reached from userspace.

Everything the laptop's own keyboard lighting understands lives behind one
ACPI-WMI method. On Windows it is the `hpqBIntM` WMI class; on Linux the same
method is `\\_SB.WMID.WMAA` under the `PNP0C14` WMI device whose GUID is
5FB7F034-2C63-45E9-BE91-3D44E2C707E4 -- confirmed on this machine by reading
`object_id` ("AA") out of /sys/bus/wmi/devices.

Why /proc/acpi/call. The kernel's own hp-wmi driver speaks this mailbox but
exposes only fans, platform profile and a handful of sensors through sysfs;
it has no lighting code at all, and the WMI bus creates no character device
for this GUID, so there is no in-kernel route to the lighting commands. The
acpi_call module (packaged: `pacman -S acpi_call`) takes an ACPI method name
and arguments from userspace and hands back the result, which is exactly the
one primitive missing. Writing the mailbox needs root, which is why the
daemon in service.py exists.

Wire format, identical to what hp-wmi.c builds in the kernel and to what the
vendor app sends through WMI:

    method:  \\_SB.WMID.WMAA(instance = 0, method_id, buffer)
    buffer:  u32 signature 0x55434553 ("SECU")
             u32 command
             u32 command type
             u32 data size
             u8  data[max(data size, 128)]
    result:  u32 signature passthrough
             u32 return code (non-zero = the firmware refused)
             u8  data[...]

The data block is padded to 128 bytes even when the command carries one byte
or none, while the size field keeps saying how many of them mean anything --
hp-wmi.c does the same (`actual_insize = max(insize, 128)`) and this firmware
insists on it. Measured here: a zero-length block makes the type query fail
with AE_AML_OPERAND_VALUE, and a one-byte block makes the colour table come
back as 34 bytes instead of 128.

`method_id` is not an opcode: it selects which output size the firmware
should answer with (1 = 0 bytes, 2 = 4, 3 = 128, 4 = 1024, 5 = 4096), the
same encoding hp-wmi.c calls encode_outsize_for_pvsz. Asking for the wrong
one is a real error -- the backlight byte answers only to a 128-byte read.

One limit is the transport's, not the firmware's, and it is worth knowing
before reading a surprising number in a log. acpi_call prints its answer
into a fixed text buffer and stops at `min(length, avail / 6)` bytes; the
stock module (mkottman, which is what Arch packages) sizes that buffer at
256, so a 136-byte reply comes back as 42 bytes -- the 8-byte header plus 34
of the 128. Nothing is wrong with the call: the missing bytes were simply
never printed. The lighting code is written to work inside that window, and
takes the whole table when a build with a larger buffer (1024 or more) is
installed. Writes are unaffected -- only the reply is text.
"""
import fcntl
import logging
import os
import re
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CALL_PATH = Path("/proc/acpi/call")
WMI_DEVICE = Path("/sys/bus/wmi/devices/5FB7F034-2C63-45E9-BE91-3D44E2C707E4-8")
METHOD = "\\_SB.WMID.WMAA"
MODULE = "acpi_call"

SIGNATURE = 0x55434553  # "SECU", little-endian
# Shortest data block the firmware will accept, whatever the command says it
# is sending. See the note in the module docstring.
MIN_DATA_SIZE = 128

CMD_DEFAULT = 0x20008   # system, fans, keyboard type
CMD_LIGHTING = 0x20009  # colours and backlight

# out size -> WMI method id, as hp-wmi.c encodes it
_METHOD_IDS = {0: 1, 4: 2, 128: 3, 1024: 4, 4096: 5}

# One ACPI call at a time, across processes. /proc/acpi/call is a single
# global buffer in the module: two callers interleaving a write and a read
# hand each other's answers back.
LOCK_PATH = Path("/run/hypr-util/acpi-call.lock")

_RESULT_BYTE = re.compile(r"0x([0-9a-fA-F]{1,2})")


class MailboxError(RuntimeError):
    """The mailbox could not be used, or refused a command."""


class MailboxUnavailable(MailboxError):
    """The transport is missing: no acpi_call, no permission, not an HP."""


class BiosError(MailboxError):
    """The firmware answered with a non-zero return code."""

    def __init__(self, command, command_type, code):
        super().__init__(
            f"BIOS refused command 0x{command:X}/0x{command_type:02X} "
            f"with return code {code}"
        )
        self.command = command
        self.command_type = command_type
        self.code = code


def wmi_present():
    """True when this machine has the HP BIOS mailbox at all."""
    return WMI_DEVICE.exists()


def transport_ready():
    """True when a call could be made right now (module loaded, and root)."""
    return CALL_PATH.exists() and os.access(CALL_PATH, os.W_OK)


def ensure_module():
    """Load acpi_call if it is not loaded yet. Root only; safe to repeat."""
    if CALL_PATH.exists():
        return True
    if os.geteuid() != 0:
        return False
    try:
        subprocess.run(
            ["modprobe", MODULE], check=True, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("could not load %s: %s", MODULE, e)
        return False
    return CALL_PATH.exists()


def _explain_unavailable():
    if not wmi_present():
        return "this machine has no HP BIOS WMI interface"
    if not CALL_PATH.exists():
        return (
            "the acpi_call kernel module is not loaded "
            "(install it with 'pacman -S acpi_call', then 'modprobe acpi_call')"
        )
    return f"{CALL_PATH} is not writable (the mailbox needs root)"


def _parse_result(text):
    """Turn acpi_call's answer into bytes.

    It prints a buffer as "{0x53, 0x45, ...}", an integer as "0x0", and an
    ACPI failure as "Error: AE_...". Anything that is not a buffer means the
    call did not reach the firmware, so it is an error here rather than an
    empty result.
    """
    text = text.strip().strip("\0")
    if not text or text == "not called":
        return None
    if text.startswith("Error:"):
        raise MailboxError(f"acpi_call: {text[6:].strip()}")
    if not text.startswith("{"):
        raise MailboxError(f"mailbox returned {text!r}, not a buffer")
    return bytes(int(m, 16) for m in _RESULT_BYTE.findall(text))


def _invoke(payload):
    """One write/read round trip through /proc/acpi/call, serialized."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Unbuffered, one write() per call: the module parses each write as a
        # complete request and keeps the answer until the next one.
        fd = os.open(str(CALL_PATH), os.O_RDWR)
        try:
            os.write(fd, payload.encode())
            os.lseek(fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
    except PermissionError as e:
        raise MailboxUnavailable(_explain_unavailable()) from e
    except FileNotFoundError as e:
        raise MailboxUnavailable(_explain_unavailable()) from e
    except OSError as e:
        raise MailboxError(f"{CALL_PATH}: {e}") from e
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            os.close(lock)
    return b"".join(chunks).decode("ascii", "replace")


def call(command, command_type, data=b"", out_size=128):
    """Send one mailbox command and return the firmware's data bytes.

    Raises MailboxUnavailable when the transport is missing and BiosError
    when the firmware answers with a non-zero return code.
    """
    if out_size not in _METHOD_IDS:
        raise ValueError(f"out size must be one of {sorted(_METHOD_IDS)}")
    if not transport_ready() and not ensure_module():
        raise MailboxUnavailable(_explain_unavailable())
    data = bytes(data)
    # The size field stays honest about how much of the block matters; only
    # the block itself is padded out.
    args = (struct.pack("<IIII", SIGNATURE, command, command_type, len(data))
            + data.ljust(MIN_DATA_SIZE, b"\x00"))
    payload = f"{METHOD} 0x0 0x{_METHOD_IDS[out_size]:x} b{args.hex()}"
    raw = _parse_result(_invoke(payload))
    if raw is None:
        raise MailboxError("acpi_call did not answer the call")
    if len(raw) < 8:
        raise MailboxError(f"short mailbox reply ({len(raw)} bytes)")
    _, code = struct.unpack_from("<II", raw, 0)
    if code:
        raise BiosError(command, command_type, code)
    body = raw[8:8 + out_size] if out_size else b""
    if len(body) < out_size:
        # Handed back rather than refused here: some commands only mean
        # their first byte, and a caller that needs more of the block
        # (read_table) checks the length itself. Worth a line in the log
        # either way -- a short answer is how an under-sized input block
        # shows up, and that was a real bug once.
        logger.info(
            "command 0x%X/0x%02X answered %d bytes, expected %d",
            command, command_type, len(body), out_size,
        )
    return body
