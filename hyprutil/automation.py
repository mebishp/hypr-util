"""Always-on background automation for hypr-util.

Owns the reactive behavior that used to live only in the tray's 2-second
poll loop -- following power-profile changes (display refresh rate + RGB
flash) and restoring keyboard lighting after it comes back (USB reconnect,
resume from sleep, or this daemon's own startup). Runs as a user-session
systemd service (see system/hypr-util-daemon.service) independent of
whether the tray or settings app happen to be running, so profile changes
made from GNOME Settings, powerprofilesctl, or the GTK app are still
reflected in refresh rate and RGB.

Also owns Focus mode enforcement (FocusController, below): the app/tray/CLI
only ever write *intent* to ~/.config/hypr-util/focus.json via focus.py's
request(); this is the one place that intent actually gets turned into
blocked sites, killed apps, a swapped wallpaper, silenced notifications, an
RGB breathe, Pomodoro timing, scheduled auto-focus, and background Google
Tasks sync -- the same "daemon is the enforcer" split fan.py/fancurve.sh
already use for the manual-override file.
"""
import logging
import os
import signal
import threading
import time
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from . import display
from . import fan
from . import focus
from . import power
from . import rgb
from . import tasks

logger = logging.getLogger(__name__)

# NetworkManager's NM_STATE_CONNECTED_GLOBAL -- the only "state" value that
# means "actually reaches the internet", not just a local link.
NM_STATE_CONNECTED_GLOBAL = 70


def _send_desktop_notification(title, body):
    """Raw org.freedesktop.Notifications D-Bus call. This daemon has no
    GApplication of its own (Gio.Notification/send_notification needs one),
    so it talks to the notification service directly -- the same interface
    GApplication would ultimately call into anyway."""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        conn.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", ("hypr-util", 0, "", title, body, [], {}, -1)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )
    except GLib.Error:
        logger.exception("failed to send desktop notification %r", title)


class _FlashCoordinator:
    """Serializes RGB 'flash' sessions (apply color -> wait -> maybe revert)
    across whichever trigger started them -- a power-profile change or a
    focus-mode toggle -- so that:

    1. A stale session's device writes can never land on the wire after a
       newer session's (and its correct revert) has already finished.
    2. Two sessions never attempt concurrent device writes at all (this
       complements, rather than replaces, the lower-level `_device_lock` in
       rgb/controller.py).

    This generalizes what used to be _apply_profile's own private
    generation counter + lock so a focus-mode breathe and a profile-change
    breathe -- which can now genuinely race each other -- share the same
    protection instead of each having their own, blind to the other.
    """

    def __init__(self):
        self._generation_lock = threading.Lock()
        self._generation = 0
        self._session_lock = threading.Lock()

    def start(self):
        """Call synchronously, before spawning the worker thread that will
        actually flash -- see the comment on the old _on_profile for why
        this can't be assigned from inside the thread itself."""
        with self._generation_lock:
            self._generation += 1
            return self._generation

    def _current(self):
        with self._generation_lock:
            return self._generation

    def run(self, generation, flash_fn):
        """Call from a worker thread. flash_fn(revert_predicate) should
        perform the actual rgb.flash*() call, passing revert_predicate
        through as its `revert=` argument."""
        with self._session_lock:
            if generation != self._current():
                return  # superseded while queued behind this lock
            try:
                flash_fn(lambda: generation == self._current())
            except Exception:
                logger.exception("RGB flash failed")


class _SerializedActionCoordinator:
    """Same shape as _FlashCoordinator (generation counter + a lock so calls
    serialize instead of racing), for actions with no revert-predicate
    argument -- currently Focus mode's site/IP blocklist apply/clear.
    Without this, _enter's threaded _apply_blocklist and _exit's threaded
    _clear_blocklist have no ordering guarantee at all: a rapid on->off->on
    can let a stale _exit's clear land *after* the new _enter's apply,
    silently unblocking sites while focus.json still says active.
    """

    def __init__(self):
        self._generation_lock = threading.Lock()
        self._generation = 0
        self._session_lock = threading.Lock()

    def start(self):
        with self._generation_lock:
            self._generation += 1
            return self._generation

    def _current(self):
        with self._generation_lock:
            return self._generation

    def run(self, generation, fn):
        with self._session_lock:
            if generation != self._current():
                return  # superseded while queued behind this lock
            try:
                fn()
            except Exception:
                logger.exception("focus blocklist action failed")


