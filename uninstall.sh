#!/usr/bin/env bash
# Teardown for setup.sh: stops and disables every service it enabled and
# removes every file it installed outside this repo, including the paths used
# by older layouts of this project.
#
# Leaves alone: the repo itself, distro packages installed by setup.sh's
# install_packages, and your settings in ~/.config/hypr-util.
set -euo pipefail

PREFIX=${PREFIX:-/usr/local}
BINDIR=$PREFIX/bin
LIBDIR=$PREFIX/lib/hypr-util

CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}
DATA_DIR=${XDG_DATA_HOME:-$HOME/.local/share}

log() { printf '[uninstall] %s\n' "$*"; }
die() { printf '[uninstall] error: %s\n' "$*" >&2; exit 1; }

[ "${EUID:-$(id -u)}" -ne 0 ] || die "do not run as root; run ./uninstall.sh as your normal user (it calls sudo itself)"
command -v sudo >/dev/null || die "sudo is required"

log "stopping and disabling services"
# --now so this both stops the running unit and clears its enablement; the
# legacy names are included so a machine installed from an older layout is
# fully cleaned up too.
systemctl --user disable --now hypr-util-daemon.service hypr-util-tray.service hypr-util-kbd-effects.service 2>/dev/null || true
sudo systemctl disable --now hypr-util-fancurve.service hypr-util-kbd.service fancurve.service 2>/dev/null || true
# The settings app is resident (hides rather than exits), so it survives
# having its files deleted out from under it.
pkill -f 'hyprutil app' 2>/dev/null || true

# The keyboard lighting driver, before the files that describe it: unloading
# first means nothing is holding the sysfs attributes when dkms tears the
# module out from under them.
log "removing the keyboard lighting driver"
sudo modprobe -r hyprkbd 2>/dev/null || true
if command -v dkms >/dev/null; then
	# --all: every kernel it was built for, not just the running one.
	for version in $(dkms status -m hyprkbd 2>/dev/null | sed -n 's|^hyprkbd/\([^,]*\),.*|\1|p' | sort -u); do
		sudo dkms remove -m hyprkbd -v "$version" --all 2>/dev/null || true
		sudo rm -rf "/usr/src/hyprkbd-$version"
	done
fi

log "removing system files"
sudo rm -rf "$LIBDIR"
sudo rm -f \
	"$BINDIR/hyprutil" \
	"$BINDIR/hypr-util-fancurve" \
	/etc/systemd/system/hypr-util-fancurve.service \
	/etc/systemd/system/hypr-util-kbd.service \
	/etc/systemd/system-sleep/hypr-util \
	/etc/udev/rules.d/99-firefly-keyboard.rules \
	/etc/udev/rules.d/99-hyprkbd.rules \
	/etc/modules-load.d/hyprkbd.conf \
	/etc/systemd/system/fancurve.service \
	/usr/local/bin/fancurve.sh \
	/usr/lib/systemd/system-sleep/hypr-util

log "removing user files"
rm -f \
	"$CONFIG_DIR/systemd/user/hypr-util-daemon.service" \
	"$CONFIG_DIR/systemd/user/hypr-util-tray.service" \
	"$CONFIG_DIR/systemd/user/hypr-util-kbd-effects.service" \
	"$DATA_DIR/applications/org.hyprnon.hyprutil.desktop" \
	"$DATA_DIR/dbus-1/services/org.hyprnon.hyprutil.service" \
	"$DATA_DIR/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" \
	"$CONFIG_DIR/autostart/hypr-util.desktop" \
	"$DATA_DIR/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg"

log "reloading"
sudo udevadm control --reload-rules 2>/dev/null || true
sudo systemctl daemon-reload 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true
gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true
update-desktop-database "$DATA_DIR/applications" >/dev/null 2>&1 || true

log "done -- settings in $CONFIG_DIR/hypr-util and this repo were left untouched"
log "for a full wipe: rm -rf $CONFIG_DIR/hypr-util"
