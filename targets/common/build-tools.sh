#!/usr/bin/env bash
# Build and install source-built tools used in VM images:
#   IOR + mdtest, simul, metabench, iozone, pjdfstest, dbench loadfile,
#   FlameGraph, drgn
#
# Expects gcc, make, autoconf, automake, libtool, curl, pip3
# to already be installed (via the package list install step).
# git is installed here as a build-time dep (not in the VM image packages).
#
# Cross-compilation support:
#   Set TARGET_ARCH to cross-compile (e.g. TARGET_ARCH=aarch64).
#   The script detects the host arch and sets up CC/--host
#   accordingly. When not cross-compiling, builds natively.
#
# Output:
#   By default, installs to /usr/local/bin.
#   Set DESTDIR to redirect (e.g. DESTDIR=/output for staged builds).
set -euo pipefail

# Pinned versions of source-built tools.  Bump in one place.
IOR_VERSION="${IOR_VERSION:-4.0.0}"
IOZONE_VERSION="${IOZONE_VERSION:-3_506}"
SIMUL_VERSION="${SIMUL_VERSION:-1.16}"
COMPILEBENCH_REF="${COMPILEBENCH_REF:-28ad3d580d84428beeda5c857f7c4b42bd90f9eb}"
CTHON04_REF="${CTHON04_REF:-86a4501a6e1e415844dc632894a85a5253cc1505}"
TOOLKIT_REF="${TOOLKIT_REF:-3a62943ca32ddc63cac90679f2f43a38b608ea68}"
DBENCH_LOADFILE_REF="${DBENCH_LOADFILE_REF:-a8e1c0fbb8bdb23ee22c0cc2e3f9b1049e537fff}"
VMTOUCH_REF="${VMTOUCH_REF:-af86e27675843b3c7e4ddfee66ddbaf44eff43c4}"
PJDFSTEST_REF="${PJDFSTEST_REF:-85a8aea9e685999ef0540392fd80535f873d7ff7}"
FLAMEGRAPH_REF="${FLAMEGRAPH_REF:-41fee1f99f9276008b7cd112fca19dc3ea84ac32}"

# Optional download cache directory (e.g. podman --mount=type=cache at
# /var/cache/ltvm-tools).  Unset or empty disables caching.
TOOLS_CACHE_DIR="${LTVM_TOOLS_CACHE:-}"

_sha256_check() {
	local expected="$1" file="$2" got
	if command -v sha256sum >/dev/null 2>&1; then
		got=$(sha256sum "$file" | awk '{print $1}')
	else
		got=$(shasum -a 256 "$file" | awk '{print $1}')
	fi
	[[ "$got" == "$expected" ]]
}

_cache_put() {
	local src="$1" name="$2"
	[[ -n "$TOOLS_CACHE_DIR" ]] || return 0
	mkdir -p "$TOOLS_CACHE_DIR" 2>/dev/null || return 0
	local tmp
	tmp=$(mktemp "${TOOLS_CACHE_DIR}/.${name}.XXXXXX" 2>/dev/null || true)
	[[ -n "$tmp" ]] || return 0
	if cp "$src" "$tmp" 2>/dev/null \
	    && chmod 0644 "$tmp" 2>/dev/null \
	    && mv -f "$tmp" "${TOOLS_CACHE_DIR}/${name}" 2>/dev/null; then
		:
	else
		rm -f "$tmp" 2>/dev/null || true
	fi
}

# Fetch a plain file (with optional sha256 verification) into $3.
fetch_file() {
	local name="$1" url="$2" dest="$3" sha256="${4:-}"
	local cached="${TOOLS_CACHE_DIR:+${TOOLS_CACHE_DIR}/${name}}"
	if [[ -n "$cached" && -s "$cached" ]]; then
		if { [[ -z "$sha256" ]] || _sha256_check "$sha256" "$cached"; } \
		    && cp "$cached" "$dest" 2>/dev/null; then
			return 0
		fi
	fi
	local tmp="${dest}.tmp.$$"
	if ! curl -fsSL --retry 3 --connect-timeout 30 --max-time 300 "$url" -o "$tmp"; then
		rm -f "$tmp"
		return 1
	fi
	if [[ -n "$sha256" ]] && ! _sha256_check "$sha256" "$tmp"; then
		echo "ERROR: sha256 mismatch for ${name}" >&2
		rm -f "$tmp"
		return 1
	fi
	mv -f "$tmp" "$dest"
	_cache_put "$dest" "$name"
}

