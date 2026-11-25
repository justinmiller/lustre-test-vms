"""Site package mirrors: ltvm_pkg/mirrors.py and targets/common/ltvm-mirrors.sh.

The script tests run it against the stock repo files of each distro's
base image (tests/fixtures/mirrors/<distro>/, copied out of the images),
with LTVM_MIRRORS_ROOT standing in for /.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ltvm_pkg import mirrors, release_package
from ltvm_pkg.target_config import TARGETS_DIR, TargetConfig

SCRIPT = TARGETS_DIR / "common" / "ltvm-mirrors.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "mirrors"
PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _fresh_warnings() -> None:
    mirrors._warned.clear()
    mirrors._load.cache_clear()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write /etc/ltvm.conf's [mirrors] section."""
    conf = tmp_path / "ltvm.conf"
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))

    def write(body: str) -> Path:
        conf.write_text("[mirrors]\n" + body)
        return conf

    return write


def _parse_podman_args(
    argv: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Parse ``--build-arg K=V`` and ``--secret id=...,src=...`` pairs."""
    build_args: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for flag, val in zip(argv[::2], argv[1::2]):
        if flag == "--build-arg":
            k, _, v = val.partition("=")
            build_args[k] = v
        elif flag == "--secret":
            parts = dict(item.split("=", 1) for item in val.split(","))
            secrets[parts["id"]] = parts["src"]
        else:
            raise AssertionError(f"unexpected podman flag {flag!r}")
    return build_args, secrets


def _args(argv: list[str]) -> dict[str, str]:
    """``--build-arg K=V`` pairs as a dict."""
    return _parse_podman_args(argv)[0]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_nothing_configured(self) -> None:
        assert mirrors.configured() == {}
        assert mirrors.ca_path() is None
        bargs, secrets = _parse_podman_args(mirrors.build_args())
        assert bargs == {
            "LTVM_MIRRORS": "",
            "LTVM_MIRROR_CA_HASH": "",
        }
        assert secrets == {}
        assert mirrors.label_args() == []

    def test_several_mirrors_in_order(self, site) -> None:
        site("debian = http://a.example/debian https://b.example/debian/\n")
        assert mirrors.configured() == {
            "debian": ["http://a.example/debian", "https://b.example/debian"]
        }

    def test_env_wins_over_the_file(self, site, monkeypatch) -> None:
        site("debian-security = http://file.example/ds\n")
        monkeypatch.setenv(
            "LTVM_MIRROR_DEBIAN_SECURITY", "http://env.example/ds"
        )
        assert mirrors.configured() == {
            "debian-security": ["http://env.example/ds"]
        }

    def test_unknown_key_warns_once(self, site, capsys) -> None:
        site("centos = http://x.example/centos\n")
        assert mirrors.configured() == {}
        mirrors.configured()
        err = capsys.readouterr().err
        assert err.count("ignoring [mirrors] centos") == 1

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x.example/debian",
            "http://x.example/a;b",
            "http://x/$arch",
            "x",
        ],
    )
    def test_unusable_url_is_dropped(self, site, capsys, url: str) -> None:
        site(f"debian = {url} http://ok.example/debian\n")
        assert mirrors.configured() == {"debian": ["http://ok.example/debian"]}
        assert "not a plain http(s) URL" in capsys.readouterr().err

    def test_missing_ca_is_an_error(self, site, tmp_path) -> None:
        site(f"ca = {tmp_path / 'nope.pem'}\n")
        with pytest.raises(mirrors.MirrorConfigError, match="cannot read"):
            mirrors.build_args()

    def test_ca_without_a_certificate_is_an_error(self, site, tmp_path) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text("not a cert\n")
        site(f"ca = {ca}\n")
        with pytest.raises(mirrors.MirrorConfigError, match="no PEM"):
            mirrors.ca_path()

    def test_ca_travels_as_secret_with_hash(self, site, tmp_path) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        site(f"ca = {ca}\n")
        bargs, secrets = _parse_podman_args(mirrors.build_args())
        assert bargs["LTVM_MIRROR_CA_HASH"].startswith("site:")
        assert secrets == {mirrors.SECRET_ID: str(ca)}

    def test_https_mirror_without_ca_uses_host_bundle(
        self, site, tmp_path, monkeypatch
    ) -> None:
        host_ca = tmp_path / "host-ca.pem"
        host_ca.write_text(PEM)
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", (host_ca,))
        site("debian = https://mirror.example/debian\n")
        bargs, secrets = _parse_podman_args(mirrors.build_args())
        assert bargs["LTVM_MIRROR_CA_HASH"] == "host"
        assert secrets == {mirrors.SECRET_ID: str(host_ca)}
        # Host CA bootstrap is not a user-configured "ca" label key.
        assert mirrors.configured_keys() == ["debian"]

    def test_http_only_mirror_passes_no_secret(
        self, site, tmp_path, monkeypatch
    ) -> None:
        host_ca = tmp_path / "host-ca.pem"
        host_ca.write_text(PEM)
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", (host_ca,))
        site("debian = http://mirror.example/debian\n")
        bargs, secrets = _parse_podman_args(mirrors.build_args())
        assert bargs["LTVM_MIRROR_CA_HASH"] == ""
        assert secrets == {}

    def test_label_names_keys_not_urls(self, site, tmp_path) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        site(f"rocky = https://internal.example/rocky\nca = {ca}\n")
        labels = mirrors.label_args()
        assert labels[:2] == ["--label", f"{mirrors.LABEL}=rocky,ca"]
        assert labels[2:] == [
            "--label",
            f"{mirrors.ID_LABEL}={mirrors.fingerprint()}",
        ]
        assert "internal" not in " ".join(labels)

    def test_no_labels_without_mirrors(self) -> None:
        assert mirrors.label_args() == []
        assert mirrors.fingerprint() == ""

    def test_fingerprint_follows_urls_and_ca(self, site, tmp_path) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        site("rocky = https://a.example/rocky\n")
        a = mirrors.fingerprint()
        site("rocky = https://b.example/rocky\n")
        b = mirrors.fingerprint()
        site(f"rocky = https://b.example/rocky\nca = {ca}\n")
        assert len({a, b, mirrors.fingerprint()}) == 3

    def test_site_file_is_read_once(self, site, monkeypatch) -> None:
        site("rocky = https://m.example/rocky\n")
        reads = []
        real = mirrors._site_values
        monkeypatch.setattr(
            mirrors, "_site_values", lambda *a: reads.append(1) or real(*a)
        )
        mirrors.build_args()
        mirrors.label_args()
        mirrors.rewrite_url("http://dl.rockylinux.org/$contentdir/9")
        assert len(reads) == 1

    def test_input_hash_does_not_see_mirrors(self, site, monkeypatch) -> None:
        """A different mirror fetches the same packages: nothing goes stale."""
        monkeypatch.setattr(
            TargetConfig,
            "meta_path",
            lambda self, *a, **k: Path("/nonexistent"),
        )
        tc = TargetConfig("rocky9")
        before = {a: tc.input_hash(a) for a in ("container", "image", "kernel")}
        site("rocky = https://m.example/rocky\nepel = https://m.example/epel\n")
        assert {a: tc.input_hash(a) for a in before} == before


class TestRewriteUrl:
    def test_whole_path_components_only(self, site) -> None:
        site("debian = http://m.example/debian\n")
        assert mirrors.rewrite_url("http://deb.debian.org/debian-security") == [
            "http://deb.debian.org/debian-security"
        ]
        assert mirrors.rewrite_url("http://deb.debian.org/debian/pool/x") == [
            "http://m.example/debian/pool/x"
        ]

    def test_either_scheme(self, site) -> None:
        site("opensuse = https://m.example/opensuse\n")
        for scheme in ("http", "https"):
            assert mirrors.rewrite_url(
                f"{scheme}://download.opensuse.org/update/leap/$VER/sle/"
            ) == ["https://m.example/opensuse/update/leap/$VER/sle/"]

    def test_every_mirror(self, site) -> None:
        site("ubuntu-ports = http://a.example/p http://b.example/p\n")
        assert mirrors.rewrite_url("http://ports.ubuntu.com/ubuntu-ports") == [
            "http://a.example/p",
            "http://b.example/p",
        ]

    def test_cross_build_sources(self, site) -> None:
        from ltvm_pkg.cross_compile import cross_info, leap_sysroot_repos

        site(
            "ubuntu-ports = http://a.example/p http://b.example/p\n"
            "opensuse = http://o.example\n"
        )
        assert (
            cross_info("aarch64", "x86_64").apt_sources_url
            == "http://a.example/p http://b.example/p"
        )
        assert leap_sysroot_repos("aarch64") == (
            "http://o.example/ports/aarch64/distribution/leap/$VER/repo/oss/",
            "http://o.example/update/leap/$VER/sle/",
        )

    def test_cross_build_sources_without_mirrors(self) -> None:
        from ltvm_pkg.cross_compile import cross_info, leap_sysroot_repos

        assert (
            cross_info("x86_64", "aarch64").apt_sources_url
            == "http://archive.ubuntu.com/ubuntu"
        )
        assert leap_sysroot_repos("x86_64")[0] == (
            "http://download.opensuse.org/distribution/leap/$VER/repo/oss/"
        )


class TestPublishRefusal:
    def _inspect(self, label: str) -> MagicMock:
        return MagicMock(returncode=0, stdout=label + "\n", stderr="")

    def test_refuses_a_container_built_with_mirrors(self) -> None:
        with patch.object(
            release_package.subprocess,
            "run",
            return_value=self._inspect("rocky,ca"),
        ):
            with pytest.raises(RuntimeError, match=r"mirrors \(rocky,ca\)"):
                release_package.refuse_site_mirrors("ltvm-build-rocky9")

    @pytest.mark.parametrize("label", ["", "<no value>"])
    def test_passes_a_stock_container(self, label: str) -> None:
        with patch.object(
            release_package.subprocess, "run", return_value=self._inspect(label)
        ):
            release_package.refuse_site_mirrors("ltvm-build-rocky9")

    def test_fails_closed_when_podman_inspect_fails(self) -> None:
        with patch.object(
            release_package.subprocess,
            "run",
            return_value=MagicMock(
                returncode=125, stdout="", stderr="no podman"
            ),
        ):
            with pytest.raises(RuntimeError, match="Cannot verify"):
                release_package.refuse_site_mirrors("ltvm-build-rocky9")

        with patch.object(
            release_package.subprocess,
            "run",
            side_effect=OSError("podman missing"),
        ):
            with pytest.raises(RuntimeError, match="Cannot verify"):
                release_package.refuse_site_mirrors("ltvm-build-rocky9")


class TestDoctor:
    def test_reports_settings(self, site) -> None:
        site("epel = https://m.example/epel\nca = /no/such/ca.pem\n")
        issues, notes = mirrors.doctor_report()
        assert notes == ["mirror epel: https://m.example/epel"]
        assert len(issues) == 1 and "cannot read" in issues[0]

    def test_counts_what_was_ignored(self, site, capsys) -> None:
        site(
            "rocky9 = https://m.example/rocky\nepel = https://m.example/e?x=1\n"
        )
        issues, notes = mirrors.doctor_report()
        assert notes == []
        assert len(issues) == 2
        assert "rocky9" in issues[0] and "not one of" in issues[0]
        assert "e?x=1" in issues[1]


class TestContainerDrift:
    def _inspect(self, monkeypatch, label: str, rc: int = 0) -> None:
        monkeypatch.setattr(
            mirrors.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=rc, stdout=label + "\n"),
        )

    def test_same_settings(self, site, monkeypatch) -> None:
        site("debian = https://m.example/debian\n")
        self._inspect(monkeypatch, mirrors.fingerprint())
        assert mirrors.container_drift("ltvm-build-debian13") is None

    def test_mirrors_added_since(self, site, monkeypatch) -> None:
        site("ubuntu-ports = https://m.example/ports\n")
        self._inspect(monkeypatch, "<no value>")
        why = mirrors.container_drift("ltvm-build-ubuntu2404")
        assert why and "ltvm build container <target> --force" in why

    def test_other_mirrors_since(self, site, monkeypatch) -> None:
        site("debian = https://new.example/debian\n")
        self._inspect(monkeypatch, "0123456789abcdef")
        assert mirrors.container_drift("ltvm-build-debian13")

    def test_mirrors_retired_since_warns(self, monkeypatch) -> None:
        # Container was built with mirrors, but none are configured now:
        # dnf/apt inside the container (ZFS, MOFED-kmods, cross builds)
        # still point at the retired mirrors, so warn.
        self._inspect(monkeypatch, "0123456789abcdef")
        why = mirrors.container_drift("ltvm-build-debian13")
        assert why and "ltvm build container <target> --force" in why

    def test_neither_configured_is_clean(self, monkeypatch) -> None:
        self._inspect(monkeypatch, "<no value>")
        assert mirrors.container_drift("ltvm-build-debian13") is None

    def test_podman_cannot_say(self, site, monkeypatch) -> None:
        site("debian = https://m.example/debian\n")
        self._inspect(monkeypatch, "", rc=125)
        assert mirrors.container_drift("ltvm-build-debian13") is None


# ---------------------------------------------------------------------------
# targets/common/ltvm-mirrors.sh
# ---------------------------------------------------------------------------


def _root(tmp_path: Path, *distros: str) -> Path:
    root = tmp_path / "root"
    for d in distros:
        shutil.copytree(FIXTURES / d, root, dirs_exist_ok=True)
    return root


def _run(
    root: Path,
    site_body: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *cmd: str,
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    conf = tmp_path / "ltvm.conf"
    conf.write_text("[mirrors]\n" + site_body)
    monkeypatch.setenv("LTVM_SITE_CONFIG", str(conf))
    bargs, secrets = _parse_podman_args(mirrors.build_args())
    env = {
        "PATH": path or os.environ["PATH"],
        "LTVM_MIRRORS_ROOT": str(root),
        "LTVM_MIRROR_CA_FILE": secrets.get(
            mirrors.SECRET_ID, str(tmp_path / "no-such-secret")
        ),
        **bargs,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *cmd],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)

    def clean(c: filecmp.dircmp[str]) -> bool:
        if c.left_only or c.right_only or c.diff_files or c.funny_files:
            return False
        return all(clean(sub) for sub in c.subdirs.values())

    return clean(cmp)


def _repo(root: Path, rel: str) -> str:
    return (root / rel).read_text()


def _enable(repo: str, section: str) -> str:
    """repo with section's enabled=0 set to 1, as dnf config-manager does."""
    head, _, rest = repo.partition(section + "\n")
    assert rest, section
    return head + section + "\n" + rest.replace("enabled=0", "enabled=1", 1)


class TestScriptDnf:
    BODY = (
        "rocky = https://m.example/rocky https://m2.example/rocky\n"
        "epel = https://m.example/epel\n"
    )

    def test_rocky9_and_epel(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "rocky9")
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        rocky = _repo(root, "etc/yum.repos.d/rocky.repo")
        assert (
            "[baseos]\nname=Rocky Linux $releasever - BaseOS\n"
            "#mirrorlist=https://mirrors.rockylinux.org/" in rocky
        )
        assert (
            "baseurl=https://m.example/rocky/$releasever/BaseOS/$basearch/os/ "
            "https://m2.example/rocky/$releasever/BaseOS/$basearch/os/" in rocky
        )
        assert "\nmirrorlist=" not in rocky
        epel = _repo(root, "etc/yum.repos.d/epel.repo")
        assert (
            "baseurl=https://m.example/epel/9/Everything/$basearch/\n" in epel
        )
        assert (
            "\n#metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-9&"
            in epel
        )
        # No commented baseurl to take: left on its metalink.
        assert filecmp.cmp(
            root / "etc/yum.repos.d/epel-cisco-openh264.repo",
            FIXTURES / "rocky9/etc/yum.repos.d/epel-cisco-openh264.repo",
            shallow=False,
        )

    def test_rocky10_epel_keeps_its_variables(
        self, tmp_path, monkeypatch
    ) -> None:
        root = _root(tmp_path, "rocky10")
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        assert (
            "baseurl=https://m.example/epel/$releasever${releasever_minor:+z}"
            "/Everything/$basearch/\n"
            in _repo(root, "etc/yum.repos.d/epel.repo")
        )

    def test_rocky8(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "rocky8")
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        base = _repo(root, "etc/yum.repos.d/Rocky-BaseOS.repo")
        assert "baseurl=https://m.example/rocky/$releasever/BaseOS/" in base
        assert "#mirrorlist=" in base

    def test_restore_is_byte_identical(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "rocky9")
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        _run(root, self.BODY, tmp_path, monkeypatch, "restore")
        assert _same_tree(root / "etc", FIXTURES / "rocky9/etc")
        assert not (root / "var/lib/ltvm-mirrors").exists()

    def test_restore_keeps_what_the_build_changed(
        self, tmp_path, monkeypatch
    ) -> None:
        # The image Dockerfiles enable crb (powertools on rocky8) between
        # apply and restore, in a file apply rewrote.
        root = _root(tmp_path, "rocky9")
        repo = root / "etc/yum.repos.d/rocky.repo"
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        repo.write_text(_enable(repo.read_text(), "[crb]"))
        _run(root, self.BODY, tmp_path, monkeypatch, "restore")
        stock = (FIXTURES / "rocky9/etc/yum.repos.d/rocky.repo").read_text()
        assert repo.read_text() == _enable(stock, "[crb]")

    def test_restore_keeps_a_replaced_file(self, tmp_path, monkeypatch) -> None:
        # A package upgrade that installs its own copy of a file apply
        # rewrote: nothing in the new one is apply's.
        root = _root(tmp_path, "rocky9")
        repo = root / "etc/yum.repos.d/rocky.repo"
        _run(root, self.BODY, tmp_path, monkeypatch, "apply")
        upgraded = (FIXTURES / "rocky9/etc/yum.repos.d/rocky.repo").read_text()
        upgraded = upgraded.replace("metadata_expire=6h", "metadata_expire=1h")
        repo.write_text(upgraded)
        _run(root, self.BODY, tmp_path, monkeypatch, "restore")
        assert repo.read_text() == upgraded

    def test_apply_is_idempotent(self, tmp_path, monkeypatch) -> None:
        once = _root(tmp_path / "a", "rocky9")
        twice = _root(tmp_path / "b", "rocky9")
        _run(once, self.BODY, tmp_path, monkeypatch, "apply")
        _run(twice, self.BODY, tmp_path, monkeypatch, "apply")
        _run(twice, self.BODY, tmp_path, monkeypatch, "apply")
        assert _same_tree(once / "etc", twice / "etc")


class TestScriptApt:
    def test_debian13(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "debian13")
        body = "debian = http://a.example/debian https://b.example/debian\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        src = _repo(root, "etc/apt/sources.list.d/debian.sources")
        assert "URIs: http://a.example/debian https://b.example/debian\n" in src
        # debian is not a prefix of debian-security.
        assert "URIs: http://deb.debian.org/debian-security\n" in src

    def test_debian13_security(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "debian13")
        body = "debian-security = http://s.example/debian-security\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        src = _repo(root, "etc/apt/sources.list.d/debian.sources")
        assert "URIs: http://deb.debian.org/debian\n" in src
        assert "URIs: http://s.example/debian-security\n" in src

    def test_ubuntu_arm64_ports(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "ubuntu2404")
        body = "ubuntu-ports = http://p.example/ubuntu-ports\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        src = _repo(root, "etc/apt/sources.list.d/ubuntu.sources")
        assert src.count("URIs: http://p.example/ubuntu-ports/\n") == 2

    def test_ubuntu_amd64(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "ubuntu2404-amd64")
        body = "ubuntu = http://u.example/ubuntu\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        src = _repo(root, "etc/apt/sources.list.d/ubuntu.sources")
        assert "URIs: http://u.example/ubuntu/\n" in src
        assert "URIs: http://security.ubuntu.com/ubuntu/\n" in src

    def test_one_line_sources_get_a_line_per_mirror(
        self, tmp_path, monkeypatch
    ) -> None:
        root = _root(tmp_path, "oneline")
        body = "debian = http://a.example/debian http://b.example/debian\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        assert _repo(root, "etc/apt/sources.list").splitlines() == [
            "# a one-line sources.list",
            "deb http://a.example/debian bookworm main",
            "deb http://b.example/debian bookworm main",
            "deb [arch=amd64 signed-by=/usr/share/keyrings/k.gpg] "
            "http://a.example/debian bookworm-updates main",
            "deb [arch=amd64 signed-by=/usr/share/keyrings/k.gpg] "
            "http://b.example/debian bookworm-updates main",
            "deb http://deb.debian.org/debian-security bookworm-security main",
            "deb http://example.com/other bookworm main",
        ]

    def test_one_line_restore_drops_the_added_lines(
        self, tmp_path, monkeypatch
    ) -> None:
        root = _root(tmp_path, "oneline")
        body = "debian = http://a.example/debian http://b.example/debian\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        _run(root, body, tmp_path, monkeypatch, "restore")
        assert _same_tree(root / "etc", FIXTURES / "oneline/etc")

    def test_restore(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "debian13")
        body = "debian = http://a.example/debian\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        (root / "var/lib/apt/lists").mkdir(parents=True)
        (
            root / "var/lib/apt/lists/a.example_debian_dists_trixie_InRelease"
        ).touch()
        (root / "var/lib/apt/lists/lock").touch()
        _run(root, body, tmp_path, monkeypatch, "restore")
        # The index named after the mirror goes; apt's own lock stays.
        assert sorted(
            p.name for p in (root / "var/lib/apt/lists").iterdir()
        ) == ["lock"]
        shutil.rmtree(root / "var")
        assert _same_tree(root, FIXTURES / "debian13")


