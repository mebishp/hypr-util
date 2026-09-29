# Desktop environment compatibility

hypr-util is not GNOME-specific. It reads code, not the name: the package
is called `hypr-util` after the machine's hostname, not Hyprland, and
despite that name it doesn't call into Hyprland's IPC either. Almost
everything it touches is a freedesktop or kernel interface that every
desktop implements the same way -- and that includes the Firefly's RGB
support, which is plain USB HID and has no desktop involvement at all.

## What's desktop-agnostic

| Piece | Talks to | Works on |
|---|---|---|
| Fan curve (`hyprutil/fan.py`) | hwmon sysfs, `powerprofilesctl` / `org.freedesktop.UPower.PowerProfiles` | Any DE, and no DE at all |
| Laptop keyboard driver (`kernel/hyprkbd/`, `hyprutil/kbd/`) | sysfs (`/sys/devices/platform/hyprkbd/`) | Any DE, and no DE at all |
| Firefly RGB (`hyprutil/rgb/`) | its `hidraw` node directly, and udev events for plug/unplug | Any DE, and no DE at all |
| Sleep/resume handling (`system/sleep/hypr-util`) | `systemd-sleep` hook | Any DE (it's a system-level hook, not session-level) |
| Power-profile watching (`hyprutil/power.py`) | `org.freedesktop.UPower.PowerProfiles` (falling back to `net.hadess.PowerProfiles`) | Any DE running `power-profiles-daemon` |
| Settings window (`hyprutil/ui/app.py`) | GTK4 + Adwaita | Any DE with GTK4 installed -- Adwaita styling follows GNOME's look everywhere, same as any other libadwaita app |
| Tray icon (`hyprutil/ui/tray.py`) | Qt6 `QSystemTrayIcon` (StatusNotifierItem) | Any DE with an SNI tray host -- built into KDE, Hyprland+waybar, and most others; GNOME needs the "AppIndicator and KStatusNotifierItem Support" extension, since it ships no tray by default |
| D-Bus activation, `.desktop` entry, udev rules | `org.freedesktop.DBus`, XDG desktop-entry spec, udev | Any DE |

## The one GNOME-specific behaviour

The automation daemon (`hyprutil/automation.py`) follows power-profile
changes by also nudging the built-in display's refresh rate --
`hyprutil/display.py` does that through `org.gnome.Mutter.DisplayConfig`,
because Wayland has no `xrandr` equivalent and that's Mutter's (GNOME's
compositor) own API for it. No other compositor exposes this interface, so
this one behaviour is effectively GNOME-only. It has nothing to do with
either keyboard's lighting.

It fails safe: `set_refresh_rate()` and `current_refresh_hz()` catch
`GLib.Error` and return `False` / `None` on any non-GNOME desktop, since the
bus name simply has no owner. Nothing crashes, nothing logs a scary error --
the refresh rate just stays wherever it was, and every other feature (fan
curve, both keyboards' lighting, tray, settings window) keeps working
exactly as on GNOME. It isn't exposed as a CLI command or a toggle in the
settings window either, so there's nothing to disable -- it's a side effect
of the daemon that quietly does nothing where it can't work.

## Bottom line

Run this on KDE Plasma, Hyprland, Sway, XFCE, or anything else: fan curve,
both keyboards' lighting (laptop and Firefly, including matching them to
each other), the settings window, and the tray icon all work exactly as
they do on GNOME. The only thing you lose is the display automatically
switching to a higher refresh rate when you leave power-saver mode --
everything else about power-profile awareness (watching it, reacting to it
in the tray and both keyboards' status flashes) is unaffected.
