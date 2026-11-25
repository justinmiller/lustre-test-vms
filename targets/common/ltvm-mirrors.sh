#!/usr/bin/env bash
# Point a build's package repos at the site's mirrors, and back again.
#
# ltvm turns [mirrors] in /etc/ltvm.conf (ltvm_pkg/mirrors.py) into two
# build args and an optional build secret, which every
# container.Dockerfile and image.Dockerfile declares:
#
#   LTVM_MIRRORS         "<upstream> <mirror> [<mirror>...]" entries
#                        joined by ";": each repo URL starting with
#                        <upstream> is rewritten to start with the
#                        mirror(s) instead
#   LTVM_MIRROR_CA_HASH  "site:<hash>" for an explicit site CA, or
#                        "host" for the host's system CA bundle
#                        (used only to bootstrap https:// apt mirrors in
#                        minimal Debian/Ubuntu images without
#                        ca-certificates); empty when no CA is passed
#   secret ltvm_mirror_ca
#                        PEM bundle mounted at /run/secrets/ltvm_mirror_ca
#                        during apply (LTVM_MIRROR_CA_FILE overrides the
#                        path for tests)
#
# Both empty -- no [mirrors] -- and every subcommand is a no-op, so the
# build is the stock one.
#
#   apply     Rewrite the dnf, apt and zypper repo files that name an
#             upstream, noting each line it changes.  Safe to run again:
#             the rhel Dockerfiles do, after epel-release adds its repos.
#   restore   Undo those lines and drop the CA.  The image build's last
#             stage runs it, so a VM gets the distro's own repos: the
#             mirrors are for building, and the build container keeps
#             them for the dnf and apt its builds run.  Only apply's own
#             lines are undone, on the file as it is now, so what the
#             build changed in between stays: `dnf config-manager
#             --set-enabled crb` edits a file apply rewrote, and putting
#             back a copy saved before it would turn crb off again.
#   dnf-epel MAJOR ARGS...
#             Run dnf with epel taken from the epel-MAJOR branch rather
#             than the epel-z-MAJOR.N one a point release pins.
#
# Only the first mirror is used where the format has room for one:
# zypper's baseurl and dnf-epel.  dnf's baseurl and apt's deb822 URIs
# take them all, and a one-line apt source gets a line per mirror.
#
# LTVM_MIRRORS_ROOT prefixes every path, for the tests; with it set the
# trust-store tools are not run.
set -euo pipefail

ROOT=${LTVM_MIRRORS_ROOT:-}
STATE=$ROOT/var/lib/ltvm-mirrors
CA_DIR=/etc/ltvm-mirrors
CA_FILE=$CA_DIR/ca.pem
APT_CA_FILE=$CA_DIR/apt-ca.pem
CA_NAME=ltvm-mirror-ca
CA_SRC=${LTVM_MIRROR_CA_FILE:-/run/secrets/ltvm_mirror_ca}
HOST_CA_MARKER='# --- ltvm-host-ca-bundle ---'

UPS=()
MIRS=()
_ltvm_mirrors=${LTVM_MIRRORS:-}
if [ -n "$_ltvm_mirrors" ]; then
	IFS=';' read -r -a _entries <<<"$_ltvm_mirrors"
	for _e in "${_entries[@]}"; do
		read -r _up _rest <<<"$_e" || true
		[ -n "${_up:-}" ] && [ -n "${_rest:-}" ] || continue
		UPS+=("$_up")
		MIRS+=("$_rest")
	done
fi

