"""hypr-util settings app: fan curves and keyboard RGB, in one window."""

import math
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .. import fan as backend
from .. import kbd as kbd_backend
from .. import rgb as rgb_backend

Adw.init()


class FanPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        page = Adw.PreferencesPage()
        self.append(page)

        # --- Status ---
        status_group = Adw.PreferencesGroup(title="Status")
        page.add(status_group)
        self.status_row = Adw.ActionRow(title="Loading...")
        status_group.add(self.status_row)

        # --- Power profile ---
        profile_group = Adw.PreferencesGroup(title="Power Profile")
        page.add(profile_group)
        self.profile_row = Adw.ComboRow(title="Active Profile")
        self.profile_model = Gtk.StringList.new(
            [backend.PROFILE_LABELS[p] for p in backend.PROFILES]
        )
        self.profile_row.set_model(self.profile_model)
        self._profile_signal_id = self.profile_row.connect(
            "notify::selected", self._on_profile_changed
        )
        profile_group.add(self.profile_row)

        # --- Fan curve editor ---
        curve_group = Adw.PreferencesGroup(title="Fan Curve")
        page.add(curve_group)
        self.curve_profile_row = Adw.ComboRow(title="Editing Curve For")
        self.curve_profile_row.set_model(
            Gtk.StringList.new([backend.PROFILE_LABELS[p] for p in backend.PROFILES])
        )
        self.curve_profile_row.connect(
            "notify::selected", lambda *_: self._load_curve()
        )
        curve_group.add(self.curve_profile_row)

        self.curve_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.curve_listbox.add_css_class("boxed-list")
        self.curve_listbox.set_margin_top(6)
        curve_group.add(self.curve_listbox)

        curve_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END
        )
        curve_btn_box.set_margin_top(8)
        add_point_btn = Gtk.Button(label="Add Point")
        add_point_btn.connect("clicked", lambda *_: self._add_curve_row(50, 128))
        save_curve_btn = Gtk.Button(label="Save Curve")
        save_curve_btn.add_css_class("suggested-action")
        save_curve_btn.connect("clicked", self._save_curve)
        curve_btn_box.append(add_point_btn)
        curve_btn_box.append(save_curve_btn)
        curve_group.add(curve_btn_box)

        # --- Manual override ---
        override_group = Adw.PreferencesGroup(title="Manual Override")
        page.add(override_group)
        self.override_switch_row = Adw.SwitchRow(
            title="Override Curve", subtitle="Force a fixed fan speed"
        )
        self.override_switch_row.connect("notify::active", self._on_override_toggle)
        override_group.add(self.override_switch_row)

        adjustment = Gtk.Adjustment(
            value=128, lower=0, upper=255, step_increment=1, page_increment=10
        )
        self.override_spin_row = Adw.SpinRow(
            title="PWM Value (0-255)", adjustment=adjustment
        )
        self.override_spin_row.connect("notify::value", self._on_override_value)
        override_group.add(self.override_spin_row)

        # --- Daemon controls ---
        daemon_group = Adw.PreferencesGroup(title="Daemon")
        page.add(daemon_group)
        daemon_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # Which of these is shown depends on whether the daemon is running --
        # Start while it's stopped, Stop + Restart while it's running (see
        # _apply_status). All three start hidden: the first status arrives
        # from a worker thread a moment after this page is built, and showing
        # every button until then would briefly offer Start for an already-
        # running daemon.
        self.start_btn = Gtk.Button(label="Start Daemon", visible=False)
        self.start_btn.connect("clicked", lambda *_: backend.service_action("start"))
        self.stop_btn = Gtk.Button(label="Stop Daemon", visible=False)
        self.stop_btn.connect("clicked", lambda *_: backend.service_action("stop"))
        self.restart_btn = Gtk.Button(label="Restart Daemon", visible=False)
        self.restart_btn.connect("clicked", lambda *_: backend.service_action("restart"))
        log_btn = Gtk.Button(label="View Log")
        log_btn.connect("clicked", self._show_log)
        daemon_btn_box.append(self.start_btn)
        daemon_btn_box.append(self.stop_btn)
        daemon_btn_box.append(self.restart_btn)
        daemon_btn_box.append(log_btn)
        daemon_row = Adw.ActionRow()
        daemon_row.set_child(daemon_btn_box)
        daemon_group.add(daemon_row)

        self._loading = False
        self._refreshing = False
        self._refresh()
        self._load_curve()
        self._poll_timer_id = None
        # Only poll while this page is actually visible (mapped) -- not
        # while the settings window is hidden (it hides rather than closes
        # on close-request) or while a different view-stack page is showing.
        # The tray polls its own status separately regardless.
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    def _on_map(self, *_):
        if self._poll_timer_id is None:
            self._poll_timer_id = GLib.timeout_add(2000, self._refresh_tick)

    def _on_unmap(self, *_):
        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
            self._poll_timer_id = None

    # -- status / profile --
    def _refresh_tick(self):
        self._refresh()
        return True

    def _refresh(self):
        # service_active()/current_power_profile() shell out (systemctl,
        # powerprofilesctl); gather them off the GTK main thread so the
        # 2-second poll never blocks the UI on a subprocess spawn. Skip if
        # the previous gather is still in flight.
        if self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._gather_status, daemon=True).start()

    def _gather_status(self):
        s = backend.read_status()
        active = backend.service_active()
        override = backend.read_override()
        profile = backend.current_power_profile()
        GLib.idle_add(self._apply_status, s, active, override, profile)

    def _apply_status(self, s, active, override, profile):
        rpm = max(s["fan1"] or 0, s["fan2"] or 0)
        temp_str = f"{s['temp']:.1f}°C" if s["temp"] is not None else "?°C"
        mode = (
            f"override {override}"
            if override != "auto"
            else f"{backend.PROFILE_LABELS.get(profile, profile)} curve"
        )
        self.status_row.set_title(f"{temp_str}  •  PWM {s['pwm']}  •  {rpm} RPM")
        self.status_row.set_subtitle(
            f"daemon {'running' if active else 'stopped'} ({mode})"
        )
        self.start_btn.set_visible(not active)
        self.stop_btn.set_visible(active)
        self.restart_btn.set_visible(active)

        self._loading = True
        idx = backend.PROFILES.index(profile) if profile in backend.PROFILES else 1
        self.profile_row.set_selected(idx)
        is_override = override != "auto"
        self.override_switch_row.set_active(is_override)
        self.override_spin_row.set_sensitive(is_override)
        if is_override:
            try:
                self.override_spin_row.set_value(int(override))
            except ValueError:
                pass
        self._loading = False
        self._refreshing = False
        return False

    def _on_profile_changed(self, row, *_):
        if self._loading:
            return
        profile = backend.PROFILES[row.get_selected()]
        backend.set_power_profile(profile)

    def _on_override_toggle(self, row, *_):
        if self._loading:
            return
        if row.get_active():
            backend.write_override(str(int(self.override_spin_row.get_value())))
        else:
            backend.write_override("auto")
        self.override_spin_row.set_sensitive(row.get_active())

    def _on_override_value(self, row, *_):
        if self._loading or not self.override_switch_row.get_active():
            return
        backend.write_override(str(int(row.get_value())))

    def _show_log(self, *_):
        r = subprocess.run(
            [
                "journalctl",
                "-u",
                backend.SERVICE,
                "-n",
                "100",
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
        )
        win = Adw.Window(
            title="hypr-util — Daemon Log", default_width=600, default_height=400
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        box.append(header)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        text_view = Gtk.TextView(editable=False, monospace=True)
        text_view.get_buffer().set_text(r.stdout)
        scrolled.set_child(text_view)
        box.append(scrolled)
        win.set_content(box)
        win.present()

    # -- curve editing --
    def _current_curve_profile(self):
        return backend.PROFILES[self.curve_profile_row.get_selected()]

    def _clear_curve_rows(self):
        child = self.curve_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.curve_listbox.remove(child)
            child = nxt

    def _load_curve(self):
        profile = self._current_curve_profile()
        self._clear_curve_rows()
        for t, p in backend.read_curve(profile):
            self._add_curve_row(t, p)

    def _add_curve_row(self, temp_c, pwm):
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        temp_adj = Gtk.Adjustment(value=temp_c, lower=0, upper=120, step_increment=1)
        temp_spin = Gtk.SpinButton(adjustment=temp_adj, valign=Gtk.Align.CENTER)
        pwm_adj = Gtk.Adjustment(value=pwm, lower=0, upper=255, step_increment=1)
        pwm_spin = Gtk.SpinButton(adjustment=pwm_adj, valign=Gtk.Align.CENTER)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        del_btn.add_css_class("flat")

        box.append(Gtk.Label(label="Temp °C", width_chars=8, xalign=0))
        box.append(temp_spin)
        box.append(Gtk.Label(label="PWM", width_chars=4, xalign=0, margin_start=12))
        box.append(pwm_spin)
        spacer = Gtk.Box(hexpand=True)
        box.append(spacer)
        box.append(del_btn)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        del_btn.connect("clicked", lambda *_: self.curve_listbox.remove(row))
        row.temp_spin = temp_spin
        row.pwm_spin = pwm_spin
        self.curve_listbox.append(row)

    def _save_curve(self, *_):
        points = []
        child = self.curve_listbox.get_first_child()
        while child is not None:
            points.append(
                (int(child.temp_spin.get_value()), int(child.pwm_spin.get_value()))
            )
            child = child.get_next_sibling()
        if len(points) >= 2:
            backend.write_curve(self._current_curve_profile(), points)


def _effect_label(effect):
    return effect.replace("_", " ").title()


def _rounded_rect(cr, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _set_source_hex(cr, hexval, alpha=1.0):
    r, g, b = rgb_backend.hex_to_rgb(hexval)
    cr.set_source_rgba(r / 255, g / 255, b / 255, alpha)


class _LookSwatch(Gtk.DrawingArea):
    """What a preset looks like, at a glance.

    A preset row led with nothing but a name before, so "Preset 3" told you
    nothing until you applied it. A solid block is one colour, the seven
    stripes are the device's rainbow, and an empty outline means the effect
    ignores colour entirely.
    """

    def __init__(self, width=46, height=24):
        super().__init__()
        self._colors = []
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_look(self, look):
        self._colors = rgb_backend.look_colors(look)
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        _rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, 6)
        if not self._colors:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
            cr.set_line_width(1)
            cr.stroke()
            cr.move_to(3, height - 3)
            cr.line_to(width - 3, 3)
            cr.stroke()
            return
        cr.clip_preserve()
        step = width / len(self._colors)
        for i, hexval in enumerate(self._colors):
            _set_source_hex(cr, hexval)
            # +1 so neighbouring stripes overlap by a subpixel; without it
            # antialiasing leaves a pale seam between them.
            cr.rectangle(i * step, 0, step + 1, height)
            cr.fill()
        cr.reset_clip()
        _rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, 6)
        cr.set_source_rgba(0, 0, 0, 0.22)
        cr.set_line_width(1)
        cr.stroke()


class RgbPage(Gtk.Box):
    """Keyboard lighting.

    One model, two levels. The keyboard shows exactly one *look* -- a
    built-in effect with a colour -- and the controls at the top always edit
    that look, applying as you change it. Presets are looks you saved,
    recalled with a click, and each one is bound to one of the keyboard's own
    hardware profiles so the lighting stays put with nothing running.

    The page also follows the keyboard: its Fn shortcuts change effect,
    brightness and profile on the device itself, and a poll picks those up
    and moves the controls to match rather than showing something stale.
    """

    APPLY_DEBOUNCE_MS = 250
    POLL_SECONDS = 2

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(content)

        self.banner = Adw.Banner(title="Keyboard not connected")
        content.append(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        page = Adw.PreferencesPage()
        scroller.set_child(page)
        content.append(scroller)

        self._look = rgb_backend.read_current()
        self._loading = False
        self._apply_timer_id = None
        self._poll_timer_id = None
        self._polling = False
        self._expected_state = None

        # --- the current look ---
        lighting = Adw.PreferencesGroup(title="Lighting")
        page.add(lighting)

        self.effect_row = Adw.ComboRow(title="Effect")
        self.effect_row.set_model(
            Gtk.StringList.new([_effect_label(e) for e in rgb_backend.EFFECTS])
        )
        self.effect_row.connect("notify::selected", self._on_effect_changed)
        lighting.add(self.effect_row)

        self.color_row = Adw.ActionRow(title="Color")
        self.color_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER
        )
        self.color_btn.connect("notify::rgba", self._on_color_changed)
        self.color_row.add_suffix(self.color_btn)
        lighting.add(self.color_row)

        self.rainbow_row = Adw.SwitchRow(
            title="Rainbow", subtitle="Cycle the keyboard's own colours instead"
        )
        self.rainbow_row.connect("notify::active", self._on_rainbow_changed)
        lighting.add(self.rainbow_row)

        self.direction_row = Adw.SwitchRow(title="Reverse direction")
        self.direction_row.connect("notify::active", self._on_direction_changed)
        lighting.add(self.direction_row)

        self.brightness_row, self.brightness_scale = self._make_slider(
            "Brightness", 0, rgb_backend.BRIGHTNESS_MAX, self._on_brightness_changed
        )
        lighting.add(self.brightness_row)

        self.speed_row, self.speed_scale = self._make_slider(
            "Speed", rgb_backend.SPEED_MIN, rgb_backend.SPEED_MAX, self._on_speed_changed
        )
        lighting.add(self.speed_row)

        # --- presets ---
        self.presets_group = Adw.PreferencesGroup(title="Presets")
        page.add(self.presets_group)
        self._preset_rows = {}
        for slot in rgb_backend.PRESET_SLOTS:
            row = Adw.ActionRow(activatable=True)
            swatch = _LookSwatch()
            row.add_prefix(swatch)
            check = Gtk.Image(icon_name="object-select-symbolic")
            row.add_suffix(check)
            menu_btn = Gtk.MenuButton(
                icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Preset options",
            )
            menu_btn.add_css_class("flat")
            menu_btn.set_popover(self._build_preset_menu(slot))
            row.add_suffix(menu_btn)
            row.connect("activated", self._on_preset_activated, slot)
            self.presets_group.add(row)
            self._preset_rows[slot] = (row, swatch, check)

        self._load_look_into_controls()
        self._refresh_presets()
        self._update_connection_state(rgb_backend.is_connected())
        rgb_backend.on_connection_change(self._on_connection_changed_from_udev)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # -- small builders --

    def _make_slider(self, title, lower, upper, handler):
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(lower=lower, upper=upper, step_increment=1, page_increment=1),
            hexpand=True, draw_value=True, digits=0, valign=Gtk.Align.CENTER,
            width_request=220,
        )
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.connect("value-changed", handler)
        row.add_suffix(scale)
        return row, scale

    def _build_preset_menu(self, slot):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for label, handler in (
            ("Save current look here", self._save_current_to),
            ("Rename…", self._rename_preset),
            ("Reset", self._reset_preset),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect(
                "clicked", lambda _b, h=handler, s=slot, p=popover: (p.popdown(), h(s))
            )
            box.append(button)
        popover.set_child(box)
        return popover

    # -- polling, scoped to when the page is visible --

    def _on_map(self, *_):
        if self._poll_timer_id is None:
            self._poll_timer_id = GLib.timeout_add_seconds(self.POLL_SECONDS, self._poll_tick)

    def _on_unmap(self, *_):
        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
            self._poll_timer_id = None

    def _poll_tick(self):
        self._refresh_presets()
        if not self._polling and rgb_backend.ready():
            self._polling = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        return True

    def _poll_worker(self):
        state = rgb_backend.read_device_state()
        GLib.idle_add(self._apply_device_state, state)

    def _apply_device_state(self, state):
        self._polling = False
        if state is None or self._apply_timer_id is not None:
            # A send of our own is pending; its own values win.
            return False
        if self._expected_state is None:
            self._expected_state = state
            return False
        watched = ("effect", "brightness", "speed", "direction")
        if all(state[k] == self._expected_state[k] for k in watched):
            self._expected_state = state
            return False
        # Something moved the keyboard out from under us -- its own Fn keys,
        # or another tool. Follow it rather than showing a stale UI.
        self._expected_state = state
        self._adopt_device_state(state)
        return False

    def _adopt_device_state(self, state):
        effect_code = state["effect"]
        if effect_code < len(rgb_backend.EFFECTS):
            self._look["effect"] = rgb_backend.EFFECTS[effect_code]
        self._look["brightness"] = state["brightness"]
        self._look["speed"] = state["speed"]
        self._look["direction"] = state["direction"]
        self._look["rainbow"] = state["color_idx"] == 7
        rgb_backend.write_current(self._look)
        self._load_look_into_controls()
        self._show_toast("Followed a change made on the keyboard")

    # -- connection --

    def _on_connection_changed_from_udev(self, connected):
        GLib.idle_add(self._update_connection_state, connected)

    def _update_connection_state(self, connected):
        self.banner.set_revealed(not connected)
        if connected:
            self._schedule_apply()
        return False

    def _show_toast(self, title):
        toast = Adw.Toast(title=title)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)

    # -- controls <-> look --

    def _load_look_into_controls(self):
        look = self._look
        self._loading = True
        self.effect_row.set_selected(rgb_backend.EFFECTS.index(look["effect"]))
        self.color_btn.set_rgba(_hex_to_rgba(look["color"]))
        self.rainbow_row.set_active(look["rainbow"])
        self.direction_row.set_active(bool(look["direction"]))
        self.brightness_scale.set_value(look["brightness"])
        # The wire value runs 1 fastest to 7 slowest; the slider reads the
        # natural way round, so right is faster.
        self.speed_scale.set_value(rgb_backend.SPEED_MAX + rgb_backend.SPEED_MIN - look["speed"])
        self._loading = False
        self._sync_control_visibility()

    def _sync_control_visibility(self):
        """Show only the controls that do something for the current look."""
        effect = self._look["effect"]
        # Several effects animate their own colours and ignore the palette
        # entirely.
        colorful = rgb_backend.supports_color(effect)
        self.color_row.set_visible(colorful and not self._look["rainbow"])
        self.rainbow_row.set_visible(colorful)
        self.direction_row.set_visible(rgb_backend.supports_direction(effect))

    def _on_effect_changed(self, row, *_):
        if self._loading:
            return
        self._look["effect"] = rgb_backend.EFFECTS[row.get_selected()]
        self._sync_control_visibility()
        self._look_changed()

    def _on_color_changed(self, btn, *_):
        if self._loading:
            return
        # Stored and shown boosted, because that is what the keyboard will
        # actually render: these LEDs wash anything less than fully saturated
        # out to pink, so a swatch showing the raw picked colour would be a
        # promise the hardware does not keep.
        hexval = rgb_backend.boost_color(_rgba_to_hex(btn.get_rgba()))
        if hexval == self._look["color"]:
            return
        self._look["color"] = hexval
        self._loading = True
        btn.set_rgba(_hex_to_rgba(hexval))
        self._loading = False
        self._look_changed()

    def _on_rainbow_changed(self, row, *_):
        if self._loading:
            return
        self._look["rainbow"] = row.get_active()
        self._sync_control_visibility()
        self._look_changed()

    def _on_direction_changed(self, row, *_):
        if self._loading:
            return
        self._look["direction"] = rgb_backend.DIRECTION_LEFT if row.get_active() else rgb_backend.DIRECTION_RIGHT
        self._look_changed()

    def _on_brightness_changed(self, scale):
        if self._loading:
            return
        self._look["brightness"] = int(scale.get_value())
        self._look_changed()

    def _on_speed_changed(self, scale):
        if self._loading:
            return
        self._look["speed"] = rgb_backend.SPEED_MAX + rgb_backend.SPEED_MIN - int(scale.get_value())
        self._look_changed()

    def _look_changed(self):
        rgb_backend.write_current(self._look)
        # Editing by hand means no preset is what the keyboard is showing any
        # more. Clearing this is what stops the tray from ticking a preset
        # whose colours are long gone.
        rgb_backend.clear_active_preset()
        self._refresh_presets()
        self._schedule_apply()

    # -- applying --

    def _schedule_apply(self, immediate=False):
        if self._apply_timer_id is not None:
            GLib.source_remove(self._apply_timer_id)
        self._apply_timer_id = GLib.timeout_add(
            0 if immediate else self.APPLY_DEBOUNCE_MS, self._fire_apply
        )

    def _fire_apply(self):
        # Coalesced: a send is a paced sequence of reports, so firing one per
        # slider step would leave the keyboard trailing the UI badly.
        self._apply_timer_id = None
        if not rgb_backend.ready():
            return False
        look = dict(self._look)
        threading.Thread(target=self._apply_worker, args=(look,), daemon=True).start()
        return False

    def _apply_worker(self, look):
        try:
            rgb_backend.apply_current(look)
            GLib.idle_add(self._after_apply)
        except Exception as e:
            GLib.idle_add(self._show_toast, f"Could not apply lighting: {e}")

    def _after_apply(self):
        # Re-baseline so our own change is not mistaken for someone else's.
        self._expected_state = None
        return False

    # -- presets --

    def _refresh_presets(self):
        active = rgb_backend.active_preset()
        for slot, (row, swatch, check) in self._preset_rows.items():
            preset = rgb_backend.read_preset(slot)
            row.set_title(GLib.markup_escape_text(preset["name"]))
            kind = _effect_label(preset["effect"])
            # Saying where a preset lives matters: the ones backed by a
            # keyboard profile keep working with nothing running, and the
            # leftover one does not.
            profile = rgb_backend.profile_for(slot)
            where = f"Keyboard profile {profile + 1}" if profile is not None else "This app only"
            row.set_subtitle(f"{kind} · {where}")
            swatch.set_look(preset)
            check.set_visible(slot == active)

    def _on_preset_activated(self, _row, slot):
        preset = rgb_backend.read_preset(slot)
        self._look = rgb_backend.normalize_look(preset)
        self._load_look_into_controls()
        rgb_backend.set_active_preset(slot)
        self._refresh_presets()
        if self._apply_timer_id is not None:
            GLib.source_remove(self._apply_timer_id)
            self._apply_timer_id = None
        threading.Thread(target=self._apply_preset_worker, args=(slot,), daemon=True).start()

    def _apply_preset_worker(self, slot):
        try:
            rgb_backend.apply_preset(slot)
            GLib.idle_add(self._after_apply)
        except Exception as e:
            GLib.idle_add(self._show_toast, f"Could not apply preset: {e}")

    def _save_current_to(self, slot):
        preset = rgb_backend.read_preset(slot)
        rgb_backend.write_preset(slot, self._look, name=preset["name"])
        rgb_backend.set_active_preset(slot)
        self._refresh_presets()
        # Re-apply so the look lands in that preset's hardware profile.
        threading.Thread(target=self._apply_preset_worker, args=(slot,), daemon=True).start()
        self._show_toast(f"Saved to {preset['name']}")

    def _rename_preset(self, slot):
        preset = rgb_backend.read_preset(slot)
        entry = Gtk.Entry(text=preset["name"], activates_default=True)
        dialog = Adw.AlertDialog(heading="Rename Preset")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_rename_response, slot, entry)
        dialog.present(self)

    def _on_rename_response(self, _dialog, response, slot, entry):
        if response != "rename":
            return
        name = entry.get_text().strip()
        if name:
            rgb_backend.rename_preset(slot, name)
            self._refresh_presets()

    def _reset_preset(self, slot):
        rgb_backend.reset_preset(slot)
        self._refresh_presets()


