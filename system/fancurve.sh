#!/bin/bash
# Multi-point fan curve daemon, driven by k10temp, controlling the hp-wmi pwm1 fan.
#
# The active curve follows power-profiles-daemon's current profile (eco/balanced/
# performance), however it was changed -- by this app's tray, gnome-control-center,
# powerprofilesctl, or anything else. Each profile has its own curve file, live-
# reloaded every tick. A manual override file can force a fixed PWM, bypassing
# the curve entirely.
#
# Two anti-hunting measures keep the fan from pulsing:
#   - the temperature fed into the curve is a rolling average, not the
#     instantaneous reading, so brief blips don't move the target PWM
#   - PWM changes are slew-rate limited per tick, faster going up (so heat
#     spikes still get cooled quickly) than going down (so the fan doesn't
#     ramp down, let temp creep back up, and ramp up again in a loop)
#
# This daemon does not stop itself on suspend -- that's handled externally by
# hypr-util-sleep.sh (a systemd-sleep hook), which stops this service before
# sleep (triggering the cleanup trap below, handing pwm1 to firmware auto
# mode) and restarts it on resume.
set -euo pipefail

INTERVAL=5
TEMP_HISTORY_SIZE=3
MAX_STEP_UP=40
MAX_STEP_DOWN=10
CONFIG_DIR="/home/hyprnon/.config/hypr-util"
CURVES_DIR="$CONFIG_DIR/curves"
OVERRIDE_FILE="$CONFIG_DIR/override"

# Fallback curve if a profile's curve file is missing: "temp_C pwm" pairs, ascending.
DEFAULT_POINTS="
35 0
40 150
50 180
65 220
75 255
"

log() {
	echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

find_hwmon_by_name() {
	for d in /sys/class/hwmon/hwmon*; do
		if [ "$(cat "$d/name" 2>/dev/null)" = "$1" ]; then
			echo "$d"
			return 0
		fi
	done
	return 1
}

HP_HWMON=$(find_hwmon_by_name hp) || true
CPU_HWMON=$(find_hwmon_by_name k10temp) || true
if [ -z "$HP_HWMON" ] || [ -z "$CPU_HWMON" ]; then
	log "could not find hwmon device(s) -- hp=${HP_HWMON:-<missing>} k10temp=${CPU_HWMON:-<missing>}"
	exit 1
fi
TEMP_INPUT="$CPU_HWMON/temp1_input"
PWM_OUTPUT="$HP_HWMON/pwm1"
PWM_ENABLE="$HP_HWMON/pwm1_enable"

cleanup() {
	log "stopping, handing pwm1 back to firmware auto mode"
	echo 2 > "$PWM_ENABLE" 2>/dev/null || true
	exit 0
}
trap cleanup TERM INT

echo 1 > "$PWM_ENABLE"
log "started, hwmon for fan=$HP_HWMON cpu=$CPU_HWMON"

current_profile() {
	powerprofilesctl get 2>/dev/null || echo "balanced"
}

curve_file_for_profile() {
	case "$1" in
	power-saver) echo "$CURVES_DIR/eco.conf" ;;
	performance) echo "$CURVES_DIR/performance.conf" ;;
	*) echo "$CURVES_DIR/balanced.conf" ;;
	esac
}

load_points() {
	local f
	f=$(curve_file_for_profile "$1")
	if [ -r "$f" ]; then
		cat "$f"
	else
		echo "$DEFAULT_POINTS"
	fi
}

# $1 = temperature in millidegrees C, $2 = active profile
interpolate() {
	local t_milli=$1 prev_t="" prev_p=""
	while read -r tc pp; do
		[ -z "${tc:-}" ] && continue
		local pt=$(( tc * 1000 ))
		if [ "$t_milli" -le "$pt" ]; then
			if [ -z "$prev_t" ]; then
				echo "$pp"
				return
			fi
			echo $(( prev_p + (pp - prev_p) * (t_milli - prev_t) / (pt - prev_t) ))
			return
		fi
		prev_t=$pt
		prev_p=$pp
	done < <(load_points "$2")
	echo "$prev_p"
}

prev_profile=""
prev_override=""
temp_hist=()
last_pwm=$(cat "$PWM_OUTPUT" 2>/dev/null || echo 0)

while true; do
	profile=$(current_profile)
	if [ "$profile" != "$prev_profile" ]; then
		log "power profile is now '$profile', using $(basename "$(curve_file_for_profile "$profile")")"
		prev_profile="$profile"
	fi

	override="auto"
	if [ -r "$OVERRIDE_FILE" ]; then
		override=$(cat "$OVERRIDE_FILE")
		if [ "$override" != "auto" ] && ! [[ "$override" =~ ^[0-9]{1,3}$ && "$override" -le 255 ]]; then
			log "ignoring invalid override value '$override'"
			override="auto"
		fi
	fi
	if [ "$override" != "$prev_override" ]; then
		if [ "$override" = "auto" ]; then
			log "manual override cleared, back to curve"
		else
			log "manual override set to pwm=$override"
		fi
		prev_override="$override"
	fi

	if [ "$override" != "auto" ]; then
		target="$override"
	else
		temp=$(cat "$TEMP_INPUT")
		temp_hist+=("$temp")
		if [ "${#temp_hist[@]}" -gt "$TEMP_HISTORY_SIZE" ]; then
			temp_hist=("${temp_hist[@]:1}")
		fi
		sum=0
		for t in "${temp_hist[@]}"; do
			sum=$(( sum + t ))
		done
		avg_temp=$(( sum / ${#temp_hist[@]} ))
		target=$(interpolate "$avg_temp" "$profile")
	fi

	diff=$(( target - last_pwm ))
	if [ "$diff" -gt "$MAX_STEP_UP" ]; then
		pwm=$(( last_pwm + MAX_STEP_UP ))
	elif [ "$diff" -lt "-$MAX_STEP_DOWN" ]; then
		pwm=$(( last_pwm - MAX_STEP_DOWN ))
	else
		pwm="$target"
	fi
	last_pwm="$pwm"

	echo "$pwm" > "$PWM_OUTPUT"
	sleep "$INTERVAL"
done