# Set REPLY to URL rewritten for each of its upstream's mirrors (space
# separated); fail when no upstream is a prefix of it.  An upstream
# matches only whole path components, so .../debian does not take
# .../debian-security.
rewrite() {
	local url=$1 i up tail m out
	REPLY=
	for ((i = 0; i < ${#UPS[@]}; i++)); do
		up=${UPS[i]}
		case $url in
		"$up") tail= ;;
		"$up"/*) tail=${url#"$up"} ;;
		*) continue ;;
		esac
		out=
		for m in ${MIRS[i]}; do
			out="$out${out:+ }$m$tail"
		done
		REPLY=$out
		return 0
	done
	return 1
}

# Note in LOG (FILE.ltvm-edits) that a rewrite of FILE wrote line NEW
# where it had OLD, in the section SEC (the last "[...]" line before it,
# "" for none), for restore to undo.  OLD "-" means NEW was added, and
# restore drops it; a real OLD line is stored with a "=" in front.
edit() {
	printf '%s\n%s\n%s\n' "$2" "$3" "$4" >>"$1"
}

# Replace FILE's contents with those of FILE.ltvm-new when the rewrite
# changed a line, keeping FILE's inode and mode, and add its edits to
# FILE's record.  The record says whether anything changed, not cmp:
# apply runs first in a build, and rockylinux's base images have neither
# diffutils nor findutils.
replace() {
	local f=$1 rel=${1#"$ROOT"}
	if [ -s "$f.ltvm-edits" ]; then
		mkdir -p "$(dirname "$STATE/edits$rel")"
		cat "$f.ltvm-edits" >>"$STATE/edits$rel"
		noted "$rel" edited
		cat "$f.ltvm-new" >"$f"
	fi
	rm -f "$f.ltvm-new" "$f.ltvm-edits"
}

# Undo the edits recorded for one file, wherever its lines now are.  A
# line the build changed since apply matches no record and is kept, as
# is a file a package upgrade replaced.  Each edit record is consumed
# once so two stanzas rewritten to the same mirror URL restore to their
# own distinct upstream URLs.
undo_edits() {
	local rec=$1 f=$ROOT${1#"$STATE/edits"} line sec="" i a b c
	local -a secs=() news=() olds=() used=()
	while IFS= read -r a && IFS= read -r b && IFS= read -r c; do
		secs+=("$a")
		news+=("$b")
		olds+=("$c")
		used+=(0)
	done <"$rec"
	[ -f "$f" ] || return 0
	while IFS= read -r line || [ -n "$line" ]; do
		case $line in
		'['*) sec=$line ;;
		esac
		for ((i = 0; i < ${#news[@]}; i++)); do
			[ "${used[i]}" -eq 0 ] || continue
			[ "${secs[i]}" = "$sec" ] && [ "${news[i]}" = "$line" ] || continue
			used[i]=1
			[ "${olds[i]}" != - ] || continue 2
			line=${olds[i]#=}
			break
		done
		printf '%s\n' "$line"
	done <"$f" >"$f.ltvm-new"
	cat "$f.ltvm-new" >"$f"
	rm -f "$f.ltvm-new"
}

# Add a path to the list LIST in $STATE, once.
noted() {
	mkdir -p "$STATE"
	grep -qxF -- "$1" "$STATE/$2" 2>/dev/null || echo "$1" >>"$STATE/$2"
}

# Note a file apply made, for restore to remove.
created() {
	noted "$1" created
}

in_list() {
	local x=$1 y
	shift
	for y in "$@"; do
		[ "$x" = "$y" ] && return 0
	done
	return 1
}

# dnf: a section whose baseurl -- live or commented out, as Rocky and
# EPEL ship it -- names an upstream gets that baseurl, rewritten and
# live, and its mirrorlist and metalink commented out, since dnf would
# take those over a baseurl.
rewrite_dnf() {
	local f=$1 log=$1.ltvm-edits line new sec=
	local -a hit=()
	while IFS= read -r line || [ -n "$line" ]; do
		case $line in
		'['*) sec=$line ;;
		baseurl=* | '#baseurl='*)
			if rewrite "${line#*baseurl=}"; then
				hit+=("$sec")
			fi
			;;
		esac
	done <"$f"
	[ ${#hit[@]} -gt 0 ] || return 0
	sec=
	while IFS= read -r line || [ -n "$line" ]; do
		case $line in
		'['*) sec=$line ;;
		esac
		new=$line
		if [ -n "$sec" ] && in_list "$sec" "${hit[@]}"; then
			case $line in
			baseurl=* | '#baseurl='*)
				if rewrite "${line#*baseurl=}"; then
					new="baseurl=$REPLY"
				fi
				;;
			mirrorlist=* | metalink=*) new="#$line" ;;
			esac
		fi
		[ "$new" = "$line" ] || edit "$log" "$sec" "$new" "=$line"
		printf '%s\n' "$new"
	done <"$f" >"$f.ltvm-new"
	replace "$f"
}

# apt, deb822 (.sources): every URI on a URIs: line.
rewrite_deb822() {
	local f=$1 log=$1.ltvm-edits line new uri out
	while IFS= read -r line || [ -n "$line" ]; do
		new=$line
		case $line in
		URIs:* | uris:*)
			out=
			for uri in ${line#*:}; do
				if rewrite "$uri"; then
					uri=$REPLY
				fi
				out="$out $uri"
			done
			new="URIs:$out"
			;;
		esac
		[ "$new" = "$line" ] || edit "$log" "" "$new" "=$line"
		printf '%s\n' "$new"
	done <"$f" >"$f.ltvm-new"
	replace "$f"
}

# apt, one-line (sources.list, .list): one line per mirror.
rewrite_list() {
	local f=$1 log=$1.ltvm-edits line m new old
	local re='^(deb(-src)?[[:space:]]+(\[[^]]*\][[:space:]]+)?)([^[:space:]]+)(.*)$'
	while IFS= read -r line || [ -n "$line" ]; do
		if [[ $line =~ $re ]] && rewrite "${BASH_REMATCH[4]}"; then
			old="=$line"
			for m in $REPLY; do
				new=${BASH_REMATCH[1]}$m${BASH_REMATCH[5]}
				edit "$log" "" "$new" "$old"
				old=-
				printf '%s\n' "$new"
			done
			continue
		fi
		printf '%s\n' "$line"
	done <"$f" >"$f.ltvm-new"
	replace "$f"
}

# zypper: baseurl takes one URL.
rewrite_zypp() {
	local f=$1 log=$1.ltvm-edits line new sec=
	while IFS= read -r line || [ -n "$line" ]; do
		new=$line
		case $line in
		'['*) sec=$line ;;
		baseurl=*)
			if rewrite "${line#baseurl=}"; then
				new="baseurl=${REPLY%% *}"
			fi
			;;
		esac
		[ "$new" = "$line" ] || edit "$log" "$sec" "$new" "=$line"
		printf '%s\n' "$new"
	done <"$f" >"$f.ltvm-new"
	replace "$f"
}

# Put the CA where each trust store looks.  apt is also told, per mirror
# host, to verify against it directly: Debian/Ubuntu base images ship no
# ca-certificates, and apt needs the CA before it can install that.
# Mode is "site" (explicit site CA -> install everywhere) or "host"
# (host's system CA bundle -> apt CaInfo only, and only when the base
# image has no /etc/ssl/certs/ca-certificates.crt yet).  When mode is
# "site" and the secret appends the host CA bundle after HOST_CA_MARKER,
# system trust stores receive only the site CA while apt's CaInfo gets
# the combined bundle so public-CA https:// mirrors work too.
install_ca() {
	local mode=$1 ms m host apt_ca=$CA_FILE
	[ -r "$CA_SRC" ] || return 0
	if [ "$mode" = host ]; then
		[ -d "$ROOT/etc/apt" ] || return 0
		[ ! -e "$ROOT/etc/ssl/certs/ca-certificates.crt" ] || return 0
	fi
	mkdir -p "$ROOT$CA_DIR"
	if [ "$mode" = site ] && grep -qxF -- "$HOST_CA_MARKER" "$CA_SRC"; then
		sed "/^$HOST_CA_MARKER$/,\$d" "$CA_SRC" >"$ROOT$CA_FILE"
		if [ -d "$ROOT/etc/apt" ]; then
			cp "$CA_SRC" "$ROOT$APT_CA_FILE"
			chmod 0644 "$ROOT$APT_CA_FILE"
			created "$APT_CA_FILE"
			apt_ca=$APT_CA_FILE
		fi
	else
		cp "$CA_SRC" "$ROOT$CA_FILE"
	fi
	# Build secrets mount 0400 root:root, and apt's https method runs as
	# the unprivileged _apt user.
	chmod 0644 "$ROOT$CA_FILE"
	created "$CA_FILE"
	if [ -d "$ROOT/etc/apt" ]; then
		mkdir -p "$ROOT/etc/apt/apt.conf.d"
		for ms in ${MIRS[@]+"${MIRS[@]}"}; do
			for m in $ms; do
				case $m in https://*) ;; *) continue ;; esac
				host=${m#https://}
				host=${host%%/*}
				host=${host%%:*}
				printf 'Acquire::https::%s::CaInfo "%s";\n' "$host" "$apt_ca"
			done
		done | sort -u >"$ROOT/etc/apt/apt.conf.d/99ltvm-mirrors"
		created /etc/apt/apt.conf.d/99ltvm-mirrors
		if [ "$mode" = site ]; then
			# ca-certificates' own postinst picks this up when it is
			# installed later.
			mkdir -p "$ROOT/usr/local/share/ca-certificates"
			cp "$ROOT$CA_FILE" "$ROOT/usr/local/share/ca-certificates/$CA_NAME.crt"
			chmod 0644 "$ROOT/usr/local/share/ca-certificates/$CA_NAME.crt"
			created "/usr/local/share/ca-certificates/$CA_NAME.crt"
		fi
	fi
	[ "$mode" = site ] || return 0
	if [ -d "$ROOT/etc/pki/ca-trust/source/anchors" ]; then
		cp "$ROOT$CA_FILE" "$ROOT/etc/pki/ca-trust/source/anchors/$CA_NAME.pem"
		chmod 0644 "$ROOT/etc/pki/ca-trust/source/anchors/$CA_NAME.pem"
		created "/etc/pki/ca-trust/source/anchors/$CA_NAME.pem"
	fi
	if [ -d "$ROOT/etc/pki/trust/anchors" ]; then
		cp "$ROOT$CA_FILE" "$ROOT/etc/pki/trust/anchors/$CA_NAME.pem"
		chmod 0644 "$ROOT/etc/pki/trust/anchors/$CA_NAME.pem"
		created "/etc/pki/trust/anchors/$CA_NAME.pem"
	fi
	update_trust
}

update_trust() {
	[ -z "$ROOT" ] || return 0
	if command -v update-ca-trust >/dev/null 2>&1; then
		update-ca-trust extract
	elif command -v update-ca-certificates >/dev/null 2>&1; then
		update-ca-certificates --fresh >/dev/null
	fi
}

cmd_apply() {
	local f ca_mode
	[ ${#UPS[@]} -gt 0 ] || [ -n "${LTVM_MIRROR_CA_HASH:-}" ] || [ -r "$CA_SRC" ] || return 0
	mkdir -p "$STATE"
	if [ ${#UPS[@]} -gt 0 ]; then
		for f in "$ROOT"/etc/yum.repos.d/*.repo; do
			[ -f "$f" ] || continue
			rewrite_dnf "$f"
		done
		for f in "$ROOT"/etc/apt/sources.list.d/*.sources; do
			[ -f "$f" ] || continue
			rewrite_deb822 "$f"
		done
		for f in "$ROOT"/etc/apt/sources.list "$ROOT"/etc/apt/sources.list.d/*.list; do
			[ -f "$f" ] || continue
			rewrite_list "$f"
		done
		for f in "$ROOT"/etc/zypp/repos.d/*.repo; do
			[ -f "$f" ] || continue
			rewrite_zypp "$f"
		done
	fi
	if [ -n "${LTVM_MIRROR_CA_HASH:-}" ] && [ ! -e "$ROOT$CA_FILE" ]; then
		case ${LTVM_MIRROR_CA_HASH} in
		host | host:*) ca_mode=host ;;
		*) ca_mode=site ;;
		esac
		if [ "$ca_mode" = site ] \
			|| { [ -d "$ROOT/etc/apt" ] && [ ! -e "$ROOT/etc/ssl/certs/ca-certificates.crt" ]; }; then
			if [ ! -r "$CA_SRC" ]; then
				echo "ltvm-mirrors: CA configured (LTVM_MIRROR_CA_HASH=$LTVM_MIRROR_CA_HASH) but secret $CA_SRC is missing" >&2
				exit 1
			fi
			install_ca "$ca_mode"
		fi
	elif [ -r "$CA_SRC" ] && [ ! -e "$ROOT$CA_FILE" ]; then
		install_ca site
	fi
	echo "ltvm-mirrors: repos now point at the site mirrors:"
	grep -rhE '^(baseurl=|URIs:|deb )' "$ROOT"/etc/yum.repos.d "$ROOT"/etc/apt \
		"$ROOT"/etc/zypp/repos.d 2>/dev/null | sort -u | sed 's/^/  /' || true
}

cmd_restore() {
	local f rel
	[ -d "$STATE" ] || return 0
	if [ -f "$STATE/edited" ]; then
		while IFS= read -r f; do
			undo_edits "$STATE/edits$f"
		done <"$STATE/edited"
	fi
	if [ -f "$STATE/created" ]; then
		while IFS= read -r rel; do
			rm -f "$ROOT$rel"
		done <"$STATE/created"
	fi
	rmdir "$ROOT$CA_DIR" 2>/dev/null || true
	# Package indexes and logs from the build carry the mirror URLs.
	rm -rf "$ROOT"/var/lib/apt/lists/*_* "$ROOT"/var/cache/dnf/* "$ROOT"/var/cache/zypp/* \
		"$ROOT"/var/log/dnf*.log "$ROOT"/var/log/hawkey.log "$ROOT"/var/log/zypper.log \
		"$ROOT"/var/log/apt/*
	rm -rf "$STATE"
	update_trust
}

cmd_dnf_epel() {
	local major=$1
	shift
	if rewrite "https://download.example/pub/epel/$major/Everything/\$basearch/"; then
		exec dnf --setopt=epel.metalink= "--setopt=epel.baseurl=${REPLY%% *}" "$@"
	fi
	exec dnf "--setopt=epel.metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-$major&arch=\$basearch" "$@"
}

case ${1:-} in
apply) cmd_apply ;;
restore) cmd_restore ;;
dnf-epel)
	shift
	cmd_dnf_epel "$@"
	;;
*)
	echo "usage: $0 apply | restore | dnf-epel MAJOR DNF-ARGS..." >&2
	exit 2
	;;
esac
