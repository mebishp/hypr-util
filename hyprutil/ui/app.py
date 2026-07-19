"""hypr-util settings app: fan curves, keyboard RGB, focus mode, and a
Google-Tasks-backed todo list, in one window."""

import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .. import fan as backend
from .. import focus as focus_backend
from .. import rgb as rgb_backend
from .. import tasks as tasks_backend

Adw.init()

# Weekday index (Monday=0) used both in the schedule editor below and by
# automation.py's FocusController._schedule_tick (time.struct_time.tm_wday).
_WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
        self.toggle_btn = Gtk.Button(label="Stop Daemon")
        self.toggle_btn.connect("clicked", self._toggle_daemon)
        restart_btn = Gtk.Button(label="Restart Daemon")
        restart_btn.connect("clicked", lambda *_: backend.service_action("restart"))
        log_btn = Gtk.Button(label="View Log")
        log_btn.connect("clicked", self._show_log)
        daemon_btn_box.append(self.toggle_btn)
        daemon_btn_box.append(restart_btn)
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
        self.toggle_btn.set_label("Stop Daemon" if active else "Start Daemon")

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

    def _toggle_daemon(self, *_):
        backend.service_action("stop" if backend.service_active() else "start")

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


class RgbPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)

        page = Adw.PreferencesPage()
        self.toast_overlay.set_child(page)

        # --- Keyboard connection status ---
        kb_group = Adw.PreferencesGroup()
        page.add(kb_group)
        self.kb_icon = Gtk.Image()
        self.kb_status_row = Adw.ActionRow(title="Keyboard")
        self.kb_status_row.add_prefix(self.kb_icon)
        kb_group.add(self.kb_status_row)

        # --- Lighting editor ---
        editor_group = Adw.PreferencesGroup(
            title="Lighting",
            description="Pick an effect and a color, then apply",
        )
        page.add(editor_group)

        editor_state = rgb_backend.read_editor_state()

        self.effect_row = Adw.ComboRow(title="Effect")
        self.effect_row.set_model(
            Gtk.StringList.new(
                [e.replace("_", " ").title() for e in rgb_backend.EFFECTS]
            )
        )
        if editor_state["effect"] in rgb_backend.EFFECTS:
            self.effect_row.set_selected(
                rgb_backend.EFFECTS.index(editor_state["effect"])
            )
        editor_group.add(self.effect_row)

        self.multi_switch_row = Adw.SwitchRow(
            title="Multiple Colors",
            subtitle="Cycle through 7 colors instead of using just one",
        )
        self.multi_switch_row.connect("notify::active", self._on_multi_toggle)
        editor_group.add(self.multi_switch_row)

        self.single_color_row = Adw.ActionRow(title="Color")
        self.single_color_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER
        )
        self.single_color_btn.set_rgba(_hex_to_rgba(editor_state["color"]))
        self.single_color_row.add_suffix(self.single_color_btn)
        editor_group.add(self.single_color_row)

        self.multi_color_row = Adw.ActionRow(title="Colors")
        multi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.color_buttons = []
        for i, hexval in enumerate(rgb_backend.DEFAULT_COLORS):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            btn.set_rgba(
                _hex_to_rgba(
                    editor_state["colors"][i]
                    if i < len(editor_state["colors"])
                    else hexval
                )
            )
            multi_box.append(btn)
            self.color_buttons.append(btn)
        self.multi_color_row.set_child(multi_box)
        editor_group.add(self.multi_color_row)
        self.single_color_row.set_visible(not editor_state["multi"])
        self.multi_color_row.set_visible(editor_state["multi"])
        self.multi_switch_row.set_active(editor_state["multi"])

        apply_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END, margin_top=4
        )
        self.apply_btn = Gtk.Button(label="Apply Now")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self._apply_now)
        apply_box.append(self.apply_btn)
        editor_group.add(apply_box)

        # --- Presets (4 standalone slots, independent of power profile) ---
        preset_group = Adw.PreferencesGroup(
            title="Presets",
            description="4 saved lighting setups, selectable here or from the tray. Loading/saving/renaming doesn't need the keyboard connected.",
        )
        page.add(preset_group)

        self.preset_row = Adw.ComboRow(title="Preset")
        self._refresh_preset_model()
        preset_group.add(self.preset_row)

        self.preset_name_row = Adw.EntryRow(title="Name")
        self.preset_name_row.set_text(
            rgb_backend.read_preset(rgb_backend.PRESET_SLOTS[0])["name"]
        )
        preset_group.add(self.preset_name_row)
        self.preset_row.connect("notify::selected", self._on_preset_selected)

        preset_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=4,
        )
        load_btn = Gtk.Button(label="Load Into Editor")
        load_btn.connect("clicked", self._load_preset)
        save_preset_btn = Gtk.Button(label="Save")
        save_preset_btn.connect("clicked", self._save_preset)
        apply_preset_btn = Gtk.Button(label="Apply")
        apply_preset_btn.add_css_class("suggested-action")
        apply_preset_btn.connect("clicked", self._apply_preset_directly)
        preset_btn_box.append(load_btn)
        preset_btn_box.append(save_preset_btn)
        preset_btn_box.append(apply_preset_btn)
        preset_group.add(preset_btn_box)

        if not rgb_backend.available():
            warn = Adw.PreferencesGroup()
            warn.add(
                Adw.ActionRow(
                    title="firefly-ctl not found",
                    subtitle=str(rgb_backend.CTL_BIN),
                )
            )
            page.add(warn)

        self._update_connection_state(rgb_backend.is_connected())
        rgb_backend.on_connection_change(self._on_connection_changed_from_udev)

    def _show_toast(self, title):
        toast = Adw.Toast(title=title)
        toast.set_timeout(1)
        self.toast_overlay.add_toast(toast)

    # -- connection awareness --
    def _on_connection_changed_from_udev(self, connected):
        # Runs on udev's background thread -- hop back to the GLib main loop.
        GLib.idle_add(self._update_connection_state, connected)

    def _update_connection_state(self, connected):
        if connected:
            self.kb_icon.set_from_icon_name("input-keyboard-symbolic")
            self.kb_status_row.set_subtitle("Connected")
        else:
            self.kb_icon.set_from_icon_name("action-unavailable-symbolic")
            self.kb_status_row.set_subtitle(
                "Not connected -- plug it in to apply lighting"
            )
        self.apply_btn.set_sensitive(connected and rgb_backend.available())
        return False

    # -- color helpers --
    def _current_colors(self):
        if self.multi_switch_row.get_active():
            return [_rgba_to_hex(btn.get_rgba()) for btn in self.color_buttons]
        hexval = _rgba_to_hex(self.single_color_btn.get_rgba())
        return [hexval] * 7

    def _set_colors(self, colors):
        for btn, hexval in zip(self.color_buttons, colors):
            btn.set_rgba(_hex_to_rgba(hexval))
        if colors:
            self.single_color_btn.set_rgba(_hex_to_rgba(colors[0]))

    def _on_multi_toggle(self, row, *_):
        is_multi = row.get_active()
        self.single_color_row.set_visible(not is_multi)
        self.multi_color_row.set_visible(is_multi)

    # -- actions --
    def _apply_now(self, *_):
        is_multi = self.multi_switch_row.get_active()
        effect = rgb_backend.EFFECTS[self.effect_row.get_selected()]
        color_idx = 7 if is_multi else 0
        colors = self._current_colors()
        single_color = _rgba_to_hex(self.single_color_btn.get_rgba())
        rgb_backend.write_editor_state(effect, is_multi, single_color, colors)

        self.apply_btn.set_sensitive(False)
        self.apply_btn.set_label("Applying…")

        def worker():
            try:
                rgb_backend.apply(effect, colors, color_idx)
                GLib.idle_add(self._apply_done, True, None)
            except Exception as e:
                GLib.idle_add(self._apply_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_done(self, success, error):
        self.apply_btn.set_label("Apply Now")
        self.apply_btn.set_sensitive(
            rgb_backend.is_connected() and rgb_backend.available()
        )
        self._show_toast("Lighting applied" if success else f"Failed: {error}")
        return False

    def _current_slot(self):
        return rgb_backend.PRESET_SLOTS[self.preset_row.get_selected()]

    def _refresh_preset_model(self):
        names = [rgb_backend.read_preset(s)["name"] for s in rgb_backend.PRESET_SLOTS]
        self.preset_row.set_model(Gtk.StringList.new(names))

    def _on_preset_selected(self, row, *_):
        slot = self._current_slot()
        self.preset_name_row.set_text(rgb_backend.read_preset(slot)["name"])

    def _load_preset(self, *_):
        preset = rgb_backend.read_preset(self._current_slot())
        self.effect_row.set_selected(rgb_backend.EFFECTS.index(preset["effect"]))
        colors = preset["colors"]
        is_multi = len(set(colors)) > 1
        self.multi_switch_row.set_active(is_multi)
        self._set_colors(colors)

    def _save_preset(self, *_):
        slot = self._current_slot()
        effect = rgb_backend.EFFECTS[self.effect_row.get_selected()]
        color_idx = 7 if self.multi_switch_row.get_active() else 0
        colors = self._current_colors()
        name = self.preset_name_row.get_text().strip() or f"Preset {slot}"
        rgb_backend.write_preset(slot, effect, colors, color_idx, name=name)
        self._refresh_preset_model()
        self.preset_row.set_selected(rgb_backend.PRESET_SLOTS.index(slot))
        self._show_toast(f"Saved “{name}”")

    def _apply_preset_directly(self, *_):
        slot = self._current_slot()
        name = rgb_backend.read_preset(slot)["name"]

        def worker():
            try:
                rgb_backend.apply_preset(slot)
                GLib.idle_add(self._apply_preset_done, True, name, None)
            except Exception as e:
                GLib.idle_add(self._apply_preset_done, False, name, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_preset_done(self, success, name, error):
        title = f"Applied “{name}”" if success else f"Failed: {error}"
        self._show_toast(title)
        return False


class TodoPage(Gtk.Box):
    """Google-Tasks-backed todo list. Every edit here applies to the local
    store instantly (tasks_backend.add_task/set_done/delete_task never touch
    the network) and schedules a debounced background sync a few seconds
    later -- see _schedule_sync. sync() is also safe to skip entirely: the
    list is fully usable offline forever, it just won't reach Google until
    a connection + sync happens (this page's "Sync Now", or the automation
    daemon's network-up/periodic trigger)."""

    SYNC_DEBOUNCE_SECONDS = 8

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)
        page = Adw.PreferencesPage()
        self.toast_overlay.set_child(page)

        self._sync_debounce_id = None
        self._oauth_cancel_event = None
        self._loading = False
        self._syncing = False

        # --- task list ---
        list_group = Adw.PreferencesGroup(title="Tasks")
        page.add(list_group)
        self.list_row = Adw.ComboRow(title="List")
        self._refresh_list_model()
        self.list_row.connect("notify::selected", self._on_list_selected)
        list_group.add(self.list_row)

        self.task_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.task_listbox.add_css_class("boxed-list")
        self.task_listbox.set_margin_top(6)
        list_group.add(self.task_listbox)

        add_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=8
        )
        self.new_task_entry = Gtk.Entry(placeholder_text="Add a task…", hexpand=True)
        self.new_task_entry.connect("activate", self._add_task)
        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._add_task)
        add_box.append(self.new_task_entry)
        add_box.append(add_btn)
        list_group.add(add_box)

        # --- Google connection ---
        conn_group = Adw.PreferencesGroup(
            title="Google Tasks",
            description="Connect to sync it with Google Tasks.",
        )
        page.add(conn_group)
        self.conn_row = Adw.ActionRow(title="Not connected")
        conn_group.add(self.conn_row)

        self.client_id_row = Adw.EntryRow(title="OAuth Client ID")
        conn_group.add(self.client_id_row)
        self.client_secret_row = Adw.PasswordEntryRow(title="OAuth Client Secret")
        conn_group.add(self.client_secret_row)

        conn_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=4,
        )
        self.sync_btn = Gtk.Button(label="Sync Now")
        self.sync_btn.connect("clicked", lambda *_: self._sync(manual=True))
        self.connect_btn = Gtk.Button(label="Connect Google")
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.connect("clicked", self._on_connect_clicked)
        conn_btn_box.append(self.sync_btn)
        conn_btn_box.append(self.connect_btn)
        conn_group.add(conn_btn_box)

        self._load_tasks()
        self._refresh_connection_status()
        self._sync(
            manual=False
        )  # cheap no-op if not connected; syncs on page open if it is

    def _show_toast(self, title):
        toast = Adw.Toast(title=title)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)

    # -- connection --

    def _on_connect_clicked(self, *_):
        if self._oauth_cancel_event is not None:
            # Already waiting -- this click means Cancel, not Connect.
            # authorize_url_and_wait() polls in 1s slices specifically so
            # this is noticed promptly instead of only after the full
            # multi-minute timeout (there was previously no way to back out
            # of a stuck/abandoned consent flow short of force-quitting).
            self._oauth_cancel_event.set()
            return

        if not tasks_backend.has_client():
            client_id = self.client_id_row.get_text().strip()
            client_secret = self.client_secret_row.get_text().strip()
            if not client_id or not client_secret:
                self._show_toast("Enter your Google OAuth client ID + secret first")
                return
            tasks_backend.save_client(client_id, client_secret)

        cancel_event = threading.Event()
        self._oauth_cancel_event = cancel_event
        self.connect_btn.set_label("Cancel")
        self.connect_btn.remove_css_class("suggested-action")
        self.connect_btn.add_css_class("destructive-action")

        def worker():
            try:
                tasks_backend.authorize_url_and_wait(
                    self._open_url, cancel_event=cancel_event
                )
                GLib.idle_add(self._connect_done, True, None)
            except Exception as e:
                GLib.idle_add(self._connect_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _open_url(self, url):
        # Must not pass Gio.AppInfo.launch_default_for_uri directly as the
        # idle callback: GLib.idle_add reschedules its callback for as long
        # as it keeps returning a truthy value, and launch_default_for_uri
        # itself returns True on success -- that reopened the browser on
        # every subsequent main-loop idle tick, forever, instead of once.
        GLib.idle_add(self._launch_url_once, url)

    @staticmethod
    def _launch_url_once(url):
        Gio.AppInfo.launch_default_for_uri(url, None)
        return False

    def _connect_done(self, success, error):
        self._oauth_cancel_event = None
        self.connect_btn.set_label("Connect Google")
        self.connect_btn.remove_css_class("destructive-action")
        self.connect_btn.add_css_class("suggested-action")
        self._refresh_connection_status()
        if success:
            self._show_toast("Connected to Google Tasks")
            self._sync(manual=True)
        elif error == "cancelled":
            self._show_toast("Connection cancelled")
        else:
            self._show_toast(f"Connection failed: {error}")
        return False

    def _refresh_connection_status(self):
        st = tasks_backend.status()
        if st["connected"]:
            pending = f" · {st['pending']} pending" if st["pending"] else ""
            self.conn_row.set_title("Connected")
            self.conn_row.set_subtitle(f"Synced with Google Tasks{pending}")
        else:
            self.conn_row.set_title("Not connected")
            self.conn_row.set_subtitle(
                "Add your OAuth client above, then Connect Google"
            )

    # -- sync --

    def _schedule_sync(self):
        if self._sync_debounce_id is not None:
            GLib.source_remove(self._sync_debounce_id)
        self._sync_debounce_id = GLib.timeout_add_seconds(
            self.SYNC_DEBOUNCE_SECONDS, self._debounced_sync
        )

    def _debounced_sync(self):
        self._sync_debounce_id = None
        self._sync(manual=False)
        return False

    def _sync(self, manual):
        if not tasks_backend.is_connected():
            if manual:
                self._show_toast("Not connected to Google yet")
            return
        if self._syncing:
            return  # a sync is already in flight -- don't pile up overlapping ones
        self._syncing = True
        self.sync_btn.set_sensitive(False)

        def worker():
            try:
                result = tasks_backend.sync()
            except Exception as e:
                # Anything other than the OAuthError tasks_backend.sync()
                # already catches internally would otherwise skip
                # _sync_done entirely, leaving sync_btn permanently
                # disabled and _syncing stuck True -- always resolve.
                result = {"ok": False, "reason": str(e)}
            GLib.idle_add(self._sync_done, result, manual)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_done(self, result, manual):
        self._syncing = False
        self.sync_btn.set_sensitive(True)
        self._refresh_list_model()
        self._load_tasks()
        self._refresh_connection_status()
        if manual:
            self._show_toast(
                "Synced" if result.get("ok") else f"Sync failed: {result.get('reason')}"
            )
        return False

    # -- task list editing --

    def _refresh_list_model(self):
        # Preserve the user's selected list across the model swap below --
        # Gtk.StringList.set_model() resets Adw.ComboRow.selected to 0, so
        # every sync used to silently jump the visible list back to the
        # first one (and subsequent adds/toggles would then go to the wrong
        # list) even though nothing about the selection was meant to change.
        current_id = getattr(self, "_lists_cache", None) and self._current_list_id()
        self._lists_cache = tasks_backend.lists() or [
            {"id": tasks_backend.DEFAULT_LIST_ID, "title": "Tasks"}
        ]
        self._loading = True
        self.list_row.set_model(
            Gtk.StringList.new([l["title"] for l in self._lists_cache])
        )
        if current_id is not None:
            idx = next(
                (i for i, l in enumerate(self._lists_cache) if l["id"] == current_id), 0
            )
            self.list_row.set_selected(idx)
        self._loading = False

    def _on_list_selected(self, *_):
        if self._loading:
            return
        self._load_tasks()

    def _current_list_id(self):
        idx = self.list_row.get_selected()
        if 0 <= idx < len(self._lists_cache):
            return self._lists_cache[idx]["id"]
        return tasks_backend.DEFAULT_LIST_ID

    def _clear_task_rows(self):
        child = self.task_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.task_listbox.remove(child)
            child = nxt

    def _load_tasks(self):
        self._clear_task_rows()
        for task in tasks_backend.tasks_for_list(self._current_list_id()):
            self._add_task_row(task)

    def _add_task_row(self, task):
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        check = Gtk.CheckButton(
            valign=Gtk.Align.CENTER, active=task["status"] == "completed"
        )
        title_label = Gtk.Label(label=task["title"], xalign=0, hexpand=True, wrap=True)
        if task["status"] == "completed":
            title_label.add_css_class("dim-label")
        box.append(check)
        box.append(title_label)
        if task.get("due"):
            due_label = Gtk.Label(label=task["due"][:10], xalign=1)
            due_label.add_css_class("dim-label")
            box.append(due_label)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        box.append(del_btn)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        task_id = task["id"]
        check.connect(
            "toggled",
            lambda btn: self._on_task_toggled(task_id, btn.get_active(), title_label),
        )
        del_btn.connect("clicked", lambda *_: self._on_task_deleted(task_id, row))
        self.task_listbox.append(row)

    def _on_task_toggled(self, task_id, done, title_label):
        tasks_backend.set_done(task_id, done)
        if done:
            title_label.add_css_class("dim-label")
        else:
            title_label.remove_css_class("dim-label")
        self._schedule_sync()

    def _on_task_deleted(self, task_id, row):
        tasks_backend.delete_task(task_id)
        self.task_listbox.remove(row)
        self._schedule_sync()

    def _add_task(self, *_):
        title = self.new_task_entry.get_text().strip()
        if not title:
            return
        task = tasks_backend.add_task(self._current_list_id(), title)
        self.new_task_entry.set_text("")
        self._add_task_row(task)
        self._schedule_sync()


class FocusPage(Gtk.Box):
    """Focus mode: this page only ever writes *intent* via
    focus_backend.request()/write_profiles() -- the automation daemon's
    FocusController (automation.py) is what actually blocks sites/apps,
    swaps the wallpaper, silences notifications, and breathes the keyboard.
    That split means focus mode keeps working even with this window closed,
    same as power-profile RGB flashing already does."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)
        page = Adw.PreferencesPage()
        self.toast_overlay.set_child(page)
        self._loading = False
        self._status_refreshing = False

        # --- toggle ---
        toggle_group = Adw.PreferencesGroup(title="Focus Mode")
        page.add(toggle_group)

        self.profile_row = Adw.ComboRow(
            title="Profile", subtitle="Which blocked sites/apps to use"
        )
        self._refresh_profile_model()
        toggle_group.add(self.profile_row)

        adjustment = Gtk.Adjustment(value=25, lower=0, upper=480, step_increment=5)
        self.duration_row = Adw.SpinRow(
            title="Duration (minutes)", subtitle="0 = indefinite", adjustment=adjustment
        )
        toggle_group.add(self.duration_row)

        self.hard_lock_row = Adw.SwitchRow(
            title="Hard Lock",
            subtitle="Refuse to turn off again until the duration above elapses",
        )
        toggle_group.add(self.hard_lock_row)

        self.focus_switch_row = Adw.SwitchRow(
            title="Focus Mode",
            subtitle="Blocks sites/apps, swaps the wallpaper, silences notifications, breathes the keyboard blue",
        )
        self.focus_switch_row.connect("notify::active", self._on_focus_toggle)
        toggle_group.add(self.focus_switch_row)

        self.status_row = Adw.ActionRow(title="Not focused")
        toggle_group.add(self.status_row)

        # --- pomodoro ---
        pomo_group = Adw.PreferencesGroup(
            title="Pomodoro",
            description="If enabled, focus mode auto-ends after the configured work/break cycles",
        )
        page.add(pomo_group)
        self.pomo_enable_row = Adw.SwitchRow(title="Enable Pomodoro")
        pomo_group.add(self.pomo_enable_row)
        self.pomo_work_row = Adw.SpinRow(
            title="Work minutes",
            adjustment=Gtk.Adjustment(value=25, lower=1, upper=180, step_increment=1),
        )
        pomo_group.add(self.pomo_work_row)
        self.pomo_break_row = Adw.SpinRow(
            title="Break minutes",
            adjustment=Gtk.Adjustment(value=5, lower=1, upper=60, step_increment=1),
        )
        pomo_group.add(self.pomo_break_row)
        self.pomo_cycles_row = Adw.SpinRow(
            title="Cycles",
            adjustment=Gtk.Adjustment(value=4, lower=1, upper=12, step_increment=1),
        )
        pomo_group.add(self.pomo_cycles_row)

        # --- blocked sites (per profile) ---
        sites_group = Adw.PreferencesGroup(
            title="Blocked Sites",
            description="Domains blocked system-wide (via /etc/hosts) for the profile selected above",
        )
        page.add(sites_group)
        self.sites_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.sites_listbox.add_css_class("boxed-list")
        self.sites_listbox.set_margin_top(6)
        sites_group.add(self.sites_listbox)
        sites_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=8,
        )
        add_site_btn = Gtk.Button(label="Add Site")
        add_site_btn.connect("clicked", lambda *_: self._add_site_row(""))
        save_profile_btn = Gtk.Button(label="Save Profile")
        save_profile_btn.add_css_class("suggested-action")
        save_profile_btn.connect("clicked", self._save_profile)
        sites_btn_box.append(add_site_btn)
        sites_btn_box.append(save_profile_btn)
        sites_group.add(sites_btn_box)

        # --- blocked apps (per profile) ---
        apps_group = Adw.PreferencesGroup(
            title="Blocked Apps",
            description="Matched against each process's name/command line and terminated while active",
        )
        page.add(apps_group)
        self.apps_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.apps_listbox.add_css_class("boxed-list")
        self.apps_listbox.set_margin_top(6)
        apps_group.add(self.apps_listbox)
        apps_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=8,
        )
        add_app_btn = Gtk.Button(label="Add App")
        add_app_btn.connect("clicked", lambda *_: self._add_app_row(""))
        save_profile_btn2 = Gtk.Button(label="Save Profile")
        save_profile_btn2.add_css_class("suggested-action")
        save_profile_btn2.connect("clicked", self._save_profile)
        apps_btn_box.append(add_app_btn)
        apps_btn_box.append(save_profile_btn2)
        apps_group.add(apps_btn_box)

        self.profile_row.connect("notify::selected", self._on_profile_row_selected)

        # --- schedule ---
        schedule_group = Adw.PreferencesGroup(
            title="Schedule",
            description="Auto-enable focus during these windows (never auto-disables a running session)",
        )
        page.add(schedule_group)
        self.schedule_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.schedule_listbox.add_css_class("boxed-list")
        self.schedule_listbox.set_margin_top(6)
        schedule_group.add(self.schedule_listbox)
        schedule_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=8,
        )
        add_schedule_btn = Gtk.Button(label="Add Window")
        add_schedule_btn.connect("clicked", lambda *_: self._add_schedule_row({}))
        save_schedule_btn = Gtk.Button(label="Save Schedule")
        save_schedule_btn.add_css_class("suggested-action")
        save_schedule_btn.connect("clicked", self._save_schedule)
        schedule_btn_box.append(add_schedule_btn)
        schedule_btn_box.append(save_schedule_btn)
        schedule_group.add(schedule_btn_box)

        # _load_state() below may call profile_row.set_selected(), which
        # fires notify::selected -> _on_profile_row_selected -- guarded by
        # _loading so that doesn't itself call _load_profile_rows() a second
        # time back to back with this explicit call. Ordered after
        # _load_state() (not before, as it used to be) so the single call
        # here always reflects the profile _load_state() actually settled
        # on, whether or not that set_selected() changed anything (GTK only
        # fires notify::selected on an actual change, so relying on the
        # signal alone would leave the sites/apps lists empty whenever the
        # saved active_profile was already index 0).
        self._load_schedule_rows()
        self._load_state()
        self._load_profile_rows()

        self._status_timer_id = None
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    def _on_map(self, *_):
        # Only poll while this page is actually visible (mapped) -- not
        # while the settings window is hidden (it hides rather than closes
        # on close-request) or while a different view-stack page is showing.
        if self._status_timer_id is None:
            self._status_timer_id = GLib.timeout_add_seconds(2, self._status_tick)

    def _on_unmap(self, *_):
        if self._status_timer_id is not None:
            GLib.source_remove(self._status_timer_id)
            self._status_timer_id = None

    def _on_profile_row_selected(self, *_):
        if self._loading:
            return
        self._load_profile_rows()

    def _show_toast(self, title):
        toast = Adw.Toast(title=title)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)

    def _listbox_rows(self, listbox):
        rows = []
        child = listbox.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _clear_listbox(self, listbox):
        for row in self._listbox_rows(listbox):
            listbox.remove(row)

    # -- state --

    def _load_state(self):
        state = focus_backend.read_state()
        self._loading = True
        names = list(self._profiles_cache.keys())
        if state.get("active_profile") in names:
            self.profile_row.set_selected(names.index(state["active_profile"]))
        pomo = state.get("pomodoro", {})
        self.pomo_enable_row.set_active(pomo.get("enabled", False))
        self.pomo_work_row.set_value(pomo.get("work_min", 25))
        self.pomo_break_row.set_value(pomo.get("break_min", 5))
        self.pomo_cycles_row.set_value(pomo.get("cycles", 4))
        self.focus_switch_row.set_active(state["active"])
        self._loading = False
        self._apply_status(state)

    def _status_tick(self):
        # read_state() + (when not active) stats_summary() are both JSON
        # file reads; gathering them on a worker thread keeps this 2s poll
        # from doing I/O on the GTK main thread, same pattern FanPage._refresh
        # already uses for its own poll. Skips this tick entirely if the
        # previous gather is still in flight.
        if self._status_refreshing:
            return True
        self._status_refreshing = True
        threading.Thread(target=self._gather_focus_status, daemon=True).start()
        return True

    def _gather_focus_status(self):
        state = focus_backend.read_state()
        today = streak_days = None
        if not state["active"]:
            today, streak_days = focus_backend.stats_summary()
        GLib.idle_add(self._apply_status_from_thread, state, today, streak_days)

    def _apply_status_from_thread(self, state, today, streak_days):
        self._apply_status(state, today, streak_days)
        self._status_refreshing = False
        return False

    def _apply_status(self, state, today=None, streak_days=None):
        locked = focus_backend.is_locked(state)
        self.focus_switch_row.set_sensitive(not locked)
        if state["active"]:
            remaining = ""
            if state.get("duration_s") and state.get("started_at"):
                left = max(
                    0, int(state["started_at"] + state["duration_s"] - time.time())
                )
                remaining = f" · {left // 60}m{left % 60:02d}s left"
                if locked:
                    remaining += " (locked)"
            self.status_row.set_title("Focused")
            self.status_row.set_subtitle(
                f"{state.get('active_profile', '')}{remaining}"
            )
        else:
            if today is None:
                today, streak_days = focus_backend.stats_summary()
            self.status_row.set_title("Not focused")
            self.status_row.set_subtitle(
                f"Today: {today // 3600}h{(today % 3600) // 60}m · {streak_days}-day streak"
            )
        # Reflect changes made elsewhere (tray, CLI, the daemon's scheduler)
        # without fighting the user's own in-flight click.
        if not self._loading and self.focus_switch_row.get_active() != state["active"]:
            self._loading = True
            self.focus_switch_row.set_active(state["active"])
            self._loading = False

    def _on_focus_toggle(self, row, *_):
        if self._loading:
            return
        active = row.get_active()
        duration_min = self.duration_row.get_value()
        try:
            focus_backend.request(
                active,
                profile=self._current_profile_name(),
                duration_s=duration_min * 60 if duration_min > 0 else None,
                hard_lock=self.hard_lock_row.get_active(),
                pomodoro=self._pomodoro_config() if active else None,
            )
        except focus_backend.HardLockError as e:
            self._loading = True
            row.set_active(True)
            self._loading = False
            self._show_toast(str(e))
            return
        self._show_toast("Focus mode on" if active else "Focus mode off")

    def _pomodoro_config(self):
        return {
            "enabled": self.pomo_enable_row.get_active(),
            "work_min": int(self.pomo_work_row.get_value()),
            "break_min": int(self.pomo_break_row.get_value()),
            "cycles": int(self.pomo_cycles_row.get_value()),
        }

    # -- profiles (blocked sites/apps) --

    def _refresh_profile_model(self):
        self._profiles_cache = focus_backend.read_profiles()
        self.profile_row.set_model(
            Gtk.StringList.new(list(self._profiles_cache.keys()))
        )

    def _current_profile_name(self):
        names = list(self._profiles_cache.keys())
        idx = self.profile_row.get_selected()
        return names[idx] if 0 <= idx < len(names) else None

    def _add_editable_row(self, listbox, text):
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        entry = Gtk.Entry(text=text, hexpand=True, valign=Gtk.Align.CENTER)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        box.append(entry)
        box.append(del_btn)
        row = Gtk.ListBoxRow()
        row.set_child(box)
        row.entry = entry
        del_btn.connect("clicked", lambda *_: listbox.remove(row))
        listbox.append(row)

    def _add_site_row(self, text):
        self._add_editable_row(self.sites_listbox, text)

    def _add_app_row(self, text):
        self._add_editable_row(self.apps_listbox, text)

    def _load_profile_rows(self):
        self._clear_listbox(self.sites_listbox)
        self._clear_listbox(self.apps_listbox)
        profile = self._profiles_cache.get(self._current_profile_name(), {})
        for s in profile.get("sites", []):
            self._add_site_row(s)
        for a in profile.get("apps", []):
            self._add_app_row(a)

    def _save_profile(self, *_):
        name = self._current_profile_name()
        if not name:
            return
        sites = [
            r.entry.get_text().strip()
            for r in self._listbox_rows(self.sites_listbox)
            if r.entry.get_text().strip()
        ]
        apps = [
            r.entry.get_text().strip()
            for r in self._listbox_rows(self.apps_listbox)
            if r.entry.get_text().strip()
        ]
        profiles = focus_backend.read_profiles()
        profiles[name] = {"sites": sites, "apps": apps}
        focus_backend.write_profiles(profiles)
        # _load_profile_rows() reads from this cache, not from disk -- without
        # updating it here too, edit -> Save -> switch profile away and back
        # would redisplay the stale pre-edit list, as if the save had been
        # lost (it wasn't; only the in-memory cache was out of date).
        self._profiles_cache[name] = profiles[name]
        self._show_toast(f"Saved “{name}”")

    # -- schedule --

    def _add_schedule_row(self, entry):
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        days_str = ",".join(
            _WEEKDAY_NAMES[d] for d in entry.get("days", []) if 0 <= d < 7
        )
        days_entry = Gtk.Entry(
            text=days_str,
            placeholder_text="mon,tue,wed",
            width_chars=16,
            valign=Gtk.Align.CENTER,
        )
        start_entry = Gtk.Entry(
            text=entry.get("start", ""),
            placeholder_text="09:00",
            width_chars=6,
            valign=Gtk.Align.CENTER,
        )
        end_entry = Gtk.Entry(
            text=entry.get("end", ""),
            placeholder_text="12:00",
            width_chars=6,
            valign=Gtk.Align.CENTER,
        )
        profile_entry = Gtk.Entry(
            text=entry.get("profile", ""),
            placeholder_text="profile name",
            hexpand=True,
            valign=Gtk.Align.CENTER,
        )
        del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        del_btn.add_css_class("flat")
        for w in (
            days_entry,
            Gtk.Label(label="from"),
            start_entry,
            Gtk.Label(label="to"),
            end_entry,
            profile_entry,
            del_btn,
        ):
            box.append(w)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        row.days_entry, row.start_entry, row.end_entry, row.profile_entry = (
            days_entry,
            start_entry,
            end_entry,
            profile_entry,
        )
        del_btn.connect("clicked", lambda *_: self.schedule_listbox.remove(row))
        self.schedule_listbox.append(row)

    def _load_schedule_rows(self):
        self._clear_listbox(self.schedule_listbox)
        for entry in focus_backend.read_state().get("schedule", []):
            self._add_schedule_row(entry)

    def _save_schedule(self, *_):
        schedule = []
        for row in self._listbox_rows(self.schedule_listbox):
            # Strip *before* checking membership and indexing -- the guard
            # used to check the stripped token but index the raw one, so
            # e.g. "mon, tue" (a space after the comma) passed the " tue"
            # guard check against the stripped value yet then raised
            # ValueError from _WEEKDAY_NAMES.index(" tue") out of this click
            # handler, silently aborting the whole save.
            stripped_days = [
                d.strip() for d in row.days_entry.get_text().lower().split(",")
            ]
            days = [
                _WEEKDAY_NAMES.index(d) for d in stripped_days if d in _WEEKDAY_NAMES
            ]
            start, end = (
                row.start_entry.get_text().strip(),
                row.end_entry.get_text().strip(),
            )
            profile = row.profile_entry.get_text().strip() or None
            if days and start and end:
                schedule.append(
                    {"days": days, "start": start, "end": end, "profile": profile}
                )
        focus_backend.update_state(schedule=schedule)
        self._show_toast("Schedule saved")


def _hex_to_rgba(hexval):
    from gi.repository import Gdk

    rgba = Gdk.RGBA()
    rgba.parse(f"#{hexval.lstrip('#')}")
    return rgba


def _rgba_to_hex(rgba):
    return "".join(
        f"{int(round(c * 255)):02x}" for c in (rgba.red, rgba.green, rgba.blue)
    )


class HyprUtilWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app, title="hypr-util", default_width=560, default_height=640
        )

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Stored on self (not just a local) so the app-level "open-page"
        # action (see HyprUtilApp) can select a page on an already-running,
        # D-Bus-activated instance -- e.g. the tray's "Open Todo".
        self.view_stack = Adw.ViewStack()
        self.view_stack.add_titled_with_icon(
            FanPage(), "fan", "Fan", "temperature-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            RgbPage(), "rgb", "RGB", "input-keyboard-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            TodoPage(), "todo", "Todo", "checkbox-checked-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            FocusPage(), "focus", "Focus", "night-light-symbolic"
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
        # remote "Activate" to the primary and drop argv entirely -- so the
        # tray's `bin/hyprutil app --page todo` Popen fallback (used only
        # when the D-Bus service file isn't installed) would lose the
        # `--page` request whenever a primary instance already happened to
        # be running, silently opening the app to whatever page it was last
        # on instead of Todo. This flag routes every invocation (primary or
        # remote) through do_command_line below instead, which still sees
        # the full argv either way.
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
        # intent. See ui/tray.py's open_todo().
        open_page_action = Gio.SimpleAction.new("open-page", GLib.VariantType.new("s"))
        open_page_action.connect("activate", self._on_open_page)
        self.add_action(open_page_action)

    def _window(self):
        # Tracked explicitly rather than derived from self.props.active_window:
        # HyprUtilWindow hides (rather than destroys) on close so the process
        # stays resident, but a hidden window isn't guaranteed to still be
        # GTK's notion of "active" -- if it isn't, this would build a *second*
        # HyprUtilWindow, re-running all four page constructors and starting
        # a second set of 2s pollers while the first (hidden) window's keep
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
    focus_backend.ensure_defaults()
    tasks_backend.ensure_defaults()

    app = HyprUtilApp()
    app.run(argv)


if __name__ == "__main__":
    main()
