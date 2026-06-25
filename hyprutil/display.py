"""Set the built-in display's refresh rate via Mutter's DisplayConfig D-Bus API.

Wayland/GNOME has no xrandr equivalent, so this talks to
org.gnome.Mutter.DisplayConfig directly. Applies temporarily (method=1) so it
takes effect immediately without GNOME's "Keep these display settings?"
confirmation dialog and without writing monitors.xml.
"""
import gi

gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJ_PATH = "/org/gnome/Mutter/DisplayConfig"


def _get_current_state():
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    result = conn.call_sync(
        BUS_NAME, OBJ_PATH, BUS_NAME, "GetCurrentState",
        None, None, Gio.DBusCallFlags.NONE, -1, None,
    )
    serial, monitors, logical_monitors, props = result.unpack()
    return conn, serial, monitors, logical_monitors, props


def _current_mode_id_for(connector, monitors):
    for monitor_spec, modes, _ in monitors:
        if monitor_spec[0] == connector:
            for mode in modes:
                if mode[6].get("is-current"):
                    return mode[0]
            return modes[0][0]
    return None


def _best_mode_id_for(connector, monitors, target_hz):
    for monitor_spec, modes, _ in monitors:
        if monitor_spec[0] != connector:
            continue
        current = next((m for m in modes if m[6].get("is-current")), modes[0])
        width, height = current[1], current[2]
        candidates = [
            m for m in modes
            if m[1] == width and m[2] == height and "refresh-rate-mode" not in m[6]
        ]
        if not candidates:
            candidates = [m for m in modes if m[1] == width and m[2] == height]
        return min(candidates, key=lambda m: abs(m[3] - target_hz))[0]
    return None


def current_refresh_hz():
    try:
        _conn, _serial, monitors, _logical, _props = _get_current_state()
    except GLib.Error:
        return None
    for monitor_spec, modes, monitor_props in monitors:
        if monitor_props.get("is-builtin"):
            for mode in modes:
                if mode[6].get("is-current"):
                    return mode[3]
    return None


def set_refresh_rate(target_hz):
    """Set the built-in display to the mode closest to target_hz at its current resolution."""
    try:
        conn, serial, monitors, logical_monitors, _props = _get_current_state()
    except GLib.Error:
        return False

    builtin_connector = None
    for monitor_spec, _modes, monitor_props in monitors:
        if monitor_props.get("is-builtin"):
            builtin_connector = monitor_spec[0]
            break
    if builtin_connector is None:
        return False

    new_mode_id = _best_mode_id_for(builtin_connector, monitors, target_hz)
    if new_mode_id is None:
        return False

    new_logical = []
    for x, y, scale, transform, primary, lm_monitors, _lm_props in logical_monitors:
        new_monitors = []
        for connector, _vendor, _product, _serial in lm_monitors:
            mode_id = (
                new_mode_id if connector == builtin_connector
                else _current_mode_id_for(connector, monitors)
            )
            new_monitors.append((connector, mode_id, {}))
        new_logical.append((x, y, scale, transform, primary, new_monitors))

    variant = GLib.Variant(
        "(uua(iiduba(ssa{sv}))a{sv})",
        (serial, 1, new_logical, {}),
    )
    try:
        conn.call_sync(
            BUS_NAME, OBJ_PATH, BUS_NAME, "ApplyMonitorsConfig",
            variant, None, Gio.DBusCallFlags.NONE, -1, None,
        )
    except GLib.Error:
        return False
    return True
