"""The localhost entries every guest's /etc/hosts must have.

On Rocky and SUSE the ``setup``/``netcfg`` package owns /etc/hosts, so
the image has one.  On Debian and Ubuntu nothing does -- the installer
writes it -- and podman bind-mounts its own /etc/hosts into every RUN,
so nothing an image build writes there survives either: the debian13
image shipped with no /etc/hosts at all.  The cluster block's ``sed -i``
then failed on every node, and nothing resolved ``localhost``.

Both the image export and the cluster hosts writer splice in
:func:`ensure_localhost_sh`, so a new image has the entries and a VM
booted from an older one gets them at the next ``cluster deploy``.

No ``127.0.1.1 <hostname>`` line, which Debian's installer adds: a
hostname that resolves to loopback makes LNet pick a 127.x NID.
"""

from __future__ import annotations

STOCK_HOSTS = (
    "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n"
)

# A live 127.0.0.1 entry naming the exact host "localhost" (not commented out,
# not a substring like "mylocalhost" or "localhost.localdomain").
_LOCALHOST_RE = (
    r"^[[:space:]]*127\.0\.0\.1([[:space:]]+[^#[:space:]]+)*"
    r"[[:space:]]+localhost([[:space:]]|#|$)"
)


def ensure_localhost_sh(path: str) -> str:
    """Shell that appends STOCK_HOSTS to *path* unless it names localhost.

    Creates *path* when it is missing.  *path* is spliced in as is, so it
    must already be shell-safe (quoted, or a literal like /etc/hosts).
    An existing file without a trailing newline gets one first, so the
    first entry does not join its last line.
    """
    printf_fmt = STOCK_HOSTS.replace("\t", "\\t").replace("\n", "\\n")
    return (
        f"{{ grep -qEs -e '{_LOCALHOST_RE}' -- {path} || "
        f'{{ [ ! -s {path} ] || [ -z "$(tail -c 1 {path})" ] || echo; '
        f"printf '{printf_fmt}'; }} >> {path}; }}"
    )
