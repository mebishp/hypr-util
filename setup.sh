#!/bin/bash
# Idempotent installer/updater for hypr-util.
#
# Safe to re-run any time (e.g. after `git pull`): only touches the system
# files that actually changed, and only restarts/reloads the services that
# own them, instead of always reinstalling everything from scratch.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
	echo "[setup] Do not run as root / with sudo. Run as your normal user:" >&2
	echo "[setup]   ./setup.sh   (it calls sudo itself for system steps)" >&2
	exit 1
fi

command -v sudo >/dev/null 2>&1 || { echo "[setup] sudo required" >&2; exit 1; }
sudo -v || { echo "[setup] this installer needs sudo privileges" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_DIR="$REPO_DIR/system"

# Any mktemp'd rendered file leaks into /tmp if we abort (set -e) before its
# own rm -- collect them here and let one trap sweep whatever's left.
_tmp_files=()
cleanup_tmp_files() {
	local f
	for f in "${_tmp_files[@]:-}"; do
		rm -f "$f" 2>/dev/null || true
	done
}
trap cleanup_tmp_files EXIT

log() {
	# stderr, not stdout: sync_file() is called as `$(sync_file ...)` at every
	# call site, which captures all of stdout -- if log() wrote there too,
	# its "installing $dest" line would land in the captured value ahead of
	# the final "changed"/"unchanged", so `[ "$result" = "changed" ]` would
	# never match and every reload/restart-on-change branch below would
	# silently never fire whenever a file actually changed.
	echo "[setup] $*" >&2
}

# Tracks whether anything actually changed, for the final summary.
changed_udev=0
changed_fancurve=0
changed_sleep_hook=0
changed_focus=0
changed_daemon=0
changed_app=0

install_packages() {
	if ! command -v pacman >/dev/null 2>&1; then
		log "pacman not found, skipping package install (install these manually: $*)"
		return
	fi

	local pkgs=(python-pyqt6 python-gobject python-pyudev libadwaita gtk4 power-profiles-daemon rust gsettings-desktop-schemas nftables)
	local missing=()
	for pkg in "${pkgs[@]}"; do
		if ! pacman -Qi "$pkg" >/dev/null 2>&1; then
			missing+=("$pkg")
		fi
	done

	if [ "${#missing[@]}" -eq 0 ]; then
		log "all required packages already installed"
	else
		log "installing missing packages: ${missing[*]}"
		sudo pacman -S --needed "${missing[@]}"
	fi
}

build_firefly_ctl() {
	if ! command -v cargo >/dev/null 2>&1; then
		log "cargo not found; skipping firefly-ctl (install rust for RGB control)"
		return
	fi
	log "building firefly-ctl"
	if ! (cd "$REPO_DIR/firefly-ctl" && cargo build); then
		log "WARNING: firefly-ctl build failed; RGB control unavailable"
		return
	fi
	sudo setcap cap_sys_admin=ep "$REPO_DIR/firefly-ctl/target/debug/firefly-ctl"
}

# sync_file <src> <dest> [use_sudo]
# Copies src -> dest only if their contents differ. Echoes "changed" or
# "unchanged" so callers can react.
sync_file() {
	local src="$1" dest="$2" use_sudo="${3:-}"
	local src_sum dest_sum
	src_sum=$(sha256sum "$src" | cut -d' ' -f1)
	dest_sum=$( { [ "$use_sudo" = "sudo" ] && sudo sha256sum "$dest" || sha256sum "$dest"; } 2>/dev/null | cut -d' ' -f1 || true)

	if [ "$src_sum" = "$dest_sum" ]; then
		echo "unchanged"
		return
	fi

	log "installing $dest"
	if [ "$use_sudo" = "sudo" ]; then
		sudo install -D -m "$(stat -c%a "$src")" "$src" "$dest"
	else
		install -D -m "$(stat -c%a "$src")" "$src" "$dest"
	fi
	echo "changed"
}

install_udev_rule() {
	local dest="/etc/udev/rules.d/99-firefly-keyboard.rules"
	if [ "$(sync_file "$SYSTEM_DIR/99-firefly-keyboard.rules" "$dest" sudo)" = "changed" ]; then
		changed_udev=1
		log "reloading udev rules"
		sudo udevadm control --reload-rules
		sudo udevadm trigger
	fi
}

install_fancurve_daemon() {
	local script_dest="/usr/local/bin/fancurve.sh"
	local unit_dest="/etc/systemd/system/fancurve.service"
	local script_changed unit_changed

	script_changed=$(sync_file "$SYSTEM_DIR/fancurve.sh" "$script_dest" sudo)
	unit_changed=$(sync_file "$SYSTEM_DIR/fancurve.service" "$unit_dest" sudo)

	if [ "$script_changed" = "changed" ] || [ "$unit_changed" = "changed" ]; then
		changed_fancurve=1
		log "reloading and restarting fancurve.service"
		sudo systemctl daemon-reload
		sudo systemctl restart fancurve.service
	fi
	sudo systemctl enable --quiet fancurve.service
	if ! systemctl is-active --quiet fancurve.service; then
		sudo systemctl start fancurve.service
	fi
}

install_sleep_hook() {
	# systemd-sleep hooks need no daemon-reload / enable step -- systemd reads
	# this directory fresh on every suspend/resume, it just has to exist.
	local dest="/usr/lib/systemd/system-sleep/hypr-util"
	if [ "$(sync_file "$SYSTEM_DIR/hypr-util-sleep.sh" "$dest" sudo)" = "changed" ]; then
		changed_sleep_hook=1
	fi
}

install_focus_blocking_helpers() {
	# Root helpers for Focus mode's site blocking (edits /etc/hosts) and
	# IP-level blocking (a dedicated nftables table -- catches connections
	# to already-resolved/cached IPs that a hosts-file change alone can't),
	# plus the polkit rule that lets the automation daemon invoke both via
	# pkexec with no interactive password prompt -- the daemon runs
	# unattended and can't answer one. None of these need a reload/restart
	# step: the scripts are invoked fresh via pkexec on every call, and
	# polkit re-reads /etc/polkit-1/rules.d on its own.
	local hosts_changed fw_changed rules_changed
	hosts_changed=$(sync_file "$SYSTEM_DIR/hypr-util-focus-hosts.sh" "/usr/local/bin/hypr-util-focus-hosts" sudo)
	fw_changed=$(sync_file "$SYSTEM_DIR/hypr-util-focus-fw.sh" "/usr/local/bin/hypr-util-focus-fw" sudo)
	rules_changed=$(sync_file "$SYSTEM_DIR/49-hypr-util-focus.rules" "/etc/polkit-1/rules.d/49-hypr-util-focus.rules" sudo)
	if [ "$hosts_changed" = "changed" ] || [ "$fw_changed" = "changed" ] || [ "$rules_changed" = "changed" ]; then
		changed_focus=1
	fi
}

install_focus_wallpaper() {
	local dest_dir="$HOME/.local/share/hypr-util"
	mkdir -p "$dest_dir"
	sync_file "$SYSTEM_DIR/focus-wallpaper.svg" "$dest_dir/focus-wallpaper.svg" >/dev/null
	sync_file "$SYSTEM_DIR/focus-wallpaper-dark.svg" "$dest_dir/focus-wallpaper-dark.svg" >/dev/null
}

install_automation_daemon() {
	local dest="$HOME/.config/systemd/user/hypr-util-daemon.service"
	local rendered result
	rendered=$(mktemp)
	_tmp_files+=("$rendered")
	sed "s|%REPO_DIR%|$REPO_DIR|g" "$SYSTEM_DIR/hypr-util-daemon.service" > "$rendered"
	chmod 644 "$rendered"
	result=$(sync_file "$rendered" "$dest")
	rm -f "$rendered"
	[ "$result" = "changed" ] && changed_daemon=1

	systemctl --user daemon-reload
	systemctl --user enable --quiet hypr-util-daemon.service
	if [ "$result" = "changed" ]; then
		log "restarting hypr-util-daemon.service"
		systemctl --user restart hypr-util-daemon.service
	elif ! systemctl --user is-active --quiet hypr-util-daemon.service; then
		systemctl --user start hypr-util-daemon.service
	fi
}

install_autostart() {
	local dest="$HOME/.config/autostart/hypr-util.desktop"
	local rendered result

	# Remove stale entries from old repo names so only one instance autostarts.
	local stale
	for stale in "$HOME/.config/autostart/hyprnonfan.desktop"; do
		if [ -f "$stale" ]; then
			log "removing stale autostart entry: $stale"
			rm -f "$stale"
		fi
	done

	rendered=$(mktemp)
	_tmp_files+=("$rendered")
	sed "s|%REPO_DIR%|$REPO_DIR|g" "$SYSTEM_DIR/hypr-util.desktop" > "$rendered"
	chmod 644 "$rendered"
	result=$(sync_file "$rendered" "$dest")
	rm -f "$rendered"

	# Reload the systemd user session generator so the correct autostart unit
	# is created immediately, without requiring a re-login.
	systemctl --user daemon-reload 2>/dev/null && log "reloaded systemd user daemon" || true
}

ensure_tray_running() {
	# The tray has no systemd unit and no installed copy of its own code to
	# diff against (its desktop entry always just points straight at this
	# repo's bin/hyprutil) -- so unlike the daemon, "did the installed file
	# change" can't tell us whether a code update needs picking up. Always
	# restart a currently-running tray so `git pull` + setup.sh reliably
	# refreshes it, same as any other update here -- and also *start* it if
	# it isn't running at all (a fresh install, or one where it crashed/was
	# never launched), rather than leaving the user with no tray icon until
	# their next graphical login picks up the autostart entry installed by
	# install_autostart.
	if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
		# No graphical session to attach a Qt tray icon to (e.g. setup.sh
		# run over SSH/a plain TTY) -- the autostart entry will start it at
		# the next graphical login instead.
		log "no graphical session detected; tray will start at next login"
		return
	fi

	if pgrep -f "bin/hyprutil tray" >/dev/null 2>&1; then
		log "restarting tray to pick up code changes"
		pkill -f "bin/hyprutil tray" || true
		# Give the old instance's instance-lock socket
		# (/run/user/<uid>/hypr-util-tray.lock) a moment to release before
		# starting the new one, so it doesn't see a still-listening stale
		# lock and refuse to start (see _acquire_instance_lock() in tray.py).
		for _ in 1 2 3 4 5 6 7 8 9 10; do
			pgrep -f "bin/hyprutil tray" >/dev/null 2>&1 || break
			sleep 0.2
		done
	else
		log "starting tray"
	fi
	nohup "$REPO_DIR/bin/hyprutil" tray >/dev/null 2>&1 &
	disown
}

install_app_launcher() {
	local icon_dest="$HOME/.local/share/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg"
	local desktop_dest="$HOME/.local/share/applications/org.hyprnon.hyprutil.desktop"
	local dbus_service_dest="$HOME/.local/share/dbus-1/services/org.hyprnon.hyprutil.service"
	local rendered_desktop rendered_service icon_changed desktop_changed
	rendered_desktop=$(mktemp)
	_tmp_files+=("$rendered_desktop")
	sed "s|%REPO_DIR%|$REPO_DIR|g" "$SYSTEM_DIR/org.hyprnon.hyprutil.desktop" > "$rendered_desktop"
	chmod 644 "$rendered_desktop"
	rendered_service=$(mktemp)
	_tmp_files+=("$rendered_service")
	sed "s|%REPO_DIR%|$REPO_DIR|g" "$SYSTEM_DIR/org.hyprnon.hyprutil.service" > "$rendered_service"
	chmod 644 "$rendered_service"

	icon_changed=$(sync_file "$SYSTEM_DIR/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg" "$icon_dest")
	desktop_changed=$(sync_file "$rendered_desktop" "$desktop_dest")
	sync_file "$rendered_service" "$dbus_service_dest" >/dev/null
	rm -f "$rendered_desktop" "$rendered_service"
	[ "$desktop_changed" = "changed" ] && changed_app=1

	if [ "$icon_changed" = "changed" ]; then
		gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
	fi
	if [ "$desktop_changed" = "changed" ]; then
		update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
	fi
}

main() {
	install_packages
	build_firefly_ctl
	install_udev_rule
	install_fancurve_daemon
	install_sleep_hook
	install_focus_blocking_helpers
	install_focus_wallpaper
	install_autostart
	install_app_launcher
	install_automation_daemon
	ensure_tray_running

	log "done"
	if [ "$changed_udev" -eq 1 ] || [ "$changed_fancurve" -eq 1 ] || [ "$changed_sleep_hook" -eq 1 ] \
		|| [ "$changed_focus" -eq 1 ] || [ "$changed_daemon" -eq 1 ] || [ "$changed_app" -eq 1 ]; then
		log "system files were updated and reloaded"
	else
		log "everything was already up to date"
	fi
}

main "$@"
