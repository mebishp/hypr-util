"""The animation loop for the laptop keyboard, and the indicators it carries.

An effect is frames: the firmware runs no animation of its own, so breathe,
sweep, fire and the rest are colours computed here and written about eight
times a second for as long as one is selected. That has to keep happening
with no window open and the screen locked, which is why it is a service
rather than a thread in the settings app.

It runs as the desktop user. The hyprkbd driver does the privileged part and
a udev rule hands the sysfs attributes over at install time, so there is no
root daemon here, no socket, and no protocol -- the version of this file
before the driver existed had all three, because reaching the firmware meant
reaching /proc/acpi/call.

Coordination is the two config files and nothing more. Whoever changes the
lighting -- the settings app, the tray, `hyprutil kbd set` -- writes the file
and then writes the hardware itself, so the change is immediate. This loop
watches both files' mtimes and picks a change up on its next frame. A static
look with no alert showing needs no frames at all, so the loop idles at a
slower poll until something changes.

The service keeps running even with the lighting switched off, because an
alert lights a dark keyboard for its couple of seconds and then puts it back.
"""
import logging
import time

from . import device, indicators, settings, state, sysinfo, zones

logger = logging.getLogger(__name__)

# How often to look for new settings while nothing is animating. The writer
# has already put its change on the keyboard by then; this is only about
# taking over the animation, so it can afford to be lazy.
IDLE_SECONDS = 1.0
# How often to re-read the power profile and battery. This is also how late
# an alert can be: a profile change has to be noticed before it can flash,
# so it is the sample rate rather than the frame rate that decides whether
# the keyboard feels like it is answering the keypress.
STATUS_SECONDS = 1.0
# Except for the CPU meter, whose whole job is to follow a number that moves.
METER_STATUS_SECONDS = 0.5
# How often to stat the two config files. Every frame is seventeen stat()
# calls a second to learn something that changes when a person moves a
# slider; four is plenty, and the writer has already put its own change on
# the keyboard by the time we notice.
STAT_SECONDS = 0.25
# Give up on an effect after this many consecutive write failures, rather
# than hammering a keyboard that has stopped answering.
MAX_FAILURES = 5


class Animator:
    """Reads the saved look and settings, keeps the keyboard showing them."""

    def __init__(self):
        self.look = zones.normalize_look({})
        self.settings = settings.normalize({})
        self.zones = 4
        self.phase = 0.0
        self.alert = None
        self._stamps = (None, None)
        self._statted = 0.0
        self._telemetry = sysinfo.Telemetry(refresh=STATUS_SECONDS)
        self._monitor = indicators.Monitor()
        self._status = {}
        self._effective = None
        self._last_frame = None
        self._failures = 0

    # -- state --

    def _stat(self):
        """Both config files' mtimes, as one comparable value."""
        out = []
        for path in (state.current_path(), settings.settings_path()):
            try:
                out.append(path.stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    def load(self):
        """Take the saved look and settings, and put them on the keyboard."""
        self._stamps = self._stat()
        self._statted = time.monotonic()
        self.look = state.read_current()
        self.settings = settings.read()
        self.phase = 0.0
        keyboard = device.keyboard()
        self.zones = (keyboard or {}).get("zones") or 4
        self._telemetry.refresh = (
            METER_STATUS_SECONDS if self.look["effect"] == "meter" else STATUS_SECONDS
        )
        self._refresh_status(force=True)
        self._draw(frame_only=False)

    def _refresh_status(self, force=False):
        """Re-read the machine, and let the monitor judge what changed."""
        self._status = self._telemetry.sample(force=force)
        self._effective = zones.apply_battery_saver(
            self.look, self.settings["battery_saver"], self._status
        )
        self.alert = self._monitor.update(
            self.settings["indicators"], self._status, self.look["on"],
        )

    @property
    def effective_look(self):
        """The look as it should actually be shown, battery saver included.

        Recomputed when the status is, not on every read: this used to be a
        property that rebuilt the dict each time, and one tick asks for it
        three times.
        """
        if self._effective is None:
            self._effective = zones.apply_battery_saver(
                self.look, self.settings["battery_saver"], self._status
            )
        return self._effective

    @property
    def animating(self):
        if self.alert is not None:
            return True
        look = self.effective_look
        return look["on"] and look["effect"] in zones.ANIMATED

    # -- drawing --

    def _draw(self, frame_only=True):
        """Write one frame, unless it would be the frame already showing.

        Skipping an identical frame is worth the comparison: the CPU meter
        redraws at the frame rate but only changes when the load sample does,
        and a slow effect at low brightness can round to the same bytes for
        several frames running. Each skipped frame is one WMI call the
        firmware does not have to service.
        """
        look = self.effective_look
        lit = zones.backlight_wanted(look, self.alert)
        if lit:
            frame = zones.compose(
                look, self.zones, self.phase, self.alert, self._status
            )
            if frame != self._last_frame:
                device.write_colors([zones.to_hex(c) for c in frame])
                self._last_frame = frame
        if not frame_only:
            device.write_backlight(lit)
            if not lit:
                # Nothing is showing, so nothing is the frame to compare the
                # next one against -- otherwise turning the lighting back on
                # to the same colours would write nothing at all.
                self._last_frame = None

    def tick(self):
        """One pass: adopt any change, refresh status, then draw a frame."""
        now = time.monotonic()
        if now - self._statted >= STAT_SECONDS:
            self._statted = now
            if self._stat() != self._stamps:
                # Debug, not info: dragging a colour in the settings app
                # rewrites this file every couple of hundred milliseconds, and
                # a line per write would bury everything else in the journal.
                logger.debug("the saved look or settings changed; picking it up")
                self.load()
                self._failures = 0
                return

        previous = self.alert
        self._refresh_status()

        if (self.alert is None) != (previous is None):
            # Arriving at or leaving an alert is the one transition that can
            # turn the backlight on or off -- an alert shows on a keyboard
            # whose lighting is switched off -- so it writes more than colours.
            if self.alert is not None:
                logger.info("alert: %s", self.alert.label)
            self._draw(frame_only=False)
            return

        if self.alert is not None:
            # The look's phase deliberately does not advance while an alert
            # holds the board: what comes back afterwards is the frame the
            # effect was on, not the one it would have reached.
            self._draw(frame_only=True)
            return

        if not self.animating:
            return
        speed = zones.SPEED_FACTORS[self.effective_look["speed"]]
        self.phase += zones.PHASE_STEP * speed
        self._draw(frame_only=True)

    def run(self):
        while True:
            try:
                self.tick()
                self._failures = 0
            except device.DeviceUnavailable as e:
                # The driver is gone or was never reachable. Nothing here can
                # fix that, and retrying every 120 ms would only fill the log.
                logger.warning("keyboard lighting unavailable: %s", e)
                time.sleep(30)
                continue
            except device.DeviceError as e:
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    logger.warning("the keyboard stopped answering: %s", e)
                    time.sleep(30)
                    self._failures = 0
            time.sleep(zones.FRAME_SECONDS if self.animating else IDLE_SECONDS)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    animator = Animator()
    try:
        animator.load()
        logger.info(
            "applied the saved look (%s), indicators %s",
            animator.look["effect"],
            "on" if animator.settings["indicators"]["enabled"] else "off",
        )
    except device.DeviceError as e:
        logger.warning("could not apply the saved look: %s", e)
    animator.run()
