"""Every guest's /etc/hosts names localhost (ltvm_pkg/guest_hosts.py)."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

import ltvm_pkg.image_build as ib
from ltvm_pkg.guest_hosts import STOCK_HOSTS, ensure_localhost_sh


def _run(script: str) -> None:
    subprocess.run(["bash", "-c", script], check=True)


class TestEnsureLocalhost:
    def test_missing_file_gets_the_stock_entries(self, tmp_path: Path) -> None:
        hosts = tmp_path / "hosts"
        _run(ensure_localhost_sh(shlex.quote(str(hosts))))
        assert hosts.read_text() == STOCK_HOSTS

    @pytest.mark.parametrize("body", ["", "# nothing here\n"])
    def test_file_without_localhost_gets_them(
        self, tmp_path: Path, body: str
    ) -> None:
        hosts = tmp_path / "hosts"
        hosts.write_text(body)
        _run(ensure_localhost_sh(shlex.quote(str(hosts))))
        assert hosts.read_text() == body + STOCK_HOSTS

    def test_no_trailing_newline_is_not_joined(self, tmp_path: Path) -> None:
        hosts = tmp_path / "hosts"
        hosts.write_text("10.0.0.5 other")
        _run(ensure_localhost_sh(shlex.quote(str(hosts))))
        assert hosts.read_text() == "10.0.0.5 other\n" + STOCK_HOSTS

    @pytest.mark.parametrize(
        "body",
        [
            "127.0.0.1   localhost localhost.localdomain\n",
            "127.0.0.1   localhost.localdomain localhost\n",
            "  127.0.0.1\tlocalhost\n::1 localhost\n",
        ],
    )
    def test_existing_entry_is_left_alone(
        self, tmp_path: Path, body: str
    ) -> None:
        hosts = tmp_path / "hosts"
        hosts.write_text(body)
        _run(ensure_localhost_sh(shlex.quote(str(hosts))))
        assert hosts.read_text() == body

    @pytest.mark.parametrize(
        "body",
        [
            "#127.0.0.1 localhost\n",
            "127.0.0.1 mylocalhost\n",
            "127.0.0.1 localhost.localdomain\n",
            "127.0.0.1 foo # localhost\n",
            "127.0.0.1 foo#bar localhost\n",
        ],
    )
    def test_commented_or_partial_entry_does_not_count(
        self, tmp_path: Path, body: str
    ) -> None:
        hosts = tmp_path / "hosts"
        hosts.write_text(body)
        _run(ensure_localhost_sh(shlex.quote(str(hosts))))
        assert hosts.read_text() == body + STOCK_HOSTS

    def test_no_loopback_hostname_line(self) -> None:
        # A hostname resolving to 127.x makes LNet pick a loopback NID.
        assert "127.0.1.1" not in STOCK_HOSTS


class TestRootfsHosts:
    """The export's step, run against an extracted rootfs."""

    @pytest.mark.parametrize("umask", ["0077", "0002"])
    def test_debian_rootfs_without_hosts(
        self, tmp_path: Path, umask: str
    ) -> None:
        (tmp_path / "etc").mkdir()
        _run(
            f"umask {umask}; " + ib._rootfs_hosts_sh(shlex.quote(str(tmp_path)))
        )
        hosts = tmp_path / "etc" / "hosts"
        assert hosts.read_text() == STOCK_HOSTS
        assert hosts.stat().st_mode & 0o777 == 0o644

    def test_rocky_rootfs_is_unchanged(self, tmp_path: Path) -> None:
        (tmp_path / "etc").mkdir()
        stock = "127.0.0.1   localhost localhost.localdomain\n"
        (tmp_path / "etc" / "hosts").write_text(stock)
        _run(ib._rootfs_hosts_sh(shlex.quote(str(tmp_path))))
        assert (tmp_path / "etc" / "hosts").read_text() == stock

    def test_symlink_is_replaced_not_followed(self, tmp_path: Path) -> None:
        # On a Linux host the export runs on the host: an absolute link
        # in the rootfs would otherwise point at the host's own file.
        outside = tmp_path / "outside"
        outside.write_text("")
        root = tmp_path / "root"
        (root / "etc").mkdir(parents=True)
        (root / "etc" / "hosts").symlink_to(outside)
        _run(ib._rootfs_hosts_sh(shlex.quote(str(root))))
        assert outside.read_text() == ""
        hosts = root / "etc" / "hosts"
        assert not hosts.is_symlink()
        assert hosts.read_text() == STOCK_HOSTS

    def test_dangling_symlink_is_replaced(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "etc").mkdir(parents=True)
        (root / "etc" / "hosts").symlink_to(tmp_path / "missing")
        _run(ib._rootfs_hosts_sh(shlex.quote(str(root))))
        hosts = root / "etc" / "hosts"
        assert not hosts.is_symlink()
        assert hosts.read_text() == STOCK_HOSTS
