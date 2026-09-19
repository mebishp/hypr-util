#!/usr/bin/env bash
# Installer/updater for hypr-util.
#
# Installs into $PREFIX (default /usr/local) plus the usual per-user XDG and
# systemd directories, then restarts everything so the running processes are
# the code that was just installed. Safe to re-run after `git pull`.
#
# Layout installed (mirrors the system/ tree in this repo):
#
#   $PREFIX/lib/hypr-util/hyprutil/     Python package
#   $PREFIX/bin/hyprutil                launcher
#   $PREFIX/bin/hypr-util-fancurve      fan curve daemon script
#   /etc/systemd/system/                hypr-util-fancurve.service
#                                       hypr-util-kbd.service
#   /etc/systemd/system-sleep/hypr-util suspend/resume hook
#   /etc/udev/rules.d/                  99-firefly-keyboard.rules
#   ~/.config/systemd/user/             hypr-util-{daemon,tray}.service
#   ~/.local/share/applications/        desktop entry
#   ~/.local/share/dbus-1/services/     D-Bus activation file
#   ~/.local/share/icons/hicolor/       app icon
set -euo pipefail

PREFIX=${PREFIX:-/usr/local}
BINDIR=$PREFIX/bin
LIBDIR=$PREFIX/lib/hypr-util

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SRC=$REPO_DIR/system
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}
DATA_DIR=${XDG_DATA_HOME:-$HOME/.local/share}
APP_CONFIG_DIR=$CONFIG_DIR/hypr-util

USER_UNITS=(hypr-util-daemon.service hypr-util-tray.service)
SYSTEM_UNITS=(hypr-util-fancurve.service hypr-util-kbd.service)

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] error: %s\n' "$*" >&2; exit 1; }

# install(1) with an explicit mode, after rendering @PLACEHOLDER@ values.
# Modes are stated here rather than copied from the checkout, so a stray
# chmod in the repo can't produce a non-executable helper or a world-writable
# unit file.
render_install() {
	local mode=$1 src=$2 dest=$3 sudo_cmd=${4:-}
	sed -e "s|@BINDIR@|$BINDIR|g" \
	    -e "s|@LIBDIR@|$LIBDIR|g" \
	    -e "s|@CONFIG_DIR@|$APP_CONFIG_DIR|g" \
	    "$src" | $sudo_cmd install -D -m "$mode" /dev/stdin "$dest"
}

check_preconditions() {
	[ "${EUID:-$(id -u)}" -ne 0 ] || die "do not run as root; run ./setup.sh as your normal user (it calls sudo itself)"
	command -v sudo >/dev/null || die "sudo is required"
	sudo -v || die "this installer needs sudo privileges"
}

install_packages() {
	if ! command -v pacman >/dev/null; then
		log "pacman not found; install these manually if missing: python-pyqt6 python-gobject python-pyudev libadwaita gtk4 power-profiles-daemon acpi_call"
		return
	fi
	# acpi_call is what carries the laptop keyboard's colours: the kernel's
	# own hp-wmi driver speaks this BIOS mailbox but exposes nothing for
	# lighting, and there is no other route to it from userspace. Everything
	# else still works without it; only the Laptop page goes dark.
	local pkgs=(python-pyqt6 python-gobject python-pyudev libadwaita gtk4 power-profiles-daemon acpi_call)
	local missing=()
	local pkg
	for pkg in "${pkgs[@]}"; do
		pacman -Qi "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
	done
	if [ "${#missing[@]}" -eq 0 ]; then
		log "all required packages present"
	else
		log "installing: ${missing[*]}"
		sudo pacman -S --needed "${missing[@]}"
	fi
}

# Stop everything before replacing files on disk.
#
# The settings app is the reason this exists: it is a resident, D-Bus-
# activated GApplication that hides its window instead of exiting, so it
# keeps owning org.hyprnon.hyprutil across a reinstall. Without quitting it
# here, every later launch re-presents the process started from the OLD code
# and the update looks like it silently did nothing.
stop_services() {
	log "stopping running instances"
	systemctl --user stop "${USER_UNITS[@]}" 2>/dev/null || true
	gdbus call --session --dest org.hyprnon.hyprutil \
		--object-path /org/hyprnon/hyprutil \
		--method org.freedesktop.Application.ActivateAction quit '[]' '{}' >/dev/null 2>&1 || true
	pkill -f 'hyprutil app' 2>/dev/null || true
	sudo systemctl stop "${SYSTEM_UNITS[@]}" 2>/dev/null || true
}

