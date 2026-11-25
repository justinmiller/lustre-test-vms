"""Guest clock synchronization: host SNTP responder and lifecycle SSH sync.

Two complementary mechanisms keep every VM's wall clock aligned with the host:

1. **Host SNTP responder** (RFC 4330 / RFC 5905): a lightweight, rootless
   UDP server bound to the VM gateway IP (``fc_gw``) on an unprivileged port
   (default ``12300``, overrideable via ``LTVM_TIME_PORT``).  Every VM image
   ships ``chrony`` pointed at ``fc_gw`` (and ``refclock PHC /dev/ptp0`` when
   KVM's PTP device is present), so guests continuously slew drift and recover
   automatically after host sleep or snapshot restores -- even in air-gapped
   labs where public NTP pools are unreachable.

   The server starts on demand when a VM launches, exits automatically after
   being idle (``DEFAULT_IDLE_TIMEOUT``), and uses a 48-byte SNTP probe to
   reuse an already-running instance across users on shared hosts.  Because
   port ``12300`` is unprivileged, any local user on a shared host could bind
   it first and serve guests arbitrary time; where local users are untrusted,
   set ``LTVM_TIME_SERVER=0`` and ``LTVM_TIME_PORT=123`` with a host
   ``chronyd``/``ntpd`` serving ``fc_gw`` instead.

2. **Lifecycle SSH sync** (:func:`sync_guest_clock`): runs over SSH after
   boot/start/restore (in ``provision_vm_ssh``) and during ``cluster deploy``
   so guests match host UTC within ~50-100 ms immediately -- including older
   images without ``chrony`` -- without perturbing an already-locked ``chrony``.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import ipaddress
import logging
import os
import platform
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vm_state import ClusterInfo, ClusterNode

log = logging.getLogger(__name__)

# Unprivileged UDP port on the VM gateway (fc_gw).  >1024 so rootless users
# can bind it without colliding with a host chronyd/ntpd on port 123.
DEFAULT_TIME_PORT = 12300

# Exit automatically once no guest has polled for this many seconds.
# chrony polls at maxpoll 4 (16s), so 10 minutes is ~37x the poll interval.
DEFAULT_IDLE_TIMEOUT = 600.0

# Seconds between NTP epoch (1900-01-01) and Unix epoch (1970-01-01).
NTP_EPOCH_DELTA = 2208988800

_NTP_PACKET_FORMAT = "!B B B b I I 4s I I I I I I I I"
_NTP_PACKET_LEN = 48
_NTP_MODE_CLIENT = 3
_NTP_MODE_SERVER = 4
_NTP_STRATUM_HOST = 2
_NTP_PRECISION = -20  # ~1 microsecond
_NTP_REF_ID = b"LTVM"


class _StopSignal(BaseException):
    """Raised by the SIGTERM/SIGINT handler to interrupt sock.recvfrom()."""


# Python 3.9 (macOS Apple-signed /usr/bin/python3) still raises a separate
# socket.timeout class that is not TimeoutError; Python 3.10+ aliases them.
_RECV_TIMEOUT: tuple[type[BaseException], ...] = (
    TimeoutError,
    getattr(socket, "timeout", TimeoutError),
)

_warned_bad_port: set[str] = set()


def configured_port() -> int:
    """Return the configured host SNTP port (``LTVM_TIME_PORT``).

    Returns ``0`` when explicitly disabled, or :data:`DEFAULT_TIME_PORT`
    when unset or invalid.
    """
    raw = os.environ.get("LTVM_TIME_PORT")
    if raw is None or not raw.strip():
        return DEFAULT_TIME_PORT
    raw = raw.strip()
    try:
        port = int(raw)
    except ValueError:
        if raw not in _warned_bad_port:
            _warned_bad_port.add(raw)
            log.warning(
                "ignoring invalid LTVM_TIME_PORT=%r (using %d)",
                raw,
                DEFAULT_TIME_PORT,
            )
        return DEFAULT_TIME_PORT
    if port == 0:
        return 0
    if not (1 <= port <= 65535):
        if raw not in _warned_bad_port:
            _warned_bad_port.add(raw)
            log.warning(
                "ignoring out-of-range LTVM_TIME_PORT=%d (using %d)",
                port,
                DEFAULT_TIME_PORT,
            )
        return DEFAULT_TIME_PORT
    return port


def server_enabled() -> bool:
    """True unless the built-in host SNTP responder is explicitly disabled."""
    if os.environ.get("LTVM_TIME_SERVER", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    return configured_port() > 0


def cmdline_args(*, macos: bool | None = None) -> str:
    """Return the optional kernel cmdline fragment for guest chrony setup.

    On Linux with :data:`DEFAULT_TIME_PORT`, omitted so standard VMs keep the
    exact same kernel command line.  When ``LTVM_TIME_SERVER=0`` or
    ``LTVM_TIME_PORT=0`` disables host SNTP, emits ``fc_ntp_port=0`` so
    ``rc.local`` leaves the distro chrony config alone instead of pointing
    guests at an unlistened port.  On macOS hosts, emits
    ``fc_ntp_acqport=12300`` so chrony reuses one UDP 4-tuple (avoiding
    macOS ``socketfilterfw`` per-poll delay) without forcing an unreserved
    port on Linux SELinux guests.
    """
    if not server_enabled():
        return " fc_ntp_port=0"
    port = configured_port()
    parts: list[str] = []
    if port != DEFAULT_TIME_PORT:
        parts.append(f"fc_ntp_port={port}")
    if macos is None:
        macos = platform.system() == "Darwin"
    if macos:
        parts.append(f"fc_ntp_acqport={DEFAULT_TIME_PORT}")
    return (" " + " ".join(parts)) if parts else ""


def ns_to_ntp(ns: int) -> tuple[int, int]:
    """Convert Unix epoch nanoseconds to an NTP 64-bit ``(sec, frac)`` pair."""
    sec = (ns // 1_000_000_000) + NTP_EPOCH_DELTA
    rem = ns % 1_000_000_000
    frac = (rem << 32) // 1_000_000_000
    return sec & 0xFFFFFFFF, frac & 0xFFFFFFFF


def ntp_to_ns(sec: int, frac: int) -> int:
    """Convert an NTP 64-bit ``(sec, frac)`` pair back to Unix nanoseconds."""
    unix_sec = sec - NTP_EPOCH_DELTA
    rem_ns = (frac * 1_000_000_000) >> 32
    return unix_sec * 1_000_000_000 + rem_ns


def build_sntp_reply(request: bytes, rx_ns: int, tx_ns: int) -> bytes | None:
    """Build a 48-byte RFC 4330 SNTP mode-4 reply for a client request.

    Returns ``None`` if *request* is malformed or not a client request.
    """
    if len(request) < _NTP_PACKET_LEN:
        return None
    li_vn_mode = request[0]
    vn = (li_vn_mode >> 3) & 0x07
    mode = li_vn_mode & 0x07
    if mode != _NTP_MODE_CLIENT or vn < 1 or vn > 4:
        return None

    poll = request[2]
    orig_sec, orig_frac = struct.unpack("!II", request[40:48])
    rx_sec, rx_frac = ns_to_ntp(rx_ns)
    tx_sec, tx_frac = ns_to_ntp(max(tx_ns, rx_ns))

    reply_li_vn_mode = (0 << 6) | (vn << 3) | _NTP_MODE_SERVER
    return struct.pack(
        _NTP_PACKET_FORMAT,
        reply_li_vn_mode,
        _NTP_STRATUM_HOST,
        poll,
        _NTP_PRECISION,
        0,  # root delay
        0,  # root dispersion
        _NTP_REF_ID,
        rx_sec,  # reference timestamp
        rx_frac,
        orig_sec,  # originate timestamp (echo client's transmit timestamp)
        orig_frac,
        rx_sec,  # receive timestamp
        rx_frac,
        tx_sec,  # transmit timestamp
        tx_frac,
    )


def probe_time_server(host: str, port: int, *, timeout: float = 0.2) -> bool:
    """Return True if an SNTP/NTP server answers on ``(host, port)``."""
    tx_sec, tx_frac = ns_to_ntp(time.time_ns())
    req = bytearray(_NTP_PACKET_LEN)
    req[0] = (0 << 6) | (4 << 3) | _NTP_MODE_CLIENT  # LI=0, VN=4, Mode=3
    struct.pack_into("!II", req, 40, tx_sec, tx_frac)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(bytes(req))
            data = sock.recv(1024)
    except OSError:
        return False

    if len(data) < _NTP_PACKET_LEN:
        return False
    mode = data[0] & 0x07
    if mode != _NTP_MODE_SERVER:
        return False
    return data[24:32] == bytes(req[40:48])


def state_dir(uid: int | None = None, *, create: bool = False) -> Path:
    """Private ``0700`` per-user state directory for SNTP pid/lock files."""
    me = os.getuid()
    target_uid = me if uid is None else uid
    # Under sudo (Ubuntu preserves $HOME; sudo -E preserves XDG_STATE_HOME),
    # root must use /root's own passwd home instead of poisoning $SUDO_USER's
    # state dir or missing a root responder started without SUDO_USER.
    if target_uid == me and not (me == 0 and os.environ.get("SUDO_USER")):
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    else:
        try:
            home = Path(pwd.getpwuid(target_uid).pw_dir)
        except KeyError:
            home = (
                Path("/root")
                if target_uid == 0
                else Path(tempfile.gettempdir())
            )
        base = home / ".local" / "state"
    d = base / "ltvm" / "timesync"
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(str(d), flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != target_uid:
                raise OSError(f"unsafe timesync state dir: {d}")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    return d


def _open_nofollow(
    path: Path,
    flags: int,
    mode: int = 0o600,
    *,
    expected_uid: int | None = None,
) -> int:
    """Open *path* without following symlinks and verify regular-file ownership."""
    owner_uid = os.getuid() if expected_uid is None else expected_uid
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags | nofollow, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid:
            raise OSError(f"unsafe timesync file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def pidfile_path(port: int | None = None, *, uid: int | None = None) -> Path:
    """Per-user pidfile path for the detached SNTP responder."""
    p = configured_port() if port is None else port
    return state_dir(uid) / f"timesync-{p}.pid"


def lockfile_path(port: int | None = None, *, uid: int | None = None) -> Path:
    """Per-user flock path serializing responder startup."""
    p = configured_port() if port is None else port
    return state_dir(uid) / f"timesync-{p}.lock"


def _pid_is_time_server(pid: int, *, expected_uid: int | None = None) -> bool:
    """True when *pid* is an alive ``ltvm_pkg`` time_sync process owned by *expected_uid*."""
    if pid <= 0:
        return False
    uid = os.getuid() if expected_uid is None else expected_uid
    if platform.system() == "Linux":
        try:
            proc_dir = Path(f"/proc/{pid}")
            if proc_dir.stat().st_uid != uid:
                return False
            cmdline = (proc_dir / "cmdline").read_bytes()
        except OSError:
            return False
        return b"time_sync.py" in cmdline and b"--bind" in cmdline
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    out = (r.stdout or "").strip()
    parts = out.split(None, 1)
    if len(parts) < 2:
        return False
    try:
        proc_uid = int(parts[0])
    except ValueError:
        return False
    return (
        proc_uid == uid and "time_sync.py" in parts[1] and "--bind" in parts[1]
    )


def stop_time_server(
    port: int | None = None, *, uid: int | None = None
) -> bool:
    """Stop this user's detached SNTP responder if running.

    Returns ``True`` if a responder was signalled, ``False`` otherwise.
    """
    target_uid = os.getuid() if uid is None else uid
    pf = pidfile_path(port, uid=target_uid)
    try:
        fd = _open_nofollow(pf, os.O_RDONLY, expected_uid=target_uid)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        pid = int(raw)
    except (OSError, ValueError):
        return False

    if not _pid_is_time_server(pid, expected_uid=target_uid):
        pf.unlink(missing_ok=True)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pf.unlink(missing_ok=True)
        return False
    except OSError:
        return False

    for _ in range(20):
        if not _pid_is_time_server(pid, expected_uid=target_uid):
            break
        time.sleep(0.05)
    pf.unlink(missing_ok=True)
    return True


def stop_all_time_servers(uid: int | None = None) -> list[int]:
    """Stop every detached SNTP responder recorded under *uid*'s state dir."""
    target_uid = os.getuid() if uid is None else uid
    d = state_dir(target_uid)
    stopped: list[int] = []
    if not d.is_dir():
        return stopped
    for pf in sorted(d.glob("timesync-*.pid")):
        suffix = pf.name.removeprefix("timesync-").removesuffix(".pid")
        if not suffix.isdigit():
            continue
        p = int(suffix)
        if stop_time_server(p, uid=target_uid):
            stopped.append(p)
    return stopped


