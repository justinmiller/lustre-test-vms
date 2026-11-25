ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:8.10
FROM ${BASE_IMAGE}

# The site's package mirrors and CA, from [mirrors] in /etc/ltvm.conf;
# a no-op without them (common/ltvm-mirrors.sh).
ARG LTVM_MIRRORS=
ARG LTVM_MIRROR_CA_HASH=
COPY common/ltvm-mirrors.sh /usr/local/sbin/ltvm-mirrors
RUN --mount=type=secret,id=ltvm_mirror_ca,target=/run/secrets/ltvm_mirror_ca bash /usr/local/sbin/ltvm-mirrors apply

# Rocky 8 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

# Enable EPEL and PowerTools repos (PowerTools = CRB on EL8).
# Also install findutils (xargs) + grep -- not present in minimal image.
RUN dnf -y install epel-release dnf-plugins-core findutils grep \
    && dnf config-manager --set-enabled powertools \
    && dnf clean all

# epel-release added repos of its own.
RUN bash /usr/local/sbin/ltvm-mirrors apply

# Copy package lists and install
COPY common/packages-base.txt /tmp/packages-base.txt
COPY common/packages-test.txt /tmp/packages-test.txt
COPY common/packages-debug.txt /tmp/packages-debug.txt
COPY common/packages-server.txt /tmp/packages-server.txt
COPY rocky8/packages-os.txt /tmp/packages-os.txt

# Parse package lists (strip comments/blanks) and install.
# Note: kernel-devel is excluded from the common lists because Lustre
# builds on the host on EL9+.  EL8 still installs it explicitly below
# (separate RUN step) because the EL8 build flow differs.
# --skip-broken: numatop/bpftrace may not be available on EL8, and
# fscrypt was never packaged for EPEL8 (it first appears in EPEL9), so
# the conf-sanity encryption tests skip on this target.  Left to
# --skip-broken rather than an explicit skip list so it starts being
# installed on its own if EPEL8 ever ships it.
RUN cat /tmp/packages-base.txt \
        /tmp/packages-test.txt \
        /tmp/packages-debug.txt \
        /tmp/packages-server.txt \
        /tmp/packages-os.txt \
    | grep -v '^\s*#' | grep -v '^\s*$' \
    | grep -v '^kernel-devel$' \
    | sort -u \
    | xargs dnf -y --allowerasing --skip-broken install \
    && dnf clean all

# Copy shared setup scripts
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
COPY common/build-tools.sh     /tmp/build-tools.sh
COPY common/setup-ssh.sh       /tmp/setup-ssh.sh
COPY common/setup-serial.sh    /tmp/setup-serial.sh
COPY common/rc.local           /etc/rc.d/rc.local
COPY common/setup-network.sh   /tmp/setup-network.sh
COPY common/setup-kdump.sh     /tmp/setup-kdump.sh
COPY common/setup-services.sh  /tmp/setup-services.sh
COPY common/lustre-tests-path.sh /etc/profile.d/lustre-tests-path.sh
RUN cat /etc/profile.d/lustre-tests-path.sh >> /etc/bashrc

# Per-NIC hook scripts invoked by rc.local (fc_nics= dispatch).
COPY common/setup-nic-softroce.sh  /usr/local/sbin/setup-nic-softroce.sh
COPY common/setup-lnet-config.sh   /usr/local/sbin/setup-lnet-config.sh
COPY common/setup-lnet-passthrough-resolve.sh \
                                   /usr/local/sbin/setup-lnet-passthrough-resolve.sh
COPY common/setup-lnet-rp-filter.sh /usr/local/sbin/setup-lnet-rp-filter.sh
RUN chmod 0755 /usr/local/sbin/setup-nic-softroce.sh \
               /usr/local/sbin/setup-lnet-config.sh \
               /usr/local/sbin/setup-lnet-passthrough-resolve.sh \
               /usr/local/sbin/setup-lnet-rp-filter.sh

# EL8-specific: kernel-devel + lustre userspace build deps.
# kernel-devel is excluded from the common package list (not needed on
# EL9+), so install it explicitly here.
RUN dnf -y install kernel-devel libnl3-devel libselinux-devel \
    && dnf clean all

# Source-built tools: IOR, mdtest, iozone, pjdfstest, FlameGraph, drgn
RUN --mount=type=cache,id=ltvm-build-tools,target=/var/cache/ltvm-tools,sharing=shared \
    LTVM_TOOLS_CACHE=/var/cache/ltvm-tools bash /tmp/build-tools.sh

# Lustre-patched e2fsprogs (pinned release)
RUN bash /tmp/build-e2fsprogs.sh

# System configuration
RUN bash /tmp/setup-ssh.sh
RUN bash /tmp/setup-serial.sh
RUN bash /tmp/setup-network.sh
RUN bash /tmp/setup-kdump.sh
RUN bash /tmp/setup-services.sh dnf-makecache.timer

# Clean up
RUN dnf clean all && rm -rf /var/cache/dnf /tmp/*