# Fetch and extract a tar archive ($3 = tar flags, e.g. -xzf or -xf) into $4.
# Validates cached archives before extraction and re-downloads if corrupt.
fetch_tar() {
	local name="$1" url="$2" tar_flags="$3" dest_dir="$4" sha256="${5:-}"
	local test_flags="${tar_flags/x/t}"
	local cached="${TOOLS_CACHE_DIR:+${TOOLS_CACHE_DIR}/${name}}"
	mkdir -p "$dest_dir"
	if [[ -n "$cached" && -s "$cached" ]]; then
		if { [[ -z "$sha256" ]] || _sha256_check "$sha256" "$cached"; } \
		    && tar "$test_flags" "$cached" >/dev/null 2>&1 \
		    && tar "$tar_flags" "$cached" -C "$dest_dir" 2>/dev/null; then
			return 0
		fi
	fi
	local tmp="/tmp/${name}.tmp.$$"
	if ! curl -fsSL --retry 3 --connect-timeout 30 --max-time 300 "$url" -o "$tmp"; then
		rm -f "$tmp"
		return 1
	fi
	if [[ -n "$sha256" ]] && ! _sha256_check "$sha256" "$tmp"; then
		echo "ERROR: sha256 mismatch for ${name}" >&2
		rm -f "$tmp"
		return 1
	fi
	if ! tar "$tar_flags" "$tmp" -C "$dest_dir"; then
		rm -f "$tmp"
		return 1
	fi
	_cache_put "$tmp" "$name"
	rm -f "$tmp"
}

# Fetch a git tree ($3 = commit ref, or empty for shallow HEAD) into $4,
# caching the extracted tree (without .git) as a .tar.gz in TOOLS_CACHE_DIR.
fetch_git_tree() {
	local name="$1" url="$2" ref="$3" dest_dir="$4"
	local archive="${name}.tar.gz"
	local cached="${TOOLS_CACHE_DIR:+${TOOLS_CACHE_DIR}/${archive}}"
	rm -rf "$dest_dir"
	mkdir -p "$dest_dir"
	if [[ -n "$cached" && -s "$cached" ]]; then
		if tar -tzf "$cached" >/dev/null 2>&1; then
			tar -xzf "$cached" -C "$dest_dir"
			return 0
		fi
		rm -f "$cached"
	fi
	local repo="/tmp/${name}.git.$$"
	rm -rf "$repo"
	if [[ -n "$ref" ]]; then
		git clone -q "$url" "$repo"
		git -C "$repo" checkout -q "$ref"
	else
		git clone -q --depth 1 "$url" "$repo"
	fi
	rm -rf "${repo}/.git"
	if [[ -n "$TOOLS_CACHE_DIR" ]]; then
		local tmp="/tmp/${archive}.tmp.$$"
		tar -czf "$tmp" -C "$repo" .
		_cache_put "$tmp" "$archive"
		rm -f "$tmp"
	fi
	cp -a "${repo}/." "$dest_dir/"
	rm -rf "$repo"
}

# Allow unit tests to source the fetch helpers without running the build.
[[ -z "${LTVM_BUILD_TOOLS_FUNCTIONS_ONLY:-}" ]] || return 0

TARGET_ARCH="${TARGET_ARCH:-$(uname -m)}"
HOST_ARCH="$(uname -m)"
DESTDIR="${DESTDIR:-}"
PREFIX="${DESTDIR}/usr/local"

# Cross-compilation setup (only set CC/CXX when cross-compiling;
# leave them unset for native builds so mpicc wrappers work)
CONFIGURE_HOST=""

CROSS_TRIPLE=""
if [[ "$TARGET_ARCH" == "aarch64" && "$HOST_ARCH" != "aarch64" ]]; then
	CROSS_TRIPLE="aarch64-linux-gnu"
elif [[ "$TARGET_ARCH" == "x86_64" && "$HOST_ARCH" != "x86_64" ]]; then
	CROSS_TRIPLE="x86_64-linux-gnu"
fi
if [[ -n "$CROSS_TRIPLE" ]]; then
	CONFIGURE_HOST="--host=$CROSS_TRIPLE"
	# RHEL cross gccs ship without a sysroot or default include path;
	# point them at one if the caller provides it.  The Debian path
	# uses multiarch, so SYSROOT stays unset there.
	if [[ -n "${SYSROOT:-}" ]]; then
		export CC="${CROSS_TRIPLE}-gcc --sysroot=${SYSROOT} -isystem ${SYSROOT}/usr/include"
		export CXX="${CROSS_TRIPLE}-g++ --sysroot=${SYSROOT} -isystem ${SYSROOT}/usr/include"
	else
		export CC="${CROSS_TRIPLE}-gcc"
		export CXX="${CROSS_TRIPLE}-g++"
	fi
	echo "--- Cross-compiling tools: ${HOST_ARCH} -> ${TARGET_ARCH}"
