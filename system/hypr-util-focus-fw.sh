#!/bin/bash
# Root helper: IP-level blocking for Focus mode via a dedicated nftables
# table, layered on top of the /etc/hosts domain block
# (hypr-util-focus-hosts.sh) rather than replacing it -- the hosts file
# only affects *fresh* DNS resolutions, so a browser that already
# resolved/cached a blocked domain's IP before focus mode turned on (or
# that reuses an already-open connection) keeps reaching it straight
# through, bypassing the hosts-file change entirely. Dropping the
# resolved IP itself at the network layer closes that gap, and
# incidentally also blocks any other hostname/CDN edge that happens to
# share the same IP.
#
# Installed to /usr/local/bin/hypr-util-focus-fw by setup.sh
# (install_focus_blocking_helpers) and invoked via `pkexec` under the same
# passwordless polkit rule as the hosts helper (see 49-hypr-util-focus.rules).
#
# Usage:
#   hypr-util-focus-fw on   < list-of-IPs (v4 or v6), one per line, on stdin
#   hypr-util-focus-fw off
set -euo pipefail

TABLE_SPEC="inet hyprutil_focus"

is_valid_ipv4() {
	[[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
	local IFS=.
	local -a octets=($1)
	local o
	for o in "${octets[@]}"; do
		# Force base-10: bash's [ x -le y ] parses a leading-zero operand
		# ("08", "09") as octal, and "8"/"9" aren't valid octal digits --
		# that's "value too great for base" (nonzero exit), which aborts
		# the whole script under set -e before any block is applied. Real
		# IPs won't have leading zeros, but this keeps odd input from
		# taking the helper down entirely.
		[ "$((10#$o))" -le 255 ] || return 1
	done
	return 0
}

is_valid_ipv6() {
	# Bounded to a plausible IPv6 literal shape: 2-8 groups of 1-4 hex
	# digits, with at most one "::" run-length compression, optionally
	# ending in an embedded IPv4 literal. The previous check
	# (^[0-9a-fA-F:]+$ plus "contains a colon") accepted garbage like
	# "12345:1", ":::", or a 9-group address -- since every accepted
	# address goes into the *same* atomic `nft -f -` batch as the valid
	# entries, one bad literal fails the whole batch and silently disables
	# **all** IP-level blocking (v4 included) while focus.json still says
	# "on". This still isn't a full RFC 4291 validator, but it rejects the
	# malformed shapes above before they ever reach nft.
	local addr="$1"
	[[ "$addr" == *:* ]] || return 1
	[[ "$addr" =~ ^[0-9a-fA-F:.]+$ ]] || return 1
	# At most one "::" compression marker.
	local double_colons
	double_colons=$(grep -o "::" <<<"$addr" | wc -l)
	[ "$double_colons" -le 1 ] || return 1
	if [[ "$addr" == *::* ]]; then
		[[ "$addr" =~ ^([0-9a-fA-F]{1,4}(:[0-9a-fA-F]{1,4})*)?::([0-9a-fA-F]{1,4}(:[0-9a-fA-F]{1,4})*)?$ ]] || return 1
	else
		local -a groups
		IFS=: read -ra groups <<<"$addr"
		[ "${#groups[@]}" -ge 2 ] && [ "${#groups[@]}" -le 8 ] || return 1
		local g
		for g in "${groups[@]}"; do
			[[ "$g" =~ ^[0-9a-fA-F]{1,4}$ ]] || return 1
		done
	fi
	return 0
}

case "${1:-}" in
on)
	v4=()
	v6=()
	while IFS= read -r ip; do
		ip="${ip%$'\r'}"
		ip="$(echo -n "$ip" | tr -d '[:space:]')"
		[ -z "$ip" ] && continue
		if is_valid_ipv4 "$ip"; then
			v4+=("$ip")
		elif is_valid_ipv6 "$ip"; then
			v6+=("$ip")
		fi
	done

	{
		echo "add table $TABLE_SPEC"
		# Flush before re-adding so switching focus profiles (a different
		# site list) never leaves a stale IP blocked from the previous set.
		echo "flush table $TABLE_SPEC"
		echo "add chain $TABLE_SPEC output { type filter hook output priority filter; policy accept; }"
		if [ "${#v4[@]}" -gt 0 ]; then
			printf -v v4_set '%s,' "${v4[@]}"
			echo "add rule $TABLE_SPEC output ip daddr { ${v4_set%,} } drop"
		fi
		if [ "${#v6[@]}" -gt 0 ]; then
			printf -v v6_set '%s,' "${v6[@]}"
			echo "add rule $TABLE_SPEC output ip6 daddr { ${v6_set%,} } drop"
		fi
	} | nft -f -
	;;
off)
	nft delete table $TABLE_SPEC 2>/dev/null || true
	;;
*)
	echo "usage: $0 on|off" >&2
	exit 1
	;;
esac