class Automation:
    def __init__(self):
        self._loop = GLib.MainLoop()
        self._flasher = _FlashCoordinator()

    def run(self):
        # Stored on self -- an unreferenced ProfileWatcher forms a reference
        # cycle with its own D-Bus proxy (proxy's signal closure keeps the
        # watcher's bound method alive, watcher keeps the proxy alive via
        # self._proxy) that has no external root, so Python's cyclic GC will
        # eventually collect it and silently kill the subscription.
        self._profile_watcher = power.ProfileWatcher(self._on_profile)
        self._start_sleep_watch()
        rgb.on_connection_change(self._on_keyboard_change)
        threading.Thread(target=self._restore_rgb, args=("startup",), daemon=True).start()

        self._focus_controller = FocusController(self._flasher)
        self._focus_controller.start()

        logger.info("automation daemon started")
        self._loop.run()

    # -- power profile --

    def _on_profile(self, profile, is_initial):
        target_hz = fan.PROFILE_REFRESH_HZ.get(profile)
        if is_initial:
            # Silently align refresh rate at startup -- this is a sync, not
            # a change the user made, so no flash.
            if target_hz is not None:
                display.set_refresh_rate(target_hz)
            return
        logger.info("power profile changed to %r", profile)
        generation = self._flasher.start()
        threading.Thread(
            target=self._apply_profile, args=(profile, target_hz, generation), daemon=True
        ).start()

    def _apply_profile(self, profile, target_hz, generation):
        if target_hz is not None:
            display.set_refresh_rate(target_hz)
        if not rgb.ready():
            return
        self._flasher.run(generation, lambda revert: rgb.flash_for_profile(profile, revert=revert))
        logger.info("flashed RGB for profile %r", profile)

    def _sync_refresh_rate_now(self):
        """One-off resync after resume/reconnect, without a flash."""
        profile = fan.current_power_profile()
        target_hz = fan.PROFILE_REFRESH_HZ.get(profile)
        if target_hz is not None:
            display.set_refresh_rate(target_hz)

    # -- resume from sleep --

    def _start_sleep_watch(self):
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            logger.warning("could not connect to system bus for logind sleep signal")
            return
        conn.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
            "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep,
        )

    def _on_prepare_for_sleep(self, connection, sender, path, iface, signal, params):
        (going_to_sleep,) = params.unpack()
        if going_to_sleep:
            return
        threading.Thread(target=self._restore, args=("resume",), daemon=True).start()

    # -- keyboard reconnect --

    def _on_keyboard_change(self, connected):
        # Called from udev's monitor thread, not the GLib main loop.
        if connected:
            threading.Thread(target=self._restore_rgb, args=("keyboard reconnect",), daemon=True).start()

    # -- shared restore path --

    def _restore(self, reason):
        logger.info("restoring state (%s)", reason)
        self._sync_refresh_rate_now()
        self._restore_rgb(reason)

    def _restore_rgb(self, reason):
        slot = rgb.active_preset()
        if slot is None or not rgb.ready():
            return
        try:
            rgb.apply_preset(slot)
            logger.info("restored RGB preset %r (%s)", slot, reason)
        except Exception:
            logger.exception("failed to restore RGB preset %r (%s)", slot, reason)


