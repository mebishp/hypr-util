"""What the keyboard says about the machine: a flash, not a reserved zone.

An indicator here is the whole keyboard, briefly. When the power profile
changes or the battery crosses a threshold, the board takes the status
colour for a couple of seconds and then goes back to exactly the effect and
colours it was running -- the phase is held while the alert is up, so a
sweep resumes mid-travel rather than restarting from the left.

That is a deliberate change from the first design, which held one zone aside
for as long as the app was running. A permanently coloured corner turns out
to be a poor status light on three counts: it is easy to stop seeing, because
nothing about it moves when the status does; it costs a quarter of the
keyboard for the whole session; and it argues with whatever effect is
running over the other three zones. A whole-board flash is the opposite of
each -- impossible to miss, free once it has passed, and it leaves the look
untouched.

Alerts fire on *changes*, not on states, which is why this module keeps the
previous sample. "The battery is at 9%" is not news eight times a second;
"the battery has just fallen below 10%" is. The one exception is a critical
battery, which re-fires on a timer for as long as it lasts, because that is
the single piece of news worth repeating.

Colours are the ones hyprutil/rgb/notify.py already flashes on the external
Firefly for the same events, so the two keyboards say the same thing in the
same colours.
"""
import math
import time

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
UNPLUGGED_COLOR = "ffaa00"

# Higher wins the board. A flat battery outranks a profile change because
# one of them can be read later and the other cannot.
PRIORITY_BATTERY_CRITICAL = 40
PRIORITY_BATTERY_LOW = 30
PRIORITY_CHARGING = 20
PRIORITY_PROFILE = 10

STYLE_SOLID = "solid"
STYLE_PULSE = "pulse"    # a slow breathe, for "this is fine but worth seeing"
STYLE_BLINK = "blink"    # a hard flash, for "do something about this"

# Rates in Hz, so an alert reads the same whatever speed the look runs at --
# it is the machine talking, not the effect.
PULSE_HZ = 0.75
BLINK_HZ = 2.5

# An alert that cut straight in and straight out would read as a glitch, so
# both edges are ramped. The tail is the longer of the two: coming back to
# the effect should feel like the alert receding, while arriving should not
# feel like waiting.
FADE_IN = 0.12
FADE_OUT = 0.4

DEFAULT_DURATION = 2.5
MIN_DURATION, MAX_DURATION = 0.5, 15.0

# How long a critical battery waits before saying so again.
DEFAULT_CRITICAL_REPEAT = 300.0


class Alert:
    """The whole keyboard, one colour, for a few seconds.

    `key` is what the alert is about rather than what it looks like, so a
    second battery warning restarts the first instead of queueing behind it.
    """

    def __init__(self, key, color, style=STYLE_SOLID, priority=0, label="",
                 duration=DEFAULT_DURATION):
        self.key = key
        self.color = zones.to_rgb(color)
        self.style = style
        self.priority = priority
        self.label = label
        self.duration = max(MIN_DURATION, float(duration))
        self.started_at = 0.0

    def start(self, now):
        self.started_at = now
        return self

    def elapsed(self, now=None):
        # `now` is optional so a caller that has no clock of its own -- the
        # preview, a one-shot apply -- gets the frame for this instant
        # rather than having to invent a timestamp.
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.started_at)

    def expired(self, now=None):
        return self.elapsed(now) >= self.duration

    def _envelope(self, elapsed):
        """How much of the board the alert has, 0 at both edges.

        Used as a crossfade against the look underneath rather than as a
        brightness. An alert that faded up from black would put a blink of
        darkness in front of every flash, which is the difference between
        the keyboard answering and the keyboard glitching.
        """
        remaining = self.duration - elapsed
        if elapsed < FADE_IN:
            return elapsed / FADE_IN
        if remaining < FADE_OUT:
            return max(0.0, remaining / FADE_OUT)
        return 1.0

    def _level(self, elapsed):
        if self.style == STYLE_PULSE:
            return 0.3 + 0.7 * (0.5 + 0.5 * math.sin(elapsed * PULSE_HZ * math.tau))
        if self.style == STYLE_BLINK:
            # Square-ish rather than sinusoidal: a critical battery should
            # read as an alarm, not as gentle breathing.
            return 1.0 if math.sin(elapsed * BLINK_HZ * math.tau) > -0.3 else 0.0
        return 1.0

    def frame(self, now=None):
        """The alert's colour at this moment, and how much of it is showing.

        The second value is for the caller to crossfade with -- see
        zones.compose, which is the only thing that should be doing it.
        """
        elapsed = self.elapsed(now)
        return zones.scale(self.color, self._level(elapsed)), self._envelope(elapsed)

    def as_dict(self, now=None):
        return {
            "key": self.key,
            "color": zones.to_hex(self.color),
            "style": self.style,
            "label": self.label,
            "remaining": round(max(0.0, self.duration - self.elapsed(now)), 2),
        }


# -- reading the machine --

BATTERY_UNKNOWN = "unknown"


def battery_state(config, telemetry):
    """Which of the battery's interesting states the machine is in.

    Collapsing the level and the charger into one word is what makes the
    alerts fall out of a comparison: a state that is the same as last time
    is not news, whatever the exact percentage did.
    """
    if telemetry.get("charging"):
        return "charging"
    level = telemetry.get("battery")
    if level is None:
        return BATTERY_UNKNOWN
    if level <= config["critical"]:
        return "critical"
    if level <= config["low"]:
        return "low"
    return "ok"


