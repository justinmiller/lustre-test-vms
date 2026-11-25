"""Podman/buildah persistent build-cache mounts (`--mount=type=cache,id=ltvm-*`).

Container and image builds cache downloaded distro packages and build
tools across rebuilds via ``RUN --mount=type=cache,id=ltvm-...`` (such
as ``ltvm-apt-<target>``, ``ltvm-dnf-<target>``, ``ltvm-build-tools``,
and ``ltvm-rdma-core``).

Buildah stores these caches outside ``podman system df`` and ``podman
volume ls``, under ``<ImageCopyTmpDir>/buildah-cache-<uid>/<hash>/``
(typically ``/var/tmp/buildah-cache-<uid>/``), with lockfiles under
``buildah-cache-lockfiles/<hash>/buildah-cache-lockfile``.  For buildah
1.31+ (podman 4.6+ / 5.x), ``<hash>`` is the first 16 hex digits of
``sha256("<id>:<uid>:<gid>")`` (with default ``uid=0, gid=0``).

Because ``apt`` creates subdirectories such as ``archives/partial``
owned by a container subuid (``_apt``), both sizing (``du -sb``) and
removal (``rm -rf``) run inside ``podman unshare`` for rootless podman.
On macOS, the cache directory lives inside the ``podman machine`` VM, so
inspection and pruning run via ``podman machine ssh`` -- and only when a
machine is already running (never starting a stopped VM).

Only cache directories matching known ``ltvm-*`` cache IDs are reported
or pruned; unrelated caches in the same podman store are left untouched.

Locking: each cache's sharing mode (``locked`` vs ``shared``) is decided
from how the Dockerfiles and :data:`_SHARED_CACHES` declare it, never
from whether a leftover buildah lockfile happens to exist on disk
(buildah never removes old lockfiles when a cache changes from
``sharing=locked`` to ``sharing=shared``).

- ``sharing=locked`` caches (``ltvm-apt-*``, ``ltvm-dnf-*``,
  ``ltvm-zypper-*``) acquire buildah's per-cache ``fcntl`` lockfile
  before removal (falling back to ltvm's exclusive podman usage lock if
  no lockfile exists on disk).
- ``sharing=shared`` caches (``ltvm-build-tools``, ``ltvm-rdma-core``,
  and any Dockerfile cache mount without ``sharing=locked``) ignore any
  leftover buildah lockfile and take ltvm's podman usage lock
  (``host_setup.podman_exclusive_lock``) exclusively without waiting,
  holding it across ``rm -rf`` so no concurrent ``ltvm`` build can use
  the cache while it is being deleted.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .host_setup import is_macos, podman_exclusive_lock

log = logging.getLogger(__name__)

# Warn in `ltvm doctor` once ltvm's build caches reach this footprint.
WARN_THRESHOLD_BYTES = 5 * 1024**3  # 5 GiB

_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_MOUNT_RE = re.compile(r"--mount=[^\s\\]+")
_ID_RE = re.compile(r"(?:^|,)id=(ltvm-[A-Za-z0-9_.-]+)(?:,|$)")

_SHARED_CACHES: dict[str, str] = {
    "ltvm-build-tools": "shared tool download cache",
    "ltvm-rdma-core": "shared rdma-core tarball cache",
}

_TARGET_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ltvm-apt-", "apt package cache"),
    ("ltvm-dnf-", "dnf package cache"),
    ("ltvm-zypper-", "zypper package cache"),
)

# Static scripts piped to `podman machine ssh <machine> sh -s -- <args...>`
# on macOS.  Only validated cache directory names (and literal mode
# prefixes "locked:" / "shared:") are passed as positional args, so no
# untrusted path or string is ever interpolated into shell code.
_MACOS_INSPECT_SCRIPT = r"""set -eu
tmp=$(podman info --format '{{.Store.ImageCopyTmpDir}}' 2>/dev/null || true)
case "$tmp" in
    /*) ;;
    *) tmp=/var/tmp ;;
esac
root="$tmp/buildah-cache-$(id -u)"
[ -d "$root" ] || exit 0
run_unshare() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        podman unshare "$@"
    fi
}
run_unshare sh -c '
    root="$1"
    shift
    for name in "$@"; do
        d="$root/$name"
        [ ! -L "$d" ] && [ -d "$d" ] || continue
        bytes=$(du -sb -- "$d" 2>/dev/null | awk "{print \$1}")
        [ -n "$bytes" ] || bytes=0
        mtime=$(find "$d" -printf "%T@\n" 2>/dev/null | sort -rn | head -n1)
        if [ -z "$mtime" ]; then
            mtime=$(stat -c "%Y" -- "$d" 2>/dev/null || echo 0)
        fi
        printf "%s\t%s\t%s\n" "$name" "$bytes" "$mtime"
    done
' _ "$root" "$@"
"""

_MACOS_REMOVE_SCRIPT = r"""set -eu
tmp=$(podman info --format '{{.Store.ImageCopyTmpDir}}' 2>/dev/null || true)
case "$tmp" in
    /*) ;;
    *) tmp=/var/tmp ;;
esac
root="$tmp/buildah-cache-$(id -u)"
run_unshare() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        podman unshare "$@"
    fi
}
has_flock_fcntl=0
if command -v flock >/dev/null 2>&1 && flock --help 2>&1 | grep -q -- '--fcntl'; then
    has_flock_fcntl=1
fi
for spec in "$@"; do
    mode=${spec%%:*}
    name=${spec#*:}
    d="$root/$name"
    lockfile="$root/buildah-cache-lockfiles/$name/buildah-cache-lockfile"
    if [ -L "$d" ]; then
        printf 'err\t%s\n' "$name"
        continue
    fi
    if [ ! -e "$d" ]; then
        printf 'ok\t%s\n' "$name"
        continue
    fi
    if [ "$mode" = "shared" ]; then
        # Caller holds ltvm's exclusive podman usage lock; never trust a
        # leftover buildah lockfile on disk for sharing=shared caches.
        if ! run_unshare rm -rf -- "$d"; then
            printf 'err\t%s\n' "$name"
            continue
        fi
        printf 'ok\t%s\n' "$name"
        continue
    fi
    if [ ! -f "$lockfile" ]; then
        printf 'nolockfile\t%s\n' "$name"
        continue
    fi
    rc=0
    if [ "$has_flock_fcntl" -eq 1 ]; then
        if [ "$(id -u)" -eq 0 ]; then
            flock --fcntl -n -E 200 "$lockfile" rm -rf -- "$d" || rc=$?
        else
            flock --fcntl -n -E 200 "$lockfile" podman unshare rm -rf -- "$d" || rc=$?
        fi
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c '
import errno, fcntl, os, subprocess, sys
lf, target = sys.argv[1], sys.argv[2]
with open(lf, "r+b") as f:
    try:
        fcntl.lockf(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if isinstance(e, BlockingIOError) or e.errno in (errno.EAGAIN, errno.EACCES):
            sys.exit(200)
        raise
    cmd = ["rm", "-rf", "--", target]
    if os.geteuid() != 0:
        cmd = ["podman", "unshare"] + cmd
    sys.exit(subprocess.run(cmd).returncode)
' "$lockfile" "$d" || rc=$?
    else
        printf 'nolock\t%s\n' "$name"
        continue
    fi
    if [ "$rc" -eq 200 ]; then
        printf 'busy\t%s\n' "$name"
        continue
    elif [ "$rc" -ne 0 ]; then
        printf 'err\t%s\n' "$name"
        continue
    fi
    # Leave buildah-cache-lockfiles/<name>/ in place so concurrent builds
    # never split locks across recreated inodes.
    printf 'ok\t%s\n' "$name"
done
"""


def _default_sharing(cache_id: str) -> str:
    """Fallback sharing mode when *cache_id* is not declared in a Dockerfile."""
    if cache_id in _SHARED_CACHES:
        return "shared"
    for prefix, _label in _TARGET_PREFIXES:
        if cache_id.startswith(prefix):
            return "locked"
    return "shared"


@dataclass(frozen=True)
class CacheEntry:
    cache_id: str  # e.g. "ltvm-apt-debian13", "ltvm-build-tools"
    dir_name: str  # buildah subdir name, e.g. "753ad0166846d169"
    bytes: int  # exact byte footprint
    mtime: float  # last-modified epoch seconds (0.0 if unknown)
    target: str | None  # target name for per-target caches, None for shared
    description: str  # human-readable purpose
    sharing: str = ""  # "locked" or "shared", decided from Dockerfile mounts

    def __post_init__(self) -> None:
        if not self.sharing:
            object.__setattr__(self, "sharing", _default_sharing(self.cache_id))


def cache_dir_hash(cache_id: str, uid: int = 0, gid: int = 0) -> str:
    """Buildah's 16-hex-char directory name for ``--mount=type=cache``."""
    raw = f"{cache_id}:{uid}:{gid}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _describe_cache_id(cache_id: str) -> tuple[str | None, str]:
    """Return ``(target, description)`` for an ``ltvm-*`` cache ID."""
    if cache_id in _SHARED_CACHES:
        return None, _SHARED_CACHES[cache_id]
    for prefix, label in _TARGET_PREFIXES:
        if cache_id.startswith(prefix):
            target = cache_id.removeprefix(prefix)
            if target:
                return target, f"{label} ({target})"
    return None, f"podman build cache ({cache_id})"


def _scan_dockerfile_cache_mounts() -> dict[str, str]:
    """Map ``id=ltvm-*`` mounts under ``targets/`` to ``"locked"`` or ``"shared"``.

    A cache is ``"locked"`` only when it is not in :data:`_SHARED_CACHES`
    and every Dockerfile mount declaring it explicitly sets
    ``sharing=locked``.  Any mount with ``sharing=shared``,
    ``sharing=private``, or no ``sharing=`` option classifies the cache
    as ``"shared"`` so cleanup never relies on leftover lockfiles.
    """
    from .target_config import TARGETS_DIR

    modes: dict[str, set[str]] = {}
    if not TARGETS_DIR.is_dir():
        return {}
    for pattern in ("**/*.Dockerfile", "**/Dockerfile"):
        for df in TARGETS_DIR.glob(pattern):
            if not df.is_file():
                continue
            try:
                text = df.read_text(errors="replace")
            except OSError:
                continue
            for m in _MOUNT_RE.finditer(text):
                spec = m.group(0).removeprefix("--mount=")
                parts = spec.split(",")
                if "type=cache" not in parts:
                    continue
                cache_id: str | None = None
                sharing = ""
                for part in parts:
                    im = _ID_RE.match(part)
                    if im:
                        cache_id = im.group(1)
                    elif part.startswith("sharing="):
                        sharing = part.removeprefix("sharing=")
                if cache_id:
                    modes.setdefault(cache_id, set()).add(sharing)

    result: dict[str, str] = {}
    for cid, seen in modes.items():
        if cid not in _SHARED_CACHES and seen == {"locked"}:
            result[cid] = "locked"
        else:
            result[cid] = "shared"
    return result