fi

# Ensure build deps are present (may have been skipped by --skip-broken)
if command -v dnf &>/dev/null; then
	dnf -y install gcc gcc-c++ make autoconf automake libtool git curl patch \
		python3-pip 2>/dev/null || true
elif command -v apt-get &>/dev/null; then
	apt-get update && apt-get install -y gcc g++ make autoconf automake patch \
		libtool git curl python3-pip 2>/dev/null || true
fi

# Install cross-compiler if cross-compiling.  Cross direction is
# implied by TARGET_ARCH vs HOST_ARCH (already captured in
# CONFIGURE_HOST above).  The triple in CONFIGURE_HOST is
# --host=<triple>; derive the package-name stem from it so we support
# either direction (aarch64 target from x86 host, x86 target from
# aarch64 host).
if [[ -n "$CONFIGURE_HOST" ]]; then
	CROSS_TRIPLE="${CONFIGURE_HOST#--host=}"
	if command -v dnf &>/dev/null; then
		dnf -y install "gcc-${CROSS_TRIPLE}" "binutils-${CROSS_TRIPLE}" 2>/dev/null || true
	elif command -v apt-get &>/dev/null; then
		# Debian's cross package naming uses x86-64-linux-gnu (hyphen)
		# rather than the RHEL x86_64-linux-gnu (underscore).
		APT_TRIPLE="${CROSS_TRIPLE//x86_64/x86-64}"
		apt-get install -y "gcc-${APT_TRIPLE}" "g++-${APT_TRIPLE}" 2>/dev/null || true
	fi
fi

mkdir -p "$PREFIX/bin"
cd /tmp

# IOR + mdtest
#
# IOR's configure auto-detects MPI via mpicc.  We have no cross-arch
# MPI in the build container (cross-building openmpi means cross-building
# libfabric + ucx + ...), so skip IOR/mdtest on cross builds; install
# them inside the VM via dnf if they're needed at test time.
#
# When the previous form of this section failed during `./configure`,
# `set -e` did *not* abort because a failing middle clause in a
# `cd && configure && make` chain is shielded by &&.  The chain has
# been split into separate commands so any failure is caught.
if [[ -z "$CROSS_TRIPLE" ]]; then
	# Add openmpi to PATH if available (EL installs to /usr/lib64/openmpi/bin)
	if [[ -d /usr/lib64/openmpi/bin ]]; then
		export PATH=/usr/lib64/openmpi/bin:$PATH
		export LD_LIBRARY_PATH="/usr/lib64/openmpi/lib:${LD_LIBRARY_PATH:-}"
	fi
	fetch_tar "ior-${IOR_VERSION}.tar.gz" \
		"https://github.com/hpc/ior/releases/download/${IOR_VERSION}/ior-${IOR_VERSION}.tar.gz" \
		-xzf /tmp
	cd "ior-${IOR_VERSION}"
	./configure
	make -j"$(nproc)"
	cp src/ior src/mdtest "$PREFIX/bin/"
	cd /tmp && rm -rf "ior-${IOR_VERSION}"

	# simul, for parallel-scale
	fetch_tar "simul-${SIMUL_VERSION}.tar.gz" \
		"https://github.com/LLNL/simul/archive/refs/tags/${SIMUL_VERSION}.tar.gz" \
		-xzf /tmp
	cd "simul-${SIMUL_VERSION}"
	# simul.c's inline begin() has no external definition, so it links
	# only under gnu89 inline semantics.
	mpicc -Wall -O2 -fgnu89-inline -o simul simul.c
	cp simul "$PREFIX/bin/"
	cd /tmp && rm -rf "simul-${SIMUL_VERSION}"

	# metabench, for parallel-scale and parallel-scale-nfs*: the 2005
	# NERSC release with Whamcloud's patches, as its toolkit RPM builds it
	_mb_check() {
		sha256sum -c - <<-EOF
		e4e1efefe65912d295964844e41055dea1bde05cfe2614ad63ec2b809730e73c  metabench.tgz
		b5f9f60ca8d4a025477eab16164c2e61f83875321aeb88d4dc202b15d2f2bedd  wc-custom.patch
		bf535fc08918fae9ee1f2d46ef0191e7c9106bc6fc7334b6c3db44bdea44545c  fix-gather-rcv-datatype.patch
		EOF
	}
	rm -rf /tmp/metabench && mkdir -p /tmp/metabench && cd /tmp/metabench
	_mb_cached="${TOOLS_CACHE_DIR:+${TOOLS_CACHE_DIR}/metabench-${TOOLKIT_REF}.tar}"
	_mb_ok=0
	if [[ -n "$_mb_cached" && -s "$_mb_cached" ]] \
	    && tar -xf "$_mb_cached" -C /tmp/metabench 2>/dev/null \
	    && _mb_check >/dev/null 2>&1; then
		_mb_ok=1
	fi
	if [[ "$_mb_ok" -eq 0 ]]; then
		rm -rf /tmp/metabench/* /tmp/toolkit /tmp/metabench.tar
		git init -q /tmp/toolkit
		git -C /tmp/toolkit fetch -q --depth 1 \
			https://review.whamcloud.com/build/toolkit "$TOOLKIT_REF"
		git -C /tmp/toolkit archive FETCH_HEAD:benchmark/metabench \
			-o /tmp/metabench.tar
		rm -rf /tmp/toolkit
		tar -xf /tmp/metabench.tar -C /tmp/metabench
		_mb_check
		_cache_put /tmp/metabench.tar "metabench-${TOOLKIT_REF}.tar"
		rm -f /tmp/metabench.tar
	fi
	tar xzf metabench.tgz
	patch -d metabench -p1 < wc-custom.patch
	patch -d metabench -p1 < fix-gather-rcv-datatype.patch
	make -C metabench CC=mpicc
	cp metabench/metabench "$PREFIX/bin/"
	cd /tmp && rm -rf /tmp/metabench

	# No RDMA in the VMs, and UCX costs ~90 MB per rank: the 32 mdsrate
	# ranks per client of parallel-scale statahead OOM a 4 GB client.
	for f in "${DESTDIR}"/etc/openmpi*/openmpi-mca-params.conf; do
		[[ -f "$f" ]] || continue
		cat >> "$f" <<-EOF
		pml = ob1
		btl = self,vader,tcp
		osc = ^ucx
		EOF
	done