def _duration(config):
    try:
        return max(MIN_DURATION, min(MAX_DURATION, float(config.get("duration", DEFAULT_DURATION))))
    except (TypeError, ValueError):
        return DEFAULT_DURATION


class Monitor:
    """Watches the machine and holds whichever alert is currently showing.

    One per process. The effects service has one driving the hardware and
    the settings app has another driving its preview; they see the same
    telemetry and so fire on the same transitions, within a sample of each
    other.
    """

    def __init__(self):
        self._seen = None
        self._sampled = None
        self._active = None
        self._critical_at = None

    @property
    def active(self):
        return self._active

    def clear(self):
        self._active = None

    def update(self, config, telemetry, lighting_on=True, now=None):
        """Take a sample, fire anything it implies, and return what is showing.

        Returns the live Alert or None. Call it as often as you like: it is
        the telemetry changing that raises an alert, not this being called.
        """
        now = time.monotonic() if now is None else now

        if not config.get("enabled") or (not lighting_on and not config.get("when_off")):
            # Still take the baseline, so switching indicators back on does
            # not immediately dump every transition that happened while they
            # were off.
            self._seen = dict(telemetry)
            self._sampled = telemetry
            self._critical_at = None
            self._active = None
            return None

        if telemetry is not self._sampled:
            # sysinfo hands back the same dict until it takes a new reading,
            # so identity is exactly "is there anything new to judge". Expiry
            # below still runs every call, which is what lets an alert end on
            # a frame boundary rather than on a sample boundary.
            self._sampled = telemetry
            for alert in self._events(config, telemetry, now):
                self._raise(alert, now)

        if self._active is not None and self._active.expired(now):
            self._active = None
        return self._active

    def _raise(self, alert, now):
        """Put an alert on the board, unless a louder one is already there."""
        current = self._active
        if current is not None and not current.expired(now):
            if alert.key != current.key and alert.priority < current.priority:
                return
        self._active = alert.start(now)

    def _events(self, config, telemetry, now):
        previous, self._seen = self._seen, dict(telemetry)
        duration = _duration(config)
        out = []

        if previous is None:
            # The first look at the machine. Everything about it is new and
            # none of it is news -- the profile did not just change, we have
            # only just started watching it. A critical battery is the
            # exception and is left for the repeat below to raise at once.
            previous = {}
            if battery_state(config.get("battery") or {"critical": 0, "low": 0},
                             telemetry) != "critical":
                return out

        profile_cfg = config.get("profile") or {}
        profile = telemetry.get("profile")
        if (profile_cfg.get("enabled") and profile in PROFILE_COLORS
                and profile != previous.get("profile")
                and previous.get("profile") is not None):
            out.append(Alert(
                "profile", PROFILE_COLORS[profile], STYLE_PULSE,
                PRIORITY_PROFILE, f"profile: {profile}", duration,
            ))

        battery_cfg = config.get("battery") or {}
        if battery_cfg.get("enabled"):
            state = battery_state(battery_cfg, telemetry)
            was = battery_state(battery_cfg, previous) if previous else None
            level = telemetry.get("battery")
            percent = f" ({level}%)" if level is not None else ""

            if state == "critical":
                repeat = _critical_repeat(battery_cfg)
                due = self._critical_at is None or now - self._critical_at >= repeat
                if state != was or due:
                    self._critical_at = now
                    out.append(Alert(
                        "battery", BATTERY_CRITICAL_COLOR, STYLE_BLINK,
                        PRIORITY_BATTERY_CRITICAL,
                        f"battery critical{percent}", duration,
                    ))
            else:
                self._critical_at = None
                if state == "low" and was != "low":
                    out.append(Alert(
                        "battery", BATTERY_LOW_COLOR, STYLE_SOLID,
                        PRIORITY_BATTERY_LOW, f"battery low{percent}", duration,
                    ))

            if battery_cfg.get("show_charging") and was is not None and state != was:
                if state == "charging":
                    out.append(Alert(
                        "charger", CHARGING_COLOR, STYLE_PULSE, PRIORITY_CHARGING,
                        f"charging{percent}", duration,
                    ))
                elif was == "charging" and state not in ("critical", "low"):
                    # Coming off the charger into a low or critical battery
                    # already has its own, louder alert above; saying both
                    # would be two flashes for one event.
                    out.append(Alert(
                        "charger", UNPLUGGED_COLOR, STYLE_PULSE, PRIORITY_CHARGING,
                        f"on battery{percent}", duration,
                    ))
        return out


def _critical_repeat(config):
    try:
        return max(30.0, float(config.get("repeat_critical", DEFAULT_CRITICAL_REPEAT)))
    except (TypeError, ValueError):
        return DEFAULT_CRITICAL_REPEAT


# A process-wide default, so the CLI's status read and whatever else asks
# share one baseline rather than each priming its own.
_shared = None


def monitor():
    global _shared
    if _shared is None:
        _shared = Monitor()
    return _shared


def wants_animation(alert):
    """True when there is an alert to keep redrawing."""
    return alert is not None
