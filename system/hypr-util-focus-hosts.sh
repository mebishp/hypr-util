#!/bin/bash
# Root helper: adds/removes a marked block of site-blocking entries in
# /etc/hosts for Focus mode.
#
# Installed to /usr/local/bin/hypr-util-focus-hosts by setup.sh
# (install_focus_hosts_helper) and invoked via `pkexec` under a
# passwordless polkit rule scoped to exactly this binary path (see
# 49-hypr-util-focus.rules) -- the caller is the automation daemon
# (automation.py's FocusController), which runs unattended and has no way
# to answer an interactive pkexec password prompt.
#
# Usage:
#   hypr-util-focus-hosts on   < list-of-domains, one per line, on stdin
#   hypr-util-focus-hosts off
set -euo pipefail

HOSTS_FILE="/etc/hosts"
BEGIN_MARKER="# >>> hypr-util focus mode -- do not edit between these lines"
END_MARKER="# <<< hypr-util focus mode"

strip_block() {
	# Emits $HOSTS_FILE with any existing marked block removed, so re-running
	# "on" (e.g. switching focus profiles) never leaves stale/duplicate
	# entries behind.
	awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
		$0 == begin { skip = 1; next }
		$0 == end { skip = 0; next }
		!skip { print }
	' "$HOSTS_FILE"
}

is_valid_hostname() {
	# Conservative allowlist (letters, digits, dots, hyphens) -- rejects
	# anything that isn't plainly a hostname, so a malformed/hostile line on
	# stdin can't inject extra /etc/hosts entries beyond the one intended.
	[[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

# /etc/hosts is read by every process doing DNS resolution on the system, so
# it must never be left truncated. A plain "producer | write-to-tmp | mv" put
# the mv one pipeline stage removed from the producer's own exit status: with
# `set -o pipefail` a failure downstream of a dead producer (e.g. ENOSPC
# mid-awk) does make the *pipeline's* exit code nonzero, but only after the
# consuming stage has already read whatever partial output it got, `chmod`ed
# it, and `mv`ed it into place -- the truncated file is committed before `set
# -e` ever gets a chance to react. Redirecting the producer group directly
# into the tmp file and checking *its own* exit status right here closes that
# gap: a mid-stream failure aborts before mv ever runs.
tmp=$(mktemp "${HOSTS_FILE}.XXXXXX")
case "${1:-}" in
on)
	if ! {
		strip_block
		echo "$BEGIN_MARKER"
		while IFS= read -r domain; do
			domain="${domain%$'\r'}"
			domain="$(echo -n "$domain" | tr -d '[:space:]')"
			[ -z "$domain" ] && continue
			is_valid_hostname "$domain" || continue
			echo "127.0.0.1 $domain"
			echo "::1 $domain"
			case "$domain" in
			www.*) ;;
			*)
				echo "127.0.0.1 www.$domain"
				echo "::1 www.$domain"
				;;
			esac
		done
		echo "$END_MARKER"
	} >"$tmp"; then
		rm -f "$tmp"
		echo "hypr-util-focus-hosts: failed to build new /etc/hosts, aborting" >&2
		exit 1
	fi
	;;
off)
	if ! strip_block >"$tmp"; then
		rm -f "$tmp"
		echo "hypr-util-focus-hosts: failed to build new /etc/hosts, aborting" >&2
		exit 1
	fi
	;;
*)
	rm -f "$tmp"
	echo "usage: $0 on|off" >&2
	exit 1
	;;
esac
chmod 644 "$tmp"
mv -f "$tmp" "$HOSTS_FILE"