class FocusController:
    """Enforces hyprutil/focus.py's focus.json: the one place site/app
    blocking, wallpaper, Do-Not-Disturb, RGB breathe, Pomodoro timing,
    scheduled auto-focus, and background Google Tasks sync actually happen.
    """

    def __init__(self, flasher):
        self._flasher = flasher
        self._blocklist_coordinator = _SerializedActionCoordinator()
        self._was_active = focus.read_state()["active"]
        self._apps_blocklist = []
        self._kill_timer_id = None
        self._pomodoro_timer_id = None
        self._pomodoro_phase = None
        self._pomodoro_cycle = 0
        self._pomodoro_cycles_total = 0
        self._pomodoro_work_min = 25
        self._pomodoro_break_min = 5
        self._last_pomodoro_config = None
        self._duration_timer_id = None
        self._fired_schedule = set()
        self._last_due_notified_count = None
        self._sync_in_progress = threading.Lock()

    def start(self):
        # Kept alive on self -- same GDBusProxy/watcher-lifetime lesson as
        # power.ProfileWatcher: an unreferenced Gio.FileMonitor's "changed"
        # subscription silently stops firing once nothing roots it.
        self._monitor = Gio.File.new_for_path(str(focus.FOCUS_FILE)).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._monitor.connect("changed", self._on_file_changed)

        GLib.timeout_add_seconds(60, self._schedule_tick)
        self._start_network_watch()

        # Enforce whatever's already on disk -- covers the daemon restarting
        # mid-session (e.g. after a hypr-util update).
        if self._was_active:
            self._enter(focus.read_state(), reflash=False)

    # -- file watch --

    def _on_file_changed(self, monitor, file, other_file, event_type):
        if event_type not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            return
        self._apply(focus.read_state())

    def _apply(self, state):
        active = state["active"]
        if active and not self._was_active:
            self._enter(state)
        elif not active and self._was_active:
            self._exit(state)
        elif active:
            # Same-active update (e.g. Pomodoro settings edited mid-session,
            # or switching profiles without turning focus off) -- resync the
            # blocklist/timers without re-breathing or re-applying the
            # wallpaper/DND.
            _, apps = focus.active_sites_and_apps(state)
            self._apps_blocklist = apps
            if apps and self._kill_timer_id is None:
                # Entered with an app-less profile, then switched (mid-
                # session) to one with apps -- _enter only starts this timer
                # when apps are non-empty *at entry*, so without this it
                # would never start and those apps would never get killed.
                self._kill_timer_id = GLib.timeout_add_seconds(3, self._kill_tick)
            elif not apps and self._kill_timer_id is not None:
                GLib.source_remove(self._kill_timer_id)
                self._kill_timer_id = None
            # Only reschedule Pomodoro if its config actually changed --
            # focus.json also gets rewritten by this daemon's own
            # update_state() (e.g. _enter persisting saved_wallpaper/
            # saved_dnd), which re-triggers this same file-monitor path
            # with active still True and an unchanged pomodoro block.
            # Unconditionally rescheduling here would restart the work/break
            # cycle (and re-flash + re-notify) on every such self-write, not
            # just on a genuine settings edit.
            pomo = state.get("pomodoro") or {}
            if pomo != self._last_pomodoro_config:
                self._reschedule_pomodoro(state)
            self._reschedule_duration_timer(state)
        self._was_active = active

    def _enter(self, state, reflash=True):
        logger.info("focus mode enabled (%s)", state.get("active_profile"))
        if reflash:
            self._flash()

        sites, apps = focus.active_sites_and_apps(state)
        self._apps_blocklist = apps
        if apps and self._kill_timer_id is None:
            self._kill_timer_id = GLib.timeout_add_seconds(3, self._kill_tick)

        # Off the GLib main thread: hosts_apply is a pkexec round trip, and
        # fw_apply's IP resolution can block for several seconds per domain
        # if the network is down/slow -- neither should stall event
        # processing (RGB flashes, other focus.json changes, ...) while
        # they run. Routed through _blocklist_coordinator (mirrors
        # _flasher/_FlashCoordinator) so a rapid on->off->on can't let a
        # stale _exit's clear land after this apply and silently unblock
        # sites while focus.json still says active.
        generation = self._blocklist_coordinator.start()
        threading.Thread(
            target=self._blocklist_coordinator.run, args=(generation, lambda: self._apply_blocklist(sites)),
            daemon=True,
        ).start()

        if reflash:
            # Only stash prior wallpaper/DND on a genuine transition, not on
            # a daemon-restart resume (where focus.json's saved_* already
            # holds the real "before focus" values -- overwriting them here
            # would instead save the *focused* wallpaper/DND as "prior").
            try:
                light, dark = focus.focus_wallpaper_uris()
                saved_wallpaper = focus.set_wallpaper(light, dark)
                saved_dnd = focus.set_dnd(True)
                focus.update_state(saved_wallpaper=saved_wallpaper, saved_dnd=saved_dnd)
            except Exception:
                logger.exception("failed to apply focus wallpaper/DND")

        self._reschedule_pomodoro(state)
        self._reschedule_duration_timer(state)

    def _exit(self, state):
        logger.info("focus mode disabled")
        self._flash()

        generation = self._blocklist_coordinator.start()
        threading.Thread(
            target=self._blocklist_coordinator.run, args=(generation, self._clear_blocklist), daemon=True,
        ).start()
        self._apps_blocklist = []
        if self._kill_timer_id is not None:
            GLib.source_remove(self._kill_timer_id)
            self._kill_timer_id = None

        try:
            focus.restore_wallpaper(state.get("saved_wallpaper"))
            focus.restore_dnd(state.get("saved_dnd"))
        except Exception:
            logger.exception("failed to restore wallpaper/DND")

        self._cancel_pomodoro()
        self._cancel_duration_timer()
        # Wall-clock, derived from focus.json's own started_at, not a
        # process-local time.monotonic() marker -- the latter resets to "now"
        # on every daemon restart, so a restart mid-session would under-count
        # the real session length. started_at survives a restart because
        # it's what's on disk, same value _enter last wrote (or that
        # request() wrote directly, for a session this daemon never saw the
        # start of).
        started_at = state.get("started_at")
        elapsed = time.time() - started_at if started_at else 0
        focus.record_session(elapsed, pomodoros=self._pomodoro_cycle)
        self._pomodoro_cycle = 0

    @staticmethod
    def _apply_blocklist(sites):
        try:
            focus.hosts_apply(sites)
        except Exception:
            logger.exception("failed to apply focus site blocklist")
        # Deliberately NOT calling focus.fw_apply(focus.resolve_ips(sites))
        # here. Several common blocklist domains sit behind shared CDN/
        # anycast infrastructure (Google's anycast ranges behind
        # youtube.com, Fastly behind reddit.com, Akamai behind others), so
        # resolve_ips() collects IPs that are also used by huge numbers of
        # completely unrelated sites -- firewalling them off broke far more
        # than the intended blocklist ("no sites are working" with the
        # default profile). /etc/hosts blocking above is domain-precise and
        # doesn't have this problem, at the cost of not catching a browser
        # that already cached a blocked domain's IP or that uses DNS-over-
        # HTTPS (which bypasses /etc/hosts entirely).
        # fw_clear() below is still wired up so anyone who already had the
        # nftables table applied from before this change gets it torn down.

    @staticmethod
    def _clear_blocklist():
        try:
            focus.hosts_clear()
        except Exception:
            logger.exception("failed to clear focus site blocklist")
        try:
            focus.fw_clear()
        except Exception:
            logger.exception("failed to clear focus IP blocklist")

    def _flash(self):
        generation = self._flasher.start()
        threading.Thread(target=self._flash_worker, args=(generation,), daemon=True).start()

    def _flash_worker(self, generation):
        if not rgb.ready():
            return
        self._flasher.run(generation, lambda revert: rgb.flash_for_focus(revert=revert))

    # -- app blocking --

    def _kill_tick(self):
        if self._apps_blocklist:
            self._kill_matching(self._apps_blocklist)
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _kill_matching(patterns):
        lowered = [p.lower() for p in patterns if p]
        if not lowered:
            return
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return
        for pid in pids:
            proc_dir = Path("/proc") / pid
            try:
                comm = (proc_dir / "comm").read_text().strip().lower()
            except OSError:
                continue
            # Exact match against comm (the kernel-truncated executable name)
            # or the basename of argv[0] -- not a substring match against the
            # whole cmdline. A substring match let a blocklist entry like
            # "steam" also hit "steamlink", or any unrelated process whose
            # cmdline happened to contain that fragment somewhere in a path,
            # which could SIGTERM processes with no relation to the intended
            # app and lose real data. Only reads /proc/<pid>/cmdline when
            # comm didn't already match, since that's the common case and
            # cmdline is the more expensive read.
            matched = comm in lowered
            if not matched:
                try:
                    argv0 = (proc_dir / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace").lower()
                except OSError:
                    continue
                matched = argv0.rsplit("/", 1)[-1] in lowered
            if matched:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    logger.info("focus mode: terminated pid %s (%s)", pid, comm)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    logger.warning("focus mode: no permission to kill pid %s (%s)", pid, comm)

    # -- pomodoro --

    def _reschedule_pomodoro(self, state):
        self._cancel_pomodoro()
        pomo = state.get("pomodoro") or {}
        self._last_pomodoro_config = pomo
        if not pomo.get("enabled"):
            return
        self._pomodoro_cycles_total = pomo.get("cycles", 4)
        self._pomodoro_work_min = pomo.get("work_min", 25)
        self._pomodoro_break_min = pomo.get("break_min", 5)
        self._pomodoro_cycle = 0
        self._start_pomodoro_phase("work")

    def _cancel_pomodoro(self):
        if self._pomodoro_timer_id is not None:
            GLib.source_remove(self._pomodoro_timer_id)
            self._pomodoro_timer_id = None
        self._pomodoro_phase = None

    # -- plain timed focus (no pomodoro) --

    def _reschedule_duration_timer(self, state):
        """Only plain (non-Pomodoro) timed sessions need this: Pomodoro's own
        cycle countdown already ends the session (_on_pomodoro_elapsed).
        Without it, `hyprutil focus on --duration 30` with no Pomodoro would
        block sites/apps, the UI's countdown would reach 0:00, and focus
        would just stay on until someone manually toggles it off."""
        self._cancel_duration_timer()
        pomo = state.get("pomodoro") or {}
        duration_s = state.get("duration_s")
        if pomo.get("enabled") or not duration_s:
            return
        started_at = state.get("started_at") or time.time()
        remaining = max(1, int(started_at + duration_s - time.time()))
        self._duration_timer_id = GLib.timeout_add_seconds(remaining, self._on_duration_elapsed)

    def _cancel_duration_timer(self):
        if self._duration_timer_id is not None:
            GLib.source_remove(self._duration_timer_id)
            self._duration_timer_id = None

    def _on_duration_elapsed(self):
        self._duration_timer_id = None
        try:
            focus.request(active=False)
        except focus.HardLockError:
            pass  # the lock's own expiry coincides with this; nothing else to do
        return GLib.SOURCE_REMOVE

    def _start_pomodoro_phase(self, phase):
        self._pomodoro_phase = phase
        minutes = self._pomodoro_work_min if phase == "work" else self._pomodoro_break_min
        _send_desktop_notification(
            "Focus: work session" if phase == "work" else "Focus: break",
            f"{minutes} min {phase} session started",
        )
        self._flash()
        self._pomodoro_timer_id = GLib.timeout_add_seconds(max(1, minutes * 60), self._on_pomodoro_elapsed)

    def _on_pomodoro_elapsed(self):
        if self._pomodoro_phase == "work":
            self._pomodoro_cycle += 1
            if self._pomodoro_cycle >= self._pomodoro_cycles_total:
                _send_desktop_notification("Focus session complete", "All pomodoro cycles finished")
                # Clear before returning -- otherwise this stays set to a now-
                # dead GLib source id, and a later _cancel_pomodoro() calls
                # GLib.source_remove() on it: a GLib critical at best, or
                # (if the id got recycled) removal of an unrelated source.
                self._pomodoro_timer_id = None
                try:
                    focus.request(active=False)
                except focus.HardLockError:
                    pass  # let a still-locked session run out on its own; nothing else to do here
                return GLib.SOURCE_REMOVE
            self._start_pomodoro_phase("break")
        else:
            self._start_pomodoro_phase("work")
        return GLib.SOURCE_REMOVE  # _start_pomodoro_phase installs the next timer itself

    # -- scheduled auto-focus --

    def _schedule_tick(self):
        state = focus.read_state()
        schedule = state.get("schedule") or []
        today = time.strftime("%Y-%m-%d", time.localtime())
        # Prune anything not from today so this set doesn't grow unboundedly
        # over the daemon's lifetime (it's process-local and was never meant
        # to be a permanent history).
        self._fired_schedule = {k for k in self._fired_schedule if k[0] == today}
        # Only ever auto-*enable* on a schedule match -- never force a
        # session off, so a user who started or extended focus manually is
        # never surprised by the scheduler cutting it short.
        if schedule and not state["active"]:
            now = time.localtime()
            weekday, hm = now.tm_wday, time.strftime("%H:%M", now)
            for entry in schedule:
                if weekday not in entry.get("days", []):
                    continue
                start, end = entry.get("start", ""), entry.get("end", "")
                if not start or not end:
                    continue
                if start <= end:
                    in_window = start <= hm < end
                else:
                    # Overnight window (e.g. 22:00-02:00): the end time is
                    # numerically *before* the start time, so the plain
                    # start <= hm < end test can never be true across
                    # midnight -- match either side of the wrap instead.
                    in_window = hm >= start or hm < end
                if not in_window:
                    continue
                # Keyed on the window's own content, not its list index --
                # reordering the schedule list (e.g. deleting an earlier
                # entry) used to shift every later entry's index, making
                # already-fired windows re-fire and not-yet-fired ones look
                # already-fired.
                key = (today, tuple(entry.get("days", [])), start, end, entry.get("profile"))
                if key in self._fired_schedule:
                    continue
                self._fired_schedule.add(key)
                logger.info("scheduled focus window matched: %r", entry)
                try:
                    focus.request(active=True, profile=entry.get("profile"))
                except focus.HardLockError:
                    pass
                break
        return GLib.SOURCE_CONTINUE

    # -- background Google Tasks sync --

    def _start_network_watch(self):
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            conn.signal_subscribe(
                "org.freedesktop.NetworkManager", "org.freedesktop.NetworkManager", "StateChanged",
                "/org/freedesktop/NetworkManager", None, Gio.DBusSignalFlags.NONE, self._on_network_state,
            )
        except GLib.Error:
            logger.warning("could not connect to system bus for NetworkManager state")
        # Slow fallback so queued task edits still flush even if the
        # NetworkManager signal is missed or unavailable (e.g. iwd-only setups).
        GLib.timeout_add_seconds(300, self._sync_tick)

    def _on_network_state(self, connection, sender, path, iface, signal_name, params):
        (new_state,) = params.unpack()
        if new_state == NM_STATE_CONNECTED_GLOBAL:
            self._sync_tick()

    def _sync_tick(self):
        # NetworkManager emits StateChanged repeatedly while connected/
        # flapping, not just on an actual transition, so a bouncing link can
        # call this many times a second; without this guard each call spawns
        # its own thread that then blocks on tasks.sync()'s internal lock,
        # piling up blocked threads. Skip instead of queueing if a sync is
        # already in flight -- the periodic 300s fallback (or the next real
        # state change) will catch anything this one skips.
        if not self._sync_in_progress.acquire(blocking=False):
            return GLib.SOURCE_CONTINUE
        threading.Thread(target=self._sync_worker, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _sync_worker(self):
        try:
            result = tasks.sync()
            if result.get("ok"):
                GLib.idle_add(self._after_sync)
        finally:
            self._sync_in_progress.release()

    def _after_sync(self):
        count = tasks.due_today_count()
        if count and count != self._last_due_notified_count:
            plural = "task" if count == 1 else "tasks"
            _send_desktop_notification("Tasks due today", f"{count} {plural} due today")
        self._last_due_notified_count = count
        return False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fan.ensure_config_defaults()
    rgb.ensure_defaults()
    focus.ensure_defaults()
    tasks.ensure_defaults()
    Automation().run()


if __name__ == "__main__":
    main()
