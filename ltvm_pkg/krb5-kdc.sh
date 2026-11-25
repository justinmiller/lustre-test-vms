#!/bin/bash
# The KDC for a Lustre cluster, run as root on one node by `ltvm cluster
# krb5`.  Usage: krb5-kdc.sh REALM "USER..." NODE...
#
# Creates the realm once, then for every node the lustre_{mgs,mds,oss,
# root}/<node> and host/<node> principals, exported to
# /root/krb5-keytabs/<node>.keytab, and a password principal for each
# user (krb5_login.sh kinit's with password = username).  Reruns keep
# the realm and its keys: -norandkey exports the current ones.
set -euo pipefail

REALM=$1
USERS=$2
shift 2
NODES="$*"

if command -v dnf >/dev/null 2>&1; then
	CONF_DIR=/var/kerberos/krb5kdc
	DB_DIR=/var/kerberos/krb5kdc
	KDC_SVC=krb5kdc
	EXTRA_REALM_LINES=""
	rpm -q krb5-server > /dev/null || dnf install -y -q krb5-server
elif command -v apt-get >/dev/null 2>&1; then
	CONF_DIR=/etc/krb5kdc
	DB_DIR=/var/lib/krb5kdc
	KDC_SVC=krb5-kdc
	# Debian's krb5-kdc.service sets ReadOnlyDirectories=/ and only
	# allows writes to /var/lib/krb5kdc; match Debian's stock layout.
	EXTRA_REALM_LINES="  database_name = $DB_DIR/principal
  key_stash_file = $CONF_DIR/stash"
	dpkg-query -W -f='${Status}\n' krb5-kdc krb5-admin-server 2>/dev/null \
		| grep -cx 'install ok installed' | grep -qx 2 || {
		DEBIAN_FRONTEND=noninteractive apt-get -qq update </dev/null
		DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
			krb5-kdc krb5-admin-server </dev/null
	}
	# Only kadmin.local is used from krb5-admin-server; keep kadmind off.
	systemctl disable --now krb5-admin-server.service 2>/dev/null || true
else
	echo "unsupported package manager (need dnf or apt-get)" >&2
	exit 1
fi

mkdir -p "$CONF_DIR" "$DB_DIR"
{
	cat <<EOF
[kdcdefaults]
 kdc_ports = 88
 kdc_tcp_ports = 88

[realms]
 $REALM = {
EOF
	if [[ -n "$EXTRA_REALM_LINES" ]]; then
		printf '%s\n' "$EXTRA_REALM_LINES"
	fi
	cat <<EOF
  acl_file = $CONF_DIR/kadm5.acl
  admin_keytab = $CONF_DIR/kadm5.keytab
  max_life = 24h
  max_renewable_life = 7d
 }
EOF
} > "$CONF_DIR/kdc.conf"
echo "*/admin@$REALM *" > "$CONF_DIR/kadm5.acl"

[[ -f "$DB_DIR/principal" ]] ||
	kdb5_util create -s -r "$REALM" -P "$(head -c 32 /dev/urandom | base64)"
systemctl enable -q "$KDC_SVC"
systemctl restart "$KDC_SVC"

has() {
	kadmin.local -q "getprinc $1" 2>/dev/null | grep -q '^Principal:'
}

mkdir -p /root/krb5-keytabs
for n in $NODES; do
	ps="lustre_mgs/$n lustre_mds/$n lustre_oss/$n lustre_root/$n host/$n"
	for p in $ps; do
		has "$p@$REALM" || kadmin.local -q "addprinc -randkey $p@$REALM"
	done
	rm -f /root/krb5-keytabs/$n.keytab
	for p in $ps; do
		kadmin.local -q \
			"ktadd -norandkey -k /root/krb5-keytabs/$n.keytab $p@$REALM"
	done
done
for u in $USERS; do
	has "$u@$REALM" || kadmin.local -q "addprinc -pw $u $u@$REALM"
done
