ARG BASE_IMAGE=ubuntu:26.04
FROM ${BASE_IMAGE}

# Ubuntu 26.04 VM base image.
# Built via podman, exported to raw ext4 for QEMU microvm use.
# No kernel installed -- QEMU passes it externally via -kernel.

ENV DEBIAN_FRONTEND=noninteractive

# Keep downloaded .debs (Ubuntu's image deletes them after each install)
# in the build cache mounted at /var/cache/apt on the apt steps below, so
# a retry after a failure does not download everything again.  The cache
# lives outside the image layers.  Both settings are put back in the last
# step, so what ships behaves like the stock ubuntu:26.04 image.
RUN if [ -f /etc/apt/apt.conf.d/docker-clean ]; then \
        mv /etc/apt/apt.conf.d/docker-clean /etc/apt/docker-clean.ltvm-saved; \
    fi \
    && printf 'APT::Keep-Downloaded-Packages "true";\nDir::Cache::pkgcache "";\nDir::Cache::srcpkgcache "";\nAcquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
        > /etc/apt/apt.conf.d/99-ltvm-keep-packages

# Copy shared package lists and the ubuntu-specific name map.
COPY common/packages-base.txt   /tmp/packages-base.txt
COPY common/packages-test.txt   /tmp/packages-test.txt
COPY common/packages-debug.txt  /tmp/packages-debug.txt
COPY common/packages-server.txt /tmp/packages-server.txt
COPY ubuntu2604/packages-os.txt /tmp/packages-os.txt
COPY ubuntu2604/package-map.txt /tmp/package-map.txt

# The base image ships /etc/default/locale as a file, and the locales
# postinst's "ln -sfr" then fails under 26.04's uutils ln.  Make it the
# symlink to /etc/locale.conf that the postinst would create.
RUN if [ -f /etc/default/locale ] && [ ! -L /etc/default/locale ] \
        && [ ! -e /etc/locale.conf ]; then \
        mv /etc/default/locale /etc/locale.conf \
        && ln -s ../locale.conf /etc/default/locale; \
    fi

# Parse the shared lists, translate RHEL names to Debian names via
# package-map.txt, drop "skip" entries (mapped to "-"), and install.
# Packages without a mapping pass through unchanged (assumes the
# name is identical on both distros).
RUN --mount=type=cache,id=ltvm-apt-ubuntu2604,target=/var/cache/apt,sharing=locked apt-get update \
    && cat /tmp/packages-base.txt /tmp/packages-test.txt \
           /tmp/packages-debug.txt /tmp/packages-server.txt \
           /tmp/packages-os.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | awk 'NR==FNR { \
                 if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next; \
                 rhel=$1; \
                 sub(/^[^[:space:]]+[[:space:]]+/, ""); \
                 map[rhel]=$0; \
                 next \
               } \
               { if ($1 in map) { if (map[$1] != "-") print map[$1] } \
                 else print $1 }' \
            /tmp/package-map.txt - \
        | tr ' ' '\n' \
        | sort -u \
        | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

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
RUN cat /etc/profile.d/lustre-tests-path.sh >> /etc/bash.bashrc

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

# Source-built tools: IOR, mdtest, iozone, pjdfstest, FlameGraph, drgn.
# IOR and iozone do not compile as GCC 15's default C23, and neither
# takes the compiler from one variable (IOR goes through mpicc), so
# put gnu17 cc/gcc wrappers first on PATH for this step only.
# Its own apt-get install has no cache mount, so its .debs and lists are
# cleaned here rather than left in the layer.
RUN mkdir /tmp/gnu17 \
    && for c in cc gcc; do \
         printf '#!/bin/sh\nexec /usr/bin/gcc -std=gnu17 "$@"\n' \
             > /tmp/gnu17/$c; \
         chmod 0755 /tmp/gnu17/$c; \
       done \
    && PATH=/tmp/gnu17:$PATH bash /tmp/build-tools.sh \
    && rm -rf /tmp/gnu17 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Lustre-patched e2fsprogs (pinned release).  Its libraries land in
# /usr/lib, which ld.so searches after the multiarch directory holding
# the distro's older libext2fs, so mke2fs fails with "undefined symbol:
# ext2fs_set_iops_group".  Put the e2fsprogs libraries (only those --
# /usr/lib also has its libblkid and libuuid) ahead of the distro's.
RUN bash /tmp/build-e2fsprogs.sh \
    && mkdir /usr/lib/e2fsprogs-wc \
    && for l in libext2fs libe2p libcom_err libss; do \
         ln -s ../$l.so.2 /usr/lib/e2fsprogs-wc/$l.so.2; \
       done \
    && echo /usr/lib/e2fsprogs-wc > /etc/ld.so.conf.d/00-e2fsprogs-wc.conf \
    && ldconfig \
    && mke2fs -V

# System configuration
RUN bash /tmp/setup-ssh.sh
RUN bash /tmp/setup-serial.sh
RUN bash /tmp/setup-network.sh
RUN bash /tmp/setup-kdump.sh
# Mask tmp.mount so /tmp stays on the root disk rather than a 50%-RAM
# tmpfs (Ubuntu 26.04 ships systemd 259, which enables tmp.mount by
# default; lctl dk writes hundreds of MB to /tmp during test suites).
RUN bash /tmp/setup-services.sh unattended-upgrades.service snapd.service snapd.socket tmp.mount
RUN systemctl disable krb5-kdc.service krb5-admin-server.service 2>/dev/null || true

# Clean up, and put back the stock apt settings changed at the top.
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* \
    && rm -f /etc/apt/apt.conf.d/99-ltvm-keep-packages \
    && if [ -f /etc/apt/docker-clean.ltvm-saved ]; then \
           mv /etc/apt/docker-clean.ltvm-saved /etc/apt/apt.conf.d/docker-clean; \
       fi
