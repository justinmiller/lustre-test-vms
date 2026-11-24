"""Unit tests for build_caches: podman/buildah --mount=type=cache mounts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ltvm_pkg import build_caches


def _cp(
    rc: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def podman_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_caches, "_podman_available", lambda: True)


class TestKnownCacheIds:
    def test_matches_buildah_sha256_layout(self) -> None:
        # Verified empirically against buildah 1.45.2 / podman 5.x.
        assert (
            build_caches.cache_dir_hash("ltvm-rdma-core") == "a55c98ecd7467cad"
        )
        assert (
            build_caches.cache_dir_hash("ltvm-build-tools")
            == "f790817c28451139"
        )
        assert (
            build_caches.cache_dir_hash("ltvm-apt-debian13")
            == "753ad0166846d169"
        )

    def test_includes_shared_per_target_and_dockerfile_ids(
        self, tmp_path: Path
    ) -> None:
        tdir = tmp_path / "targets"
        (tdir / "custom").mkdir(parents=True)
        (tdir / "custom" / "image.Dockerfile").write_text(
            "RUN --mount=id=ltvm-custom-xyz,type=cache,target=/var/cache/x true\n"
            "RUN --mount=type=cache,id=ltvm-custom-locked,target=/var/cache/l,sharing=locked true\n"
            "RUN --mount=type=cache,id=ltvm-apt-rocky9,target=/var/cache/apt,sharing=shared true\n"
            "RUN --mount=type=cache,id=other-project,target=/tmp/y true\n"
        )
        import ltvm_pkg.target_config as cfg

        with (
            patch.object(cfg, "TARGETS_DIR", tdir),
            patch.object(
                cfg, "list_targets", return_value=["debian13", "rocky9"]
            ),
        ):
            known = build_caches.known_cache_ids()

        assert known[build_caches.cache_dir_hash("ltvm-build-tools")] == (
            "ltvm-build-tools",
            None,
            "shared tool download cache",
            "shared",
        )
        assert known[build_caches.cache_dir_hash("ltvm-rdma-core")] == (
            "ltvm-rdma-core",
            None,
            "shared rdma-core tarball cache",
            "shared",
        )
        assert known[build_caches.cache_dir_hash("ltvm-apt-debian13")] == (
            "ltvm-apt-debian13",
            "debian13",
            "apt package cache (debian13)",
            "locked",
        )
        # Explicit sharing=shared in a Dockerfile overrides per-target default.
        assert known[build_caches.cache_dir_hash("ltvm-apt-rocky9")][3] == (
            "shared"
        )
        assert known[build_caches.cache_dir_hash("ltvm-dnf-rocky9")] == (
            "ltvm-dnf-rocky9",
            "rocky9",
            "dnf package cache (rocky9)",
            "locked",
        )
        # Omitted sharing= is treated conservatively as shared.
        assert known[build_caches.cache_dir_hash("ltvm-custom-xyz")][3] == (
            "shared"
        )
        assert known[build_caches.cache_dir_hash("ltvm-custom-locked")][3] == (
            "locked"
        )
        # Non-ltvm IDs are never registered.
        assert build_caches.cache_dir_hash("other-project") not in known
        # Unhashed fallback works for older buildah too.
        assert known["ltvm-build-tools"][0] == "ltvm-build-tools"


class TestListCachesLinux:
    def test_lists_only_ltvm_caches_sorted_largest_first(
        self,
        tmp_path: Path,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(build_caches.os, "getuid", lambda: 1000)
        monkeypatch.setattr(build_caches.os, "geteuid", lambda: 1000)

        cache_root = tmp_path / "buildah-cache-1000"
        apt_dir = cache_root / build_caches.cache_dir_hash("ltvm-apt-debian13")
        tools_dir = cache_root / build_caches.cache_dir_hash("ltvm-build-tools")
        other_dir = cache_root / "deadbeef00001111"
        lock_dir = cache_root / "buildah-cache-lockfiles"
        for d in (apt_dir, tools_dir, other_dir, lock_dir):
            d.mkdir(parents=True)

        def fake_podman(
            *args: str, input: str | None = None, timeout: float | None = None
        ) -> Any:
            if args[:2] == ("info", "--format"):
                return _cp(out=f"{tmp_path}\n")
            raise AssertionError(f"unexpected podman {args}")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            assert cmd[:4] == ["podman", "unshare", "sh", "-c"]
            return _cp(
                out=f"{apt_dir}\t600000000\t1700000100\n"
                f"{tools_dir}\t54000000\t1700000200\n"
            )

        monkeypatch.setattr(build_caches, "_podman", fake_podman)
        monkeypatch.setattr(build_caches.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "ltvm_pkg.host_setup.hold_podman_usage", lambda: None
        )

        entries = build_caches.list_caches()
        assert entries is not None
        assert [(e.cache_id, e.bytes, e.target) for e in entries] == [
            ("ltvm-apt-debian13", 600_000_000, "debian13"),
            ("ltvm-build-tools", 54_000_000, None),
        ]

    def test_returns_empty_when_cache_root_missing(
        self,
        tmp_path: Path,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out=f"{tmp_path}\n")
        )
        assert build_caches.list_caches() == []

    def test_returns_none_when_podman_info_fails(
        self,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(rc=125)
        )
        assert build_caches.list_caches() is None

    def test_rejects_go_no_value_and_falls_back_to_tmpdir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(build_caches.os, "getuid", lambda: 1000)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out="<no value>\n")
        )
        assert (
            build_caches._linux_cache_root() == tmp_path / "buildah-cache-1000"
        )

    def test_recursive_mtime_and_symlink_skip(
        self,
        tmp_path: Path,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(build_caches.os, "getuid", lambda: 1000)
        monkeypatch.setattr(build_caches.os, "geteuid", lambda: 0)
        root = tmp_path / "buildah-cache-1000"
        apt_h = build_caches.cache_dir_hash("ltvm-apt-debian13")
        sym_h = build_caches.cache_dir_hash("ltvm-build-tools")
        deep = root / apt_h / "archives" / "partial"
        deep.mkdir(parents=True)
        old_ts = 1_600_000_000.0
        new_ts = 1_750_000_000.0
        deb = deep / "pkg.deb"
        deb.write_bytes(b"deb")
        os.utime(deb, (new_ts, new_ts))
        for d in (deep, deep.parent, root / apt_h):
            os.utime(d, (old_ts, old_ts))
        # Symlink in cache root must be ignored.
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / sym_h).symlink_to(outside)

        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out=f"{tmp_path}\n")
        )
        # Force fallback path so it runs identically on macOS & Linux hosts.
        monkeypatch.setattr(
            build_caches.subprocess,
            "run",
            lambda *a, **k: _cp(rc=1),
        )
        entries = build_caches.list_caches()
        assert entries is not None
        assert [e.cache_id for e in entries] == ["ltvm-apt-debian13"]
        assert entries[0].mtime == pytest.approx(new_ts, abs=1.0)


class TestListCachesMacOS:
    def test_macos_scripts_have_valid_sh_syntax_and_rootful_guard(self) -> None:
        for script in (
            build_caches._MACOS_INSPECT_SCRIPT,
            build_caches._MACOS_REMOVE_SCRIPT,
        ):
            r = subprocess.run(
                ["sh", "-n"], input=script, capture_output=True, text=True
            )
            assert r.returncode == 0, r.stderr
            assert 'if [ "$(id -u)" -eq 0 ]' in script
        assert "--fcntl" in build_caches._MACOS_REMOVE_SCRIPT
        assert 'rm -rf -- "$lockdir"' not in build_caches._MACOS_REMOVE_SCRIPT

    def test_handles_null_json_from_podman_machine_list(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: True)
        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out="null\n")
        )
        assert build_caches._running_machine_name() is None
        assert build_caches.list_caches() is None

    def test_never_starts_stopped_podman_machine(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: True)
        calls: list[tuple[str, ...]] = []

        def fake_podman(
            *args: str, input: str | None = None, timeout: float | None = None
        ) -> Any:
            calls.append(args)
            if args[:2] == ("machine", "list"):
                return _cp(out=json.dumps([{"Name": "m", "Running": False}]))
            raise AssertionError(
                f"unexpected podman call on stopped machine: {args}"
            )

        monkeypatch.setattr(build_caches, "_podman", fake_podman)
        assert build_caches.list_caches() is None
        assert calls == [("machine", "list", "--format", "json")]

    def test_parses_ssh_output_and_passes_known_names(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: True)
        apt_h = build_caches.cache_dir_hash("ltvm-apt-debian13")
        rdma_h = build_caches.cache_dir_hash("ltvm-rdma-core")
        ssh_out = (
            f"0968be5f8ce10146\t621000000\t1700000000\n"
            f"{apt_h}\t610000000\t1700000100\n"
            f"{rdma_h}\t4200000\t1700000200\n"
        )
        ssh_args: list[tuple[str, ...]] = []

        def fake_podman(
            *args: str, input: str | None = None, timeout: float | None = None
        ) -> Any:
            if args[:2] == ("machine", "list"):
                return _cp(
                    out=json.dumps(
                        [{"Name": "pm0", "Default": True, "Running": True}]
                    )
                )
            if args[:3] == ("machine", "ssh", "pm0"):
                ssh_args.append(args)
                return _cp(out=ssh_out)
            raise AssertionError(f"unexpected podman {args}")

        monkeypatch.setattr(build_caches, "_podman", fake_podman)
        entries = build_caches.list_caches()
        assert entries is not None
        assert [(e.cache_id, e.bytes) for e in entries] == [
            ("ltvm-apt-debian13", 610_000_000),
            ("ltvm-rdma-core", 4_200_000),
        ]
        # Known dir names are passed as args so unrelated caches are never du'd.
        assert ssh_args and ssh_args[0][3:6] == ("sh", "-s", "--")
        assert apt_h in ssh_args[0][6:]
        assert rdma_h in ssh_args[0][6:]


class TestRemoveCaches:
    def test_linux_skips_fcntl_locked_cache_and_keeps_lockdir(
        self,
        tmp_path: Path,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sys

        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(build_caches.os, "getuid", lambda: 1000)
        monkeypatch.setattr(build_caches.os, "geteuid", lambda: 0)

        root = tmp_path / "buildah-cache-1000"
        h_busy = build_caches.cache_dir_hash("ltvm-apt-debian13")
        h_free = build_caches.cache_dir_hash("ltvm-apt-ubuntu2404")
        (root / h_busy).mkdir(parents=True)
        (root / h_free).mkdir(parents=True)
        lock_busy = (
            root / "buildah-cache-lockfiles" / h_busy / "buildah-cache-lockfile"
        )
        lock_busy.parent.mkdir(parents=True)
        lock_busy.write_bytes(b"")
        lock_free = (
            root / "buildah-cache-lockfiles" / h_free / "buildah-cache-lockfile"
        )
        lock_free.parent.mkdir(parents=True)
        lock_free.write_bytes(b"")

        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out=f"{tmp_path}\n")
        )

        e_busy = build_caches.CacheEntry(
            "ltvm-apt-debian13", h_busy, 100, 0.0, "debian13", "apt"
        )
        e_free = build_caches.CacheEntry(
            "ltvm-apt-ubuntu2404", h_free, 50, 0.0, "ubuntu2404", "apt"
        )

        # Hold a POSIX fcntl lock in a child process (matching Buildah's
        # containers/storage/pkg/lockfile).
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys; "
                "f = open(sys.argv[1], 'r+b'); "
                "fcntl.lockf(f.fileno(), fcntl.LOCK_EX); "
                "print('LOCKED', flush=True); "
                "sys.stdin.read()",
                str(lock_busy),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "LOCKED"
            res = build_caches.remove_caches([e_busy, e_free])
        finally:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=5)

        assert res[h_busy] == (False, "in use by a running build")
        assert res[h_free] == (True, None)
        assert (root / h_busy).exists()
        assert not (root / h_free).exists()
        # Lockdir stays in place to preserve Buildah's lock inode invariant.
        assert lock_free.is_file()

    def test_refuses_unsafe_dir_names(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        for bad_name in ("../escape", "-rf", ".hidden", "abc\n"):
            bad = build_caches.CacheEntry("bad", bad_name, 1, 0.0, None, "bad")
            res = build_caches.remove_caches([bad])
            assert res[bad_name][0] is False
            assert "unsafe" in (res[bad_name][1] or "")

    def test_linux_shared_cache_ignores_leftover_lockfile_and_checks_usage_lock(
        self,
        tmp_path: Path,
        podman_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Leftover buildah lockfiles for sharing=shared caches must not fool
        remove_caches into deleting while another ltvm build holds the podman
        usage lock."""
        import sys

        import ltvm_pkg.host_setup as hs

        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(build_caches.os, "getuid", lambda: 1000)
        monkeypatch.setattr(build_caches.os, "geteuid", lambda: 0)

        root = tmp_path / "buildah-cache-1000"
        h_tools = build_caches.cache_dir_hash("ltvm-build-tools")
        (root / h_tools).mkdir(parents=True)
        # Plant an UNLOCKED leftover buildah lockfile (as buildah leaves behind
        # when a cache previously used sharing=locked).
        leftover = (
            root
            / "buildah-cache-lockfiles"
            / h_tools
            / "buildah-cache-lockfile"
        )
        leftover.parent.mkdir(parents=True)
        leftover.write_bytes(b"")

        entry = build_caches.CacheEntry(
            "ltvm-build-tools", h_tools, 100, 0.0, None, "shared tool cache"
        )
        assert entry.sharing == "shared"

        monkeypatch.setattr(
            build_caches, "_podman", lambda *a, **k: _cp(out=f"{tmp_path}\n")
        )

        # Simulate another ltvm process running `podman build` (holding the
        # podman usage lock shared).
        usage_lock = hs._podman_lock_path()
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; "
                "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_SH); "
                "print('HELD', flush=True); "
                "sys.stdin.read()",
                str(usage_lock),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "HELD"
            res = build_caches.remove_caches([entry])
            assert res[h_tools][0] is False
            assert "running build" in (res[h_tools][1] or "")
            assert (root / h_tools).is_dir()
        finally:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=5)

        # Once the build exits and releases the usage lock, removal succeeds.
        res2 = build_caches.remove_caches([entry])
        assert res2[h_tools] == (True, None)
        assert not (root / h_tools).exists()

    def test_macos_batch_remove_separates_locked_and_shared_modes(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locked caches pass locked:<dir> (buildah fcntl); shared caches pass
        shared:<dir> under podman_exclusive_lock (and skip when contested)."""
        import contextlib

        monkeypatch.setattr(build_caches, "is_macos", lambda: True)
        h1 = build_caches.cache_dir_hash("ltvm-apt-debian13")
        h2 = build_caches.cache_dir_hash("ltvm-apt-ubuntu2404")
        h3 = build_caches.cache_dir_hash("ltvm-build-tools")
        h4 = build_caches.cache_dir_hash("ltvm-rdma-core")
        seen_args: list[tuple[str, ...]] = []

        def fake_podman(
            *args: str, input: str | None = None, timeout: float | None = None
        ) -> Any:
            seen_args.append(args)
            if args[:2] == ("machine", "list"):
                return _cp(out=json.dumps([{"Name": "pm0", "Running": True}]))
            if args[:3] == ("machine", "ssh", "pm0"):
                specs = args[6:]
                out_lines = []
                for spec in specs:
                    mode, name = spec.split(":", 1)
                    if mode == "locked" and name == h1:
                        out_lines.append(f"ok\t{h1}")
                    elif mode == "locked" and name == h2:
                        out_lines.append(f"nolock\t{h2}")
                    elif mode == "shared":
                        out_lines.append(f"ok\t{name}")
                return _cp(out="\n".join(out_lines) + "\n")
            raise AssertionError(f"unexpected podman {args}")

        monkeypatch.setattr(build_caches, "_podman", fake_podman)

        entries = [
            build_caches.CacheEntry(
                "ltvm-apt-debian13", h1, 100, 0.0, "debian13", "apt"
            ),
            build_caches.CacheEntry(
                "ltvm-apt-ubuntu2404", h2, 80, 0.0, "ubuntu2404", "apt"
            ),
            build_caches.CacheEntry(
                "ltvm-build-tools", h3, 50, 0.0, None, "tools"
            ),
            build_caches.CacheEntry(
                "ltvm-rdma-core", h4, 25, 0.0, None, "rdma"
            ),
        ]

        # When another build holds the podman usage lock, shared caches are
        # skipped without even running SSH for them, while locked caches use
        # their per-cache fcntl lockfile.
        @contextlib.contextmanager
        def busy_lock():
            yield False

        monkeypatch.setattr(build_caches, "podman_exclusive_lock", busy_lock)
        res_busy = build_caches.remove_caches(entries)
        assert res_busy[h1] == (True, None)
        assert res_busy[h2] == (
            False,
            "cannot verify buildah fcntl lock in podman machine",
        )
        assert res_busy[h3] == (False, "in use by a running build")
        assert res_busy[h4] == (False, "in use by a running build")
        assert (
            "machine",
            "ssh",
            "pm0",
            "sh",
            "-s",
            "--",
            f"locked:{h1}",
            f"locked:{h2}",
        ) in seen_args
        assert not any(
            any(a.startswith("shared:") for a in call) for call in seen_args
        )

        # When idle, exclusive lock is acquired and shared caches pass shared:.
        @contextlib.contextmanager
        def idle_lock():
            yield True

        seen_args.clear()
        monkeypatch.setattr(build_caches, "podman_exclusive_lock", idle_lock)
        res_idle = build_caches.remove_caches(entries)
        assert res_idle[h3] == (True, None)
        assert res_idle[h4] == (True, None)
        assert (
            "machine",
            "ssh",
            "pm0",
            "sh",
            "-s",
            "--",
            f"shared:{h3}",
            f"shared:{h4}",
        ) in seen_args


class TestDoctorReport:
    def test_silent_when_podman_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "_podman_available", lambda: False)
        assert build_caches.doctor_report() == ([], [])

    def test_macos_stopped_machine_reports_not_checked(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: True)
        monkeypatch.setattr(build_caches, "_running_machine_name", lambda: None)
        warnings, info = build_caches.doctor_report()
        assert warnings == []
        assert info == [
            "podman build caches: not checked (podman machine stopped)"
        ]

    def test_below_threshold_is_info_only(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(
            build_caches,
            "list_caches",
            lambda: [
                build_caches.CacheEntry(
                    "ltvm-apt-debian13",
                    "a",
                    610_000_000,
                    0.0,
                    "debian13",
                    "apt",
                ),
                build_caches.CacheEntry(
                    "ltvm-build-tools", "b", 54_000_000, 0.0, None, "tools"
                ),
            ],
        )
        warnings, info = build_caches.doctor_report()
        assert warnings == []
        assert len(info) == 1
        assert "2 cache(s), 633.2 MiB" in info[0]
        assert "ltvm-apt-debian13 581.7 MiB" in info[0]
        assert "ltvm-build-tools 51.5 MiB" in info[0]

    def test_above_threshold_warns_with_prune_hint(
        self, podman_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_caches, "is_macos", lambda: False)
        monkeypatch.setattr(
            build_caches,
            "list_caches",
            lambda: [
                build_caches.CacheEntry(
                    "ltvm-apt-debian13",
                    "a",
                    build_caches.WARN_THRESHOLD_BYTES + 100_000_000,
                    0.0,
                    "debian13",
                    "apt",
                )
            ],
        )
        warnings, info = build_caches.doctor_report()
        assert len(info) == 1
        assert len(warnings) == 1
        assert "ltvm clean --build-cache --apply" in warnings[0]
