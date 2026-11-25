"""Package mirrors, and an optional CA to trust for them, for the builds.

A site whose network is slow to the distros' own hosts, or that keeps
its own copies, names them once for every user::

    [mirrors]
    debian = http://debian.osuosl.org/debian https://repomirror.example.com/debian
    rocky  = https://mirror.example.com/rocky
    ca     = /etc/pki/ca-trust/source/anchors/example-ca.pem

Each key stands for the upstream prefixes in ``UPSTREAMS``; a repo URL
beginning with one is rewritten to begin with the mirror instead, so a
mirror is given as its equivalent of that prefix (``rocky`` replaces
``dl.rockylinux.org/$contentdir``).  Several mirrors, space-separated,
are tried in order where the package manager can (dnf, apt).  A repo
with no mirror configured keeps its upstream.

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

The values travel as ``--build-arg`` / ``--secret`` only, never through
the input hash: pointing a site at another mirror fetches the same
packages, so it must not make every artifact stale.
"""

from __future__ import annotations

import configparser
import contextlib
import fnmatch
import functools
import hashlib
import os
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import site_config

SECTION = "mirrors"
CA_KEY = "ca"
SECRET_ID = "ltvm_mirror_ca"
LABEL = "ltvm.site-mirrors"
NO_MIRRORS_ENV = "LTVM_NO_MIRRORS"

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


class MirrorConfigError(RuntimeError):
    """A setting that cannot be used, and must not be skipped quietly."""


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
    for problem in dict.fromkeys(problems):
        print(f"warning: {problem}", file=sys.stderr)
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


def build_args() -> list[str]:
    """``podman build`` arguments handing the settings to ltvm-mirrors.sh.

    Always passes both ARGs (empty when nothing is configured) so every
    build of a Dockerfile sees the same ARGs.  The CA PEM itself travels
    as a build secret rather than a build arg, so large CA bundles never
    hit MAX_ARG_STRLEN or land in image history.
    """
    entries = ";".join(f"{up} {' '.join(urls)}" for up, urls in _pairs())
    ca = ca_path()
    ca_hash = (
        hashlib.sha256(_settings().ca_pem).hexdigest()[:16]
        if ca is not None
        else ""
    )
    args = [
        "--build-arg",
        f"LTVM_MIRRORS={entries}",
        "--build-arg",
        f"LTVM_MIRROR_CA_HASH={ca_hash}",
    ]
    if ca is not None:
        src = ca if ca.is_absolute() else ca.resolve()
        args += ["--secret", f"id={SECRET_ID},src={src}"]
    return args


def _containerignore_filter(
    root: Path,
) -> Callable[[str, list[str]], set[str]] | None:
    """Return a ``shutil.copytree`` ignore callable for *root*/.containerignore."""
    ignore_file = root / ".containerignore"
    if not ignore_file.is_file():
        return None
    patterns: list[str] = []
    for raw in ignore_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.rstrip("/"))
    if not patterns:
        return None

    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if any(fnmatch.fnmatchcase(name, pat) for pat in patterns)
        }

    return _ignore


@contextlib.contextmanager
def podman_build_context(
    context_dir: Path,
) -> Iterator[tuple[Path | None, Path]]:
    """Yield ``(cwd, context_dir)`` for a ``podman build`` call.

    On macOS, ``podman-remote build --secret id=...,src=...`` stages each
    secret into a ``podman-build-secret-*`` temp file in
    ``filepath.Dir(filepath.EvalSymlinks(context_dir))`` (and older
    releases used CWD) and leaves it behind after the build.  When
    passing a CA secret on macOS, stage *context_dir* into a short-lived
    temp directory (honoring ``.containerignore`` so multi-GB ``*.ext4``
    images in cross-build output directories are skipped) and run
    ``podman build`` with that temp directory as ``cwd`` so every
    ``podman-build-secret-*`` file lands inside the temp directory and
    disappears on exit.

    Off macOS, or when no CA secret is configured, yields ``(None,
    context_dir.resolve())`` without copying anything.
    """
    resolved = context_dir.resolve()
    if platform.system() != "Darwin" or ca_path() is None:
        yield None, resolved
        return
    with tempfile.TemporaryDirectory(prefix="ltvm-podman-build-") as d:
        tmp = Path(d)
        staged = tmp / "context"
        shutil.copytree(
            resolved,
            staged,
            symlinks=True,
            ignore=_containerignore_filter(resolved),
        )
        yield tmp, staged


def label_args() -> list[str]:
    """``podman build`` arguments marking an image built with mirrors."""
    keys = configured_keys()
    if not keys:
        return []
    return ["--label", f"{LABEL}={','.join(keys)}"]


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