class TestScriptZypper:
    def test_first_mirror_only(self, tmp_path, monkeypatch) -> None:
        root = _root(tmp_path, "opensuse156")
        body = "opensuse = https://o.example/opensuse https://o2.example/opensuse\n"
        _run(root, body, tmp_path, monkeypatch, "apply")
        assert (
            "baseurl=https://o.example/opensuse/distribution/leap/$releasever/repo/oss/\n"
            in _repo(root, "etc/zypp/repos.d/repo-oss.repo")
        )
        # codecs.opensuse.org is not download.opensuse.org.
        assert "baseurl=http://codecs.opensuse.org/" in _repo(
            root, "etc/zypp/repos.d/repo-openh264.repo"
        )
        _run(root, body, tmp_path, monkeypatch, "restore")
        assert _same_tree(root / "etc", FIXTURES / "opensuse156/etc")
        assert not (root / "var/lib/ltvm-mirrors").exists()


class TestScriptCa:
    def test_apt_trusts_it_per_https_mirror_host(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", ())
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        ca.chmod(0o400)  # podman build secrets mount 0400 root:root
        root = _root(tmp_path, "debian13")
        (root / "etc/apt/apt.conf.d").mkdir()
        body = (
            "debian = https://repo.example:8443/debian http://plain.example/debian\n"
            f"ca = {ca}\n"
        )
        _run(root, body, tmp_path, monkeypatch, "apply")
        installed = root / "etc/ltvm-mirrors/ca.pem"
        assert installed.read_text() == PEM
        # _apt drops privileges and must be able to read CaInfo.
        assert stat.S_IMODE(installed.stat().st_mode) == 0o644
        assert (root / "etc/apt/apt.conf.d/99ltvm-mirrors").read_text() == (
            'Acquire::https::repo.example::CaInfo "/etc/ltvm-mirrors/ca.pem";\n'
        )
        assert (
            root / "usr/local/share/ca-certificates/ltvm-mirror-ca.crt"
        ).read_text() == PEM
        _run(root, body, tmp_path, monkeypatch, "restore")
        assert not (root / "etc/ltvm-mirrors").exists()
        assert not (root / "etc/apt/apt.conf.d/99ltvm-mirrors").exists()
        assert not any((root / "usr/local/share/ca-certificates").iterdir())

    def test_host_ca_bootstraps_minimal_apt_only(
        self, tmp_path, monkeypatch
    ) -> None:
        host_ca = tmp_path / "host-ca.pem"
        host_ca.write_text(PEM)
        host_ca.chmod(0o400)
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", (host_ca,))
        body = "debian = https://repo.example/debian\n"

        # Minimal Debian image without ca-certificates gets apt CaInfo only.
        root = _root(tmp_path / "minimal", "debian13")
        (root / "etc/apt/apt.conf.d").mkdir()
        _run(root, body, tmp_path, monkeypatch, "apply")
        installed = root / "etc/ltvm-mirrors/ca.pem"
        assert installed.read_text() == PEM
        assert stat.S_IMODE(installed.stat().st_mode) == 0o644
        assert (root / "etc/apt/apt.conf.d/99ltvm-mirrors").read_text() == (
            'Acquire::https::repo.example::CaInfo "/etc/ltvm-mirrors/ca.pem";\n'
        )
        assert not (root / "usr/local/share/ca-certificates").exists()
        _run(root, body, tmp_path, monkeypatch, "restore")
        assert not (root / "etc/ltvm-mirrors").exists()
        assert not (root / "etc/apt/apt.conf.d/99ltvm-mirrors").exists()

        # Image that already ships ca-certificates ignores the host bundle.
        with_certs = _root(tmp_path / "with_certs", "debian13")
        (with_certs / "etc/ssl/certs").mkdir(parents=True)
        (with_certs / "etc/ssl/certs/ca-certificates.crt").write_text(PEM)
        _run(with_certs, body, tmp_path, monkeypatch, "apply")
        assert not (with_certs / "etc/ltvm-mirrors").exists()
        assert not (with_certs / "etc/apt/apt.conf.d/99ltvm-mirrors").exists()

        # Non-apt image (RHEL) ignores the host bundle.
        rhel = _root(tmp_path / "rhel", "rocky9")
        anchors = rhel / "etc/pki/ca-trust/source/anchors"
        anchors.mkdir(parents=True)
        _run(
            rhel,
            "rocky = https://m.example/rocky\n",
            tmp_path,
            monkeypatch,
            "apply",
        )
        assert not (rhel / "etc/ltvm-mirrors").exists()
        assert not any(anchors.iterdir())

    def test_rhel_anchor(self, tmp_path, monkeypatch) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        root = _root(tmp_path, "rocky9")
        anchors = root / "etc/pki/ca-trust/source/anchors"
        anchors.mkdir(parents=True)
        _run(root, f"ca = {ca}\n", tmp_path, monkeypatch, "apply")
        assert (anchors / "ltvm-mirror-ca.pem").read_text() == PEM
        _run(root, f"ca = {ca}\n", tmp_path, monkeypatch, "restore")
        assert not any(anchors.iterdir())


class TestScriptNoop:
    def test_nothing_configured_touches_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        root = _root(tmp_path, "rocky9", "debian13", "opensuse156")
        for cmd in ("apply", "restore"):
            _run(root, "", tmp_path, monkeypatch, cmd)
        assert not (root / "var").exists()
        assert _same_tree(
            root / "etc/yum.repos.d", FIXTURES / "rocky9/etc/yum.repos.d"
        )


class TestScriptDnfEpel:
    """dnf-epel: pdsh comes from the epel-10 branch, mirrored or not."""

    def _dnf(self, tmp_path: Path) -> str:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        dnf = bindir / "dnf"
        dnf.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
        dnf.chmod(0o755)
        return f"{bindir}:{os.environ['PATH']}"

    def test_mirrored(self, tmp_path, monkeypatch) -> None:
        r = _run(
            tmp_path,
            "epel = https://a.example/epel https://b.example/epel\n",
            tmp_path,
            monkeypatch,
            "dnf-epel",
            "10",
            "-y",
            "install",
            "pdsh",
            path=self._dnf(tmp_path),
        )
        assert r.stdout.splitlines() == [
            "--setopt=epel.metalink=",
            "--setopt=epel.baseurl=https://a.example/epel/10/Everything/$basearch/",
            "-y",
            "install",
            "pdsh",
        ]

    def test_stock(self, tmp_path, monkeypatch) -> None:
        r = _run(
            tmp_path,
            "",
            tmp_path,
            monkeypatch,
            "dnf-epel",
            "10",
            "install",
            "pdsh",
            path=self._dnf(tmp_path),
        )
        assert r.stdout.splitlines() == [
            "--setopt=epel.metalink=https://mirrors.fedoraproject.org/metalink"
            "?repo=epel-10&arch=$basearch",
            "install",
            "pdsh",
        ]


class TestDockerfiles:
    """Every container and image build applies the mirrors before it
    installs anything."""

    @pytest.mark.parametrize(
        "dockerfile",
        sorted(TARGETS_DIR.glob("*/container.Dockerfile"))
        + sorted(TARGETS_DIR.glob("*/image.Dockerfile")),
        ids=lambda p: f"{p.parent.name}/{p.name}",
    )
    def test_applies_first(self, dockerfile: Path) -> None:
        lines = dockerfile.read_text().splitlines()
        runs = [i for i, line in enumerate(lines) if line.startswith("RUN ")]
        assert lines[runs[0]] == (
            "RUN --mount=type=secret,id=ltvm_mirror_ca,"
            "target=/run/secrets/ltvm_mirror_ca "
            "bash /usr/local/sbin/ltvm-mirrors apply"
        )
        assert "ARG LTVM_MIRRORS=" in lines[: runs[0]]
        assert "ARG LTVM_MIRROR_CA_HASH=" in lines[: runs[0]]

    def test_image_restores_last(self) -> None:
        from ltvm_pkg import image_build

        assert "ltvm-mirrors restore" in image_build.RESTORE_MIRRORS_LINE


class TestPodmanBuildCwd:
    def test_cleans_up_leaked_secret_temp_files_on_success_and_failure(
        self,
    ) -> None:
        with mirrors.podman_build_cwd() as d:
            assert d.is_dir()
            leaked = d / "podman-build-secret-123456"
            leaked.write_text("dummy")
            saved = d
        assert not saved.exists()

        with pytest.raises(RuntimeError, match="boom"):
            with mirrors.podman_build_cwd() as d2:
                (d2 / "podman-build-secret-999999").write_text("dummy")
                saved2 = d2
                raise RuntimeError("boom")
        assert not saved2.exists()

    def test_relative_ca_path_resolved_in_build_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca = tmp_path / "site-ca.pem"
        ca.write_text(
            "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LTVM_MIRROR_CA", "site-ca.pem")
        _, secrets = _parse_podman_args(mirrors.build_args())
        assert Path(secrets[mirrors.SECRET_ID]).is_absolute()
        assert Path(secrets[mirrors.SECRET_ID]) == ca.resolve()

    def test_container_build_uses_scratch_cwd(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from ltvm_pkg.kernel_build import _ensure_container_image

        cfg = MagicMock()
        cfg.name = "rocky9"
        cfg.arch = "x86_64"
        cfg.container_image = "rockylinux:9"
        cfg.target_dir = tmp_path
        cfg.kernel_deb_source = ""
        cfg.container_tag = "ltvm-build-rocky9"
        cfg.variant_name = "base"
        (tmp_path / "container.Dockerfile").write_text("FROM scratch\n")

        seen_cwd: list[Path | None] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            cwd = kwargs.get("cwd")
            assert isinstance(cwd, Path)
            assert cwd.is_dir()
            assert cwd != Path.cwd()
            (cwd / "podman-build-secret-42").write_text("leaked")
            seen_cwd.append(cwd)
            return MagicMock(returncode=0)

        with patch(
            "ltvm_pkg.kernel_build.subprocess.run", side_effect=fake_run
        ):
            _ensure_container_image(cfg)

        assert len(seen_cwd) == 1
        assert seen_cwd[0] is not None
        assert not seen_cwd[0].exists()

    def test_homebrew_ca_preferred_over_etc_ssl_cert_pem(self) -> None:
        bundles = mirrors._HOST_CA_BUNDLES
        assert Path("/opt/homebrew/etc/ca-certificates/cert.pem") in bundles
        assert Path("/usr/local/etc/ca-certificates/cert.pem") in bundles
        assert bundles.index(
            Path("/opt/homebrew/etc/ca-certificates/cert.pem")
        ) < bundles.index(Path("/etc/ssl/cert.pem"))

    def test_no_mirrors_env_and_empty_key_override(
        self, site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text(PEM)
        site(
            f"rocky = https://m.example/rocky\n"
            f"debian = https://m.example/debian\n"
            f"ca = {ca}\n"
        )
        assert set(mirrors.configured()) == {"rocky", "debian"}
        assert mirrors.ca_path() == ca

        # Per-key empty env clears only that key from /etc/ltvm.conf.
        monkeypatch.setenv("LTVM_MIRROR_ROCKY", "")
        monkeypatch.setenv("LTVM_MIRROR_CA", "")
        assert mirrors.configured() == {"debian": ["https://m.example/debian"]}
        assert mirrors.ca_path() is None

        # Global LTVM_NO_MIRRORS=1 clears everything and reports cleanly.
        monkeypatch.setenv("LTVM_NO_MIRRORS", "1")
        assert mirrors.configured() == {}
        assert mirrors.ca_path() is None
        assert mirrors.label_args() == []
        assert mirrors.fingerprint() == ""
        bargs, secrets = _parse_podman_args(mirrors.build_args())
        assert bargs == {"LTVM_MIRRORS": "", "LTVM_MIRROR_CA_HASH": ""}
        assert secrets == {}
        issues, notes = mirrors.doctor_report()
        assert issues == []
        assert any("LTVM_NO_MIRRORS" in n for n in notes)

    def test_site_ca_with_https_mirror_splits_system_and_apt_ca(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site_pem = (
            "-----BEGIN CERTIFICATE-----\nSITE\n-----END CERTIFICATE-----\n"
        )
        host_pem = (
            "-----BEGIN CERTIFICATE-----\nHOST\n-----END CERTIFICATE-----\n"
        )
        ca = tmp_path / "site-ca.pem"
        ca.write_text(site_pem)
        host_ca = tmp_path / "host-ca.pem"
        host_ca.write_text(host_pem)
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", (host_ca,))
        body = f"debian = https://public.example/debian\nca = {ca}\n"

        root = _root(tmp_path, "debian13")
        (root / "etc/pki/ca-trust/source/anchors").mkdir(parents=True)
        _run(root, body, tmp_path, monkeypatch, "apply")

        # System trust stores receive ONLY the site CA.
        sys_deb = (
            root / "usr/local/share/ca-certificates/ltvm-mirror-ca.crt"
        ).read_text()
        sys_rhel = (
            root / "etc/pki/ca-trust/source/anchors/ltvm-mirror-ca.pem"
        ).read_text()
        assert sys_deb == site_pem
        assert sys_rhel == site_pem
        assert "HOST" not in sys_deb

        # apt CaInfo points to the combined site+host bundle so public HTTPS
        # mirrors work before ca-certificates is installed.
        apt_conf = (root / "etc/apt/apt.conf.d/99ltvm-mirrors").read_text()
        assert 'CaInfo "/etc/ltvm-mirrors/apt-ca.pem"' in apt_conf
        apt_ca = (root / "etc/ltvm-mirrors/apt-ca.pem").read_text()
        assert "SITE" in apt_ca and "HOST" in apt_ca

        _run(root, body, tmp_path, monkeypatch, "restore")
        assert not (root / "etc/ltvm-mirrors").exists()
        assert not (root / "etc/apt/apt.conf.d/99ltvm-mirrors").exists()

    def test_restore_handles_two_stanzas_sharing_one_mirror_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ubuntu + ubuntu-security both pointed at the exact same mirror URL:
        # undo_edits must consume each record once so both distinct upstreams
        # come back intact.
        body = (
            "ubuntu = https://mirror.example/ubuntu\n"
            "ubuntu-security = https://mirror.example/ubuntu\n"
        )
        root = _root(tmp_path, "ubuntu2404-amd64")
        src = root / "etc/apt/sources.list.d/ubuntu.sources"
        orig = src.read_text()
        assert "archive.ubuntu.com/ubuntu" in orig
        assert "security.ubuntu.com/ubuntu" in orig

        _run(root, body, tmp_path, monkeypatch, "apply")
        rewritten = src.read_text()
        assert "archive.ubuntu.com" not in rewritten
        assert "security.ubuntu.com" not in rewritten
        assert rewritten.count("URIs: https://mirror.example/ubuntu/") == 2

        _run(root, body, tmp_path, monkeypatch, "restore")
        assert src.read_text() == orig

    def test_restore_cleans_zypp_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "opensuse = http://mirror.example/opensuse\n"
        root = _root(tmp_path, "opensuse156")
        zypp_cache = root / "var/cache/zypp/raw/repo-oss"
        zypp_cache.mkdir(parents=True)
        (zypp_cache / "repomd.xml").write_text("<repomd/>")
        _run(root, body, tmp_path, monkeypatch, "apply")
        _run(root, body, tmp_path, monkeypatch, "restore")
        assert not zypp_cache.exists()

    def test_restore_removes_package_manager_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "rocky = http://mirror.internal.corp/rocky\n"
        root = _root(tmp_path, "rocky9")
        log_dir = root / "var/log"
        apt_log_dir = log_dir / "apt"
        apt_log_dir.mkdir(parents=True)
        logs = [
            log_dir / "dnf.log",
            log_dir / "dnf.rpm.log",
            log_dir / "hawkey.log",
            log_dir / "zypper.log",
            apt_log_dir / "history.log",
        ]
        for lf in logs:
            lf.write_text("fetched from http://mirror.internal.corp/rocky\n")
        _run(root, body, tmp_path, monkeypatch, "apply")
        _run(root, body, tmp_path, monkeypatch, "restore")
        for lf in logs:
            assert not lf.exists(), f"{lf.name} should be removed by restore"

    def test_host_ca_scoped_to_debian_and_excluded_from_fingerprint(
        self, site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_ca = tmp_path / "host-ca.pem"
        host_ca.write_text(
            "-----BEGIN CERTIFICATE-----\nHOST1\n-----END CERTIFICATE-----\n"
        )
        site_ca = tmp_path / "site-ca.pem"
        site_ca.write_text(
            "-----BEGIN CERTIFICATE-----\nSITE\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setattr(mirrors, "_HOST_CA_BUNDLES", (host_ca,))

        # HTTPS mirror without explicit site CA:
        site("rocky = https://mirror.example/rocky\n")
        fp_before = mirrors.fingerprint()
        # RHEL and SUSE never get the host CA bundle.
        bargs_rhel, secrets_rhel = _parse_podman_args(
            mirrors.build_args(os_family="rhel")
        )
        assert bargs_rhel["LTVM_MIRROR_CA_HASH"] == ""
        assert secrets_rhel == {}
        bargs_suse, secrets_suse = _parse_podman_args(
            mirrors.build_args(os_family="suse")
        )
        assert bargs_suse["LTVM_MIRROR_CA_HASH"] == ""
        assert secrets_suse == {}
        # Debian gets constant "host" (never hashes host CA bundle, so
        # routine host OS CA updates do not invalidate Debian/Ubuntu layers).
        bargs_deb, secrets_deb = _parse_podman_args(
            mirrors.build_args(os_family="debian")
        )
        assert bargs_deb["LTVM_MIRROR_CA_HASH"] == "host"
        assert secrets_deb[mirrors.SECRET_ID] == str(host_ca)

        # Updating the host CA bundle moves neither fingerprint() nor
        # LTVM_MIRROR_CA_HASH (for either host-only or site+host builds).
        host_ca.write_text(
            "-----BEGIN CERTIFICATE-----\nHOST2\n-----END CERTIFICATE-----\n"
        )
        assert mirrors.fingerprint() == fp_before
        bargs_deb_after, _ = _parse_podman_args(
            mirrors.build_args(os_family="debian")
        )
        assert bargs_deb_after["LTVM_MIRROR_CA_HASH"] == "host"

        # With an explicit site CA, RHEL gets only site CA (no HOST_CA_MARKER),
        # while Debian gets the combined site+host bundle, yet both share the
        # exact same site:<hash> build-arg (unaffected by host CA updates).
        site(f"rocky = https://mirror.example/rocky\nca = {site_ca}\n")
        bargs_rhel2, secrets_rhel2 = _parse_podman_args(
            mirrors.build_args(tmp_path, os_family="rhel")
        )
        assert bargs_rhel2["LTVM_MIRROR_CA_HASH"].startswith("site:")
        assert secrets_rhel2[mirrors.SECRET_ID] == str(site_ca)
        bargs_deb2, secrets_deb2 = _parse_podman_args(
            mirrors.build_args(tmp_path, os_family="debian")
        )
        assert (
            bargs_deb2["LTVM_MIRROR_CA_HASH"]
            == bargs_rhel2["LTVM_MIRROR_CA_HASH"]
        )
        assert (
            mirrors._HOST_CA_MARKER
            in Path(secrets_deb2[mirrors.SECRET_ID]).read_bytes()
        )
        host_ca.write_text(
            "-----BEGIN CERTIFICATE-----\nHOST3\n-----END CERTIFICATE-----\n"
        )
        bargs_deb3, secrets_deb3 = _parse_podman_args(
            mirrors.build_args(tmp_path, os_family="debian")
        )
        assert (
            bargs_deb3["LTVM_MIRROR_CA_HASH"]
            == bargs_deb2["LTVM_MIRROR_CA_HASH"]
        )
        assert b"HOST3" in Path(secrets_deb3[mirrors.SECRET_ID]).read_bytes()

    def test_apply_fails_when_ca_configured_but_secret_missing(
        self, tmp_path: Path
    ) -> None:
        root = _root(tmp_path, "rocky9")
        env = {
            "LTVM_MIRRORS_ROOT": str(root),
            "LTVM_MIRRORS": "https://dl.rockylinux.org/$contentdir https://m.example/rocky",
            "LTVM_MIRROR_CA_HASH": "site:0123456789abcdef",
            "LTVM_MIRROR_CA_FILE": str(tmp_path / "does-not-exist.pem"),
        }
        r = subprocess.run(
            ["bash", str(SCRIPT), "apply"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0
        assert "secret" in r.stderr and "missing" in r.stderr

        # Once the CA is already installed (e.g. a second RUN after epel-release
        # without --mount=type=secret), apply succeeds without needing the secret.
        ca_file = root / "etc" / "ltvm-mirrors" / "ca.pem"
        ca_file.parent.mkdir(parents=True, exist_ok=True)
        ca_file.write_text(PEM)
        r2 = subprocess.run(
            ["bash", str(SCRIPT), "apply"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        assert r2.returncode == 0, r2.stderr

        # Minimal Debian image with host CA mode also fails if secret is missing.
        deb_dir = tmp_path / "deb"
        deb_dir.mkdir()
        deb_root = _root(deb_dir, "debian13")
        r3 = subprocess.run(
            ["bash", str(SCRIPT), "apply"],
            env={
                **os.environ,
                "LTVM_MIRRORS_ROOT": str(deb_root),
                "LTVM_MIRRORS": "http://deb.debian.org/debian https://m.example/debian",
                "LTVM_MIRROR_CA_HASH": "host",
                "LTVM_MIRROR_CA_FILE": str(tmp_path / "does-not-exist.pem"),
            },
            capture_output=True,
            text=True,
        )
        assert r3.returncode != 0
        assert "secret" in r3.stderr and "missing" in r3.stderr

    def test_shell_quote_ver_url_keeps_ver_expansion_safe(self) -> None:
        from ltvm_pkg.cross_compile import shell_quote_ver_url

        quoted = shell_quote_ver_url(
            "https://mirror.example/opensuse/distribution/leap/$VER/repo/oss/"
        )
        out = subprocess.check_output(
            ["sh", "-c", f'VER=15.6; printf "%s" {quoted}'],
            text=True,
        )
        assert (
            out
            == "https://mirror.example/opensuse/distribution/leap/15.6/repo/oss/"
        )

        # Shell metacharacters outside $VER stay literal and are never executed.
        tricky = shell_quote_ver_url(
            'https://mirror.example/$(echo PWN); `id` "q"/leap/$VER/repo/oss/'
        )
        out2 = subprocess.check_output(
            ["sh", "-c", f'VER=15.6; printf "%s" {tricky}'],
            text=True,
        )
        assert (
            out2
            == 'https://mirror.example/$(echo PWN); `id` "q"/leap/15.6/repo/oss/'
        )

    def test_zfs_and_mofed_builds_check_container_drift(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        zfs_src = (repo / "ltvm_pkg" / "zfs_build.py").read_text()
        mofed_src = (repo / "ltvm_pkg" / "mofed_kmod_build.py").read_text()
        assert "mirrors.container_drift(" in zfs_src
        assert "mirrors.container_drift(" in mofed_src
