#!/bin/bash
# Kerberos client configuration for one Lustre node, run as root by
# `ltvm cluster krb5`.  Usage: krb5-node.sh REALM KDC_HOST
set -euo pipefail

REALM=$1
KDC=$2

# in images built since they joined packages-test.txt; install for older ones
if command -v dnf >/dev/null 2>&1; then
	pkgs="krb5-workstation sssd-kcm keyutils"
	KDC_LOG="FILE:/var/log/krb5kdc.log"
	rpm -q $pkgs > /dev/null || dnf install -y -q $pkgs
elif command -v apt-get >/dev/null 2>&1; then
	pkgs="krb5-user sssd-kcm keyutils"
	# Debian's krb5-kdc.service sets ReadOnlyDirectories=/, so /var/log is
	# read-only for the KDC; send KDC logs to syslog/journald instead.
	KDC_LOG="SYSLOG:INFO:DAEMON"
	dpkg-query -W -f='${Status}\n' $pkgs 2>/dev/null | grep -cx 'install ok installed' \
		| grep -qx 3 || {
		DEBIAN_FRONTEND=noninteractive apt-get -qq update </dev/null
		DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $pkgs </dev/null
	}
else
	echo "unsupported package manager (need dnf or apt-get)" >&2
	exit 1
fi

# sssd-kcm's drop-in makes KCM: the default ccache ahead of anything
# krb5.conf says; keep the keyring default (sanity-krb5 test_11 asks for
# KCM itself).
mkdir -p /etc/krb5.conf.d
if [[ -f /etc/krb5.conf.d/kcm_default_ccache ]]; then
	sed -i 's/^[[:space:]]*default_ccache_name/#&/' \
		/etc/krb5.conf.d/kcm_default_ccache
fi
systemctl enable -q --now sssd-kcm.socket

mkdir -p /etc/request-key.d
echo "create lgssc * * $(command -v lgss_keyring) %o %k %t %d %c %u %g %T %P %S" \
	> /etc/request-key.d/lgssc.conf

# Lustre names a node's principals after the reverse lookup of its NID.
ip=$(ip -4 -o route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
name=$(getent hosts "$ip" | awk '{print $2}')
[[ "$name" == "$(hostname -s)" ]] ||
	{ echo "reverse lookup of $ip is '$name', not $(hostname -s)"; exit 1; }

# One default_realm line, and ticket_lifetime indented: sanity-krb5
# greps the former, and test_200/201 rewrite the latter with
# 's+[^#]ticket_lifetime.*+...+'.
cat > /etc/krb5.conf <<EOF
includedir /etc/krb5.conf.d/

[logging]
 default = FILE:/var/log/krb5libs.log
 kdc = $KDC_LOG
 admin_server = FILE:/var/log/kadmind.log

[libdefaults]
 default_realm = $REALM
 dns_lookup_realm = false
 dns_lookup_kdc = false
 ticket_lifetime = 24h
 renew_lifetime = 7d
 forwardable = true
 rdns = false
 dns_canonicalize_hostname = false
 qualify_shortname = ""
 default_ccache_name = KEYRING:persistent:%{uid}

[realms]
 $REALM = {
  kdc = $KDC
  admin_server = $KDC
 }
EOF