def known_cache_ids() -> dict[str, tuple[str, str | None, str, str]]:
    """Map buildah cache dir names -> ``(cache_id, target, description, sharing)``.

    Includes shared caches, per-target caches for every target declared
    in ``targets.yaml``, and any ``id=ltvm-*`` cache mount found in the
    target Dockerfiles.  Registers buildah 1.31+'s ``sha256(id:0:0)[:16]``
    first, plus ``sha256(id)[:16]`` and literal ``id`` fallbacks for
    older buildah releases.
    """
    from .target_config import list_targets

    df_modes = _scan_dockerfile_cache_mounts()
    ids: set[str] = set(_SHARED_CACHES) | set(df_modes)
    try:
        targets = list(list_targets())
    except Exception:
        targets = []
    for t in targets:
        for prefix, _label in _TARGET_PREFIXES:
            ids.add(f"{prefix}{t}")

    # Register primary hashes first so a fallback can never shadow one.
    by_dir: dict[str, tuple[str, str | None, str, str]] = {}
    meta_by_id = {
        cid: (
            cid,
            *_describe_cache_id(cid),
            df_modes.get(cid, _default_sharing(cid)),
        )
        for cid in sorted(ids)
    }
    for cid, meta in meta_by_id.items():
        by_dir.setdefault(cache_dir_hash(cid), meta)
    for cid, meta in meta_by_id.items():
        by_dir.setdefault(hashlib.sha256(cid.encode()).hexdigest()[:16], meta)
        by_dir.setdefault(cid, meta)
    return by_dir


