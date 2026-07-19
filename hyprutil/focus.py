"""Focus mode: state + the individual side-effect actions (blocking sites,
killing distracting apps, wallpaper, Do-Not-Disturb, session stats).

This module only reads/writes state and performs actions on request -- it
never decides *when* to call them. That decision belongs to the automation
daemon's FocusController (automation.py), which watches FOCUS_FILE for
changes and is the one place all these side effects actually get triggered
from. The app, tray, and CLI are all thin "intent" writers that call
request() and nothing else -- same split as fan.py's override file feeding
the root fancurve.sh daemon.
"""
import contextlib
import fcntl
import json
import logging
import socket
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

from .util import CONFIG_DIR, atomic_write_text

logger = logging.getLogger(__name__)

FOCUS_FILE = CONFIG_DIR / "focus.json"
PROFILES_FILE = CONFIG_DIR / "focus-profiles.json"
STATS_FILE = CONFIG_DIR / "focus-stats.json"
LOCK_FILE = CONFIG_DIR / ".focus.lock"

# Root helpers installed by setup.sh (install_focus_blocking_helpers),
# invoked via pkexec under a passwordless polkit rule scoped to exactly
# these two binaries -- see system/hypr-util-focus-{hosts,fw}.sh and
# system/49-hypr-util-focus.rules.
HOSTS_HELPER = "/usr/local/bin/hypr-util-focus-hosts"
FW_HELPER = "/usr/local/bin/hypr-util-focus-fw"

WALLPAPER_DIR = Path.home() / ".local" / "share" / "hypr-util"
WALLPAPER_LIGHT = WALLPAPER_DIR / "focus-wallpaper.svg"
WALLPAPER_DARK = WALLPAPER_DIR / "focus-wallpaper-dark.svg"

DEFAULT_SITES = [
    "youtube.com", "reddit.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "tiktok.com", "netflix.com", "twitch.tv",
]
DEFAULT_APPS = ["discord", "steam", "lutris", "heroic"]

DEFAULT_PROFILES = {
    "Deep Work": {"sites": list(DEFAULT_SITES), "apps": list(DEFAULT_APPS)},
    "Reading": {"sites": list(DEFAULT_SITES), "apps": []},
}

DEFAULT_STATE = {
    "active": False,
    "active_profile": "Deep Work",
    "hard_lock": False,
    "started_at": None,
    "duration_s": None,
    "pomodoro": {"enabled": False, "work_min": 25, "break_min": 5, "cycles": 4},
    "saved_wallpaper": None,
    "saved_dnd": None,
    "schedule": [],  # [{"days": [0-6], "start": "HH:MM", "end": "HH:MM", "profile": name}]
}


def ensure_defaults():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not FOCUS_FILE.exists():
        write_state(DEFAULT_STATE)
    if not PROFILES_FILE.exists():
        write_profiles(DEFAULT_PROFILES)
    if not STATS_FILE.exists():
        atomic_write_text(STATS_FILE, json.dumps({}, indent=2))


@contextlib.contextmanager
def _process_lock():
    """flock'd critical section for focus.json read-modify-write, mirroring
    tasks.py's _process_lock. Without this, request() (called from the app,
    tray, or CLI -- any process) and update_state() (called only by the
    daemon's FocusController) can race: e.g. the daemon reads active=True to
    merge in saved_wallpaper while a concurrent `hyprutil focus off` writes
    active=False, then the daemon's stale write lands last and clobbers the
    "off" back to active=True -- focus gets stuck on with no running UI able
    to turn it off (short of killing the daemon)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# -- state --

def read_state():
    if FOCUS_FILE.exists():
        try:
            state = json.loads(FOCUS_FILE.read_text())
            merged = dict(DEFAULT_STATE)
            merged.update(state)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_STATE)


def write_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(FOCUS_FILE, json.dumps(state, indent=2))


def update_state(**kwargs):
    """Merge a few keys into the state file -- used by the daemon to persist
    things it needs to survive its own restart (e.g. saved_wallpaper)."""
    with _process_lock():
        state = read_state()
        state.update(kwargs)
        write_state(state)
        return state


# -- focus profiles (named site/app blocklists) --

def read_profiles():
    if PROFILES_FILE.exists():
        try:
            profiles = json.loads(PROFILES_FILE.read_text())
            if profiles:
                return profiles
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_PROFILES)


def write_profiles(profiles):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(PROFILES_FILE, json.dumps(profiles, indent=2))


def active_sites_and_apps(state=None):
    state = state or read_state()
    profile = read_profiles().get(state.get("active_profile"), {})
    return profile.get("sites", []), profile.get("apps", [])


# -- intent (called by app/tray/CLI) --

class HardLockError(Exception):
    """request(active=False) was refused because a hard-locked session
    hasn't finished yet."""


def _remaining_lock_seconds(state):
    started, duration = state.get("started_at"), state.get("duration_s")
    if started is None or not duration:
        return 0.0
    return max(0.0, (started + duration) - time.time())


def is_locked(state=None):
    state = state or read_state()
    return bool(state.get("active") and state.get("hard_lock") and _remaining_lock_seconds(state) > 0)


def request(active, profile=None, duration_s=None, hard_lock=False, pomodoro=None):
    """Write focus *intent*. Enforced here (not just disabled in the UI) so
    the app, tray, and CLI all honor an in-progress hard-lock the same way,
    whichever of them someone tries to turn focus off early from."""
    with _process_lock():
        state = read_state()
        if not active and is_locked(state):
            raise HardLockError(f"focus is locked for {int(_remaining_lock_seconds(state))}s more")
        if active:
            profiles = read_profiles()
            state["active"] = True
            state["active_profile"] = profile or state.get("active_profile") or next(iter(profiles), None)
            state["started_at"] = time.time()
            state["duration_s"] = duration_s
            # A lock with no end time could never release itself -- only honor
            # hard_lock when there's an actual duration to unlock at.
            state["hard_lock"] = bool(hard_lock) and duration_s is not None
            if pomodoro is not None:
                state["pomodoro"] = pomodoro
        else:
            state["active"] = False
            state["hard_lock"] = False
        write_state(state)
        return state


