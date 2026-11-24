"""ltvm cluster krb5: which node is the KDC, and what each node is sent."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from ltvm_pkg import cluster_krb5
from ltvm_pkg.vm_state import ClusterInfo


def _cluster(*nodes: tuple[str, list[str]]) -> ClusterInfo:
    return ClusterInfo(
        name="co9",
        nodes=[{"name": n, "roles": r} for n, r in nodes],
    )


def test_kdc_is_first_mds() -> None:
    c = _cluster(
        ("co9-mgs", ["mgs"]),
        ("co9-mds1", ["mds"]),
        ("co9-mds2", ["mds"]),
        ("co9-c1", ["client"]),
    )
    assert cluster_krb5.kdc_node(c).name == "co9-mds1"


def test_kdc_falls_back_to_mgs() -> None:
    c = _cluster(("co9-mgs", ["mgs", "oss"]), ("co9-c1", ["client"]))
    assert cluster_krb5.kdc_node(c).name == "co9-mgs"


def test_setup_configures_every_node_then_the_kdc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _cluster(
        ("co9-mds", ["mgs", "mds"]),
        ("co9-oss", ["oss"]),
        ("co9-c1", ["client"]),
    )
    ips = {"co9-mds": "10.0.0.1", "co9-oss": "10.0.0.2", "co9-c1": "10.0.0.3"}
    monkeypatch.setattr(
        cluster_krb5.VMInfo, "load", lambda name: SimpleNamespace(ip=ips[name])
    )
    calls: list[tuple[str, str, object]] = []

    def fake_ssh(ip, command, *, stdin=None, timeout):
        calls.append((ip, command, stdin))
        out = (
            b"KEYTAB" if command.startswith("cat /root/krb5-keytabs/") else b""
        )
        return subprocess.CompletedProcess([], 0, out, b"")

    monkeypatch.setattr(cluster_krb5, "_ssh", fake_ssh)
    cluster_krb5.setup(c, realm="EX.TEST", users=("u1",))

    node_runs = [
        x for x in calls if x[2] == cluster_krb5.NODE_SCRIPT.read_bytes()
    ]
    assert [ip for ip, _, _ in node_runs] == list(ips.values())
    assert all(cmd == "bash -s -- EX.TEST co9-mds" for _, cmd, _ in node_runs)

    kdc_runs = [
        x for x in calls if x[2] == cluster_krb5.KDC_SCRIPT.read_bytes()
    ]
    assert len(kdc_runs) == 1
    ip, cmd, _ = kdc_runs[0]
    assert ip == "10.0.0.1"
    assert cmd == "bash -s -- EX.TEST u1 co9-mds co9-oss co9-c1"

    # each node's keytab comes from the KDC and goes to that node
    installs = [x for x in calls if x[2] == b"KEYTAB"]
    assert [ip for ip, _, _ in installs] == list(ips.values())
    for (ip, cmd, _), name in zip(installs, ips):
        assert f"lustre_root/{name}@EX.TEST" in cmd
        assert "su - u1" in cmd


def test_setup_stops_on_a_failing_node(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _cluster(("co9-mds", ["mgs", "mds"]), ("co9-c1", ["client"]))
    monkeypatch.setattr(
        cluster_krb5.VMInfo, "load", lambda name: SimpleNamespace(ip=name)
    )

    def fake_ssh(ip, command, *, stdin=None, timeout):
        rc = 1 if ip == "co9-c1" else 0
        return subprocess.CompletedProcess([], rc, b"", b"reverse lookup")

    monkeypatch.setattr(cluster_krb5, "_ssh", fake_ssh)
    with pytest.raises(SystemExit):
        cluster_krb5.setup(c)


@pytest.mark.parametrize(
    "families,should_pass",
    [
        (["rhel", "rhel"], True),
        (["debian", "debian"], True),
        (["rhel", "debian"], True),
        (["suse", "suse"], False),
        (["rhel", "suse"], False),
    ],
)
def test_cmd_cluster_krb5_checks_all_nodes_os_family(
    families: list[str],
    should_pass: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    from ltvm_pkg import vm_cluster, vm_commands

    c = _cluster(("co9-mds", ["mgs", "mds"]), ("co9-c1", ["client"]))
    fam_by_name = {"co9-mds": families[0], "co9-c1": families[1]}

    monkeypatch.setattr(cluster_krb5.ClusterInfo, "load", lambda name: c)
    monkeypatch.setattr(
        cluster_krb5.vm_claim, "require_all", lambda names, op: None
    )
    monkeypatch.setattr(vm_cluster, "_node_state", lambda name: "up")
    monkeypatch.setattr(
        cluster_krb5.VMInfo,
        "load",
        lambda name: SimpleNamespace(name=name, ip="10.0.0.1"),
    )
    monkeypatch.setattr(
        vm_commands,
        "_os_family_for_vm",
        lambda vm, cmd: fam_by_name[vm.name],
    )
    ran: list[str] = []
    monkeypatch.setattr(
        cluster_krb5,
        "setup",
        lambda cluster, **kw: ran.append(cluster.name),
    )

    ns = argparse.Namespace(name="co9", realm="LTVM.TEST", timeout=600)
    if should_pass:
        cluster_krb5.cmd_cluster_krb5(ns)
        assert ran == ["co9"]
    else:
        with pytest.raises(SystemExit):
            cluster_krb5.cmd_cluster_krb5(ns)
        assert ran == []


def test_krb5_scripts_syntax_and_debian_support() -> None:
    for script in (cluster_krb5.NODE_SCRIPT, cluster_krb5.KDC_SCRIPT):
        r = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr

    node_sh = cluster_krb5.NODE_SCRIPT.read_text()
    assert "krb5-user sssd-kcm keyutils" in node_sh
    assert "DEBIAN_FRONTEND=noninteractive" in node_sh
    assert 'KDC_LOG="SYSLOG:INFO:DAEMON"' in node_sh

    kdc_sh = cluster_krb5.KDC_SCRIPT.read_text()
    assert "/etc/krb5kdc" in kdc_sh
    assert "/var/lib/krb5kdc" in kdc_sh
    assert "krb5-kdc krb5-admin-server" in kdc_sh
    assert "KDC_SVC=krb5-kdc" in kdc_sh
    assert "systemctl disable --now krb5-admin-server.service" in kdc_sh


@pytest.mark.parametrize("target", ["debian13", "ubuntu2404", "ubuntu2604"])
def test_debian_package_maps_include_krb5_server_and_sssd_kcm(
    target: str,
) -> None:
    from pathlib import Path

    lines = [
        line.split()
        for line in (
            Path(__file__).resolve().parent.parent
            / "targets"
            / target
            / "package-map.txt"
        )
        .read_text()
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ["krb5-server", "krb5-kdc", "krb5-admin-server"] in lines
    assert ["sssd-kcm", "-"] not in lines


@pytest.mark.parametrize(
    ("target", "want_crypto"),
    [
        ("rocky8", True),
        ("rocky9", True),
        ("rocky9-64k", True),
        ("rocky10", True),
        ("rocky10-16k", True),
        ("debian13", True),
        ("ubuntu2604-server", True),
        ("ubuntu2404", False),
        ("ubuntu2604", False),
        ("mainline", False),
    ],
)
def test_targets_configure_gss_and_crypto(
    target: str, want_crypto: bool
) -> None:
    from ltvm_pkg.target_config import TargetConfig

    tc = TargetConfig(target)
    assert "--enable-gss=yes" in tc.configure_args
    if want_crypto:
        assert "--enable-crypto=yes" in tc.configure_args
    else:
        assert "--enable-crypto=yes" not in tc.configure_args
