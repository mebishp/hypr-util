#!/bin/bash
# Symmetric teardown for setup.sh: stops/disables the services it enabled,
# removes every file it installed outside this repo, clears Focus mode's
# /etc/hosts block + nftables table, and strips the setcap grant on
# firefly-ctl. Doesn't touch the repo itself or distro packages installed by
# install_packages -- only what setup.sh scattered across the filesystem.
set -euo pipefail

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
	echo "[uninstall] Do not run as root / with sudo. Run as your normal user:" >&2
	echo "[uninstall]   ./uninstall.sh   (it calls sudo itself for system steps)" >&2
	exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[uninstall] $*" >&2; }

log "clearing focus mode blocks (hosts + nftables), if any"
if command -v pkexec >/dev/null 2>&1 && [ -x /usr/local/bin/hypr-util-focus-hosts ]; then
	pkexec /usr/local/bin/hypr-util-focus-hosts off 2>/dev/null || true
fi
if command -v pkexec >/dev/null 2>&1 && [ -x /usr/local/bin/hypr-util-focus-fw ]; then
	pkexec /usr/local/bin/hypr-util-focus-fw off 2>/dev/null || true
fi

log "stopping/disabling user services"
systemctl --user stop hypr-util-daemon.service 2>/dev/null || true
systemctl --user disable hypr-util-daemon.service 2>/dev/null || true
pkill -f "bin/hyprutil tray" 2>/dev/null || true

log "stopping/disabling system services"
sudo systemctl stop fancurve.service 2>/dev/null || true
sudo systemctl disable fancurve.service 2>/dev/null || true

log "removing installed system files"
sudo rm -f \
	/etc/udev/rules.d/99-firefly-keyboard.rules \
	/etc/systemd/system/fancurve.service \
	/usr/local/bin/fancurve.sh \
	/usr/lib/systemd/system-sleep/hypr-util \
	/usr/local/bin/hypr-util-focus-hosts \
	/usr/local/bin/hypr-util-focus-fw \
	/etc/polkit-1/rules.d/49-hypr-util-focus.rules
sudo udevadm control --reload-rules 2>/dev/null || true
sudo systemctl daemon-reload 2>/dev/null || true

log "removing installed user files"
rm -f \
	"$HOME/.config/systemd/user/hypr-util-daemon.service" \
	"$HOME/.config/autostart/hypr-util.desktop" \
	"$HOME/.local/share/applications/org.hyprnon.hyprutil.desktop" \
	"$HOME/.local/share/dbus-1/services/org.hyprnon.hyprutil.service" \
	"$HOME/.local/share/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg" \
	"$HOME/.local/share/hypr-util/focus-wallpaper.svg" \
	"$HOME/.local/share/hypr-util/focus-wallpaper-dark.svg"
rmdir "$HOME/.local/share/hypr-util" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

if [ -f "$REPO_DIR/firefly-ctl/target/debug/firefly-ctl" ]; then
	log "removing setcap grant on firefly-ctl"
	sudo setcap -r "$REPO_DIR/firefly-ctl/target/debug/firefly-ctl" 2>/dev/null || true
fi

log "done -- repo contents, ~/.config/hypr-util state, and distro packages were left untouched"
log "remove those yourself if you want a full wipe: rm -rf ~/.config/hypr-util $REPO_DIR"
