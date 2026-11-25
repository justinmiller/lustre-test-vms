"""Tests for site package mirrors: config, ltvm-mirrors.sh, and wiring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import (
    cross_compile,
    image_build,
    mirrors,
    release_package,
)
from ltvm_pkg.target_config import TARGETS_DIR, TargetConfig

SCRIPT = TARGETS_DIR / "common" / "ltvm-mirrors.sh"
FAKE_PEM = (
    "-----BEGIN CERTIFICATE-----\nMIIBFAKECERTDATA\n-----END CERTIFICATE-----\n"
)

# Representative stock repo snippets (no external fixture files needed).
ROCKY_REPO = """\
[baseos]
name=Rocky Linux $releasever - BaseOS
mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=BaseOS-$releasever$rltype
#baseurl=http://dl.rockylinux.org/$contentdir/$releasever/BaseOS/$basearch/os/
enabled=1

[crb]
name=Rocky Linux $releasever - CRB
mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=CRB-$releasever$rltype
#baseurl=http://dl.rockylinux.org/$contentdir/$releasever/CRB/$basearch/os/
enabled=0
"""

EPEL_REPO = """\
[epel]
name=Extra Packages for Enterprise Linux $releasever - $basearch
#baseurl=https://download.example/pub/epel/$releasever/Everything/$basearch/
metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-$releasever&arch=$basearch
enabled=1
"""

DEBIAN_SOURCES = """\
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main
"""

UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates
Components: main universe

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main universe
"""

ZYPPER_REPO = """\
[repo-oss]
name=Main Repository
enabled=1
baseurl=http://download.opensuse.org/distribution/leap/$releasever/repo/oss/
"""


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf = tmp_path / "ltvm.conf"
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    for k in list(os.environ):
        if k.startswith("LTVM_MIRROR_") or k == mirrors.NO_MIRRORS_ENV:
            monkeypatch.delenv(k)
    mirrors._load.cache_clear()

    def write(body: str) -> Path:
        conf.write_text(f"[mirrors]\n{body}\n")
        mirrors._load.cache_clear()
        return conf

    return write


@pytest.fixture
def ca_file(tmp_path: Path) -> Path:
    p = tmp_path / "site-ca.pem"
    p.write_text(FAKE_PEM)
    return p


