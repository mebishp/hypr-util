"""hypr-util settings app: fan curves + keyboard RGB, in one window."""
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .. import fan as backend
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
        self.profile_model = Gtk.StringList.new([backend.PROFILE_LABELS[p] for p in backend.PROFILES])
        self.profile_row.set_model(self.profile_model)
        self._profile_signal_id = self.profile_row.connect("notify::selected", self._on_profile_changed)
        profile_group.add(self.profile_row)

        # --- Fan curve editor ---
        curve_group = Adw.PreferencesGroup(title="Fan Curve")
        page.add(curve_group)
        self.curve_profile_row = Adw.ComboRow(title="Editing Curve For")
        self.curve_profile_row.set_model(Gtk.StringList.new([backend.PROFILE_LABELS[p] for p in backend.PROFILES]))
        self.curve_profile_row.connect("notify::selected", lambda *_: self._load_curve())
        curve_group.add(self.curve_profile_row)

        self.curve_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.curve_listbox.add_css_class("boxed-list")
        self.curve_listbox.set_margin_top(6)
        curve_group.add(self.curve_listbox)

        curve_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
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
        self.override_switch_row = Adw.SwitchRow(title="Override Curve", subtitle="Force a fixed fan speed")
        self.override_switch_row.connect("notify::active", self._on_override_toggle)
        override_group.add(self.override_switch_row)

        adjustment = Gtk.Adjustment(value=128, lower=0, upper=255, step_increment=1, page_increment=10)
        self.override_spin_row = Adw.SpinRow(title="PWM Value (0-255)", adjustment=adjustment)
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
        self._refresh()
        self._load_curve()
        GLib.timeout_add(2000, self._refresh_tick)

    # -- status / profile --
    def _refresh_tick(self):
        self._refresh()
        return True

    def _refresh(self):
        s = backend.read_status()
        active = backend.service_active()
        override = backend.read_override()
        profile = backend.current_power_profile()
        rpm = max(s["fan1"] or 0, s["fan2"] or 0)
        temp_str = f"{s['temp']:.1f}°C" if s["temp"] is not None else "?°C"
        mode = f"override {override}" if override != "auto" else f"{backend.PROFILE_LABELS.get(profile, profile)} curve"
        self.status_row.set_title(f"{temp_str}  •  PWM {s['pwm']}  •  {rpm} RPM")
        self.status_row.set_subtitle(f"daemon {'running' if active else 'stopped'} ({mode})")
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
            ["journalctl", "-u", backend.SERVICE, "-n", "100", "--no-pager", "-o", "cat"],
            capture_output=True, text=True,
        )
        win = Adw.Window(title="hypr-util — Daemon Log", default_width=600, default_height=400)
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
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
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
            points.append((int(child.temp_spin.get_value()), int(child.pwm_spin.get_value())))
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

        self.effect_row = Adw.ComboRow(title="Effect")
        self.effect_row.set_model(Gtk.StringList.new([e.replace("_", " ").title() for e in rgb_backend.EFFECTS]))
        editor_group.add(self.effect_row)

        self.multi_switch_row = Adw.SwitchRow(
            title="Multiple Colors",
            subtitle="Cycle through 7 colors instead of using just one",
        )
        self.multi_switch_row.connect("notify::active", self._on_multi_toggle)
        editor_group.add(self.multi_switch_row)

        self.single_color_row = Adw.ActionRow(title="Color")
        self.single_color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER)
        self.single_color_btn.set_rgba(_hex_to_rgba(rgb_backend.DEFAULT_COLORS[0]))
        self.single_color_row.add_suffix(self.single_color_btn)
        editor_group.add(self.single_color_row)

        self.multi_color_row = Adw.ActionRow(title="Colors")
        multi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.color_buttons = []
        for hexval in rgb_backend.DEFAULT_COLORS:
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            btn.set_rgba(_hex_to_rgba(hexval))
            multi_box.append(btn)
            self.color_buttons.append(btn)
        self.multi_color_row.set_child(multi_box)
        editor_group.add(self.multi_color_row)
        self.multi_color_row.set_visible(False)

        apply_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END, margin_top=4)
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
        self.preset_name_row.set_text(rgb_backend.read_preset(rgb_backend.PRESET_SLOTS[0])["name"])
        preset_group.add(self.preset_name_row)
        self.preset_row.connect("notify::selected", self._on_preset_selected)

        preset_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END, margin_top=4)
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
            warn.add(Adw.ActionRow(
                title="firefly-ctl not found",
                subtitle=str(rgb_backend.CTL_BIN),
            ))
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
            self.kb_status_row.set_subtitle("Not connected -- plug it in to apply lighting")
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
        effect = rgb_backend.EFFECTS[self.effect_row.get_selected()]
        color_idx = 7 if self.multi_switch_row.get_active() else 0
        colors = self._current_colors()

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
        self.apply_btn.set_sensitive(rgb_backend.is_connected() and rgb_backend.available())
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


def _hex_to_rgba(hexval):
    from gi.repository import Gdk
    rgba = Gdk.RGBA()
    rgba.parse(f"#{hexval.lstrip('#')}")
    return rgba


def _rgba_to_hex(rgba):
    return "".join(f"{int(round(c * 255)):02x}" for c in (rgba.red, rgba.green, rgba.blue))


class HyprUtilWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="hypr-util", default_width=560, default_height=640)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        view_stack = Adw.ViewStack()
        view_stack.add_titled_with_icon(FanPage(), "fan", "Fan", "temperature-symbolic")
        view_stack.add_titled_with_icon(RgbPage(), "rgb", "RGB", "input-keyboard-symbolic")

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(view_stack)


class HyprUtilApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.hyprnon.hyprutil")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = HyprUtilWindow(self)
        win.present()


def main():
    backend.ensure_config_defaults()
    rgb_backend.ensure_defaults()

    app = HyprUtilApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
