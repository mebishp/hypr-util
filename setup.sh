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
#   /usr/src/hyprkbd-<version>/         keyboard lighting driver, built by DKMS
#   /etc/modules-load.d/hyprkbd.conf    loads it at boot
#   /etc/systemd/system/                hypr-util-fancurve.service
#                                       hypr-util-kbd.service
#   /etc/systemd/system-sleep/hypr-util suspend/resume hook
#   /etc/udev/rules.d/                  99-firefly-keyboard.rules
#                                       99-hyprkbd.rules
#   ~/.config/systemd/user/             hypr-util-{daemon,tray}.service
#                                       hypr-util-kbd-effects.service
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

USER_UNITS=(hypr-util-daemon.service hypr-util-tray.service hypr-util-kbd-effects.service)
SYSTEM_UNITS=(hypr-util-fancurve.service hypr-util-kbd.service)

# The laptop keyboard lighting driver. Version comes from its own dkms.conf,
# so bumping it in one place is enough.
KMOD_NAME=hyprkbd
KMOD_SRC=$REPO_DIR/kernel/hyprkbd
KMOD_VERSION=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$KMOD_SRC/dkms.conf")

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
	    -e "s|@OWNER@|$(id -u):$(id -g)|g" \
	    "$src" | $sudo_cmd install -D -m "$mode" /dev/stdin "$dest"
}

check_preconditions() {
	[ "${EUID:-$(id -u)}" -ne 0 ] || die "do not run as root; run ./setup.sh as your normal user (it calls sudo itself)"
	command -v sudo >/dev/null || die "sudo is required"
	sudo -v || die "this installer needs sudo privileges"
}

# Prints, one per line, whichever of $2... isn't installed according to $1
# (a package manager's "is this installed" query, e.g. "pacman -Qi" or
# "dpkg -s" -- unquoted on purpose, so a two-word query splits back into a
# command and its flag).
missing_packages() {
	local query=$1; shift
	local pkg
	for pkg in "$@"; do
		$query "$pkg" >/dev/null 2>&1 || printf '%s\n' "$pkg"
	done
}

# dkms builds the laptop keyboard lighting driver (kernel/hyprkbd), which is
# what carries the colours: the kernel's own hp-wmi driver speaks the same
# BIOS mailbox but has no lighting code, and nothing else reaches it.
# Everything else here still works without it; only the Laptop page goes
# dark.
#
# Package names for the same six dependencies, one set per manager -- distro
# package sets for GTK4/libadwaita's introspection data in particular are not
# uniform, so these are a best effort. Add another branch the same shape to
# support one not listed here.
install_packages() {
	local mgr pkgs query installer

	if command -v pacman >/dev/null; then
		mgr=pacman
		pkgs=(python-pyqt6 python-gobject python-pyudev libadwaita gtk4 power-profiles-daemon dkms)
		query="pacman -Qi"
		installer="sudo pacman -S --needed"
	elif command -v apt-get >/dev/null; then
		mgr=apt
		pkgs=(python3-pyqt6 python3-gi python3-pyudev gir1.2-adw-1 gir1.2-gtk-4.0 power-profiles-daemon dkms)
		query="dpkg -s"
		installer="sudo apt-get install -y"
	elif command -v dnf >/dev/null; then
		mgr=dnf
		pkgs=(python3-pyqt6 python3-gobject python3-pyudev libadwaita gtk4 power-profiles-daemon dkms)
		query="rpm -q"
		installer="sudo dnf install -y"
	elif command -v zypper >/dev/null; then
		mgr=zypper
		pkgs=(python3-PyQt6 python3-gobject python3-pyudev libadwaita-1-0 typelib-1_0-Adw-1 gtk4 typelib-1_0-Gtk-4_0 power-profiles-daemon dkms)
		query="rpm -q"
		installer="sudo zypper install -y"
	else
		log "no supported package manager found (pacman/apt/dnf/zypper); install these manually if missing: PyQt6 bindings, PyGObject, pyudev, libadwaita, GTK4, power-profiles-daemon, dkms, and your kernel's headers"
		return
	fi

	local missing=()
	mapfile -t missing < <(missing_packages "$query" "${pkgs[@]}")

	if [ "${#missing[@]}" -eq 0 ]; then
		log "all required packages present"
		return
	fi

	log "installing: ${missing[*]}"
	[ "$mgr" = apt ] && sudo apt-get update
	$installer "${missing[@]}"
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

# Build and install the laptop keyboard lighting driver through DKMS, so it
# survives a kernel upgrade without anyone remembering it exists.
#
# Not fatal when it fails. Everything else in this project works on a machine
# with no kernel headers or no HP mailbox; only the Laptop keyboard page
# needs the driver, and it says so itself when the module is missing.
install_kernel_module() {
	local dest=/usr/src/$KMOD_NAME-$KMOD_VERSION

	if ! command -v dkms >/dev/null; then
		log "dkms not installed; skipping the keyboard lighting driver"
		return
	fi
	if [ ! -d "/usr/lib/modules/$(uname -r)/build" ] && [ ! -d "/lib/modules/$(uname -r)/build" ]; then
		log "no kernel headers for $(uname -r); skipping the keyboard lighting driver"
		log "  install them (e.g. 'pacman -S linux-headers') and re-run ./setup.sh"
		return
	fi

	# A previously loaded copy holds the sysfs attributes open; the new one
	# cannot register the same platform device until it is gone.
	sudo modprobe -r "$KMOD_NAME" 2>/dev/null || true
	# Re-registering the same version is what a re-run after `git pull`
	# does, and dkms refuses to overwrite one in place.
	sudo dkms remove -m "$KMOD_NAME" -v "$KMOD_VERSION" --all 2>/dev/null || true

	log "building $KMOD_NAME $KMOD_VERSION with dkms"
	sudo rm -rf "$dest"
	sudo install -d -m 755 "$dest"
	sudo install -m 644 "$KMOD_SRC"/hyprkbd.c "$KMOD_SRC"/Makefile "$KMOD_SRC"/dkms.conf "$dest/"

	if ! sudo dkms install -m "$KMOD_NAME" -v "$KMOD_VERSION"; then
		log "the keyboard lighting driver did not build; the Laptop page will stay dark"
		log "  see /var/lib/dkms/$KMOD_NAME/$KMOD_VERSION/build/make.log"
		return
	fi

	sudo install -D -m 644 "$SRC/modules-load/hyprkbd.conf" /etc/modules-load.d/hyprkbd.conf
	sudo modprobe "$KMOD_NAME" || log "the driver installed but would not load; check dmesg"

	# A second driver writing the same firmware colour table fights this one
	# for it. Worth saying out loud rather than leaving as a mystery.
	if lsmod | grep -q '^hp_rgb_lighting'; then
		log "warning: hp_rgb_lighting is also loaded and drives the same colour table"
		log "  remove it with 'sudo modprobe -r hp_rgb_lighting' if the colours fight"
	fi
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
	# Rendered, not copied: it carries the uid this install is for.
	render_install 644 "$SRC/udev/99-hyprkbd.rules" /etc/udev/rules.d/99-hyprkbd.rules sudo
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
	install_kernel_module
	install_program
	install_system_units
	install_user_files
	start_services
	log "done -- installed to $PREFIX, run 'hyprutil --help' to check"
}

main "$@"