def _allowed_network(bind_host: str) -> ipaddress.IPv4Network | None:
    """Return the IPv4 subnet allowed to query a responder on *bind_host*.

    ``0.0.0.0`` accepts any source; loopback accepts ``127.0.0.0/8``; a
    specific bridge address (e.g. ``192.168.105.1``) restricts replies to
    its ``/24`` subnet.
    """
    try:
        addr = ipaddress.IPv4Address(bind_host)
    except ValueError:
        return None
    if addr.is_unspecified:
        return None
    if addr.is_loopback:
        return ipaddress.IPv4Network("127.0.0.0/8")
    return ipaddress.IPv4Network(f"{addr}/24", strict=False)


def serve(
    bind_host: str,
    port: int,
    *,
    pidfile: Path | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
) -> int:
    """Run the UDP SNTP responder loop until signalled or idle."""
    allowed_net = _allowed_network(bind_host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.bind((bind_host, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return 0
            log.warning("time_sync: cannot bind %s:%d: %s", bind_host, port, e)
            return 1

        if pidfile is not None:
            try:
                fd = _open_nofollow(
                    pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(f"{os.getpid()}\n")
            except OSError:
                pass

        def _handle_stop(_signum: int, _frame: object) -> None:
            raise _StopSignal()

        poll_slice = min(5.0, idle_timeout) if idle_timeout > 0 else 5.0
        sock.settimeout(max(poll_slice, 0.05))
        last_activity = time.monotonic()

        try:
            signal.signal(signal.SIGTERM, _handle_stop)
            signal.signal(signal.SIGINT, _handle_stop)
            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                except _RECV_TIMEOUT:
                    if (
                        idle_timeout > 0
                        and time.monotonic() - last_activity >= idle_timeout
                    ):
                        break
                    continue
                except OSError:
                    continue

                if allowed_net is not None:
                    try:
                        if ipaddress.IPv4Address(addr[0]) not in allowed_net:
                            continue
                    except ValueError:
                        continue

                rx_ns = time.time_ns()
                try:
                    reply = build_sntp_reply(data, rx_ns, time.time_ns())
                except struct.error:
                    continue
                if reply is not None:
                    last_activity = time.monotonic()
                    try:
                        sock.sendto(reply, addr)
                    except OSError:
                        pass
        except _StopSignal:
            pass
    finally:
        sock.close()
        if pidfile is not None:
            try:
                fd = _open_nofollow(pidfile, os.O_RDONLY)
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    owner_pid = fh.read().strip()
                if owner_pid == str(os.getpid()):
                    pidfile.unlink(missing_ok=True)
            except OSError:
                pass
    return 0


def _server_python() -> str:
    """Return the Python interpreter to run the detached SNTP responder.

    On macOS with the Application Firewall enabled, Homebrew/pyenv Pythons
    are ad-hoc signed and have incoming UDP silently dropped.  Apple's
    built-in ``/usr/bin/python3`` is Apple-signed and automatically
    permitted, and this module's server loop uses only Python 3.9+ stdlib.
    """
    if platform.system() == "Darwin" and os.access("/usr/bin/python3", os.X_OK):
        return "/usr/bin/python3"
    return sys.executable


def ensure_time_server(
    host: str | None = None,
    port: int | None = None,
    *,
    startup_timeout: float = 2.0,
) -> bool:
    """Ensure the host SNTP responder is answering on ``(host, port)``.

    Non-fatal: returns ``False`` and logs a debug/warning message if the
    responder is disabled or could not be started.
    """
    if not server_enabled():
        return False

    if host is None:
        from .vm_state import GATEWAY

        host = GATEWAY
    if port is None:
        port = configured_port()
    if port <= 0:
        return False

    # Fast path: reuse an already-running responder (even one owned by
    # another user on a shared multi-user host).
    if probe_time_server(host, port):
        return True

    try:
        state_dir(create=True)
        lf = lockfile_path(port)
        pf = pidfile_path(port)
        fd = _open_nofollow(lf, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a+") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            if probe_time_server(host, port):
                return True

            script_path = str(Path(__file__).resolve())
            cmd = [
                _server_python(),
                "-I",
                script_path,
                "--bind",
                host,
                "--port",
                str(port),
                "--pidfile",
                str(pf),
            ]
            proc = subprocess.Popen(
                cmd,
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if probe_time_server(host, port, timeout=0.1):
                    return True
                if proc.poll() is not None:
                    # May have exited 0 due to EADDRINUSE race with another
                    # user starting it at the same instant.
                    return probe_time_server(host, port, timeout=0.2)
                time.sleep(0.05)
    except OSError as e:
        log.debug(
            "time_sync: could not start responder on %s:%d: %s", host, port, e
        )
        return False

    log.debug(
        "time_sync: responder on %s:%d did not answer within %.1fs",
        host,
        port,
        startup_timeout,
    )
    return False


_SYNC_PROBE_CMD = (
    "chronyc -n tracking 2>/dev/null; echo @@; "
    "date +%s.%N 2>/dev/null || date +%s"
)
_SYNC_STEP_TEMPLATE = "date -u -s @{ts} >/dev/null"
# Maximum one-way SSH latency compensation applied when stepping a guest clock.
_MAX_RTT_COMPENSATION_S = 2.0


def build_sync_clock_cmd(host_ts: float | None = None) -> str:
    """Return the remote shell command to set a guest's clock to *host_ts*."""
    ts = time.time() if host_ts is None else host_ts
    return _SYNC_STEP_TEMPLATE.format(ts=f"{ts:.6f}")


def sync_guest_clock(ip: str, *, timeout: int = 10) -> bool:
    """Sync the guest's wall clock to host UTC over SSH.

    Probes chrony's tracking state and guest wall clock first while
    measuring SSH round-trip time ``[t0, t1]``.  When chrony is already
    locked within 100 ms and the guest wall clock agrees with host time
    within ``0.5 s + rtt / 2``, skips stepping so SSH connection latency
    on a loaded host never steps an already-locked guest backwards.
    Otherwise steps the clock, compensating for half the measured RTT.

    Non-fatal: returns ``False`` and logs a warning on failure so VM
    lifecycle operations never fail over a transient clock sync error.
    """
    from .vm_net import run_ssh

    t0 = time.time()
    try:
        probe = run_ssh(
            ip, _SYNC_PROBE_CMD, timeout=timeout, retry_connect=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("clock sync probe on %s failed: %s", ip, e)
        return False
    t1 = time.time()

    if probe.returncode == 255:
        err = (probe.stderr or "").strip()
        log.warning(
            "clock sync probe on %s failed (rc=255)%s",
            ip,
            f": {err}" if err else "",
        )
        return False

    rtt = max(0.0, t1 - t0)
    parts = (probe.stdout or "").split("@@\n", 1)
    if len(parts) == 2:
        tracking = parse_chrony_tracking(parts[0])
        guest_ts = _parse_guest_timestamp(parts[1])
        t_mid = (t0 + t1) / 2.0
        if (
            tracking["synchronized"]
            and tracking["offset_ms"] is not None
            and abs(tracking["offset_ms"]) < 100.0
            and guest_ts is not None
            and abs(guest_ts - t_mid) <= 0.5 + (rtt / 2.0)
        ):
            return True

    step_ts = time.time() + min(rtt / 2.0, _MAX_RTT_COMPENSATION_S)
    cmd = build_sync_clock_cmd(step_ts)
    try:
        r = run_ssh(ip, cmd, timeout=timeout, retry_connect=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("clock sync on %s failed: %s", ip, e)
        return False
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        log.warning(
            "clock sync on %s exited %d%s",
            ip,
            r.returncode,
            f": {err}" if err else "",
        )
        return False
    return True


# Warn in `ltvm cluster status` when inter-node or host-to-VM skew
# exceeds this threshold (1 second).  Kerberos fails at 300 s and
# Lustre test timestamps/timeouts drift well before that; 1 s catches
# broken/unsynchronized chrony or post-sleep drift without false
# positives from SSH round-trip latency on unlocked guests.
SKEW_WARN_MS = 1000.0
_CLOCK_PROBE_TIMEOUT = 10
# If chrony's NTP offset and SSH wall-clock offset disagree by more
# than this, the guest is likely synced to an external source whose
# clock differs from the host.
_HOST_CHRONY_DISAGREE_MS = 500.0

_REF_ID_RE = re.compile(r"^Reference ID\s*:\s*([0-9A-Fa-f]+)\s*\(([^)]*)\)")
_SYS_TIME_RE = re.compile(
    r"^System time\s*:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"\s+seconds\s+(fast|slow)\b"
)
_LEAP_RE = re.compile(r"^Leap status\s*:\s*(.+)$")


def parse_chrony_tracking(text: str) -> dict[str, Any]:
    """Parse ``chronyc tracking`` output into lock state and signed offset.

    Returns a dict with ``available``, ``synchronized``, ``source``,
    ``offset_ms`` (positive = fast of NTP time, negative = slow), and
    ``leap_status``.
    """
    ref_hex: str | None = None
    source: str | None = None
    offset_ms: float | None = None
    leap_status: str | None = None

    for line in text.splitlines():
        line = line.strip()
        m = _REF_ID_RE.match(line)
        if m:
            ref_hex = m.group(1).upper()
            name = m.group(2).strip()
            if name:
                source = name
            elif ref_hex != "00000000":
                source = ref_hex
            continue
        m = _SYS_TIME_RE.match(line)
        if m:
            val_ms = float(m.group(1)) * 1000.0
            offset_ms = val_ms if m.group(2) == "fast" else -val_ms
            continue
        m = _LEAP_RE.match(line)
        if m:
            leap_status = m.group(1).strip()

    available = leap_status is not None or offset_ms is not None
    synchronized = (
        available
        and leap_status == "Normal"
        and ref_hex is not None
        and ref_hex != "00000000"
    )
    return {
        "available": available,
        "synchronized": synchronized,
        "source": source if synchronized else None,
        "offset_ms": offset_ms,
        "leap_status": leap_status,
    }


def _parse_guest_timestamp(raw: str) -> float | None:
    """Parse ``date +%s.%N`` (or integer ``date +%s``) output safely."""
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not line:
        return None
    if re.match(r"^\d+(?:\.\d+)?$", line):
        return float(line)
    # Minimal date implementations without %N print e.g. "1700000000.%N".
    m = re.match(r"^(\d+)", line)
    return float(m.group(1)) if m else None


def probe_node_clock(
    node: ClusterNode, *, timeout: int = _CLOCK_PROBE_TIMEOUT
) -> dict[str, Any]:
    """Probe one cluster node's chrony lock state and clock offset over SSH.

    A stopped, missing, or unresponsive node is reported as unreachable
    rather than waited for.
    """
    from .qemu_run import is_running
    from .vm_net import run_ssh
    from .vm_state import VMInfo, VMNotFound

    unreachable: dict[str, Any] = {
        "node": node.name,
        "reachable": False,
        "chrony": "unreachable",
        "synchronized": False,
        "source": None,
        "offset_ms": None,
        "chrony_offset_ms": None,
        "host_offset_ms": None,
        "rtt_ms": None,
    }
    try:
        vm = VMInfo.load(node.name)
    except (VMNotFound, ValueError):
        return unreachable
    if not is_running(vm):
        return unreachable

    # Use `chronyc -n` to skip reverse-DNS lookups on the refclock IP
    # (which can stall for 5-15s on distros without nss-myhostname) and
    # run `date` last so its timestamp sits right next to host `t1`,
    # avoiding SSH connect/auth handshake latency in `host_offset_ms`.
    cmd = "chronyc -n tracking 2>/dev/null; echo @@; date +%s.%N 2>/dev/null"
    t0 = time.time()
    try:
        r = run_ssh(vm.ip, cmd, timeout=timeout, retry_connect=False)
    except (OSError, subprocess.SubprocessError):
        return unreachable
    t1 = time.time()

    parts = (r.stdout or "").split("@@\n", 1)
    if r.returncode == 255 or len(parts) != 2:
        return unreachable

    tracking = parse_chrony_tracking(parts[0])
    guest_ts = _parse_guest_timestamp(parts[1])
    host_offset_ms = (
        round((guest_ts - t1) * 1000.0, 3) if guest_ts is not None else None
    )
    rtt_ms = max(0.0, (t1 - t0) * 1000.0)
    chrony_offset_ms = (
        round(tracking["offset_ms"], 3)
        if tracking["offset_ms"] is not None
        else None
    )
    if tracking["synchronized"]:
        chrony_state = "locked"
        if (
            chrony_offset_ms is not None
            and host_offset_ms is not None
            and abs(host_offset_ms - chrony_offset_ms)
            > max(_HOST_CHRONY_DISAGREE_MS, rtt_ms)
        ):
            # Wall clock jumped since chrony's last poll, or chrony is
            # locked to a source whose clock disagrees with the host.
            effective_offset: float | None = host_offset_ms
        elif chrony_offset_ms is not None:
            effective_offset = chrony_offset_ms
        else:
            effective_offset = host_offset_ms
    elif tracking["available"]:
        chrony_state = "unlocked"
        effective_offset = host_offset_ms
    else:
        chrony_state = "unavailable"
        effective_offset = host_offset_ms

    return {
        "node": node.name,
        "reachable": True,
        "chrony": chrony_state,
        "synchronized": tracking["synchronized"],
        "source": tracking["source"],
        "offset_ms": effective_offset,
        "chrony_offset_ms": chrony_offset_ms,
        "host_offset_ms": host_offset_ms,
        "rtt_ms": round(rtt_ms, 3),
    }


def _node_host_offset_ms(row: dict[str, Any]) -> float | None:
    """Best estimate of a reachable node's offset relative to the host (ms).

    Uses chrony's sub-ms offset when locked to the host SNTP responder,
    falling back to the SSH-bracketed wall-clock offset when unlocked,
    when chrony is absent, or when chrony's reference disagrees with the
    host by more than ``max(_HOST_CHRONY_DISAGREE_MS, rtt_ms)``.
    """
    if not row.get("reachable"):
        return None
    off = row.get("offset_ms")
    host_off = row.get("host_offset_ms")
    rtt_ms = float(row.get("rtt_ms") or 0.0)
    disagree_ms = max(_HOST_CHRONY_DISAGREE_MS, rtt_ms)
    if row.get("synchronized") and off is not None:
        if (
            host_off is not None
            and abs(float(host_off) - float(off)) > disagree_ms
        ):
            return float(host_off)
        return float(off)
    if host_off is not None:
        return float(host_off)
    return float(off) if off is not None else None


def _fmt_threshold_ms(val: float) -> str:
    if val >= 1000.0 and val % 1000.0 == 0:
        return f"{int(val // 1000)} s"
    if val >= 1000.0:
        return f"{val / 1000.0:.2f} s"
    return f"{val:.0f} ms"


def _fmt_offset_ms(val: float, *, signed: bool = True) -> str:
    sign = "+" if signed else ""
    mag = abs(val)
    if mag >= 10_000.0:
        return f"{val / 1000.0:{sign}.1f} s"
    if mag >= 1_000.0:
        return f"{val / 1000.0:{sign}.2f} s"
    if mag >= 1.0:
        return f"{val:{sign}.1f} ms"
    return f"{val:{sign}.3f} ms"


def summarize_clock_status(
    rows: list[dict[str, Any]],
    *,
    warn_threshold_ms: float = SKEW_WARN_MS,
) -> dict[str, Any]:
    """Compute inter-node and host-to-VM skew and warnings from node rows."""
    offsets = [
        float(r["offset_ms"])
        for r in rows
        if r.get("reachable") and r.get("offset_ms") is not None
    ]
    inter_node_skew_ms = (
        round(max(offsets) - min(offsets), 3) if len(offsets) >= 2 else None
    )

    host_offsets = [
        off for r in rows if (off := _node_host_offset_ms(r)) is not None
    ]
    max_host_offset_ms = (
        round(max(abs(o) for o in host_offsets), 3) if host_offsets else None
    )

    skew_candidates = [
        v for v in (inter_node_skew_ms, max_host_offset_ms) if v is not None
    ]
    max_skew_ms = max(skew_candidates) if skew_candidates else None

    warnings: list[str] = []
    unsynced = [
        str(r["node"])
        for r in rows
        if r.get("reachable") and not r.get("synchronized")
    ]
    if unsynced:
        warnings.append(f"unsynchronized clock on: {', '.join(unsynced)}")

    thr_str = _fmt_threshold_ms(warn_threshold_ms)
    if (
        inter_node_skew_ms is not None
        and inter_node_skew_ms > warn_threshold_ms
    ):
        warnings.append(
            f"inter-node clock skew "
            f"{_fmt_offset_ms(inter_node_skew_ms, signed=False)} "
            f"exceeds {thr_str}"
        )
    if (
        max_host_offset_ms is not None
        and max_host_offset_ms > warn_threshold_ms
    ):
        warnings.append(
            f"host-to-VM clock skew "
            f"{_fmt_offset_ms(max_host_offset_ms, signed=False)} "
            f"exceeds {thr_str}"
        )

    return {
        "nodes": rows,
        "max_skew_ms": max_skew_ms,
        "inter_node_skew_ms": inter_node_skew_ms,
        "max_host_offset_ms": max_host_offset_ms,
        "warn_threshold_ms": warn_threshold_ms,
        "warnings": warnings,
    }


def cluster_clock_status(
    cluster: ClusterInfo,
    *,
    warn_threshold_ms: float = SKEW_WARN_MS,
) -> dict[str, Any]:
    """Probe every cluster node's clock in parallel and summarize skew."""
    nodes = cluster.get_nodes()
    with ThreadPoolExecutor(max_workers=len(nodes) or 1) as pool:
        rows = list(pool.map(probe_node_clock, nodes))
    return summarize_clock_status(rows, warn_threshold_ms=warn_threshold_ms)


def print_clock_status(clock: dict[str, Any] | None) -> None:
    """Print the ``clock:`` section of ``ltvm cluster status``."""
    if not clock:
        return
    rows = clock.get("nodes") or []
    if not rows:
        return
    print()
    print("clock:")
    for row in rows:
        node = str(row["node"])
        if not row.get("reachable"):
            print(f"  {node:<20} unreachable")
            continue
        state = str(row.get("chrony") or "unknown")
        src = row.get("source")
        src_col = f"({src})" if src else ""
        off = row.get("offset_ms")
        off_col = _fmt_offset_ms(float(off)) if off is not None else "-"
        print(f"  {node:<20} {state:<12} {src_col:<16} {off_col}")

    inter_skew = clock.get("inter_node_skew_ms")
    host_skew = clock.get("max_host_offset_ms")
    max_skew = clock.get("max_skew_ms")
    threshold = float(clock.get("warn_threshold_ms", SKEW_WARN_MS))
    parts: list[str] = []
    if inter_skew is not None:
        parts.append(
            f"{_fmt_offset_ms(float(inter_skew), signed=False)} (inter-node)"
        )
    if host_skew is not None:
        parts.append(
            f"{_fmt_offset_ms(float(host_skew), signed=False)} (vs host)"
        )
    if parts:
        suffix = (
            f"  WARNING: exceeds {_fmt_threshold_ms(threshold)}"
            if max_skew is not None and float(max_skew) > threshold
            else ""
        )
        print(f"  max skew: {', '.join(parts)}{suffix}")

    for w in clock.get("warnings") or []:
        if parts and (
            w.startswith("inter-node clock skew")
            or w.startswith("host-to-VM clock skew")
        ):
            continue
        print(f"  WARNING: {w}")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ltvm_pkg.time_sync",
        description="Lightweight host SNTP responder for ltvm guests.",
    )
    parser.add_argument("--bind", required=True, help="Gateway IP to bind")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_TIME_PORT, help="UDP port"
    )
    parser.add_argument("--pidfile", type=Path, default=None)
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="Exit after this many idle seconds (0 to disable)",
    )
    args = parser.parse_args(argv)
    return serve(
        args.bind,
        args.port,
        pidfile=args.pidfile,
        idle_timeout=args.idle_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(_main())
