"""Golden values for TargetConfig.input_hash.

``input_hash`` is the staleness key: it is written into every artifact's
meta.json and compared against a freshly computed one to decide whether
to rebuild.  So changing what it produces, for inputs that did not
change, silently invalidates every built artifact on every machine --
and the cost is a kernel rebuild per target, not a warning.

These goldens exist so a refactor of the hash *composition* (splitting
it into named components for `build status --why`, say) cannot do that
by accident.  They are not a specification of the algorithm: if you
deliberately change what feeds the hash, these values must be updated in
the same commit, and everyone rebuilds. That is the signal, and it
should be a conscious one.

They depend on targets.yaml and on the files the hash reads
(Dockerfiles, kernel config fragments, package lists, the inner build
scripts), so an intentional edit to any of those also moves them.
"""

from __future__ import annotations

import pytest

from ltvm_pkg.target_config import TargetConfig

# (target, artifact, kernel, variant, expected)
GOLDEN = [
    ("rocky8", "container", None, None, "ae0485de3ca17351"),
    ("rocky8", "kernel", None, None, "78f55921ba96459a"),
    ("rocky8", "image", None, None, "e7e15be8e2605dfd"),
    ("rocky9", "container", None, None, "008f4e44e6be344b"),
    ("rocky9", "kernel", None, None, "7e79d71c25ca6c06"),
    ("rocky9", "image", None, None, "1ea0364fb3bf2f62"),
    ("rocky9-64k", "container", None, None, "776c53d3031309a6"),
    ("rocky9-64k", "kernel", None, None, "c3a54e865fb41c3c"),
    ("rocky9-64k", "image", None, None, "c1854190b1b4b39b"),
    ("rocky10", "container", None, None, "25489b034ece9e61"),
    ("rocky10", "kernel", None, None, "7de2b845e33fbeee"),
    ("rocky10", "image", None, None, "6db51424f1277be6"),
    ("rocky10-16k", "container", None, None, "1abe3896f9fe92c8"),
    ("rocky10-16k", "kernel", None, None, "aa757838090b4852"),
    ("rocky10-16k", "image", None, None, "e5181d4f9dbcb2a0"),
    ("mainline", "container", None, None, "952757182b5e7a35"),
    ("mainline", "kernel", None, None, "145fc824ca1285b7"),
    ("mainline", "image", None, None, "2512e1b2c5e127fd"),
    ("ubuntu2404", "container", None, None, "178534ef488aa84b"),
    ("ubuntu2404", "kernel", None, None, "09c8607b7f125a7e"),
    ("ubuntu2404", "image", None, None, "5ae94ab9c80b1167"),
    ("ubuntu2604", "container", None, None, "0506541d35f3d5bf"),
    ("ubuntu2604", "kernel", None, None, "62ffd841ecb0594a"),
    ("ubuntu2604", "image", None, None, "4233d58a2c38b278"),
    ("ubuntu2604-server", "container", None, None, "1ec98b797a4c27fc"),
    ("ubuntu2604-server", "kernel", None, None, "e369856eaba8f00d"),
    ("ubuntu2604-server", "image", None, None, "63a410ec58108a5d"),
    ("debian13", "container", None, None, "d72e6ce55760527d"),
    ("debian13", "kernel", None, None, "92f7fcae576ab23e"),
    ("debian13", "image", None, None, "f207b6c616bc6ce9"),
    ("opensuse156", "container", None, None, "aec416f56a4b56f8"),
    ("opensuse156", "kernel", None, None, "a74c30cdb209d311"),
    ("opensuse156", "image", None, None, "9bde0d7249b11ed7"),
    # A variant must not perturb the base hashes above, and must differ
    # from them.
    ("rocky9", "container", None, "mofed-24", "936ea75d99eb7a74"),
    ("rocky9", "image", None, "mofed-24", "670a65b7e89363ef"),
    # An explicitly named kernel.
    ("rocky9", "kernel", "5.14-rhel9.5", None, "ace413737ea07990"),
    ("rocky9", "image", "5.14-rhel9.5", None, "0ad3978aa06831db"),
]


@pytest.fixture(autouse=True)
def _no_built_kernel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An image's hash folds in its built kernel's input_hash when one
    exists; pin the goldens without it, or they hold only on the host
    whose artifacts/ produced them."""
    monkeypatch.setattr(
        TargetConfig, "meta_path", lambda self, *a, **k: tmp_path / "none"
    )


@pytest.mark.parametrize(
    "target,artifact,kernel,variant,expected",
    GOLDEN,
    ids=[
        f"{t}-{a}{'-' + k if k else ''}{'-' + v if v else ''}"
        for t, a, k, v, _ in GOLDEN
    ],
)
def test_input_hash_is_unchanged(
    target: str,
    artifact: str,
    kernel: str | None,
    variant: str | None,
    expected: str,
) -> None:
    tc = TargetConfig(target, variant=variant or "base")
    got = tc.input_hash(artifact, kernel=kernel, variant=variant)
    assert got == expected, (
        f"input_hash({target}, {artifact}, kernel={kernel}, "
        f"variant={variant}) changed: {expected} -> {got}.\n"
        f"Every built {artifact} for {target} just became stale. If that "
        f"is intended, update the golden in the same commit."
    )


def test_extra_bytes_still_fold_in() -> None:
    """kernel_build passes the Lustre patch series through `extra`.

    Without it, editing a patch in place would not invalidate the cached
    vmlinuz -- the exact workflow ltvm exists for.
    """
    tc = TargetConfig("rocky9")
    assert tc.input_hash("kernel", extra=b"patchbytes") == "b1c5f843e43da0b0"
    assert tc.input_hash("kernel", extra=b"patchbytes") != tc.input_hash(
        "kernel"
    )


def test_shmrdma_sources_skip_mofed_images(monkeypatch) -> None:
    """A MOFED image never carries shmrdma.ko, so its sources are not an
    input of that image; they are of the base image."""
    from ltvm_pkg import shmrdma

    base = TargetConfig("rocky9")
    mofed = TargetConfig("rocky9", variant="mofed-24")
    before = (
        base.input_hash("image"),
        mofed.input_hash("image", variant="mofed-24"),
    )
    monkeypatch.setattr(shmrdma, "kmod_source_hash", lambda: b"edited")
    after = (
        base.input_hash("image"),
        mofed.input_hash("image", variant="mofed-24"),
    )
    assert before[0] != after[0]
    assert before[1] == after[1]


def test_configure_args_excluded_from_artifact_hashes() -> None:
    """configure_args is only read by lustre_build; changing it must not rebuild kernels."""
    tc = TargetConfig("rocky9")
    before = {a: tc.input_hash(a) for a in ("container", "kernel", "image")}
    tc._data = {
        **tc._data,
        "configure_args": ["--enable-gss", "--enable-extra"],
    }
    after = {a: tc.input_hash(a) for a in ("container", "kernel", "image")}
    assert after == before
