"""Package mirrors, and a CA to trust for them, for the builds.

A site whose network is slow to the distros' own hosts, or that keeps
its own copies, names them once for every user::

    [mirrors]
    debian = http://debian.osuosl.org/debian https://repomirror.example.com/debian
    rocky  = https://mirror.example.com/rocky
    ca     = /etc/pki/ca-trust/source/anchors/example-ca.pem

Each key stands for the upstream prefixes in ``UPSTREAMS``; a repo URL
beginning with one is rewritten to begin with the mirror instead, so a
mirror is given as its equivalent of that prefix (``rocky`` replaces
``dl.rockylinux.org/pub/rocky``).  Several mirrors, space-separated, are
tried in order where the package manager can (dnf, apt).  A repo with no
mirror configured keeps its upstream.

Precedence, highest first: ``LTVM_NO_MIRRORS=1`` (disables all mirrors
and CA), ``LTVM_MIRROR_<KEY>`` (``LTVM_MIRROR_DEBIAN_SECURITY``,
``LTVM_MIRROR_CA``; setting to empty string clears that key from
/etc/ltvm.conf), then ``[mirrors]`` in /etc/ltvm.conf.

What it covers, and what it does not:

- The distro repos the build container and the VM image install from,
  rewritten in the build by ``targets/common/ltvm-mirrors.sh``, and the
  ones ltvm adds itself for a cross build's sysroot (``rewrite_url``).
  Downloads that are not a package repo -- GitHub, PyPI, the kernel
  SRPM -- still go to their own hosts, through the proxy variables.
- The build only.  The image's last stage puts the stock repos back
  and drops the CA, so a VM's own dnf or apt uses the distro's hosts.
  The build container keeps them, because the ZFS and cross builds run
  dnf and apt in it; it is labelled ``ltvm.site-mirrors`` and ``target
  publish`` refuses to ship it, so a site's internal hosts never reach a
  release.

The values travel as ``--build-arg`` only, never through the input hash:
pointing a site at another mirror fetches the same packages, so it must
not make every artifact stale.  podman's layer cache does key on build
args, so the layers after the change are rebuilt.  What that leaves out
is a build container built before the change, whose repo files and CA
are the old ones while ``rewrite_url`` gives a cross build's sysroot the
new: the container carries ``ltvm.site-mirrors-id``, a digest of the
settings it was built with, and ``container_drift`` names the rebuild.

The settings are read once per state of the site file and the
``LTVM_MIRROR_*`` environment, and what is wrong with them is both
warned about and kept for ``ltvm doctor`` to count.
"""

from __future__ import annotations

import configparser
import contextlib
import functools
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import site_config

SECTION = "mirrors"
CA_KEY = "ca"
SECRET_ID = "ltvm_mirror_ca"
LABEL = "ltvm.site-mirrors"
ID_LABEL = "ltvm.site-mirrors-id"
NO_MIRRORS_ENV = "LTVM_NO_MIRRORS"
_HOST_CA_MARKER = b"# --- ltvm-host-ca-bundle ---\n"

# Standard host CA bundles, used to bootstrap TLS verification for
# https:// mirrors inside minimal Debian/Ubuntu base images that ship
# without ca-certificates.  On macOS, prefer Homebrew's Mozilla bundle
# over /etc/ssl/cert.pem (whose Keychain-exported certs can fail under
# Linux container TLS stacks).
_HOST_CA_BUNDLES: tuple[Path, ...] = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
    Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
    Path("/usr/local/etc/ca-certificates/cert.pem"),
    Path("/etc/ssl/cert.pem"),
)

# key -> the upstream prefixes (no scheme) its mirror replaces.  Each is
# matched over http and https, on whole path components.
UPSTREAMS: dict[str, tuple[str, ...]] = {
    "rocky": ("dl.rockylinux.org/$contentdir",),
    "epel": ("download.example/pub/epel",),
    "debian": ("deb.debian.org/debian",),
    "debian-security": (
        "deb.debian.org/debian-security",
        "security.debian.org/debian-security",
    ),
    "ubuntu": ("archive.ubuntu.com/ubuntu",),
    "ubuntu-security": ("security.ubuntu.com/ubuntu",),
    # arm64's archive and security updates both.
    "ubuntu-ports": ("ports.ubuntu.com/ubuntu-ports",),
    "opensuse": ("download.opensuse.org",),
}

# Characters a mirror URL may hold.  The in-container script splits on
# whitespace and ";" and splices URLs into repo files, so nothing that
# would need quoting there.
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~%+@/-]*)?"
)

_warned: set[str] = set()


class MirrorConfigError(RuntimeError):
    """A setting that cannot be used, and must not be skipped quietly."""