def _podman_available() -> bool:
    """True when ``podman`` is on PATH.  Patched off in tests/conftest.py."""
    return shutil.which("podman") is not None


def _podman(
    *args: str,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    from .host_setup import hold_podman_usage

    hold_podman_usage()
    return subprocess.run(
        ["podman", *args],
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _running_machine_name() -> str | None:
    """Name of the running macOS podman machine, or None.  Never starts one."""
    try:
        r = _podman("machine", "list", "--format", "json", timeout=15)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    running = [
        m for m in rows if isinstance(m, dict) and m.get("Running") is True
    ]
    if not running:
        return None
    for m in running:
        if m.get("Default") and m.get("Name"):
            return str(m["Name"])
    name = running[0].get("Name")
    return str(name) if name else "podman-machine-default"


def _linux_cache_root() -> Path | None:
    """Resolve ``<ImageCopyTmpDir>/buildah-cache-<uid>`` on Linux."""
    try:
        r = _podman(
            "info", "--format", "{{.Store.ImageCopyTmpDir}}", timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    raw = r.stdout.strip()
    if raw.startswith("/"):
        parent = raw
    else:
        tmpdir = os.environ.get("TMPDIR", "").strip()
        parent = tmpdir if tmpdir.startswith("/") else "/var/tmp"
    return Path(parent) / f"buildah-cache-{os.getuid()}"


def _fallback_dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for fname in files:
            with contextlib.suppress(OSError):
                st = os.lstat(os.path.join(root, fname))
                total += st.st_size
    return total


def _dir_latest_mtime(path: Path) -> float:
    latest = 0.0
    with contextlib.suppress(OSError):
        latest = path.stat().st_mtime
    for root, dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                mt = os.lstat(os.path.join(root, name)).st_mtime
                if mt > latest:
                    latest = mt
    return latest


_LINUX_INSPECT_SH = (
    'for d in "$@"; do '
    'b=$(du -sb -- "$d" 2>/dev/null | awk "{print \\$1}"); [ -n "$b" ] || b=0; '
    'm=$(find "$d" -printf "%T@\\n" 2>/dev/null | sort -rn | head -n1); '
    '[ -n "$m" ] || m=$(stat -c "%Y" -- "$d" 2>/dev/null || echo 0); '
    'printf "%s\\t%s\\t%s\\n" "$d" "$b" "$m"; '
    "done"
)


def _measure_linux_dirs(paths: list[Path]) -> dict[Path, tuple[int, float]]:
    """Return ``(bytes, recursive_max_mtime)`` for *paths* via ``podman unshare``."""
    if not paths:
        return {}
    str_paths = [str(p) for p in paths]
    cmd = (
        ["sh", "-c", _LINUX_INSPECT_SH, "_", *str_paths]
        if os.geteuid() == 0
        else [
            "podman",
            "unshare",
            "sh",
            "-c",
            _LINUX_INSPECT_SH,
            "_",
            *str_paths,
        ]
    )
    info: dict[Path, tuple[int, float]] = {}
    try:
        if cmd[0] == "podman":
            from .host_setup import hold_podman_usage

            hold_podman_usage()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    with contextlib.suppress(ValueError):
                        info[Path(parts[0])] = (int(parts[1]), float(parts[2]))
    except (OSError, subprocess.SubprocessError):
        pass
    for p in paths:
        if p not in info:
            info[p] = (_fallback_dir_bytes(p), _dir_latest_mtime(p))
    return info


def _list_raw_linux(
    known: dict[str, tuple[str, str | None, str, str]],
) -> list[tuple[str, int, float]] | None:
    root = _linux_cache_root()
    if root is None:
        return None
    if not root.is_dir():
        return []
    matched: list[Path] = []
    try:
        for child in root.iterdir():
            if (
                child.name != "buildah-cache-lockfiles"
                and child.name in known
                and not child.is_symlink()
                and child.is_dir()
            ):
                matched.append(child)
    except OSError:
        return None
    if not matched:
        return []
    stats = _measure_linux_dirs(matched)
    return [(p.name, *stats.get(p, (0, 0.0))) for p in matched]


def _list_raw_macos(
    machine: str,
    known: dict[str, tuple[str, str | None, str, str]],
) -> list[tuple[str, int, float]] | None:
    names = sorted(n for n in known if _safe_dir_name(n))
    if not names:
        return []
    try:
        r = _podman(
            "machine",
            "ssh",
            machine,
            "sh",
            "-s",
            "--",
            *names,
            input=_MACOS_INSPECT_SCRIPT,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows: list[tuple[str, int, float]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, raw_bytes, raw_mtime = parts
        try:
            rows.append((name, int(raw_bytes), float(raw_mtime)))
        except ValueError:
            continue
    return rows


def _entries_from_raw(
    raw: list[tuple[str, int, float]],
    known: dict[str, tuple[str, str | None, str, str]],
) -> list[CacheEntry]:
    entries: list[CacheEntry] = []
    for dir_name, nbytes, mtime in raw:
        meta = known.get(dir_name)
        if meta is None:
            continue
        cache_id, target, desc, sharing = meta
        entries.append(
            CacheEntry(
                cache_id=cache_id,
                dir_name=dir_name,
                bytes=nbytes,
                mtime=mtime,
                target=target,
                description=desc,
                sharing=sharing,
            )
        )
    entries.sort(key=lambda e: (-e.bytes, e.cache_id))
    return entries


def list_caches() -> list[CacheEntry] | None:
    """Return existing ltvm build-cache mounts, or ``None`` if unchecked.

    Returns ``[]`` when podman is not installed or no ltvm caches exist,
    and ``None`` when podman could not be queried (e.g. macOS podman
    machine is stopped or did not answer).
    """
    if not _podman_available():
        return []
    known = known_cache_ids()
    if is_macos():
        machine = _running_machine_name()
        if machine is None:
            return None
        raw = _list_raw_macos(machine, known)
    else:
        raw = _list_raw_linux(known)
    if raw is None:
        return None
    return _entries_from_raw(raw, known)


def _safe_dir_name(name: str) -> bool:
    return (
        bool(_SAFE_DIR_RE.fullmatch(name))
        and name not in (".", "..", "buildah-cache-lockfiles")
        and "/" not in name
        and ":" not in name
    )


def _delete_linux_dir(target: Path) -> tuple[bool, str | None]:
    if os.geteuid() == 0:
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, str(e)
        return True, None
    try:
        r = _podman("unshare", "rm", "-rf", "--", str(target), timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, r.stderr.strip() or f"rm exited {r.returncode}"
    return True, None


def _remove_linux_unlocked(
    root: Path, dir_name: str
) -> tuple[bool, str | None]:
    """Remove *dir_name* while caller holds :func:`podman_exclusive_lock`."""
    target = root / dir_name
    if target.is_symlink():
        return False, "refused symlink in build cache directory"
    if not target.exists():
        return True, None
    return _delete_linux_dir(target)


def _remove_linux_locked(
    root: Path, dir_name: str
) -> tuple[bool, str | None] | None:
    """Remove a ``sharing=locked`` cache via buildah's fcntl lockfile.

    Returns ``None`` when the buildah lockfile does not exist on disk so
    the caller can fall back to :func:`podman_exclusive_lock`.
    """
    target = root / dir_name
    lockfile = (
        root / "buildah-cache-lockfiles" / dir_name / "buildah-cache-lockfile"
    )
    if target.is_symlink():
        return False, "refused symlink in build cache directory"
    if not target.exists():
        return True, None

    try:
        lock_fd = lockfile.open("r+b")
    except FileNotFoundError:
        return None
    except OSError as e:
        return False, f"cannot open buildah lockfile: {e}"

    try:
        try:
            fcntl.lockf(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if isinstance(e, BlockingIOError) or e.errno in (
                errno.EAGAIN,
                errno.EACCES,
            ):
                return False, "in use by a running build"
            return False, f"cannot lock buildah cache: {e}"
        ok, err = _delete_linux_dir(target)
    finally:
        with contextlib.suppress(OSError):
            fcntl.lockf(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()

    # Leave buildah-cache-lockfiles/<dir_name>/ in place so concurrent
    # builds never split fcntl locks across recreated inodes.
    return ok, err


def _macos_remove_batch(
    machine: str, specs: list[str]
) -> tuple[dict[str, str] | None, str | None]:
    """Run ``_MACOS_REMOVE_SCRIPT`` for ``mode:dir_name`` specs."""
    try:
        r = _podman(
            "machine",
            "ssh",
            machine,
            "sh",
            "-s",
            "--",
            *specs,
            input=_MACOS_REMOVE_SCRIPT,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if r.returncode != 0:
        err = r.stderr.strip() or f"podman machine ssh exited {r.returncode}"
        return None, err
    seen: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            seen[parts[1]] = parts[0]
    return seen, None


def remove_caches(
    entries: Iterable[CacheEntry],
) -> dict[str, tuple[bool, str | None]]:
    """Remove *entries* safely, skipping any in use by an active build.

    ``sharing=locked`` caches are guarded by buildah's per-cache ``fcntl``
    lockfile (falling back to :func:`podman_exclusive_lock` when no
    lockfile exists).  ``sharing=shared`` caches ignore any leftover
    buildah lockfile on disk and are removed only while this process
    holds :func:`podman_exclusive_lock`.

    Returns ``{dir_name: (ok, error_message_or_none)}``.
    """
    items = list(entries)
    if not items:
        return {}
    if not _podman_available():
        return {e.dir_name: (False, "podman is not available") for e in items}

    results: dict[str, tuple[bool, str | None]] = {}
    valid: list[CacheEntry] = []
    for e in items:
        if not _safe_dir_name(e.dir_name):
            results[e.dir_name] = (False, f"refused unsafe name {e.dir_name!r}")
        else:
            valid.append(e)
    if not valid:
        return results

    locked_entries = [e for e in valid if e.sharing == "locked"]
    shared_entries = [e for e in valid if e.sharing != "locked"]

    if is_macos():
        machine = _running_machine_name()
        if machine is None:
            for e in valid:
                results[e.dir_name] = (False, "podman machine is not running")
            return results

        need_exclusive: list[CacheEntry] = list(shared_entries)
        if locked_entries:
            seen, err = _macos_remove_batch(
                machine, [f"locked:{e.dir_name}" for e in locked_entries]
            )
            if seen is None:
                for e in locked_entries:
                    results[e.dir_name] = (False, err)
            else:
                for e in locked_entries:
                    status = seen.get(e.dir_name)
                    if status == "ok":
                        results[e.dir_name] = (True, None)
                    elif status == "busy":
                        results[e.dir_name] = (
                            False,
                            "in use by a running build",
                        )
                    elif status == "nolock":
                        results[e.dir_name] = (
                            False,
                            "cannot verify buildah fcntl lock in podman machine",
                        )
                    elif status == "nolockfile":
                        need_exclusive.append(e)
                    else:
                        results[e.dir_name] = (
                            False,
                            "could not remove cache directory",
                        )

        if need_exclusive:
            with podman_exclusive_lock() as exclusive:
                if not exclusive:
                    for e in need_exclusive:
                        results[e.dir_name] = (
                            False,
                            "in use by a running build",
                        )
                else:
                    seen, err = _macos_remove_batch(
                        machine,
                        [f"shared:{e.dir_name}" for e in need_exclusive],
                    )
                    if seen is None:
                        for e in need_exclusive:
                            results[e.dir_name] = (False, err)
                    else:
                        for e in need_exclusive:
                            status = seen.get(e.dir_name)
                            if status == "ok":
                                results[e.dir_name] = (True, None)
                            else:
                                results[e.dir_name] = (
                                    False,
                                    "could not remove cache directory",
                                )
        return results

    root = _linux_cache_root()
    if root is None:
        for e in valid:
            results[e.dir_name] = (False, "could not locate podman cache dir")
        return results

    need_exclusive = list(shared_entries)
    for e in locked_entries:
        res = _remove_linux_locked(root, e.dir_name)
        if res is None:
            need_exclusive.append(e)
        else:
            results[e.dir_name] = res

    if need_exclusive:
        with podman_exclusive_lock() as exclusive:
            if not exclusive:
                for e in need_exclusive:
                    results[e.dir_name] = (False, "in use by a running build")
            else:
                for e in need_exclusive:
                    results[e.dir_name] = _remove_linux_unlocked(
                        root, e.dir_name
                    )
    return results


def _human(n: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def doctor_report() -> tuple[list[str], list[str]]:
    """``(warnings, info)`` about ltvm's podman build caches for ``ltvm doctor``.

    Works on both Linux and macOS; on macOS never starts a stopped
    podman machine.
    """
    if not _podman_available():
        return [], []
    if is_macos():
        machine = _running_machine_name()
        if machine is None:
            return [], [
                "podman build caches: not checked (podman machine stopped)"
            ]
        known = known_cache_ids()
        raw = _list_raw_macos(machine, known)
        caches = _entries_from_raw(raw, known) if raw is not None else None
    else:
        caches = list_caches()
    if caches is None:
        return [], ["podman build caches: not checked (podman did not answer)"]
    if not caches:
        return [], ["podman build caches: none yet"]

    total = sum(c.bytes for c in caches)
    shown = [f"{c.cache_id} {_human(c.bytes)}" for c in caches[:5]]
    if len(caches) > 5:
        shown.append(f"+{len(caches) - 5} more")
    info = [
        f"podman build caches: {len(caches)} cache(s), {_human(total)} "
        f"({', '.join(shown)})"
    ]
    warnings: list[str] = []
    if total >= WARN_THRESHOLD_BYTES:
        warnings.append(
            f"podman build caches hold {_human(total)} across {len(caches)} "
            f"cache(s) -- `ltvm clean --build-cache --apply` prunes them"
        )
    return warnings, info