else
	echo "--- Skipping IOR/mdtest (cross-compile; no cross-arch MPI toolchain)"
fi

# iozone (needs -Wno-error=implicit-* for GCC 14+)
#
# Skipped on cross builds: the linux-AMD64 target's Makefile has a
# literal `x86_64` token (TARGET-substitution) that the cross gcc
# treats as an input filename and aborts; the linux-arm target works
# native but we'd need to special-case the cross side anyway.  Drop
# iozone in cross images; install via dnf inside the VM.
if [[ -z "$CROSS_TRIPLE" ]]; then
	fetch_tar "iozone${IOZONE_VERSION}.tar" \
		"https://www.iozone.org/src/current/iozone${IOZONE_VERSION}.tar" \
		-xf /tmp
	cd "iozone${IOZONE_VERSION}/src/current"
	EXTRA_CFLAGS="-Wno-error=implicit-int -Wno-error=implicit-function-declaration"
	# iozone uses arch-specific make targets
	case "$TARGET_ARCH" in
		aarch64) IOZONE_TARGET="linux-arm" ;;
		*)       IOZONE_TARGET="linux-AMD64" ;;
	esac
	make -j"$(nproc)" "$IOZONE_TARGET" \
		CC="${CC:-cc}" \
		CFLAGS="-O3 $EXTRA_CFLAGS" \
		C_OPT="-O3 $EXTRA_CFLAGS"
	cp iozone "$PREFIX/bin/"
	cd /tmp && rm -rf "iozone${IOZONE_VERSION}"
else
	echo "--- Skipping iozone (cross-compile; linux-AMD64 Makefile mishandles cross gcc)"
fi

# pjdfstest
fetch_git_tree "pjdfstest-${PJDFSTEST_REF}" \
	"https://github.com/pjd/pjdfstest.git" \
	"$PJDFSTEST_REF" /tmp/pjdfstest
cd /tmp/pjdfstest
autoreconf -ifs
./configure ${CONFIGURE_HOST:+"$CONFIGURE_HOST"}
make -j"$(nproc)"
cp pjdfstest "$PREFIX/bin/"
# pjdfstest.sh runs $PJDFSTEST_DIR/**/*.t, default /usr/share/pjdfstest,
# and the .t files find the binary beside their tests/ directory.
mkdir -p "${DESTDIR}/usr/share/pjdfstest"
cp -a tests pjdfstest "${DESTDIR}/usr/share/pjdfstest/"
cd /tmp && rm -rf /tmp/pjdfstest