# Paths used by versions of this project before the current layout. Left
# behind they are not merely clutter: the old fancurve.service would keep
# running a second fan daemon fighting this one over pwm1, and the old
# autostart entry would launch a second tray.
remove_legacy() {
	log "removing pre-existing installs from the old layout"
	systemctl --user disable --now hypr-util-tray.service 2>/dev/null || true
	sudo systemctl disable --now fancurve.service 2>/dev/null || true
	sudo rm -f \
		/etc/systemd/system/fancurve.service \
		/usr/local/bin/fancurve.sh \
		/usr/lib/systemd/system-sleep/hypr-util
	rm -f \
		"$CONFIG_DIR/autostart/hypr-util.desktop" \
		"$CONFIG_DIR/autostart/hyprnonfan.desktop" \
		"$DATA_DIR/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg"
}

install_program() {
	log "installing program files into $PREFIX"
	# Wipe the whole directory rather than just the package: it also used to
	# hold the firefly-ctl helper binary, which no longer exists.
	sudo rm -rf "$LIBDIR"
	sudo install -d -m 755 "$LIBDIR"
	sudo cp -r "$REPO_DIR/hyprutil" "$LIBDIR/hyprutil"
	sudo find "$LIBDIR/hyprutil" -name __pycache__ -type d -prune -exec rm -rf {} +
	sudo chown -R root:root "$LIBDIR/hyprutil"
	sudo find "$LIBDIR/hyprutil" -type d -exec chmod 755 {} +
	sudo find "$LIBDIR/hyprutil" -type f -exec chmod 644 {} +

	render_install 755 "$SRC/bin/hyprutil.in" "$BINDIR/hyprutil" sudo
	render_install 755 "$SRC/bin/hypr-util-fancurve" "$BINDIR/hypr-util-fancurve" sudo
}

install_system_units() {
	log "installing system units and rules"
	sudo install -D -m 644 "$SRC/udev/99-firefly-keyboard.rules" /etc/udev/rules.d/99-firefly-keyboard.rules
	sudo install -D -m 755 "$SRC/sleep/hypr-util" /etc/systemd/system-sleep/hypr-util
	local unit
	for unit in "${SYSTEM_UNITS[@]}"; do
		render_install 644 "$SRC/systemd/system/$unit" "/etc/systemd/system/$unit" sudo
	done

	sudo udevadm control --reload-rules
	sudo udevadm trigger
	sudo systemctl daemon-reload
}

install_user_files() {
	log "installing user units, desktop entry, and icon"
	local unit
	for unit in "${USER_UNITS[@]}"; do
		render_install 644 "$SRC/systemd/user/$unit" "$CONFIG_DIR/systemd/user/$unit"
	done
	render_install 644 "$SRC/desktop/org.hyprnon.hyprutil.desktop" "$DATA_DIR/applications/org.hyprnon.hyprutil.desktop"
	render_install 644 "$SRC/dbus/org.hyprnon.hyprutil.service" "$DATA_DIR/dbus-1/services/org.hyprnon.hyprutil.service"
	install -D -m 644 "$SRC/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" \
		"$DATA_DIR/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg"

	gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true
	update-desktop-database "$DATA_DIR/applications" >/dev/null 2>&1 || true
	systemctl --user daemon-reload
}

start_services() {
	log "enabling and starting services"
	sudo systemctl enable --now "${SYSTEM_UNITS[@]}"

	systemctl --user enable "${USER_UNITS[@]}" >/dev/null
	if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
		systemctl --user start "${USER_UNITS[@]}"
	else
		# graphical-session.target isn't reached from a plain TTY/SSH login,
		# and the tray needs a display to attach its icon to; both units are
		# enabled, so they start at the next graphical login.
		log "no graphical session detected; daemon and tray start at next login"
	fi
}

main() {
	check_preconditions
	install_packages
	stop_services
	remove_legacy
	install_program
	install_system_units
	install_user_files
	start_services
	log "done -- installed to $PREFIX, run 'hyprutil --help' to check"
}

main "$@"