def _warn(problem: str) -> None:
    if problem not in _warned:
        _warned.add(problem)
        print(f"warning: {problem}", file=sys.stderr)


def _env_name(key: str) -> str:
    return "LTVM_MIRROR_" + key.upper().replace("-", "_")


@dataclass(frozen=True)
class _Settings:
    mirrors: dict[str, list[str]]  # key -> mirror URLs, in order
    ca: Path | None
    ca_pem: bytes
    ca_error: str | None
    problems: tuple[str, ...]  # settings ignored, for doctor


def _site_values(site: Path, problems: list[str]) -> dict[str, str]:
    parser = configparser.ConfigParser()
    try:
        parser.read(site)
    except configparser.Error as e:
        problems.append(f"ignoring [{SECTION}] in {site}: {e}")
        return {}
    if not parser.has_section(SECTION):
        return {}
    values = dict(parser.items(SECTION, raw=True))
    for key in values:
        if key != CA_KEY and key not in UPSTREAMS:
            problems.append(
                f"ignoring [{SECTION}] {key} in {site}: not one of "
                f"{', '.join(sorted(UPSTREAMS))}, {CA_KEY}"
            )
    return values


def _disabled_by_env() -> bool:
    return os.environ.get(NO_MIRRORS_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _raw(key: str, site: Path, values: dict[str, str]) -> tuple[str, str]:
    """The setting for *key* and where it came from.

    An explicit ``LTVM_MIRROR_<KEY>=""`` overrides ``[mirrors]`` in
    ``/etc/ltvm.conf`` so a single key can be disabled for one run.
    """
    env_name = _env_name(key)
    if env_name in os.environ:
        return os.environ[env_name].strip(), env_name
    return values.get(key, "").strip(), f"[{SECTION}] {key} in {site}"


def _read_ca(raw: str, origin: str) -> tuple[Path, bytes]:
    path = Path(raw).expanduser()
    try:
        pem = path.read_bytes()
    except OSError as e:
        raise MirrorConfigError(f"{origin}: cannot read {path}: {e.strerror}")
    if b"-----BEGIN CERTIFICATE-----" not in pem:
        raise MirrorConfigError(f"{origin}: {path} holds no PEM certificate")
    return path, pem


@functools.lru_cache(maxsize=4)
def _load(site: Path, _site_state: object, _env: object) -> _Settings:
    if _disabled_by_env():
        return _Settings({}, None, b"", None, ())
    problems: list[str] = []
    values = _site_values(site, problems)
    out: dict[str, list[str]] = {}
    for key in UPSTREAMS:
        raw, origin = _raw(key, site, values)
        urls = []
        for url in raw.split():
            url = url.rstrip("/")
            if _URL_RE.fullmatch(url):
                urls.append(url)
            else:
                problems.append(
                    f"ignoring {url!r} in {origin}: not a plain http(s) URL"
                )
        if urls:
            out[key] = urls
    ca: Path | None = None
    pem = b""
    ca_error = None
    raw, origin = _raw(CA_KEY, site, values)
    if raw:
        try:
            ca, pem = _read_ca(raw, origin)
        except MirrorConfigError as e:
            ca_error = str(e)
    for problem in problems:
        _warn(problem)
    return _Settings(out, ca, pem, ca_error, tuple(problems))


def _settings() -> _Settings:
    site = site_config.path()
    try:
        st = site.stat()
        state: object = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        state = None
    env = tuple(
        sorted(
            (k, v)
            for k, v in os.environ.items()
            if k.startswith("LTVM_MIRROR_") or k == NO_MIRRORS_ENV
        )
    )
    return _load(site, state, env)


def configured() -> dict[str, list[str]]:
    """Each mirror key that is set -> its mirror URLs, in order.

    A URL that is not a plain http(s) URL warns once and is dropped.
    """
    return {k: list(v) for k, v in _settings().mirrors.items()}


def ca_path() -> Path | None:
    """The CA bundle to trust, or None.  An unusable one is an error.

    A CA named and then skipped would fail the build later, as a TLS
    error from inside a package manager that never says why.
    """
    s = _settings()
    if s.ca_error:
        raise MirrorConfigError(s.ca_error)
    return s.ca


def configured_keys() -> list[str]:
    """What is set, by name only -- never the URLs."""
    keys = list(configured())
    if ca_path() is not None:
        keys.append(CA_KEY)
    return keys


def _pairs() -> list[tuple[str, list[str]]]:
    """(upstream URL prefix, mirrors), longest prefix first."""
    pairs = [
        (f"{scheme}://{up}", urls)
        for key, urls in _settings().mirrors.items()
        for up in UPSTREAMS[key]
        for scheme in ("http", "https")
    ]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def rewrite_url(url: str) -> list[str]:
    """*url* on each of its upstream's mirrors, or ``[url]`` with none."""
    for up, urls in _pairs():
        if url == up or url.startswith(up + "/"):
            return [m + url[len(up) :] for m in urls]
    return [url]


def _has_https_mirror(s: _Settings) -> bool:
    return any(
        url.startswith("https://")
        for urls in s.mirrors.values()
        for url in urls
    )


def _host_ca_bundle() -> tuple[Path, bytes] | None:
    """The host's system CA bundle, or None if none is readable."""
    for path in _HOST_CA_BUNDLES:
        try:
            pem = path.read_bytes()
        except OSError:
            continue
        if b"-----BEGIN CERTIFICATE-----" in pem:
            return path, pem
    return None


def _combined_ca_path(
    digest: str, payload: bytes, scratch_dir: Path | None
) -> Path:
    """Write a combined site+host CA PEM bundle for podman --secret."""
    if scratch_dir is not None:
        dest = scratch_dir / f"ltvm-mirror-ca-{digest}.pem"
        dest.write_bytes(payload)
        dest.chmod(0o600)
        return dest
    cache_base = Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    )
    cache_dir = cache_base / "ltvm" / "mirrors"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        cache_dir.chmod(0o700)
    dest = cache_dir / f"ca-{digest}.pem"
    fd, tmp_name = tempfile.mkstemp(prefix=".ca-", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return dest


def _ca_hash_and_source(
    s: _Settings,
    *,
    include_host_ca: bool = True,
) -> tuple[str, Path | None, bytes | None]:
    """Compute ``(ca_hash, direct_path, combined_payload)`` for *s*.

    *include_host_ca* controls whether the host's system CA bundle is
    bootstrapped for ``https://`` mirrors.  Only minimal Debian/Ubuntu
    base images (which ship without ``ca-certificates``) need it; RHEL
    and SUSE images already carry a trust store, and :func:`fingerprint`
    omits it so routine host CA updates do not trigger false drift
    warnings or invalidate non-apt build caches.

    ``ca_hash`` (sent as ``--build-arg LTVM_MIRROR_CA_HASH``) never
    hashes the host CA bundle: it hashes only an explicit site CA
    (``site:<digest>``) or emits the constant ``"host"`` when only the
    host CA bundle is bootstrapped, so routine host OS CA updates do
    not invalidate every Debian/Ubuntu build layer.
    """
    if s.ca is not None:
        site_digest = hashlib.sha256(s.ca_pem).hexdigest()[:16]
        if include_host_ca and _has_https_mirror(s):
            host = _host_ca_bundle()
            if host is not None:
                _, host_pem = host
                site_pem = (
                    s.ca_pem if s.ca_pem.endswith(b"\n") else s.ca_pem + b"\n"
                )
                combined = site_pem + _HOST_CA_MARKER + host_pem
                return f"site:{site_digest}", None, combined
        return f"site:{site_digest}", s.ca, None
    if include_host_ca and _has_https_mirror(s):
        host = _host_ca_bundle()
        if host is not None:
            path, _pem = host
            return "host", path, None
    return "", None, None


def _ca_secret(
    scratch_dir: Path | None = None,
    *,
    os_family: str | None = None,
) -> tuple[str, Path | None]:
    """(LTVM_MIRROR_CA_HASH value, secret file path) for podman build.

    An explicit site ``ca`` is passed as ``site:<hash>`` and installed
    into the image's trust store.  For minimal Debian/Ubuntu base images
    (``os_family == "debian"`` or unspecified) with ``https://`` mirrors,
    a readable host CA bundle is appended after ``_HOST_CA_MARKER``
    (or passed alone as ``host`` when no site CA is configured)
    so apt's ``CaInfo`` can verify HTTPS mirrors before ca-certificates
    is installed.  RHEL and SUSE base images already ship a trust store
    and never receive the host CA bundle.
    """
    ca_path()  # raises on an unusable CA
    include_host_ca = os_family in (None, "debian")
    ca_hash, direct_path, combined = _ca_hash_and_source(
        _settings(), include_host_ca=include_host_ca
    )
    if combined is not None:
        file_digest = hashlib.sha256(combined).hexdigest()[:16]
        return ca_hash, _combined_ca_path(file_digest, combined, scratch_dir)
    return ca_hash, direct_path


def _arg_values(
    scratch_dir: Path | None = None,
    *,
    os_family: str | None = None,
) -> tuple[str, str, Path | None]:
    """The LTVM_MIRRORS, LTVM_MIRROR_CA_HASH, and secret-file values."""
    entries = ";".join(f"{up} {' '.join(urls)}" for up, urls in _pairs())
    ca_hash, ca_src = _ca_secret(scratch_dir=scratch_dir, os_family=os_family)
    return entries, ca_hash, ca_src


def fingerprint() -> str:
    """A digest of the site mirror settings, "" when nothing is set.

    Hashes only the configured mirrors and explicit site CA (never the
    host's system CA bundle) so routine host CA bundle updates do not
    trigger false container drift warnings.
    """
    entries = ";".join(f"{up} {' '.join(urls)}" for up, urls in _pairs())
    ca_path()
    ca_hash, _, _ = _ca_hash_and_source(_settings(), include_host_ca=False)
    if not entries and not ca_hash:
        return ""
    return hashlib.sha256(f"{entries}\n{ca_hash}".encode()).hexdigest()[:16]


def build_args(
    scratch_dir: Path | None = None,
    *,
    os_family: str | None = None,
) -> list[str]:
    """``podman build`` arguments handing the settings to ltvm-mirrors.sh.

    Always passes both ARGs (empty when nothing is configured) so every
    build of a Dockerfile sees the same ARGs.  The CA PEM itself travels
    as a build secret rather than a build arg, so large CA bundles never
    hit MAX_ARG_STRLEN or land in image history.
    """
    entries, ca_hash, ca_src = _arg_values(
        scratch_dir=scratch_dir, os_family=os_family
    )
    args = [
        "--build-arg",
        f"LTVM_MIRRORS={entries}",
        "--build-arg",
        f"LTVM_MIRROR_CA_HASH={ca_hash}",
    ]
    if ca_src is not None:
        src = ca_src if ca_src.is_absolute() else ca_src.resolve()
        args += ["--secret", f"id={SECRET_ID},src={src}"]
    return args


@contextlib.contextmanager
def podman_build_cwd() -> Iterator[Path]:
    """Yield a scratch directory for ``podman build``'s working directory.

    On macOS, ``podman-remote build --secret id=...,src=...`` stages the
    secret into a ``podman-build-secret-*`` temp file in its own CWD and
    leaves it behind after the build.  Running ``podman build`` inside a
    short-lived temp directory keeps the user's working tree clean while
    preserving all path arguments (which are absolute).
    """
    with tempfile.TemporaryDirectory(prefix="ltvm-podman-build-") as d:
        yield Path(d)


def label_args() -> list[str]:
    """``podman build`` arguments marking an image built with mirrors."""
    keys = configured_keys()
    if not keys:
        return []
    return [
        "--label",
        f"{LABEL}={','.join(keys)}",
        "--label",
        f"{ID_LABEL}={fingerprint()}",
    ]


def container_drift(container_tag: str) -> str | None:
    """Why *container_tag* does not match the current settings, or None.

    The build container keeps the mirrored repo files and CA it was
    built with, so ZFS, MOFED-kmod, cross-sysroot, and image-tools
    builds running inside it all use those repos.  Warns whenever the
    container's recorded ``ltvm.site-mirrors-id`` differs from the
    current settings -- including when mirrors were later retired or
    unset while the container still points at them.  Returns None when
    both match (including when neither uses mirrors) or when podman
    cannot say.
    """
    try:
        current = fingerprint()
    except MirrorConfigError as e:
        return f"cannot check package mirror drift for '{container_tag}': {e}"
    try:
        r = subprocess.run(
            [
                "podman",
                "image",
                "inspect",
                "--format",
                f'{{{{index .Labels "{ID_LABEL}"}}}}',
                container_tag,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    stdout = getattr(r, "stdout", None)
    if not isinstance(stdout, str):
        return None
    built = stdout.strip()
    if built == "<no value>":
        built = ""
    if built == current:
        return None
    if not current:
        return (
            f"build container '{container_tag}' was built with site package "
            f"mirrors, but [{SECTION}] / LTVM_MIRROR_* now configure none; "
            "dnf/apt inside the container will still use the old mirrors.  "
            "Rebuild it to restore stock repos: "
            "ltvm build container <target> --force"
        )
    return (
        f"build container '{container_tag}' was built with other package "
        f"mirror settings than [{SECTION}] / LTVM_MIRROR_* now give; "
        "container repos and CA may not match the current mirrors.  "
        "If package installs fail, rebuild it: "
        "ltvm build container <target> --force"
    )


def doctor_report() -> tuple[list[str], list[str]]:
    """(issues, notes) about the mirror settings, for doctor."""
    if _disabled_by_env():
        return [], [f"mirrors: disabled by {NO_MIRRORS_ENV}"]
    issues: list[str] = [f"mirrors: {p}" for p in _settings().problems]
    notes: list[str] = []
    for key, urls in configured().items():
        notes.append(f"mirror {key}: {' '.join(urls)}")
    try:
        ca = ca_path()
    except MirrorConfigError as e:
        issues.append(f"mirror ca: {e}")
    else:
        if ca is not None:
            notes.append(f"mirror ca: {ca}")
    return issues, notes