# dbench's loadfile: the distro package ships only the binary, and
# rundbench skips without client.txt.
mkdir -p "${DESTDIR}/usr/share/dbench"
fetch_file "dbench-client-${DBENCH_LOADFILE_REF}.load" \
	"https://raw.githubusercontent.com/sahlberg/dbench/${DBENCH_LOADFILE_REF}/loadfiles/client.load" \
	"${DESTDIR}/usr/share/dbench/client.txt"

# compilebench (parallel-scale): Josef Bacik's python3 port.  It reads its
# dataset files from the directory it is run in, which the suite cd's to
# ($cbench_DIR, exported by lustre-tests-path.sh).
mkdir -p "${DESTDIR}/opt/compilebench"
for f in compilebench dataset-patched dataset-patched-compiled \
	dataset-unpatched dataset-unpatched-compiled; do
	fetch_file "compilebench-${COMPILEBENCH_REF}-${f}" \
		"https://raw.githubusercontent.com/josefbacik/compilebench/${COMPILEBENCH_REF}/$f" \
		"${DESTDIR}/opt/compilebench/$f"
done
sed -i '1s|.*|#!/usr/bin/python3|' "${DESTDIR}/opt/compilebench/compilebench"
chmod 755 "${DESTDIR}/opt/compilebench/compilebench"

# vmtouch (sanity 600a-d, sanity-sec): EPEL10 has no package, so build
# the one C file here for every target; where the distro packages it too,
# both are the same tool.
fetch_file "vmtouch-${VMTOUCH_REF}.c" \
	"https://raw.githubusercontent.com/hoytech/vmtouch/${VMTOUCH_REF}/vmtouch.c" \
	/tmp/vmtouch.c
mkdir -p "$PREFIX/bin"
${CC:-gcc} -Wall -O2 -std=c99 -o "$PREFIX/bin/vmtouch" /tmp/vmtouch.c
rm -f /tmp/vmtouch.c

# connectathon (parallel-scale, parallel-scale-nfs*): the suite runs
# $cnt_DIR/runtests, from a built tree.
if [[ -z "$CROSS_TRIPLE" ]]; then
	fetch_git_tree "cthon04-${CTHON04_REF}" \
		"https://git.linux-nfs.org/cgit/steved/cthon04.git" \
		"$CTHON04_REF" /tmp/cthon04
	# tests.init is tracked, but make's built-in %: %.sh rule overwrites
	# it with tests.init.sh whenever checkout left that one newer.
	touch /tmp/cthon04/tests.init
	# runtests needs these four; tools/ wants libtirpc, which the image lacks.
	for d in basic general special lock; do
		make -C /tmp/cthon04/$d
	done
	mkdir -p "${DESTDIR}/opt"
	cp -a /tmp/cthon04 "${DESTDIR}/opt/connectathon"
	rm -rf /tmp/cthon04
fi

# auster by name, without the tests directory on PATH (lustre-tests-path.sh).
# exec by full path: auster finds the tree from its own $0.
cat > "$PREFIX/bin/auster" <<'AUSTER'
#!/bin/sh
for d in /usr/lib64/lustre/tests /usr/lib/lustre/tests; do
	[ -x "$d/auster" ] && exec "$d/auster" "$@"
done
echo "auster: Lustre tests are not installed" >&2
exit 127
AUSTER
chmod 755 "$PREFIX/bin/auster"

# FlameGraph (pure perl scripts -- no compilation needed)
fetch_git_tree "FlameGraph-${FLAMEGRAPH_REF}" \
	"https://github.com/brendangregg/FlameGraph.git" \
	"$FLAMEGRAPH_REF" "$PREFIX/FlameGraph"
for f in flamegraph.pl stackcollapse-perf.pl stackcollapse.pl difffolded.pl; do
    ln -sf "$PREFIX/FlameGraph/$f" "$PREFIX/bin/$f"
done

# drgn (Python crash analysis) -- skip when cross-compiling
# (needs target-arch Python headers + C extensions)
if [[ -z "$CONFIGURE_HOST" ]]; then
	pip3 install --break-system-packages drgn 2>/dev/null \
	    || pip3 install drgn 2>/dev/null \
	    || echo "WARNING: drgn install failed (non-fatal)"
	rm -rf /root/.cache/pip
else
	echo "--- Skipping drgn (cross-compile; install on target instead)"
fi