def _hex_to_rgba(hexval):
    from gi.repository import Gdk

    rgba = Gdk.RGBA()
    rgba.parse(f"#{hexval.lstrip('#')}")
    return rgba


def _rgba_to_hex(rgba):
    return "".join(
        f"{int(round(c * 255)):02x}" for c in (rgba.red, rgba.green, rgba.blue)
    )


class LaptopKbdPage(Gtk.Box):
    """The laptop's own keyboard: four zones, driven through the BIOS.

    A different keyboard from the RGB page's, with a different set of
    honest limits. There are four zones and no more, the firmware runs no
    animations of its own (so an effect is this app writing frames), and
    everything needs the root service, because the mailbox that carries the
    colours is root-only. When that service is missing the page says so and
    says what to run, rather than showing controls that do nothing.
    """

    APPLY_DEBOUNCE_MS = 200
    PREVIEW_INTERVAL_MS = 120

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(content)

        self.banner = Adw.Banner(title="Keyboard lighting service not available")
        content.append(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        page = Adw.PreferencesPage()
        scroller.set_child(page)
        content.append(scroller)

        self._look = kbd_backend.read_current()
        self._zones = 4
        self._loading = False
        self._apply_timer_id = None
        self._preview_timer_id = None
        self._phase = 0.0
        self._busy = False

        preview_group = Adw.PreferencesGroup()
        page.add(preview_group)
        self.preview = _ZonePreview()
        preview_row = Adw.ActionRow()
        preview_row.set_child(self.preview)
        preview_group.add(preview_row)

        lighting = Adw.PreferencesGroup(
            title="Backlight",
            description="The keyboard built into the laptop, lit in four zones",
        )
        page.add(lighting)

        self.on_row = Adw.SwitchRow(title="Lighting")
        self.on_row.connect("notify::active", self._on_switch_changed)
        lighting.add(self.on_row)

        self.effect_row = Adw.ComboRow(title="Effect")
        self.effect_row.set_model(
            Gtk.StringList.new([kbd_backend.EFFECT_LABELS[e] for e in kbd_backend.EFFECTS])
        )
        self.effect_row.connect("notify::selected", self._on_effect_changed)
        lighting.add(self.effect_row)

        self.brightness_row, self.brightness_scale = self._make_slider(
            "Brightness", 0, kbd_backend.BRIGHTNESS_MAX, self._on_brightness_changed
        )
        lighting.add(self.brightness_row)

        self.speed_row, self.speed_scale = self._make_slider(
            "Speed", kbd_backend.SPEED_MIN, kbd_backend.SPEED_MAX, self._on_speed_changed
        )
        lighting.add(self.speed_row)

        self.zones_group = Adw.PreferencesGroup(title="Zones")
        page.add(self.zones_group)
        self._zone_rows = {}
        for zone in kbd_backend.DISPLAY_ORDER:
            row = Adw.ActionRow(title=kbd_backend.ZONE_NAMES[zone])
            button = Gtk.ColorDialogButton(
                dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER
            )
            button.connect("notify::rgba", self._on_zone_color_changed, zone)
            row.add_suffix(button)
            self.zones_group.add(row)
            self._zone_rows[zone] = (row, button)

        all_row = Adw.ActionRow(
            title="All zones", subtitle="Paint every zone the same colour"
        )
        self.all_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER
        )
        self.all_btn.connect("notify::rgba", self._on_all_color_changed)
        all_row.add_suffix(self.all_btn)
        self.zones_group.add(all_row)

        factory = Gtk.Button(label="Factory colours", margin_top=8)
        factory.connect("clicked", self._on_factory_clicked)
        self.zones_group.add(factory)

        self._load_look_into_controls()
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # -- small builders --

    def _make_slider(self, title, lower, upper, handler):
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(lower=lower, upper=upper, step_increment=1, page_increment=1),
            hexpand=True, draw_value=True, digits=0, valign=Gtk.Align.CENTER,
            width_request=220,
        )
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.connect("value-changed", handler)
        row.add_suffix(scale)
        return row, scale

    # -- service state --

    def _on_map(self, *_):
        self._refresh_status()
        if self._preview_timer_id is None:
            self._preview_timer_id = GLib.timeout_add(
                self.PREVIEW_INTERVAL_MS, self._preview_tick
            )

    def _on_unmap(self, *_):
        if self._preview_timer_id is not None:
            GLib.source_remove(self._preview_timer_id)
            self._preview_timer_id = None

    def _refresh_status(self):
        threading.Thread(target=self._status_worker, daemon=True).start()

    def _status_worker(self):
        try:
            reply = kbd_backend.status()
        except Exception as e:
            reply = {"ok": False, "error": str(e), "unreachable": True}
        GLib.idle_add(self._apply_status, reply)

    def _apply_status(self, reply):
        keyboard = reply.get("keyboard") or {}
        usable = bool(keyboard.get("usable"))
        if reply.get("unreachable"):
            self.banner.set_title(
                "Lighting service not running — start it with "
                "sudo systemctl start hypr-util-kbd.service"
            )
        elif not keyboard:
            self.banner.set_title(reply.get("error") or "No controllable keyboard lighting")
        elif not usable:
            # A per-key board answers every one of these calls and lights
            # nothing by them; saying so beats letting someone chase a
            # colour picker that cannot work.
            self.banner.set_title(
                f"This keyboard is {keyboard.get('describe')} — the firmware "
                "cannot drive it"
            )
        self.banner.set_revealed(not usable)
        self.set_sensitive_controls(usable)
        if usable:
            self._zones = int(keyboard.get("zones") or 4)
            look = reply.get("look")
            if look and not self._busy:
                self._look = kbd_backend.normalize_look(look)
                self._load_look_into_controls()
        return False

    def set_sensitive_controls(self, sensitive):
        for widget in (self.on_row, self.effect_row, self.brightness_row,
                       self.speed_row, self.zones_group):
            widget.set_sensitive(sensitive)

    # -- controls <-> look --

    def _load_look_into_controls(self):
        look = self._look
        self._loading = True
        self.on_row.set_active(look["on"])
        self.effect_row.set_selected(kbd_backend.EFFECTS.index(look["effect"]))
        self.brightness_scale.set_value(look["brightness"])
        self.speed_scale.set_value(look["speed"])
        for zone, (_row, button) in self._zone_rows.items():
            button.set_rgba(_hex_to_rgba(look["colors"][zone]))
        self._loading = False
        self._sync_control_visibility()

    def _sync_control_visibility(self):
        """Show only what this look can use."""
        animated = self._look["effect"] in kbd_backend.ANIMATED
        self.speed_row.set_visible(animated)
        # Cycle and wave run the whole spectrum and never read the zone
        # colours, so the pickers would be lying if they stayed live.
        picks_colors = self._look["effect"] in ("static", "breathe")
        self.zones_group.set_visible(picks_colors)
        for zone, (row, _button) in self._zone_rows.items():
            row.set_visible(zone < self._zones or self._zones == 1)

    def _on_switch_changed(self, row, *_):
        if self._loading:
            return
        self._look["on"] = row.get_active()
        self._look_changed()

    def _on_effect_changed(self, row, *_):
        if self._loading:
            return
        self._look["effect"] = kbd_backend.EFFECTS[row.get_selected()]
        self._phase = 0.0
        self._sync_control_visibility()
        self._look_changed()

    def _on_brightness_changed(self, scale):
        if self._loading:
            return
        self._look["brightness"] = int(scale.get_value())
        self._look_changed()

    def _on_speed_changed(self, scale):
        if self._loading:
            return
        self._look["speed"] = int(scale.get_value())
        self._look_changed()

    def _on_zone_color_changed(self, button, _param, zone):
        if self._loading:
            return
        hexval = _rgba_to_hex(button.get_rgba())
        if self._look["colors"][zone] == hexval:
            return
        self._look["colors"][zone] = hexval
        self._look["on"] = True
        self._loading = True
        self.on_row.set_active(True)
        self._loading = False
        self._look_changed()

    def _on_all_color_changed(self, button, *_):
        if self._loading:
            return
        hexval = _rgba_to_hex(button.get_rgba())
        self._look["colors"] = [hexval] * len(self._look["colors"])
        self._look["on"] = True
        self._load_look_into_controls()
        self._look_changed()

    def _on_factory_clicked(self, *_):
        self._look["colors"] = list(kbd_backend.FACTORY_COLORS)
        self._look["on"] = True
        self._load_look_into_controls()
        self._look_changed()

    def _look_changed(self):
        self.preview.set_look(self._look, self._zones, self._phase)
        self._schedule_apply()

    # -- applying --

    def _schedule_apply(self):
        if self._apply_timer_id is not None:
            GLib.source_remove(self._apply_timer_id)
        self._apply_timer_id = GLib.timeout_add(self.APPLY_DEBOUNCE_MS, self._fire_apply)

    def _fire_apply(self):
        # Coalesced: dragging a slider produces a change per pixel and every
        # one of them is a pair of ACPI calls on the other side of a socket.
        self._apply_timer_id = None
        look = kbd_backend.normalize_look(self._look)
        self._busy = True
        threading.Thread(target=self._apply_worker, args=(look,), daemon=True).start()
        return False

    def _apply_worker(self, look):
        error = None
        try:
            kbd_backend.apply(look)
        except Exception as e:
            error = str(e)
        GLib.idle_add(self._after_apply, error)

    def _after_apply(self, error):
        self._busy = False
        if error:
            self._show_toast(error)
        return False

    def _show_toast(self, title):
        toast = Adw.Toast(title=title)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    # -- preview --

    def _preview_tick(self):
        """Run the drawn keyboard from the same frame function the daemon
        writes to the hardware, so the preview is the effect rather than an
        impression of it."""
        if self._look["on"] and self._look["effect"] in kbd_backend.ANIMATED:
            self._phase += kbd_backend.zones.PHASE_STEP * kbd_backend.zones.SPEED_FACTORS[
                self._look["speed"]
            ]
        self.preview.set_look(self._look, self._zones, self._phase)
        return True


