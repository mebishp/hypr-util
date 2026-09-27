"""hypr-util settings app: fan curves and keyboard RGB, in one window."""

import math
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import fan as backend
from .. import kbd as kbd_backend

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


def _rounded_rect(cr, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


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
    honest limits. There are four zones and no more, and the firmware runs
    no animations of its own -- so an effect is frames, written here while
    the page is open and by the effects service once it is closed.

    Reaching the hardware is a sysfs write, handed to this user by the
    hyprkbd driver's udev rule. When the driver is missing, or the board is
    a per-key one the firmware will not drive, the page says which and says
    what to run, rather than showing controls that do nothing.
    """

    APPLY_DEBOUNCE_MS = 200
    # The keyboard's own frame rate while something is moving. When nothing
    # is, the preview only has to be quick enough to notice a status flash
    # starting, and a quarter of the work is a quarter of the work.
    PREVIEW_INTERVAL_MS = 120
    PREVIEW_IDLE_MS = 500

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay = Adw.ToastOverlay()
        self.append(self.toast_overlay)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(content)

        self.banner = Adw.Banner(title="Keyboard lighting not available")
        content.append(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        page = Adw.PreferencesPage()
        scroller.set_child(page)
        content.append(scroller)

        self._look = kbd_backend.read_current()
        self._settings = kbd_backend.read_settings()
        self._alert = None
        self._preview_interval = self.PREVIEW_INTERVAL_MS
        self._loading_settings = False
        self._zones = 4
        self._loading = False
        self._apply_timer_id = None
        self._preview_timer_id = None
        self._phase = 0.0
        self._busy = False

        preview_group = Adw.PreferencesGroup(
            description="Click a section to change its colour"
        )
        page.add(preview_group)
        self.preview = _ZonePreview()
        self.preview.connect("zone-activated", self._on_preview_zone)
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

        self.zones_group = Adw.PreferencesGroup(
            title="Zones",
            description="Or pick one here -- the same four colours either way",
        )
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

        self._build_presets(page)
        self._build_indicators(page)
        self._build_saver(page)

        self._load_look_into_controls()
        self._load_settings_into_controls()
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)


    # -- presets --

    def _build_presets(self, page):
        """Four slots, each an editable name with Apply and Save beside it.

        An entry row rather than a plain one so renaming a preset is where
        you would look for it -- in the name -- instead of behind a menu.
        """
        group = Adw.PreferencesGroup(
            title="Presets",
            description="Save a whole look -- effect, colours, brightness and speed",
        )
        page.add(group)
        self._preset_rows = {}
        for slot in kbd_backend.presets.PRESET_SLOTS:
            stored = kbd_backend.presets.read(slot)
            row = Adw.EntryRow(title=f"Slot {slot}")
            row.set_text(stored["name"])
            row.connect("apply", self._on_preset_renamed, slot)
            row.set_show_apply_button(True)

            apply_btn = Gtk.Button(
                icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Put this preset on the keyboard",
            )
            apply_btn.add_css_class("flat")
            apply_btn.connect("clicked", self._on_preset_apply, slot)
            row.add_suffix(apply_btn)

            save_btn = Gtk.Button(
                icon_name="document-save-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Save the current look into this slot",
            )
            save_btn.add_css_class("flat")
            save_btn.connect("clicked", self._on_preset_save, slot)
            row.add_suffix(save_btn)

            group.add(row)
            self._preset_rows[slot] = row
        self.presets_group = group

    def _on_preset_apply(self, _button, slot):
        self._busy = True
        threading.Thread(
            target=self._preset_apply_worker, args=(slot,), daemon=True
        ).start()

    def _preset_apply_worker(self, slot):
        error = None
        try:
            kbd_backend.apply_preset(slot)
        except Exception as e:
            error = str(e)
        GLib.idle_add(self._after_preset_apply, slot, error)

    def _after_preset_apply(self, slot, error):
        self._busy = False
        if error:
            self._show_toast(error)
            return False
        # Adopt what was just applied, so the controls and the preview show
        # the preset rather than the look it replaced.
        self._look = kbd_backend.read_current()
        self._phase = 0.0
        self._load_look_into_controls()
        self._alert = self.preview.set_look(
            self._look, self._zones, self._phase, self._settings
        )
        self._show_toast(f"Applied {kbd_backend.presets.read(slot)['name']}")
        return False

    def _on_preset_save(self, _button, slot):
        name = self._preset_rows[slot].get_text().strip() or None
        try:
            stored = kbd_backend.save_preset(slot, self._look, name=name)
        except Exception as e:
            self._show_toast(str(e))
            return
        self._preset_rows[slot].set_text(stored["name"])
        self._show_toast(f"Saved into {stored['name']}")

    def _on_preset_renamed(self, row, slot):
        name = row.get_text().strip()
        if not name:
            row.set_text(kbd_backend.presets.read(slot)["name"])
            return
        kbd_backend.presets.rename(slot, name)

    # -- status flashes --

    def _make_zone_combo(self, title, subtitle, handler):
        row = Adw.ComboRow(title=title, subtitle=subtitle)
        row.set_model(Gtk.StringList.new(
            [kbd_backend.ZONE_NAMES[z] for z in kbd_backend.DISPLAY_ORDER]
        ))
        row.connect("notify::selected", handler)
        return row

    def _zone_from_combo(self, row):
        return kbd_backend.DISPLAY_ORDER[row.get_selected()]

    def _combo_index_for_zone(self, zone):
        try:
            return kbd_backend.DISPLAY_ORDER.index(zone)
        except ValueError:
            return 0

    def _build_indicators(self, page):
        group = Adw.PreferencesGroup(
            title="Status flashes",
            description=(
                "The whole keyboard takes a colour for a moment when the "
                "machine changes, and then the effect you were running comes "
                "back exactly where it left off."
            ),
        )
        page.add(group)

        self.ind_enabled_row = Adw.SwitchRow(title="Flash on changes")
        self.ind_enabled_row.connect("notify::active", self._on_indicator_toggle)
        group.add(self.ind_enabled_row)

        self.ind_when_off_row = Adw.SwitchRow(
            title="Flash with the lighting off",
            subtitle="The backlight comes on for the flash and goes back off after it",
        )
        self.ind_when_off_row.connect("notify::active", self._on_indicator_toggle)
        group.add(self.ind_when_off_row)

        self.ind_duration_row = Adw.SpinRow.new_with_range(
            kbd_backend.indicators.MIN_DURATION,
            kbd_backend.indicators.MAX_DURATION, 0.5,
        )
        self.ind_duration_row.set_title("Hold for")
        self.ind_duration_row.set_subtitle("seconds")
        self.ind_duration_row.set_digits(1)
        self.ind_duration_row.connect("notify::value", self._on_indicator_toggle)
        group.add(self.ind_duration_row)

        self.ind_profile_row = Adw.SwitchRow(
            title="Power profile",
            subtitle="Green saver, yellow balanced, red performance",
        )
        self.ind_profile_row.connect("notify::active", self._on_indicator_toggle)
        group.add(self.ind_profile_row)

        self.ind_battery_row = Adw.SwitchRow(
            title="Battery",
            subtitle="Amber on falling below low, blinking red below critical",
        )
        self.ind_battery_row.connect("notify::active", self._on_indicator_toggle)
        group.add(self.ind_battery_row)

        self.ind_low_row = Adw.SpinRow.new_with_range(1, 100, 1)
        self.ind_low_row.set_title("Low battery at")
        self.ind_low_row.set_subtitle("percent")
        self.ind_low_row.connect("notify::value", self._on_indicator_toggle)
        group.add(self.ind_low_row)

        self.ind_critical_row = Adw.SpinRow.new_with_range(1, 100, 1)
        self.ind_critical_row.set_title("Critical at")
        self.ind_critical_row.set_subtitle("percent")
        self.ind_critical_row.connect("notify::value", self._on_indicator_toggle)
        group.add(self.ind_critical_row)

        self.ind_charging_row = Adw.SwitchRow(
            title="Charger",
            subtitle="Green going in, amber coming out",
        )
        self.ind_charging_row.connect("notify::active", self._on_indicator_toggle)
        group.add(self.ind_charging_row)

        self.ind_status_row = Adw.ActionRow(
            title="Showing now", subtitle="nothing"
        )
        group.add(self.ind_status_row)
        self.indicators_group = group

    def _on_indicator_toggle(self, *_):
        if self._loading_settings:
            return
        low = int(self.ind_low_row.get_value())
        critical = int(self.ind_critical_row.get_value())
        self._push_settings({"indicators": {
            "enabled": self.ind_enabled_row.get_active(),
            "when_off": self.ind_when_off_row.get_active(),
            "duration": round(self.ind_duration_row.get_value(), 1),
            "profile": {"enabled": self.ind_profile_row.get_active()},
            "battery": {
                "enabled": self.ind_battery_row.get_active(),
                "low": low,
                "critical": critical,
                "show_charging": self.ind_charging_row.get_active(),
            },
        }})

    # -- battery saver --

    def _build_saver(self, page):
        group = Adw.PreferencesGroup(
            title="Battery saver",
            description=(
                "Four lit zones are a real draw. On battery the colours are "
                "scaled back without touching the look you chose -- plug in "
                "and it returns exactly as it was."
            ),
        )
        page.add(group)

        self.saver_row = Adw.SwitchRow(title="Dim on battery")
        self.saver_row.connect("notify::active", self._on_saver_changed)
        group.add(self.saver_row)

        self.saver_brightness_row, self.saver_brightness_scale = self._make_slider(
            "Brightness cap", 0, kbd_backend.BRIGHTNESS_MAX, self._on_saver_changed
        )
        group.add(self.saver_brightness_row)

        self.saver_static_row = Adw.SwitchRow(
            title="Stop animations on battery",
            subtitle="The cost of an effect is the writes, not the brightness",
        )
        self.saver_static_row.connect("notify::active", self._on_saver_changed)
        group.add(self.saver_static_row)
        self.saver_group = group

    def _on_saver_changed(self, *_):
        if self._loading_settings:
            return
        self._push_settings({"battery_saver": {
            "enabled": self.saver_row.get_active(),
            "brightness": int(self.saver_brightness_scale.get_value()),
            "static_only": self.saver_static_row.get_active(),
        }})

    # -- settings plumbing --

    def _push_settings(self, patch):
        """Save a settings change and put it on the keyboard.

        Applied to the local copy first so the preview updates on the same
        frame, rather than a beat later when the worker comes back.
        """
        def merge(into, changes):
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(into.get(key), dict):
                    merge(into[key], value)
                else:
                    into[key] = value

        merge(self._settings, patch)
        self.preview.set_look(self._look, self._zones, self._phase, self._settings)
        threading.Thread(
            target=self._settings_worker, args=(patch,), daemon=True
        ).start()

    def _settings_worker(self, patch):
        try:
            saved = kbd_backend.update_settings(patch)
        except Exception as e:
            GLib.idle_add(self._show_toast, str(e))
            return
        GLib.idle_add(self._adopt_settings, saved)

    def _adopt_settings(self, saved):
        # Adopted back from the file because normalize() clamps -- a critical
        # threshold typed above the low one comes back corrected, and the
        # spin button should show the corrected number.
        self._settings = saved
        self._load_settings_into_controls()
        return False

    def _load_settings_into_controls(self):
        config = self._settings
        self._loading_settings = True
        indicators = config["indicators"]
        self.ind_enabled_row.set_active(indicators["enabled"])
        self.ind_when_off_row.set_active(indicators["when_off"])
        self.ind_duration_row.set_value(indicators["duration"])
        self.ind_profile_row.set_active(indicators["profile"]["enabled"])
        battery = indicators["battery"]
        self.ind_battery_row.set_active(battery["enabled"])
        self.ind_low_row.set_value(battery["low"])
        self.ind_critical_row.set_value(battery["critical"])
        self.ind_charging_row.set_active(battery["show_charging"])

        saver = config["battery_saver"]
        self.saver_row.set_active(saver["enabled"])
        self.saver_brightness_scale.set_value(saver["brightness"])
        self.saver_static_row.set_active(saver["static_only"])

        self._loading_settings = False
        self._sync_settings_sensitivity()

    def _sync_settings_sensitivity(self):
        """Grey out what a switched-off section cannot use."""
        indicators = self._settings["indicators"]
        on = indicators["enabled"]
        for widget in (self.ind_when_off_row, self.ind_duration_row,
                       self.ind_profile_row, self.ind_battery_row,
                       self.ind_charging_row, self.ind_status_row):
            widget.set_sensitive(on)
        battery_on = on and indicators["battery"]["enabled"]
        for widget in (self.ind_low_row, self.ind_critical_row):
            widget.set_sensitive(battery_on)

        saver_on = self._settings["battery_saver"]["enabled"]
        self.saver_brightness_row.set_sensitive(saver_on)
        self.saver_static_row.set_sensitive(saver_on)

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
            self._preview_interval = self.PREVIEW_INTERVAL_MS
            self._preview_timer_id = GLib.timeout_add(
                self._preview_interval, self._preview_tick
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
            self.banner.set_title(reply.get("error") or "Keyboard lighting unavailable")
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

        if reply.get("settings"):
            self._settings = reply["settings"]
        if reply.get("saving"):
            self.saver_row.set_subtitle("Active now -- running on battery")
        else:
            self.saver_row.set_subtitle("")

        if usable:
            self._zones = int(keyboard.get("zones") or 4)
            look = reply.get("look")
            if look and not self._busy:
                self._look = kbd_backend.normalize_look(look)
                self._load_look_into_controls()
        return False

    def set_sensitive_controls(self, sensitive):
        for widget in (self.on_row, self.effect_row, self.brightness_row,
                       self.speed_row, self.zones_group, self.presets_group,
                       self.indicators_group, self.saver_group):
            widget.set_sensitive(sensitive)
        if sensitive:
            # Re-apply the finer rules the blanket enable above just undid.
            self._sync_settings_sensitivity()

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
        self._sync_effect_subtitle()

    def _sync_effect_subtitle(self):
        """Say what the chosen effect does, under the picker.

        Ten effects is more than a name can carry on its own -- "sweep" and
        "pulse" mean nothing until you have seen them.
        """
        self.effect_row.set_subtitle(
            kbd_backend.EFFECT_DESCRIPTIONS.get(self._look["effect"], "")
        )

    def _sync_control_visibility(self):
        """Show only what this look can use."""
        effect = self._look["effect"]
        self.speed_row.set_visible(effect in kbd_backend.ANIMATED)
        # Several effects paint their own hues and never read the zone
        # colours, so the pickers would be lying if they stayed live.
        picks_colors = effect not in kbd_backend.COLOURLESS_EFFECTS
        self.zones_group.set_visible(picks_colors)
        self.preview.set_clickable(picks_colors)

        # The effects that run as one picture across the board fold WASD
        # into the left zone, so its own picker has nothing to set. Greyed
        # and labelled rather than hidden: a row that vanishes when you
        # change effect reads as a bug, and the reason is worth saying.
        merged = kbd_backend.merges_wasd(effect)
        for zone, (row, _button) in self._zone_rows.items():
            row.set_visible(zone < self._zones or self._zones == 1)
            if zone == kbd_backend.ZONE_WASD:
                row.set_sensitive(not merged)
                row.set_subtitle(
                    "Runs with the left zone for this effect" if merged else ""
                )

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
        self._sync_effect_subtitle()
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

    def _on_preview_zone(self, _preview, zone):
        """A band in the preview was clicked: pick that zone's colour.

        Pointing at the part of the keyboard you mean is the whole of the
        interaction -- no scrolling to a list and working out which of four
        names the patch under your left hand answers to.
        """
        if self._look["effect"] in kbd_backend.COLOURLESS_EFFECTS:
            self._show_toast(
                f"{kbd_backend.EFFECT_LABELS[self._look['effect']]} paints its "
                "own colours"
            )
            return
        if zone == kbd_backend.ZONE_WASD and kbd_backend.merges_wasd(self._look["effect"]):
            # The cluster is not drawn for these effects, but a click landing
            # on where it would be should still do the obvious thing.
            zone = kbd_backend.ZONE_LEFT
        dialog = Gtk.ColorDialog(
            with_alpha=False, title=f"{kbd_backend.ZONE_NAMES[zone]} zone"
        )
        dialog.choose_rgba(
            self.get_root(), _hex_to_rgba(self._look["colors"][zone]), None,
            self._on_preview_zone_picked, zone,
        )

    def _on_preview_zone_picked(self, dialog, result, zone):
        try:
            rgba = dialog.choose_rgba_finish(result)
        except GLib.Error:
            return      # dismissed, which is not a failure worth a toast
        if rgba is None:
            return
        hexval = _rgba_to_hex(rgba)
        if self._look["colors"][zone] == hexval:
            return
        self._look["colors"][zone] = hexval
        self._look["on"] = True
        # Through the loader so the zone's own picker button follows the
        # click -- the two are the same setting and must not disagree.
        self._load_look_into_controls()
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
        self._alert = self.preview.set_look(
            self._look, self._zones, self._phase, self._settings
        )
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
        """Run the drawn keyboard from the same frame function the service
        writes to the hardware, so the preview is the effect rather than an
        impression of it.

        The tick also polls for status flashes, so it cannot simply stop
        when the look is static -- but it can slow right down, and it does
        while there is nothing moving to draw.
        """
        # The phase is deliberately not advanced under an alert, matching
        # the service: what comes back afterwards is the frame the effect
        # was on, not the one it would have reached.
        animated = self._look["on"] and self._look["effect"] in kbd_backend.ANIMATED
        if animated and self._alert is None:
            self._phase += kbd_backend.zones.PHASE_STEP * kbd_backend.zones.SPEED_FACTORS[
                self._look["speed"]
            ]
        self._alert = self.preview.set_look(
            self._look, self._zones, self._phase, self._settings
        )
        label = self._alert.label if self._alert is not None else "nothing"
        if label != self.ind_status_row.get_subtitle():
            self.ind_status_row.set_subtitle(label)

        wanted = (self.PREVIEW_INTERVAL_MS if animated or self._alert is not None
                  else self.PREVIEW_IDLE_MS)
        if wanted != self._preview_interval:
            self._preview_interval = wanted
            self._preview_timer_id = GLib.timeout_add(wanted, self._preview_tick)
            return False
        return True


class _ZonePreview(Gtk.DrawingArea):
    """The keyboard as it is lit: three bands across, with the WASD cluster
    picked out over the left one -- except for the effects that run as one
    picture across the board, where WASD belongs to the left band and drawing
    it separately would be drawing a seam that is not there.

    Clicking a band emits `zone-activated` with the zone it stands for, which
    is how colours are changed: pointing at the part of the keyboard you mean
    beats scrolling to a list and working out which name it has.
    """

    __gtype_name__ = "HyprUtilZonePreview"
    __gsignals__ = {
        "zone-activated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    PAD = 6

    def __init__(self, height=96):
        super().__init__()
        self._colors = []
        self._on = True
        self._merged = False
        self._alert = None
        self._clickable = True
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        click = Gtk.GestureClick()
        click.connect("released", self._on_released)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        self._pointer_over = False
        self.set_tooltip_text("Click a section to change its colour")

    def set_look(self, look, zones, phase, config=None):
        """Draw the frame the keyboard would be showing. Returns the alert.

        The composed frame, not the raw look: the preview has to show an
        alert and the battery saver too, or it quietly disagrees with the
        keyboard sitting under the screen.
        """
        colors, lit, alert = kbd_backend.preview(look, zones, phase, config)
        merged = kbd_backend.merges_wasd(look["effect"])
        if (colors == self._colors and lit == self._on
                and merged == self._merged and (alert is None) == (self._alert is None)):
            # Eight times a second, most of them identical: a static look
            # only changes when someone moves a control, and cairo does not
            # need telling to repaint the same pixels.
            return alert
        self._colors, self._on = colors, lit
        self._merged, self._alert = merged, alert
        self.queue_draw()
        return alert

    def set_clickable(self, clickable):
        """Whether clicking a band means anything for the current effect."""
        self._clickable = clickable
        self.set_tooltip_text(
            "Click a section to change its colour" if clickable
            else "This effect paints its own colours"
        )

    def _zone_color(self, zone):
        if zone < len(self._colors):
            return self._colors[zone]
        return self._colors[0] if self._colors else (0, 0, 0)

    # -- geometry, shared by the drawing and the hit test --

    def _bands(self):
        if len(self._colors) == 1:
            return [0, 0, 0]
        return [kbd_backend.ZONE_LEFT, kbd_backend.ZONE_MIDDLE, kbd_backend.ZONE_RIGHT]

    def _cluster(self, step, h):
        """Where the WASD patch sits, or None when it is not drawn.

        Not drawn when the effect folds WASD into the left zone, and not
        during an alert -- both of those are meant to read as one board.
        """
        if len(self._colors) <= kbd_backend.ZONE_WASD or self._merged or self._alert:
            return None
        cluster_w, cluster_h = min(74.0, step * 0.8), min(34.0, h * 0.42)
        return (self.PAD + step * 0.5 - cluster_w / 2, self.PAD + h * 0.52,
                cluster_w, cluster_h)

    def _zone_at(self, x, y):
        """Which zone the pointer is over, or None."""
        pad = self.PAD
        w, h = self.get_width() - 2 * pad, self.get_height() - 2 * pad
        if w <= 0 or h <= 0 or not (pad <= x <= pad + w and pad <= y <= pad + h):
            return None
        bands = self._bands()
        step = w / len(bands)
        cluster = self._cluster(step, h)
        if cluster is not None:
            cx, cy, cw, ch = cluster
            if cx <= x <= cx + cw and cy <= y <= cy + ch:
                return kbd_backend.ZONE_WASD
        return bands[min(len(bands) - 1, int((x - pad) / step))]

    def _on_released(self, gesture, n_press, x, y):
        if not self._clickable or n_press != 1:
            return
        zone = self._zone_at(x, y)
        if zone is not None:
            self.emit("zone-activated", zone)

    def _on_motion(self, _controller, x, y):
        # Only on a change: motion fires per pixel of travel, and each
        # set_cursor_from_name builds a fresh Gdk.Cursor.
        over = bool(self._clickable and self._zone_at(x, y) is not None)
        if over != self._pointer_over:
            self._pointer_over = over
            self.set_cursor_from_name("pointer" if over else None)

    def _on_leave(self, _controller):
        if self._pointer_over:
            self._pointer_over = False
            self.set_cursor_from_name(None)

    def _draw(self, _area, cr, width, height):
        pad = self.PAD
        w, h = width - 2 * pad, height - 2 * pad
        bands = self._bands()
        step = w / len(bands)
        alpha = 1.0 if self._on else 0.18
        _rounded_rect(cr, pad, pad, w, h, 10)
        cr.clip_preserve()
        for i, zone in enumerate(bands):
            r, g, b = self._zone_color(zone)
            cr.set_source_rgba(r / 255, g / 255, b / 255, alpha)
            # +1 so neighbouring bands overlap by a subpixel; without it
            # antialiasing leaves a pale seam between them.
            cr.rectangle(pad + i * step, pad, step + 1, h)
            cr.fill()
        cr.reset_clip()

        cluster = self._cluster(step, h)
        if cluster is not None:
            x, y, cluster_w, cluster_h = cluster
            r, g, b = self._zone_color(kbd_backend.ZONE_WASD)
            _rounded_rect(cr, x, y, cluster_w, cluster_h, 6)
            cr.set_source_rgba(r / 255, g / 255, b / 255, alpha)
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
            LaptopKbdPage(), "kbd", "Keyboard", "keyboard-brightness-symbolic"
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

    app = HyprUtilApp()
    app.run(argv)


if __name__ == "__main__":
    main()