def _run_script(
    root: Path,
    *args: str,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    entries = ";".join(
        f"{up} {' '.join(urls)}" for up, urls in mirrors._pairs()
    )
    ca = mirrors.ca_path()
    ca_hash = ""
    if ca is not None:
        import hashlib

        ca_hash = hashlib.sha256(ca.read_bytes()).hexdigest()[:16]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LTVM_MIRRORS_ROOT": str(root),
        "LTVM_MIRRORS": entries,
        "LTVM_MIRROR_CA_HASH": ca_hash,
    }
    if ca is not None:
        env["LTVM_MIRROR_CA_FILE"] = str(ca)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _seed(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


# ---------------------------------------------------------------------------
# ltvm_pkg/mirrors.py
# ---------------------------------------------------------------------------


class TestConfigAndArgs:
    def test_empty_without_config(self, site) -> None:
        assert mirrors.configured() == {}
        assert mirrors.ca_path() is None
        assert mirrors.label_args() == []
        assert mirrors.build_args() == [
            "--build-arg",
            "LTVM_MIRRORS=",
            "--build-arg",
            "LTVM_MIRROR_CA_HASH=",
        ]

    def test_precedence_and_clear_and_disable(
        self, site, ca_file, monkeypatch
    ) -> None:
        site(
            "debian = http://site.example/debian\n"
            "rocky = https://site.example/rocky\n"
            f"ca = {ca_file}\n"
        )
        assert mirrors.configured() == {
            "debian": ["http://site.example/debian"],
            "rocky": ["https://site.example/rocky"],
        }
        assert mirrors.ca_path() == ca_file

        # Env overrides one key and clears another with "".
        monkeypatch.setenv("LTVM_MIRROR_DEBIAN", "https://env.example/debian/")
        monkeypatch.setenv("LTVM_MIRROR_ROCKY", "")
        assert mirrors.configured() == {
            "debian": ["https://env.example/debian"]
        }

        # LTVM_NO_MIRRORS=1 disables all mirrors and CA.
        monkeypatch.setenv("LTVM_NO_MIRRORS", "1")
        assert mirrors.configured() == {}
        assert mirrors.ca_path() is None
        assert mirrors.label_args() == []
        issues, notes = mirrors.doctor_report()
        assert issues == []
        assert any("LTVM_NO_MIRRORS" in n for n in notes)

    def test_multi_mirror_order_and_rewrite_boundaries(self, site) -> None:
        site(
            "debian = http://m1.example/deb https://m2.example/deb\n"
            "debian-security = https://sec.example/deb-sec\n"
        )
        assert mirrors.rewrite_url("http://deb.debian.org/debian") == [
            "http://m1.example/deb",
            "https://m2.example/deb",
        ]
        # Whole path component match: debian must not match debian-security.
        assert mirrors.rewrite_url(
            "http://deb.debian.org/debian-security/dists"
        ) == ["https://sec.example/deb-sec/dists"]
        # Unmatched upstream passes through unchanged.
        assert mirrors.rewrite_url("https://archive.ubuntu.com/ubuntu") == [
            "https://archive.ubuntu.com/ubuntu"
        ]

    def test_invalid_keys_urls_and_ca_reported(
        self, site, tmp_path, capsys, monkeypatch
    ) -> None:
        bad_ca = tmp_path / "not-a-cert.pem"
        bad_ca.write_text("hello")
        site(
            "bogus = http://x.example\n"
            "debian = ftp://bad.example http://good.example/deb\n"
            f"ca = {bad_ca}\n"
        )
        assert mirrors.configured() == {"debian": ["http://good.example/deb"]}
        assert "bogus" in capsys.readouterr().err
        # Repeated reads hit the cache and do not re-warn.
        assert mirrors.configured() == {"debian": ["http://good.example/deb"]}
        assert capsys.readouterr().err == ""
        with pytest.raises(mirrors.MirrorConfigError, match="no PEM"):
            mirrors.ca_path()
        issues, notes = mirrors.doctor_report()
        assert len(issues) == 3
        assert any("bogus" in i for i in issues)
        assert any("ftp://bad.example" in i for i in issues)
        assert any("no PEM" in i for i in issues)
        assert any("mirror debian:" in n for n in notes)
        # Loading a second site config in the same process still warns.
        other = tmp_path / "other.conf"
        other.write_text("[mirrors]\nbogus = http://y.example\n")
        monkeypatch.setenv("LTVM_SITE_CONFIG", str(other))
        assert mirrors.configured() == {}
        assert "bogus" in capsys.readouterr().err

    def test_build_args_and_labels_and_cwd(self, site, ca_file) -> None:
        site(f"rocky = https://m.example/rocky\nca = {ca_file}\n")
        args = mirrors.build_args()
        assert "--build-arg" in args
        assert any(
            a.startswith("LTVM_MIRRORS=https://dl.rockylinux.org") for a in args
        )
        assert any(
            a.startswith("LTVM_MIRROR_CA_HASH=") and len(a.split("=")[1]) == 16
            for a in args
        )
        assert "--secret" in args
        assert f"id={mirrors.SECRET_ID},src={ca_file.resolve()}" in args
        assert mirrors.label_args() == [
            "--label",
            f"{mirrors.LABEL}=rocky,ca",
        ]
        with mirrors.podman_build_cwd() as d:
            assert d.is_dir()
            (d / "podman-build-secret-1").write_text("leak")
        assert not d.exists()


# ---------------------------------------------------------------------------
# targets/common/ltvm-mirrors.sh
# ---------------------------------------------------------------------------


class TestMirrorsScript:
    def test_noop_without_mirrors(self, site, tmp_path) -> None:
        _seed(
            tmp_path, {"/etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES}
        )
        _run_script(tmp_path, "apply")
        assert (
            tmp_path / "etc/apt/sources.list.d/debian.sources"
        ).read_text() == DEBIAN_SOURCES
        assert not (tmp_path / "var/lib/ltvm-mirrors").exists()

    def test_dnf_multi_mirror_preserves_crb_and_second_apply(
        self, site, tmp_path
    ) -> None:
        site(
            "rocky = https://m1.example/rocky https://m2.example/rocky\n"
            "epel = https://m1.example/epel\n"
        )
        _seed(
            tmp_path,
            {
                "/etc/yum.repos.d/rocky.repo": ROCKY_REPO,
                "/var/cache/dnf/meta.bin": "cached",
                "/var/log/dnf.log": "https://m1.example/rocky\n",
            },
        )
        _run_script(tmp_path, "apply")
        rocky = tmp_path / "etc/yum.repos.d/rocky.repo"
        after = rocky.read_text()
        assert (
            "baseurl=https://m1.example/rocky/$releasever/BaseOS/$basearch/os/"
            " https://m2.example/rocky/$releasever/BaseOS/$basearch/os/"
            in after
        )
        assert "#mirrorlist=" in after

        # Simulate `dnf config-manager --set-enabled crb` and epel-release install.
        rocky.write_text(after.replace("enabled=0", "enabled=1"))
        _seed(tmp_path, {"/etc/yum.repos.d/epel.repo": EPEL_REPO})
        _run_script(tmp_path, "apply")
        epel = tmp_path / "etc/yum.repos.d/epel.repo"
        assert (
            "baseurl=https://m1.example/epel/$releasever/Everything/$basearch/"
            in epel.read_text()
        )
        assert "#metalink=" in epel.read_text()

        # Restore keeps crb enabled=1 while restoring stock repo URLs and wiping caches/logs.
        _run_script(tmp_path, "restore")
        assert rocky.read_text() == ROCKY_REPO.replace("enabled=0", "enabled=1")
        assert epel.read_text() == EPEL_REPO
        assert not (tmp_path / "var/cache/dnf/meta.bin").exists()
        assert not (tmp_path / "var/log/dnf.log").exists()
        assert not (tmp_path / "var/lib/ltvm-mirrors").exists()

    def test_deb822_multi_mirror_and_shared_mirror_restore(
        self, site, tmp_path
    ) -> None:
        # Both archive and security rewritten to the exact same mirror URL:
        # undo_edits must consume each record once so each stanza restores
        # to its own original upstream URL.
        site(
            "debian = http://m1.example/deb https://m2.example/deb\n"
            "debian-security = http://sec.example/deb-sec\n"
            "ubuntu = http://same.example/ubuntu\n"
            "ubuntu-security = http://same.example/ubuntu\n"
        )
        _seed(
            tmp_path,
            {
                "/etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES,
                "/etc/apt/sources.list.d/ubuntu.sources": UBUNTU_SOURCES,
                "/var/lib/apt/lists/m1.example_InRelease": "idx",
                "/var/log/apt/history.log": "log",
            },
        )
        _run_script(tmp_path, "apply")
        deb = tmp_path / "etc/apt/sources.list.d/debian.sources"
        ubu = tmp_path / "etc/apt/sources.list.d/ubuntu.sources"
        assert (
            "URIs: http://m1.example/deb https://m2.example/deb"
            in deb.read_text()
        )
        assert "URIs: http://sec.example/deb-sec" in deb.read_text()
        assert ubu.read_text().count("URIs: http://same.example/ubuntu/") == 2

        _run_script(tmp_path, "restore")
        assert deb.read_text() == DEBIAN_SOURCES
        assert ubu.read_text() == UBUNTU_SOURCES
        assert not (
            tmp_path / "var/lib/apt/lists/m1.example_InRelease"
        ).exists()
        assert not (tmp_path / "var/log/apt/history.log").exists()

    def test_zypp_uses_first_mirror_and_restores(self, site, tmp_path) -> None:
        site("opensuse = https://m1.example/suse https://m2.example/suse\n")
        _seed(
            tmp_path,
            {
                "/etc/zypp/repos.d/repo-oss.repo": ZYPPER_REPO,
                "/var/cache/zypp/solv/oss": "cached",
            },
        )
        _run_script(tmp_path, "apply")
        repo = tmp_path / "etc/zypp/repos.d/repo-oss.repo"
        assert (
            "baseurl=https://m1.example/suse/distribution/leap/$releasever/repo/oss/"
            in repo.read_text()
        )
        assert "m2.example" not in repo.read_text()

        _run_script(tmp_path, "restore")
        assert repo.read_text() == ZYPPER_REPO
        assert not (tmp_path / "var/cache/zypp/solv/oss").exists()

    def test_ca_installed_readable_by_apt_and_removed_on_restore(
        self, site, ca_file, tmp_path
    ) -> None:
        site(f"debian = https://mirror.internal:8443/debian\nca = {ca_file}\n")
        _seed(
            tmp_path, {"/etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES}
        )
        (tmp_path / "etc/pki/ca-trust/source/anchors").mkdir(parents=True)
        (tmp_path / "etc/pki/trust/anchors").mkdir(parents=True)

        _run_script(tmp_path, "apply")
        installed = tmp_path / "etc/ltvm-mirrors/ca.pem"
        assert installed.read_text() == FAKE_PEM
        assert installed.stat().st_mode & 0o777 == 0o644
        apt_conf = tmp_path / "etc/apt/apt.conf.d/99ltvm-mirrors"
        assert (
            'Acquire::https::mirror.internal::CaInfo "/etc/ltvm-mirrors/ca.pem";'
            in apt_conf.read_text()
        )
        assert (
            tmp_path / "usr/local/share/ca-certificates/ltvm-mirror-ca.crt"
        ).exists()
        assert (
            tmp_path / "etc/pki/ca-trust/source/anchors/ltvm-mirror-ca.pem"
        ).exists()
        assert (tmp_path / "etc/pki/trust/anchors/ltvm-mirror-ca.pem").exists()

        # Second apply succeeds without needing the secret mounted again.
        _run_script(
            tmp_path,
            "apply",
            extra_env={"LTVM_MIRROR_CA_FILE": str(tmp_path / "gone.pem")},
        )

        _run_script(tmp_path, "restore")
        assert not installed.exists()
        assert not apt_conf.exists()
        assert not (
            tmp_path / "usr/local/share/ca-certificates/ltvm-mirror-ca.crt"
        ).exists()
        assert not (
            tmp_path / "etc/pki/ca-trust/source/anchors/ltvm-mirror-ca.pem"
        ).exists()
        assert not (
            tmp_path / "etc/pki/trust/anchors/ltvm-mirror-ca.pem"
        ).exists()

    def test_missing_ca_secret_fails_loudly(
        self, site, ca_file, tmp_path
    ) -> None:
        site(f"rocky = https://m.example/rocky\nca = {ca_file}\n")
        _seed(tmp_path, {"/etc/yum.repos.d/rocky.repo": ROCKY_REPO})
        r = _run_script(
            tmp_path,
            "apply",
            check=False,
            extra_env={"LTVM_MIRROR_CA_FILE": str(tmp_path / "missing.pem")},
        )
        assert r.returncode == 1
        assert "is missing" in r.stderr

    def test_dnf_epel_rewrites_or_falls_back(self, site, tmp_path) -> None:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        dnf = fake_bin / "dnf"
        dnf.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        dnf.chmod(0o755)
        path_env = f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"

        r_stock = _run_script(
            tmp_path,
            "dnf-epel",
            "10",
            "install",
            "pdsh",
            extra_env={"PATH": path_env},
        )
        assert any(
            "mirrors.fedoraproject.org/metalink" in line
            for line in r_stock.stdout.splitlines()
        )

        site("epel = https://m1.example/epel https://m2.example/epel\n")
        r_mir = _run_script(
            tmp_path,
            "dnf-epel",
            "10",
            "install",
            "pdsh",
            extra_env={"PATH": path_env},
        )
        lines = r_mir.stdout.splitlines()
        assert "--setopt=epel.metalink=" in lines
        assert (
            "--setopt=epel.baseurl=https://m1.example/epel/10/Everything/$basearch/"
            in lines
        )


# ---------------------------------------------------------------------------
# Cross-compile, Dockerfiles, publish refusal, and input_hash invariance
# ---------------------------------------------------------------------------


class TestWiring:
    def test_cross_compile_urls_and_quoting(self, site) -> None:
        site(
            "opensuse = https://m.example/suse\n"
            "ubuntu-ports = http://m1.example/ports https://m2.example/ports\n"
        )
        oss, sle = cross_compile.leap_sysroot_repos("aarch64")
        assert (
            oss
            == "https://m.example/suse/ports/aarch64/distribution/leap/$VER/repo/oss/"
        )
        assert sle == "https://m.example/suse/update/leap/$VER/sle/"
        xinfo = cross_compile.cross_info("aarch64", "x86_64")
        assert (
            xinfo.apt_sources_url
            == "http://m1.example/ports https://m2.example/ports"
        )

        # $VER expands, shell metacharacters are safely escaped.
        tricky = 'https://m.example/$(id); `uname` "q"/leap/$VER/oss/'
        quoted = cross_compile.shell_quote_ver_url(tricky)
        r = subprocess.run(
            ["bash", "-c", f"VER=15.6; printf '%s' {quoted}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert r.stdout == 'https://m.example/$(id); `uname` "q"/leap/15.6/oss/'

    def test_all_dockerfiles_wire_mirrors(self) -> None:
        dockerfiles = sorted(TARGETS_DIR.glob("*/*.Dockerfile"))
        assert dockerfiles
        for path in dockerfiles:
            text = path.read_text()
            assert "ARG LTVM_MIRRORS=" in text, path
            assert "ARG LTVM_MIRROR_CA_HASH=" in text, path
            assert "ltvm-mirrors apply" in text, path
            assert f"id={mirrors.SECRET_ID}" in text, path
        assert "ltvm-mirrors restore" in image_build.RESTORE_MIRRORS_LINE

    def test_refuse_site_mirrors_blocks_labelled_and_fails_closed(self) -> None:
        clean = MagicMock(returncode=0, stdout="<no value>\n", stderr="")
        with patch(
            "ltvm_pkg.release_package.subprocess.run", return_value=clean
        ) as run_mock:
            release_package.refuse_site_mirrors("ltvm-build-rocky9")
        fmt = run_mock.call_args.args[0][4]
        assert fmt == '{{with .Labels}}{{index . "ltvm.site-mirrors"}}{{end}}'

        mirrored = MagicMock(returncode=0, stdout="rocky,ca\n", stderr="")
        with (
            patch(
                "ltvm_pkg.release_package.subprocess.run", return_value=mirrored
            ),
            pytest.raises(RuntimeError, match="LTVM_NO_MIRRORS=1"),
        ):
            release_package.refuse_site_mirrors("ltvm-build-rocky9")

        failed = MagicMock(returncode=125, stdout="", stderr="no such image")
        with (
            patch(
                "ltvm_pkg.release_package.subprocess.run", return_value=failed
            ),
            pytest.raises(RuntimeError, match="Cannot verify"),
        ):
            release_package.refuse_site_mirrors("ltvm-build-rocky9")

    def test_input_hash_does_not_see_mirrors(self, site, ca_file) -> None:
        tc_plain = TargetConfig("rocky9")
        plain = (
            tc_plain.input_hash("container"),
            tc_plain.input_hash("kernel"),
            tc_plain.input_hash("image"),
        )
        site(f"rocky = https://m.example/rocky\nca = {ca_file}\n")
        tc_mir = TargetConfig("rocky9")
        assert plain == (
            tc_mir.input_hash("container"),
            tc_mir.input_hash("kernel"),
            tc_mir.input_hash("image"),
        )