class _ZonePreview(Gtk.DrawingArea):
    """The four zones as they sit on the keyboard: three bands across, with
    the WASD cluster picked out over the left one."""

    def __init__(self, height=96):
        super().__init__()
        self._colors = []
        self._on = True
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_look(self, look, zones, phase):
        self._colors = kbd_backend.frame_colors(look, zones, phase)
        self._on = look["on"]
        self.queue_draw()

    def _zone_color(self, zone):
        if zone < len(self._colors):
            return self._colors[zone]
        return self._colors[0] if self._colors else (0, 0, 0)

    def _draw(self, _area, cr, width, height):
        pad = 6
        w, h = width - 2 * pad, height - 2 * pad
        bands = [kbd_backend.ZONE_LEFT, kbd_backend.ZONE_MIDDLE, kbd_backend.ZONE_RIGHT]
        if len(self._colors) == 1:
            bands = [0, 0, 0]
        step = w / len(bands)
        _rounded_rect(cr, pad, pad, w, h, 10)
        cr.clip_preserve()
        for i, zone in enumerate(bands):
            r, g, b = self._zone_color(zone)
            alpha = 1.0 if self._on else 0.18
            cr.set_source_rgba(r / 255, g / 255, b / 255, alpha)
            # +1 so neighbouring bands overlap by a subpixel; without it
            # antialiasing leaves a pale seam between them.
            cr.rectangle(pad + i * step, pad, step + 1, h)
            cr.fill()
        cr.reset_clip()

        if len(self._colors) > kbd_backend.ZONE_WASD:
            r, g, b = self._zone_color(kbd_backend.ZONE_WASD)
            cluster_w, cluster_h = min(74.0, step * 0.8), min(34.0, h * 0.42)
            x = pad + step * 0.5 - cluster_w / 2
            y = pad + h * 0.52
            _rounded_rect(cr, x, y, cluster_w, cluster_h, 6)
            cr.set_source_rgba(r / 255, g / 255, b / 255, 1.0 if self._on else 0.18)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.set_line_width(1)
            cr.stroke()
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(10)
            luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            cr.set_source_rgba(0, 0, 0, 0.8) if luma > 0.55 else cr.set_source_rgba(1, 1, 1, 0.9)
            extents = cr.text_extents("WASD")
            cr.move_to(x + (cluster_w - extents.width) / 2, y + cluster_h / 2 + 3.5)
            cr.show_text("WASD")

        _rounded_rect(cr, pad + 0.5, pad + 0.5, w - 1, h - 1, 10)
        cr.set_source_rgba(0, 0, 0, 0.22)
        cr.set_line_width(1)
        cr.stroke()


class HyprUtilWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app, title="hypr-util", default_width=560, default_height=640
        )

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Stored on self (not just a local) so the app-level "open-page"
        # action (see HyprUtilApp) can select a page on an already-running,
        # D-Bus-activated instance.
        self.view_stack = Adw.ViewStack()
        self.view_stack.add_titled_with_icon(
            FanPage(), "fan", "Fan", "temperature-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            RgbPage(), "rgb", "RGB", "input-keyboard-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            LaptopKbdPage(), "kbd", "Laptop", "keyboard-brightness-symbolic"
        )

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(
            stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE
        )
        header.set_title_widget(switcher)
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.view_stack)

        # Hide rather than destroy on close, so the process (and its already-
        # imported GTK4/libadwaita) stays resident and the next D-Bus
        # Activate (see do_activate below) just re-presents this window
        # instead of paying a cold GTK start again. Real exit is Ctrl+Q.
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, *_):
        self.hide()
        return True


class HyprUtilApp(Adw.Application):
    def __init__(self):
        # HANDLES_COMMAND_LINE: without it, when an instance is already
        # running, GApplication's default behavior is to forward a plain
        # remote "Activate" to the primary and drop argv entirely -- so a
        # `bin/hyprutil app --page rgb` invocation would lose the `--page`
        # request whenever a primary instance already happened to be
        # running, silently opening the app to whatever page it was last on.
        # This flag routes every invocation (primary or remote) through
        # do_command_line below instead, which still sees the full argv
        # either way.
        super().__init__(
            application_id="org.hyprnon.hyprutil",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._win = None

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        # Lets the tray (and anything else) navigate to a specific page on
        # an already-running, D-Bus-activated instance via the standard
        # org.freedesktop.Application.ActivateAction method -- plain
        # Activate (used for "Open hypr-util...") has no way to carry that
        # intent.
        open_page_action = Gio.SimpleAction.new("open-page", GLib.VariantType.new("s"))
        open_page_action.connect("activate", self._on_open_page)
        self.add_action(open_page_action)

    def _window(self):
        # Tracked explicitly rather than derived from self.props.active_window:
        # HyprUtilWindow hides (rather than destroys) on close so the process
        # stays resident, but a hidden window isn't guaranteed to still be
        # GTK's notion of "active" -- if it isn't, this would build a *second*
        # HyprUtilWindow, re-running both page constructors and starting a
        # second set of 2s pollers while the first (hidden) window's keep
        # running too, accumulating orphaned windows + timers on repeated
        # open/close.
        if self._win is None:
            self._win = HyprUtilWindow(self)
        return self._win

    def do_activate(self):
        self._window().present()

    def _open_page(self, page):
        win = self._window()
        win.view_stack.set_visible_child_name(page)
        win.present()

    def _on_open_page(self, action, parameter):
        self._open_page(parameter.get_string())

    def do_command_line(self, command_line):
        # Called for *every* invocation once HANDLES_COMMAND_LINE is set --
        # both the primary's own initial run and any later remote run get
        # forwarded here with their real argv (including "--gapplication-
        # service", which GLib strips before this ever sees it -- that
        # option is intercepted at a lower level regardless of this flag).
        args = command_line.get_arguments()
        page = None
        if "--page" in args:
            idx = args.index("--page")
            if idx + 1 < len(args):
                page = args[idx + 1]
        if page:
            self._open_page(page)
        else:
            self.activate()
        return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv)

    backend.ensure_config_defaults()
    rgb_backend.ensure_defaults()

    app = HyprUtilApp()
    app.run(argv)


if __name__ == "__main__":
    main()
