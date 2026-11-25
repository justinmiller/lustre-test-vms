"""Tests for guest clock synchronization (RTC, host SNTP responder, SSH sync, chrony)."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import qemu_run, time_sync, vm_cluster, vm_net

# Capture real functions before conftest's autouse fixture patches them.
_REAL_ENSURE_TIME_SERVER = time_sync.ensure_time_server
_REAL_SYNC_GUEST_CLOCK = time_sync.sync_guest_clock
_REAL_PROBE_TIME_SERVER = time_sync.probe_time_server
_REAL_STOP_TIME_SERVER = time_sync.stop_time_server
_REAL_STOP_ALL_TIME_SERVERS = time_sync.stop_all_time_servers

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestQemuRtcArgs:
    def test_x86_includes_driftfix_slew(self) -> None:
        assert qemu_run.rtc_args("x86_64") == [
            "-rtc",
            "base=utc,clock=host,driftfix=slew",
        ]

    def test_empty_arch_defaults_to_x86(self) -> None:
        assert qemu_run.rtc_args("") == [
            "-rtc",
            "base=utc,clock=host,driftfix=slew",
        ]

    def test_aarch64_omits_driftfix_slew(self) -> None:
        # pl031 on aarch64 virt warns if driftfix=slew is passed.
        assert qemu_run.rtc_args("aarch64") == ["-rtc", "base=utc,clock=host"]


class TestTimeSyncConfig:
    def test_default_port_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LTVM_TIME_PORT", raising=False)
        with patch("ltvm_pkg.time_sync.platform.system", return_value="Linux"):
            assert time_sync.configured_port() == time_sync.DEFAULT_TIME_PORT
            assert time_sync.cmdline_args() == ""

    def test_default_port_macos_adds_acqport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LTVM_TIME_PORT", raising=False)
        with patch("ltvm_pkg.time_sync.platform.system", return_value="Darwin"):
            assert (
                time_sync.cmdline_args()
                == f" fc_ntp_acqport={time_sync.DEFAULT_TIME_PORT}"
            )

    def test_custom_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LTVM_TIME_PORT", "123")
        with patch("ltvm_pkg.time_sync.platform.system", return_value="Linux"):
            assert time_sync.configured_port() == 123
            assert time_sync.cmdline_args() == " fc_ntp_port=123"
        with patch("ltvm_pkg.time_sync.platform.system", return_value="Darwin"):
            assert (
                time_sync.cmdline_args()
                == f" fc_ntp_port=123 fc_ntp_acqport={time_sync.DEFAULT_TIME_PORT}"
            )

    def test_zero_port_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LTVM_TIME_PORT", "0")
        monkeypatch.delenv("LTVM_TIME_SERVER", raising=False)
        assert time_sync.configured_port() == 0
        assert time_sync.server_enabled() is False
        assert time_sync.cmdline_args() == " fc_ntp_port=0"

    @pytest.mark.parametrize("bad", ["notanumber", "-1", "70000"])
    def test_invalid_port_falls_back(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_TIME_PORT", bad)
        assert time_sync.configured_port() == time_sync.DEFAULT_TIME_PORT

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
    def test_server_disabled_via_env(
        self, val: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LTVM_TIME_SERVER", val)
        assert time_sync.server_enabled() is False
        assert time_sync.cmdline_args() == " fc_ntp_port=0"

    def test_allowed_network_scoping(self) -> None:
        assert time_sync._allowed_network("0.0.0.0") is None
        assert str(time_sync._allowed_network("127.0.0.1")) == "127.0.0.0/8"
        net = time_sync._allowed_network("192.168.105.1")
        assert str(net) == "192.168.105.0/24"
        assert net is not None
        import ipaddress

        assert ipaddress.IPv4Address("192.168.105.42") in net
        assert ipaddress.IPv4Address("10.0.0.5") not in net

    def test_server_enabled_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LTVM_TIME_SERVER", raising=False)
        monkeypatch.delenv("LTVM_TIME_PORT", raising=False)
        assert time_sync.server_enabled() is True

    def test_server_python_prefers_apple_signed_on_macos(self) -> None:
        with (
            patch("ltvm_pkg.time_sync.platform.system", return_value="Darwin"),
            patch("ltvm_pkg.time_sync.os.access", return_value=True),
        ):
            assert time_sync._server_python() == "/usr/bin/python3"

        with patch("ltvm_pkg.time_sync.platform.system", return_value="Linux"):
            assert time_sync._server_python() == time_sync.sys.executable


class TestSntpCodec:
    def test_ns_ntp_roundtrip(self) -> None:
        ns = 1_763_940_000_123_456_789
        sec, frac = time_sync.ns_to_ntp(ns)
        rt = time_sync.ntp_to_ns(sec, frac)
        assert abs(rt - ns) <= 1  # 32-bit fractional second ~= 0.23 ns

    @pytest.mark.parametrize("version", [3, 4])
    def test_build_sntp_reply_valid(self, version: int) -> None:
        req = bytearray(48)
        req[0] = (0 << 6) | (version << 3) | 3  # client mode
        req[2] = 4  # poll
        struct.pack_into("!II", req, 40, 0xDEADBEEF, 0x12345678)

        rx_ns = 1_763_940_000_500_000_000
        tx_ns = rx_ns + 50_000
        reply = time_sync.build_sntp_reply(bytes(req), rx_ns, tx_ns)
        assert reply is not None
        assert len(reply) == 48

        li_vn_mode, stratum, poll, precision, rdel, rdisp, refid = (
            struct.unpack_from("!BBbbII4s", reply, 0)
        )
        assert (li_vn_mode >> 6) == 0  # LI = no warning
        assert ((li_vn_mode >> 3) & 0x7) == version
        assert (li_vn_mode & 0x7) == 4  # server mode
        assert stratum == 2
        assert poll == 4
        assert precision < 0
        assert rdel == 0 and rdisp == 0
        assert refid == b"LTVM"

        # Origin timestamp echoes client transmit timestamp verbatim.
        assert reply[24:32] == bytes(req[40:48])
        # Reference, receive, and transmit timestamps match host time.
        ref_sec, ref_frac = struct.unpack_from("!II", reply, 16)
        rx_sec, rx_frac = struct.unpack_from("!II", reply, 32)
        tx_sec, tx_frac = struct.unpack_from("!II", reply, 40)
        assert abs(time_sync.ntp_to_ns(ref_sec, ref_frac) - rx_ns) <= 1
        assert abs(time_sync.ntp_to_ns(rx_sec, rx_frac) - rx_ns) <= 1
        assert abs(time_sync.ntp_to_ns(tx_sec, tx_frac) - tx_ns) <= 1

    def test_build_sntp_reply_rejects_bad_packets(self) -> None:
        now = time.time_ns()
        assert time_sync.build_sntp_reply(b"short", now, now) is None
        # Mode 4 (server) must not be answered (avoids reflection loops).
        srv = bytearray(48)
        srv[0] = (0 << 6) | (4 << 3) | 4
        assert time_sync.build_sntp_reply(bytes(srv), now, now) is None
        # Invalid version 0 or 7
        bad_vn = bytearray(48)
        bad_vn[0] = (0 << 6) | (0 << 3) | 3
        assert time_sync.build_sntp_reply(bytes(bad_vn), now, now) is None

    @pytest.mark.parametrize("poll_val", [0, 4, 127, 128, 255])
    def test_build_sntp_reply_handles_unsigned_poll(
        self, poll_val: int
    ) -> None:
        req = bytearray(48)
        req[0] = (0 << 6) | (4 << 3) | 3
        req[2] = poll_val
        reply = time_sync.build_sntp_reply(
            bytes(req), 1_763_940_000_000_000_000, 1_763_940_000_001_000_000
        )
        assert reply is not None
        assert reply[2] == poll_val


class TestSntpServerLoopback:
    def test_serve_and_probe_and_idle_exit(self, tmp_path: Path) -> None:
        # Reserve an ephemeral localhost UDP port, then hand it to serve().
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        pidfile = tmp_path / "timesync.pid"
        rc_holder: list[int] = []

        def _run() -> None:
            # signal.signal only works in main thread; patch it inside thread.
            with patch("ltvm_pkg.time_sync.signal.signal"):
                rc_holder.append(
                    time_sync.serve(
                        "127.0.0.1", port, pidfile=pidfile, idle_timeout=0.35
                    )
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if _REAL_PROBE_TIME_SERVER("127.0.0.1", port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("SNTP server did not respond to probe")

        assert pidfile.exists()
        # Server exits cleanly once idle_timeout elapses.
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert rc_holder == [0]
        assert not pidfile.exists()

    def test_ensure_and_stop_time_server_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("LTVM_TIME_SERVER", "1")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        monkeypatch.setenv("LTVM_TIME_PORT", str(port))

        with (
            patch(
                "ltvm_pkg.time_sync.probe_time_server",
                side_effect=_REAL_PROBE_TIME_SERVER,
            ),
            patch(
                "ltvm_pkg.time_sync.stop_time_server",
                side_effect=_REAL_STOP_TIME_SERVER,
            ),
        ):
            assert _REAL_ENSURE_TIME_SERVER(
                "127.0.0.1", port, startup_timeout=3.0
            )
            try:
                sd = time_sync.state_dir()
                assert sd.stat().st_mode & 0o777 == 0o700
                pf = time_sync.pidfile_path(port)
                assert pf.is_file()
                pid = int(pf.read_text().strip())
                assert time_sync._pid_is_time_server(pid)
                assert not time_sync._pid_is_time_server(
                    pid, expected_uid=pid + 99999
                )

                # SIGTERM wakes serve() immediately (not after 5s timeout).
                t0 = time.monotonic()
                assert _REAL_STOP_TIME_SERVER(port) is True
                assert time.monotonic() - t0 < 1.5
                assert not pf.exists()
            finally:
                _REAL_STOP_TIME_SERVER(port)

    def test_state_dir_rejects_symlink_without_chmodding_target(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        xdg = tmp_path / "xdg"
        (xdg / "ltvm").mkdir(parents=True)
        (xdg / "ltvm" / "timesync").symlink_to(target)
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
        with pytest.raises(OSError):
            time_sync.state_dir(create=True)
        # Target directory permissions must not have been altered.
        assert target.stat().st_mode & 0o777 == 0o755

    def test_state_dir_ignores_sudo_user_home_for_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-xdg"))
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setattr(time_sync.os, "getuid", lambda: 0)
        fake_pw = MagicMock(pw_dir="/root")
        monkeypatch.setattr(time_sync.pwd, "getpwuid", lambda _u: fake_pw)
        assert time_sync.state_dir(0) == Path(
            "/root/.local/state/ltvm/timesync"
        )

    def test_open_nofollow_respects_expected_uid_and_rejects_symlink(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "pid"
        f.write_text("123\n")
        sym = tmp_path / "sym"
        sym.symlink_to(f)
        with pytest.raises(OSError):
            time_sync._open_nofollow(sym, os.O_RDONLY)
        with pytest.raises(OSError):
            time_sync._open_nofollow(
                f, os.O_RDONLY, expected_uid=os.getuid() + 99999
            )
        fd = time_sync._open_nofollow(f, os.O_RDONLY, expected_uid=os.getuid())
        os.close(fd)

    def test_stop_all_time_servers_scans_state_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        d = time_sync.state_dir(create=True)
        (d / "timesync-12300.pid").write_text("111\n")
        (d / "timesync-12345.pid").write_text("222\n")
        (d / "timesync-notaport.pid").write_text("333\n")
        seen: list[int] = []

        def fake_stop(port: int, *, uid: int | None = None) -> bool:
            seen.append(port)
            return port == 12300

        with patch(
            "ltvm_pkg.time_sync.stop_time_server", side_effect=fake_stop
        ):
            stopped = _REAL_STOP_ALL_TIME_SERVERS()
        assert seen == [12300, 12345]
        assert stopped == [12300]

    def test_ensure_time_server_passes_isolated_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("LTVM_TIME_SERVER", "1")
        monkeypatch.setenv("LTVM_TIME_PORT", "12399")
        launched: list[tuple[list[str], dict[str, object]]] = []

        def fake_popen(cmd, **kw):
            launched.append((list(cmd), dict(kw)))
            m = MagicMock()
            m.poll.return_value = None
            return m

        calls = iter([False, False, True])
        with (
            patch(
                "ltvm_pkg.time_sync.probe_time_server",
                side_effect=lambda *a, **k: next(calls),
            ),
            patch(
                "ltvm_pkg.time_sync.subprocess.Popen", side_effect=fake_popen
            ),
        ):
            assert _REAL_ENSURE_TIME_SERVER("127.0.0.1", 12399) is True
        assert len(launched) == 1
        assert launched[0][0][1] == "-I"
        assert launched[0][1].get("cwd") == "/"

    def test_host_uninstall_stops_all_user_and_root_responders(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from ltvm_pkg import host_uninstall

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        with (
            patch(
                "ltvm_pkg.time_sync.stop_all_time_servers",
                return_value=[12300, 12345],
            ),
            patch("ltvm_pkg.host_uninstall.os.geteuid", return_value=1000),
            patch("ltvm_pkg.time_sync.probe_time_server", return_value=True),
            patch(
                "ltvm_pkg.host_uninstall.sudo_run",
                return_value=subprocess.CompletedProcess([], 0, "12300\n", ""),
            ) as m_sudo,
        ):
            msg = host_uninstall._time_sync_responder()
        assert msg == "stopped (port 12300, port 12345, root port 12300)"
        assert "-I" in m_sudo.call_args[0][0]


class TestSyncGuestClock:
    def test_build_sync_clock_cmd_formats_utc_epoch(self) -> None:
        assert (
            time_sync.build_sync_clock_cmd(1700000000.5)
            == "date -u -s @1700000000.500000 >/dev/null"
        )

    def test_locked_guest_not_stepped_under_high_ssh_latency(self) -> None:
        """High SSH latency on a loaded host must never step a locked guest."""
        # t0=1000.0, SSH takes 2.0s (t1=1002.0), guest is within 1 ms of host
        # (guest_ts=1001.8 when `date` ran near end of SSH call).
        probe_out = (
            "Reference ID    : C0A86901 (192.168.105.1)\n"
            "System time     : 0.000412345 seconds fast of NTP time\n"
            "Leap status     : Normal\n"
            "@@\n"
            "1001.800000000\n"
        )
        times = iter([1000.0, 1002.0])
        with (
            patch(
                "ltvm_pkg.time_sync.time.time", side_effect=lambda: next(times)
            ),
            patch(
                "ltvm_pkg.vm_net.run_ssh",
                return_value=MagicMock(
                    returncode=0, stdout=probe_out, stderr=""
                ),
            ) as m_ssh,
        ):
            assert _REAL_SYNC_GUEST_CLOCK("192.168.100.5") is True
        # Only the probe ran; no `date -u -s` step was issued.
        assert m_ssh.call_count == 1

    @pytest.mark.parametrize(
        "tracking,guest_ts,t0,t1",
        [
            # Post-sleep / snapshot restore: chrony still reports Normal/0.4ms,
            # but guest wall clock is 5s behind host -> must step.
            (
                "Reference ID    : C0A86901 (192.168.105.1)\n"
                "System time     : 0.000412345 seconds fast of NTP time\n"
                "Leap status     : Normal\n",
                "995.000000000",
                1000.0,
                1000.4,
            ),
            # Large offset reported by chrony -> must step.
            (
                "Reference ID    : C0A86901 (192.168.105.1)\n"
                "System time     : 3600.123456 seconds slow of NTP time\n"
                "Leap status     : Normal\n",
                "1000.200000000",
                1000.0,
                1000.4,
            ),
            # Fresh boot / not yet synchronized -> must step.
            (
                "Reference ID    : 00000000 ()\n"
                "System time     : 0.000000000 seconds fast of NTP time\n"
                "Leap status     : Not synchronised\n",
                "1000.200000000",
                1000.0,
                1000.4,
            ),
            # Missing chronyc -> empty tracking output -> must step.
            ("", "1000.200000000", 1000.0, 1000.4),
        ],
    )
    def test_steps_with_half_rtt_compensation_when_needed(
        self,
        tracking: str,
        guest_ts: str,
        t0: float,
        t1: float,
    ) -> None:
        probe_out = f"{tracking}@@\n{guest_ts}\n"
        times = iter([t0, t1, 1000.5])
        with (
            patch(
                "ltvm_pkg.time_sync.time.time", side_effect=lambda: next(times)
            ),
            patch(
                "ltvm_pkg.vm_net.run_ssh",
                side_effect=[
                    MagicMock(returncode=0, stdout=probe_out, stderr=""),
                    MagicMock(returncode=0, stdout="", stderr=""),
                ],
            ) as m_ssh,
        ):
            assert _REAL_SYNC_GUEST_CLOCK("192.168.100.5") is True
        assert m_ssh.call_count == 2
        # Step timestamp = 1000.5 + rtt/2 = 1000.5 + 0.2 = 1000.7
        assert (
            m_ssh.call_args_list[1][0][1]
            == "date -u -s @1000.700000 >/dev/null"
        )

    def test_sync_guest_clock_non_fatal_on_failure(self) -> None:
        # SSH failure during probe (rc=255) returns False without stepping.
        with patch(
            "ltvm_pkg.vm_net.run_ssh",
            return_value=MagicMock(
                returncode=255, stdout="", stderr="ssh error"
            ),
        ) as m_ssh:
            assert _REAL_SYNC_GUEST_CLOCK("192.168.100.5") is False
            assert m_ssh.call_count == 1

        # Timeout during probe returns False.
        with patch(
            "ltvm_pkg.vm_net.run_ssh",
            side_effect=subprocess.TimeoutExpired("ssh", 10),
        ):
            assert _REAL_SYNC_GUEST_CLOCK("192.168.100.5") is False

        # Step command failing returns False.
        with patch(
            "ltvm_pkg.vm_net.run_ssh",
            side_effect=[
                MagicMock(returncode=0, stdout="@@\n1000.0\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="date failed"),
            ],
        ):
            assert _REAL_SYNC_GUEST_CLOCK("192.168.100.5") is False


class TestLifecycleWiring:
    def test_provision_vm_ssh_syncs_clock(self) -> None:
        vm = MagicMock()
        vm.name = "co1-test"
        vm.ip = "192.168.100.42"
        with (
            patch("ltvm_pkg.vm_net.wait_for_ssh"),
            patch("ltvm_pkg.vm_net.register_ssh_name"),
            patch("ltvm_pkg.vm_net.deploy_ssh_key"),
            patch("ltvm_pkg.time_sync.sync_guest_clock") as m_sync,
        ):
            vm_net.provision_vm_ssh(vm, timeout=30)
        m_sync.assert_called_once_with("192.168.100.42")

    def test_sync_cluster_clocks_fans_out_and_is_non_fatal(self) -> None:
        n1, n2 = MagicMock(), MagicMock()
        n1.name, n2.name = "co1-mds", "co1-oss"
        ips = {"co1-mds": "192.168.100.10", "co1-oss": "192.168.100.11"}
        with (
            patch("ltvm_pkg.time_sync.ensure_time_server") as m_ensure,
            patch(
                "ltvm_pkg.time_sync.sync_guest_clock",
                side_effect=lambda ip: ip == "192.168.100.10",
            ) as m_sync,
        ):
            failed = vm_cluster._sync_cluster_clocks([n1, n2], ips)
        m_ensure.assert_called_once()
        assert m_sync.call_count == 2
        assert failed == ["co1-oss"]


class TestImageChronyWiring:
    def test_chrony_in_packages_base(self) -> None:
        pkgs = [
            line.strip()
            for line in (REPO_ROOT / "targets/common/packages-base.txt")
            .read_text()
            .splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert "chrony" in pkgs

    def test_rc_local_chrony_section_syntax_and_config(
        self, tmp_path: Path
    ) -> None:
        rc = (REPO_ROOT / "targets/common/rc.local").read_text()
        # Verify valid bash syntax for the whole script.
        r = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "targets/common/rc.local")],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "server ${FC_GW} port ${FC_NTP_PORT:-12300}" in rc
        assert "acquisitionport ${FC_NTP_ACQPORT}" in rc
        assert "makestep 1.0 -1" in rc
        assert "modprobe ptp_kvm" in rc
        assert "KVM virtual PTP" in rc
        assert "refclock PHC /dev/${_ptp##*/}" in rc
        assert "systemctl restart --no-block" in rc

        # Execute the extracted chrony block with stubbed /proc/cmdline,
        # /sys/class/ptp, and /etc/chrony.conf for Linux, macOS (acqport),
        # passthrough-NIC vs KVM PTP selection, and disabled (port=0).
        start = rc.index("# --- Time synchronization")
        end = rc.index("# --- Extra NICs")
        block = rc[start:end]

        def run_block(
            cmdline: str, ptp_clocks: dict[str, str] | None = None
        ) -> str:
            cmdline_f = tmp_path / "cmdline"
            cmdline_f.write_text(cmdline + "\n")
            conf = tmp_path / "chrony.conf"
            conf.write_text("# stock\n")
            ptp_root = tmp_path / "sys_ptp"
            if ptp_root.exists():
                import shutil

                shutil.rmtree(ptp_root)
            ptp_root.mkdir()
            for dev, cname in (ptp_clocks or {}).items():
                d = ptp_root / dev
                d.mkdir()
                (d / "clock_name").write_text(cname + "\n")
            patched = (
                block.replace("/proc/cmdline", str(cmdline_f))
                .replace("/sys/class/ptp", str(ptp_root))
                .replace("/etc/chrony.conf", str(conf))
                .replace("[ -d /etc/chrony ]", "false")
            )
            script = (
                "FC_GW=192.168.105.1\n"
                "chronyd() { return 0; }\n"
                "systemctl() { return 0; }\n"
                f"{patched}\n"
            )
            subprocess.run(["bash", "-c", script], check=True)
            return conf.read_text()

        linux_conf = run_block("fc_gw=192.168.105.1")
        assert "server 192.168.105.1 port 12300" in linux_conf
        assert "acquisitionport" not in linux_conf
        assert "refclock PHC" not in linux_conf

        # Passthrough NIC ptp0 must NOT be used as a PHC refclock.
        nic_only_conf = run_block("fc_gw=192.168.105.1", {"ptp0": "mlx5_ptp"})
        assert "refclock PHC" not in nic_only_conf

        # KVM virtual PTP on ptp1 IS selected while NIC ptp0 is skipped.
        kvm_ptp_conf = run_block(
            "fc_gw=192.168.105.1",
            {"ptp0": "mlx5_ptp", "ptp1": "KVM virtual PTP"},
        )
        assert (
            "refclock PHC /dev/ptp1 poll 2 dpoll -2 offset 0 prefer"
            in kvm_ptp_conf
        )
        assert "/dev/ptp0" not in kvm_ptp_conf

        macos_conf = run_block("fc_gw=192.168.105.1 fc_ntp_acqport=12300")
        assert "server 192.168.105.1 port 12300" in macos_conf
        assert "acquisitionport 12300" in macos_conf

        disabled_conf = run_block("fc_gw=192.168.105.1 fc_ntp_port=0")
        assert disabled_conf == "# stock\n"


_CHRONY_LOCKED_FAST = """\
Reference ID    : C0A86901 (_gateway)
Stratum         : 3
Ref time (UTC)  : Tue Nov 24 14:43:39 2026
System time     : 0.000049123 seconds fast of NTP time
Last offset     : +0.000041200 seconds
RMS offset      : 0.000050100 seconds
Frequency       : 1.234 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.123 ppm
Root delay      : 0.000400000 seconds
Root dispersion : 0.000100000 seconds
Update interval : 16.0 seconds
Leap status     : Normal
"""

_CHRONY_LOCKED_SLOW = """\
Reference ID    : 50484330 (PHC0)
Stratum         : 1
System time     : 0.000250000 seconds slow of NTP time
Leap status     : Normal
"""

_CHRONY_UNLOCKED = """\
Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Leap status     : Not synchronised
"""


class TestChronyTrackingParser:
    def test_parse_chrony_locked_fast(self) -> None:
        info = time_sync.parse_chrony_tracking(_CHRONY_LOCKED_FAST)
        assert info["available"] is True
        assert info["synchronized"] is True
        assert info["source"] == "_gateway"
        assert info["offset_ms"] == pytest.approx(0.049123)
        assert info["leap_status"] == "Normal"

    def test_parse_chrony_locked_slow_phc(self) -> None:
        info = time_sync.parse_chrony_tracking(_CHRONY_LOCKED_SLOW)
        assert info["available"] is True
        assert info["synchronized"] is True
        assert info["source"] == "PHC0"
        assert info["offset_ms"] == pytest.approx(-0.25)

    def test_parse_chrony_unlocked(self) -> None:
        info = time_sync.parse_chrony_tracking(_CHRONY_UNLOCKED)
        assert info["available"] is True
        assert info["synchronized"] is False
        assert info["source"] is None
        assert info["leap_status"] == "Not synchronised"

    @pytest.mark.parametrize("text", ["", "506 Cannot talk to daemon\n"])
    def test_parse_chrony_unavailable(self, text: str) -> None:
        info = time_sync.parse_chrony_tracking(text)
        assert info["available"] is False
        assert info["synchronized"] is False
        assert info["source"] is None
        assert info["offset_ms"] is None