# -- side effects (called by the daemon's FocusController only) --

def _run_helper(args, **kwargs):
    """Run a pkexec'd focus helper and log a warning on a nonzero exit --
    previously these were all check=False with the return code discarded
    outright, so a missing helper binary or an unauthorized/misconfigured
    polkit rule meant nothing actually got blocked while the UI still showed
    focus as "on", with no signal anywhere that it silently failed."""
    result = subprocess.run(args, check=False, **kwargs)
    if result.returncode != 0:
        logger.warning("focus helper %r exited %d", args, result.returncode)
    return result


def hosts_apply(sites):
    if not sites:
        hosts_clear()
        return
    payload = "\n".join(sites) + "\n"
    # pkexec, not a direct call -- the daemon runs as the regular user;
    # writing /etc/hosts needs root. Passwordless because of the polkit
    # rule installed for this one exact binary path (see
    # system/49-hypr-util-focus.rules) -- the daemon has no way to answer
    # an interactive pkexec password prompt.
    _run_helper(["pkexec", HOSTS_HELPER, "on"], input=payload, text=True, timeout=10)


def hosts_clear():
    _run_helper(["pkexec", HOSTS_HELPER, "off"], timeout=10)


def resolve_ips(sites, per_lookup_timeout=3):
    """Best-effort DNS resolution of each blocked domain (and its www.
    variant) to its IPv4/IPv6 addresses, for fw_apply(). This exists
    because /etc/hosts only affects *fresh* resolutions -- a browser that
    already resolved/cached a domain's IP before focus mode turned on (or
    that reuses an already-open connection) keeps reaching it straight
    through a hosts-file change. Dropping the IP itself at the network
    layer closes that gap. A domain that fails to resolve (offline, typo,
    site down) is just skipped -- not fatal, and every lookup is bounded so
    one unreachable domain can't stall this for long."""
    socket.setdefaulttimeout(per_lookup_timeout)
    try:
        ips = set()
        for domain in sites:
            hosts = (domain,) if domain.startswith("www.") else (domain, f"www.{domain}")
            for host in hosts:
                try:
                    for info in socket.getaddrinfo(host, None):
                        ips.add(info[4][0])
                except OSError:
                    continue
        return sorted(ips)
    finally:
        socket.setdefaulttimeout(None)


def fw_apply(ips):
    if not ips:
        fw_clear()
        return
    payload = "\n".join(ips) + "\n"
    # Same pkexec/polkit rationale as hosts_apply -- see there.
    _run_helper(["pkexec", FW_HELPER, "on"], input=payload, text=True, timeout=15)


def fw_clear():
    _run_helper(["pkexec", FW_HELPER, "off"], timeout=10)


_BACKGROUND_SCHEMA = "org.gnome.desktop.background"
_NOTIF_SCHEMA = "org.gnome.desktop.notifications"


def focus_wallpaper_uris():
    return f"file://{WALLPAPER_LIGHT}", f"file://{WALLPAPER_DARK}"


def set_wallpaper(uri, uri_dark):
    settings = Gio.Settings.new(_BACKGROUND_SCHEMA)
    prior = {
        "picture-uri": settings.get_string("picture-uri"),
        "picture-uri-dark": settings.get_string("picture-uri-dark"),
    }
    settings.set_string("picture-uri", uri)
    settings.set_string("picture-uri-dark", uri_dark)
    return prior


def restore_wallpaper(prior):
    if not prior:
        return
    settings = Gio.Settings.new(_BACKGROUND_SCHEMA)
    settings.set_string("picture-uri", prior["picture-uri"])
    settings.set_string("picture-uri-dark", prior["picture-uri-dark"])


def set_dnd(on):
    settings = Gio.Settings.new(_NOTIF_SCHEMA)
    prior = settings.get_boolean("show-banners")
    settings.set_boolean("show-banners", not on)
    return prior


def restore_dnd(prior):
    if prior is None:
        return
    settings = Gio.Settings.new(_NOTIF_SCHEMA)
    settings.set_boolean("show-banners", prior)


# -- stats --

def _read_stats():
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def record_session(seconds, pomodoros=0):
    if seconds <= 0:
        return
    stats = _read_stats()
    day = time.strftime("%Y-%m-%d")
    entry = stats.setdefault(day, {"seconds": 0, "pomodoros": 0})
    entry["seconds"] += int(seconds)
    entry["pomodoros"] += pomodoros
    atomic_write_text(STATS_FILE, json.dumps(stats, indent=2))


def today_total():
    return stats_summary()[0]


def streak():
    """Consecutive days (including today) with at least one focus session."""
    return stats_summary()[1]


def stats_summary():
    """(today's total seconds, streak) from a single read of
    focus-stats.json -- today_total() and streak() used to each read+parse
    it independently, even though every call site (FocusPage's 2s tick) always
    wants both at once."""
    stats = _read_stats()
    today = stats.get(time.strftime("%Y-%m-%d"), {}).get("seconds", 0)
    n = 0
    day = time.time()
    while True:
        key = time.strftime("%Y-%m-%d", time.localtime(day))
        if stats.get(key, {}).get("seconds", 0) > 0:
            n += 1
            day -= 86400
        else:
            break
    return today, n
