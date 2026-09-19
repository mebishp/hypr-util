"""The root side of laptop keyboard lighting, and the client that talks to it.

The mailbox needs root, and the settings app is a desktop application, so
the two are split: a small daemon owns the hardware and a socket carries
the handful of things a user may ask of it. The alternative -- handing the
desktop session write access to /proc/acpi/call -- would be handing it the
ability to call any ACPI method on the machine, which is a much larger
thing than a keyboard colour.

The socket is /run/hypr-util/kbd.sock, owned by whoever owns the config
directory the daemon was pointed at, mode 0600. The protocol is one JSON
object per line, one reply per request:

    {"op": "status"}                 what keyboard, what look, is it lit
    {"op": "apply", "look": {...}}   apply a look and keep showing it
    {"op": "reload"}                 re-read the saved look and apply it
    {"op": "probe"}                  raw diagnostics, for `hyprutil kbd probe`

The daemon also owns the animation: an effect is frames written ~8 times a
second, and it has to keep running with no GUI open, through the screen
being locked, for as long as the effect is selected.
"""
import json
import logging
import os
import socket
import threading
import time

from . import mailbox, state, zones

logger = logging.getLogger(__name__)

RUN_DIR = "/run/hypr-util"
SOCKET_PATH = os.path.join(RUN_DIR, "kbd.sock")
TIMEOUT_SECONDS = 4
# How long to wait before asking the firmware again after it refused to
# answer at all. A board that has no lighting should not be probed in a hot
# loop for the life of the machine.
REDETECT_SECONDS = 30


class ServiceUnavailable(RuntimeError):
    """The daemon is not running, or would not answer."""


# --------------------------------------------------------------- client ---

class Client:
    """Talks to the daemon. Cheap to construct; one connection per call."""

    def __init__(self, path=None, timeout=TIMEOUT_SECONDS):
        # Resolved now rather than bound as a default at import time, so a
        # test (or a second daemon) can move the socket.
        self.path = path or SOCKET_PATH
        self.timeout = timeout

    def request(self, op, **fields):
        payload = json.dumps({"op": op, **fields}) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                sock.sendall(payload.encode())
                chunks = []
                while not (chunks and chunks[-1].endswith(b"\n")):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except (OSError, socket.timeout) as e:
            raise ServiceUnavailable(f"keyboard lighting service: {e}") from e
        try:
            return json.loads(b"".join(chunks).decode() or "{}")
        except json.JSONDecodeError as e:
            raise ServiceUnavailable(f"malformed reply from the service: {e}") from e

    def running(self):
        try:
            self.request("status")
            return True
        except ServiceUnavailable:
            return False


# --------------------------------------------------------------- daemon ---

