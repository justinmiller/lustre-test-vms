ARG BASE_IMAGE=quay.io/rockylinux/rockylinux:10.1
FROM ${BASE_IMAGE}

# The site's package mirrors and CA, from [mirrors] in /etc/ltvm.conf;
# a no-op without them (common/ltvm-mirrors.sh).
ARG LTVM_MIRRORS=
ARG LTVM_MIRROR_CA_HASH=
COPY common/ltvm-mirrors.sh /usr/local/sbin/ltvm-mirrors
RUN --mount=type=secret,id=ltvm_mirror_ca,target=/run/secrets/ltvm_mirror_ca bash /usr/local/sbin/ltvm-mirrors apply

# Rocky 10 build container for kernel and Lustre builds.
# GCC 14 matches the EL10 6.12.0 kernel build environment.

# Enable CRB + EPEL repos
RUN dnf -y install dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb

# epel-release added repos of its own.
RUN bash /usr/local/sbin/ltvm-mirrors apply

# Install build packages from the canonical shared list.
# Match rocky8/rocky9 (no --skip-broken) so dependency resolution
# failures fail loud instead of silently dropping packages.
COPY common/packages-dev.txt /tmp/packages-dev.txt
RUN cat /tmp/packages-dev.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | xargs dnf -y --allowerasing install \
    && dnf clean all && rm -f /tmp/packages-dev.txt

# Whamcloud-patched e2fsprogs (required for server builds).
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN bash /tmp/build-e2fsprogs.sh && rm /tmp/build-e2fsprogs.sh

# Cross-compilers for the opposite arch (best-effort; see rocky9).
RUN dnf -y install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu 2>/dev/null || true \
    && dnf -y install gcc-x86_64-linux-gnu binutils-x86_64-linux-gnu 2>/dev/null || true \
    && dnf clean all

ENV PATH="/usr/lib64/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
