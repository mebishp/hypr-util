#!/bin/bash
# systemd-sleep hook: stop the fan curve daemon before suspend and hand pwm1
# back to firmware auto mode, then restart it on resume.
#
# Without this, fancurve.service (see fancurve.service/fancurve.sh) keeps
# pwm1_enable in manual mode across suspend -- the fan is pinned at its last
# PWM the whole time the lid is closed instead of being managed by firmware.
# Stopping the service runs its existing TERM trap (pwm1_enable=2), so this
# hook doesn't duplicate that logic, just triggers it at the right time.
#
# Installed to /usr/lib/systemd/system-sleep/hypr-util by setup.sh. systemd
# invokes every script in that directory as "<script> pre|post <sleep|suspend|...>"
# around every suspend/hibernate/hybrid-sleep, already running as root.
set -euo pipefail

SERVICE="fancurve.service"
FLAG="/run/hypr-util-fancurve-was-active"

case "$1" in
pre)
	if systemctl is-active --quiet "$SERVICE"; then
		touch "$FLAG"
		systemctl stop "$SERVICE"
	fi
	;;
post)
	if [ -e "$FLAG" ]; then
		rm -f "$FLAG"
		systemctl start "$SERVICE"
	fi
	;;
esac