class Daemon:
    """Owns the keyboard: detection, the saved look, and the effect frames."""

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path
        self._lock = threading.Lock()      # serializes every mailbox call
        self._keyboard = None
        self._look = zones.normalize_look({})
        self._error = None
        self._last_detect = 0.0
        self._phase = 0.0
        self._stop = threading.Event()
        self._effect_thread = None

    # -- hardware --

    def _detect(self, force=False):
        """Find the keyboard, at most every REDETECT_SECONDS."""
        now = time.monotonic()
        if self._keyboard is not None and not force:
            return self._keyboard
        if not force and now - self._last_detect < REDETECT_SECONDS:
            return self._keyboard
        self._last_detect = now
        try:
            self._keyboard = zones.detect()
            self._error = None if self._keyboard else "no controllable keyboard lighting"
        except mailbox.MailboxError as e:
            self._keyboard = None
            self._error = str(e)
            logger.warning("keyboard lighting unavailable: %s", e)
        return self._keyboard

    def _apply(self, look, phase=None, frame_only=False):
        """Write one look. Caller holds the lock."""
        kbd = self._detect()
        if kbd is None or not kbd.usable:
            raise mailbox.MailboxError(self._error or "no controllable keyboard lighting")
        zones.apply_look(
            look, kbd.zones, self._phase if phase is None else phase, frame_only=frame_only
        )

    def apply(self, look):
        look = zones.normalize_look(look)
        with self._lock:
            self._look = look
            self._phase = 0.0
            self._apply(look)
        self._sync_effect()
        return look

    def reload(self):
        """Re-read the saved look and put it back on the keyboard."""
        return self.apply(state.read_current())

    # -- effects --

    def _sync_effect(self):
        running = self._look["on"] and self._look["effect"] in zones.ANIMATED
        if running and self._effect_thread is None:
            self._effect_thread = threading.Thread(
                target=self._effect_loop, name="kbd-effect", daemon=True
            )
            self._effect_thread.start()

    def _effect_loop(self):
        failures = 0
        while not self._stop.is_set():
            time.sleep(zones.FRAME_SECONDS)
            with self._lock:
                look = self._look
                if not (look["on"] and look["effect"] in zones.ANIMATED):
                    self._effect_thread = None
                    return
                self._phase += zones.PHASE_STEP * zones.SPEED_FACTORS[look["speed"]]
                try:
                    self._apply(look, frame_only=True)
                    failures = 0
                except mailbox.MailboxError as e:
                    failures += 1
                    if failures >= 5:
                        logger.warning("effect stopped: %s", e)
                        self._effect_thread = None
                        return

    # -- requests --

    def status(self):
        with self._lock:
            kbd = self._detect()
            backlight = None
            if kbd is not None:
                try:
                    backlight = zones.read_backlight()
                except mailbox.MailboxError:
                    backlight = None
            return {
                "ok": True,
                "keyboard": kbd.as_dict() if kbd else None,
                "look": self._look,
                "backlight": backlight,
                "lit": zones.is_lit(backlight) if backlight is not None else None,
                "error": self._error,
            }

    # Read-only commands worth trying one by one when something is wrong.
    # Each is a pure query -- nothing here writes to the machine -- and
    # between them they separate the three things that can fail: the
    # transport, the 0x20008 command space, and the lighting itself.
    PROBE_CALLS = (
        # name,              command,  type, data,      out size
        ("keyboard_type/4", 0x20008, 0x2B, b"", 4),
        ("keyboard_type/128", 0x20008, 0x2B, b"", 128),
        ("max_fan/4", 0x20008, 0x26, b"\x00" * 4, 4),   # is any 4-byte answer usable?
        ("lighting_supported/128", 0x20009, 0x01, b"\x00", 128),
        ("colour_table/128", 0x20009, 0x02, b"\x00", 128),
        ("backlight/128", 0x20009, 0x04, b"\x00", 128),
    )

    def probe(self):
        """Everything that decides whether this works, for the CLI."""
        out = {
            "ok": True,
            "wmi_present": mailbox.wmi_present(),
            "acpi_call": mailbox.CALL_PATH.exists(),
            "transport_ready": mailbox.transport_ready(),
            "root": os.geteuid() == 0,
        }
        with self._lock:
            kbd = self._detect(force=True)
            out["keyboard"] = kbd.as_dict() if kbd else None
            out["error"] = self._error
            for name, fn in (
                ("colors", lambda: zones.read_colors(kbd.zones if kbd else 4)),
                ("backlight", zones.read_backlight),
            ):
                try:
                    out[name] = fn()
                except mailbox.MailboxError as e:
                    out[name] = None
                    out.setdefault("failures", {})[name] = str(e)
            out["readable_zones"] = zones.readable_zones()
            out["reply_bytes"] = zones.last_read_length
            calls = {}
            for name, command, command_type, data, out_size in self.PROBE_CALLS:
                try:
                    reply = mailbox.call(command, command_type, data, out_size=out_size)
                    calls[name] = {"bytes": len(reply), "first": reply[:6].hex()}
                except mailbox.MailboxError as e:
                    calls[name] = {"error": str(e)}
            out["calls"] = calls
        return out

    def handle(self, request):
        op = request.get("op")
        try:
            if op == "status":
                return self.status()
            if op == "apply":
                self.apply(request.get("look") or {})
                return self.status()
            if op == "reload":
                self.reload()
                return self.status()
            if op == "probe":
                return self.probe()
            return {"ok": False, "error": f"unknown request {op!r}"}
        except mailbox.MailboxError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # a bug here must not take the daemon down
            logger.exception("request %r failed", op)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # -- serving --

    def _bind(self):
        os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        # Hand the socket to the desktop user this was installed for. Without
        # an owner the socket is root-only and the settings app -- the whole
        # reason the daemon exists -- cannot reach it.
        owner = os.environ.get("HYPR_UTIL_CONFIG_DIR")
        if owner:
            try:
                info = os.stat(owner)
                os.chown(self.socket_path, info.st_uid, info.st_gid)
            except OSError as e:
                logger.warning("could not hand %s to the desktop user: %s", self.socket_path, e)
        server.listen(8)
        return server

    def serve_forever(self):
        mailbox.ensure_module()
        try:
            self.reload()
            logger.info("applied the saved look")
        except mailbox.MailboxError as e:
            logger.warning("could not apply the saved look: %s", e)
        server = self._bind()
        logger.info("listening on %s", self.socket_path)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except OSError:
                    continue
                with conn:
                    conn.settimeout(TIMEOUT_SECONDS)
                    try:
                        data = conn.recv(1 << 20)
                        request = json.loads(data.decode() or "{}")
                    except (OSError, ValueError) as e:
                        logger.debug("bad request: %s", e)
                        continue
                    reply = self.handle(request)
                    try:
                        conn.sendall((json.dumps(reply) + "\n").encode())
                    except OSError:
                        pass
        finally:
            self._stop.set()
            server.close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.geteuid() != 0:
        raise SystemExit(
            "hypr-util-kbd must run as root: the BIOS mailbox is root-only.\n"
            "It is normally started by hypr-util-kbd.service."
        )
    Daemon().serve_forever()
