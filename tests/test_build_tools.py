"""Tests for targets/common/build-tools.sh download caching and Dockerfile wiring."""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
from pathlib import Path

import pytest

from ltvm_pkg import image_build as ib
from ltvm_pkg.target_config import TARGETS_DIR

BUILD_TOOLS = TARGETS_DIR / "common" / "build-tools.sh"


def _run_helper(
    body: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f'LTVM_BUILD_TOOLS_FUNCTIONS_ONLY=1 source "{BUILD_TOOLS}"\n'
        f"{body}\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )


class TestBuildToolsStaticChecks:
    def test_valid_bash_syntax(self) -> None:
        r = subprocess.run(
            ["bash", "-n", str(BUILD_TOOLS)], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr

    def test_no_git_protocol_urls(self) -> None:
        text = BUILD_TOOLS.read_text()
        assert "git://" not in text
        assert "https://git.linux-nfs.org/cgit/steved/cthon04.git" in text

    def test_all_git_fetches_pin_explicit_ref(self) -> None:
        import re

        text = BUILD_TOOLS.read_text()
        assert "PJDFSTEST_REF=" in text
        assert "FLAMEGRAPH_REF=" in text
        assert 'fetch_git_tree "pjdfstest-${PJDFSTEST_REF}"' in text
        assert 'fetch_git_tree "FlameGraph-${FLAMEGRAPH_REF}"' in text
        # Every fetch_git_tree invocation must pass a non-empty ref.
        body = text.split("LTVM_BUILD_TOOLS_FUNCTIONS_ONLY", 1)[1]
        joined = body.replace("\\\n", " ")
        calls = [
            line.strip()
            for line in joined.splitlines()
            if re.match(r"^\s*fetch_git_tree\b", line)
        ]
        assert len(calls) >= 3
        for call in calls:
            assert '""' not in call, f"unpinned fetch_git_tree call: {call}"

    def test_every_image_dockerfile_mounts_tools_cache(self) -> None:
        dockerfiles = sorted(TARGETS_DIR.glob("*/image.Dockerfile"))
        assert dockerfiles
        for df in dockerfiles:
            text = df.read_text()
            assert (
                "--mount=type=cache,id=ltvm-build-tools,"
                "target=/var/cache/ltvm-tools,sharing=shared"
            ) in text, f"{df} missing ltvm-build-tools cache mount"
            assert "LTVM_TOOLS_CACHE=/var/cache/ltvm-tools" in text, (
                f"{df} missing LTVM_TOOLS_CACHE env var"
            )

    def test_metabench_verifies_sha256_on_hit_and_before_cache_put(
        self,
    ) -> None:
        text = BUILD_TOOLS.read_text()
        assert 'rm -f "$_mb_cached"' not in text
        assert "&& _mb_check >/dev/null 2>&1" in text
        miss_start = text.index('if [[ "$_mb_ok" -eq 0 ]]; then')
        miss_body = text[miss_start:]
        assert miss_body.index("_mb_check") < miss_body.index(
            '_cache_put /tmp/metabench.tar "metabench-'
        )

    def test_cross_patch_dockerfile_handles_all_real_dockerfiles(
        self, tmp_path: Path
    ) -> None:
        prebuilt = tmp_path / "_prebuilt"
        for sub in ("usr/local/bin", "opt/connectathon", "usr/bin", "sbin"):
            (prebuilt / sub).mkdir(parents=True)
        for df in sorted(TARGETS_DIR.glob("*/image.Dockerfile")):
            patched = ib.cross_patch_dockerfile(df.read_text(), prebuilt)
            assert "bash /tmp/build-tools.sh" not in patched, df
            assert "bash /tmp/build-e2fsprogs.sh" not in patched, df
            assert "COPY _prebuilt/usr/local/ /usr/local/" in patched, df


class TestFetchHelpers:
    def test_fetch_file_caches_and_verifies_sha256(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "upstream.txt"
        src.write_text("hello build-tools\n")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        cache = tmp_path / "cache"
        dest1 = tmp_path / "dest1.txt"
        dest2 = tmp_path / "dest2.txt"

        # Cache miss: curl copies upstream file and populates cache.
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_file "sample.txt" "file://{src}" "{dest1}" "{digest}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert dest1.read_text() == "hello build-tools\n"
        assert (cache / "sample.txt").read_text() == "hello build-tools\n"

        # Cache hit: curl must not be called (fail if invoked).
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            'curl() { echo "unexpected curl" >&2; return 99; }\n'
            f'fetch_file "sample.txt" "https://unused.example/x" "{dest2}" "{digest}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert dest2.read_text() == "hello build-tools\n"
        # Cache entries must be 0644 (not mktemp's 0600) so non-root users in
        # the guest can read cached files like /usr/share/dbench/client.txt.
        assert (cache / "sample.txt").stat().st_mode & 0o777 == 0o644
        assert dest2.stat().st_mode & 0o777 == 0o644

        # Corrupt cached entry (bad sha256): self-heals from upstream.
        (cache / "sample.txt").write_text("corrupted\n")
        dest3 = tmp_path / "dest3.txt"
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_file "sample.txt" "file://{src}" "{dest3}" "{digest}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert dest3.read_text() == "hello build-tools\n"
        assert (cache / "sample.txt").read_text() == "hello build-tools\n"

    def test_fetch_file_rejects_bad_upstream_sha256(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "upstream.txt"
        src.write_text("unexpected content\n")
        cache = tmp_path / "cache"
        dest = tmp_path / "dest.txt"

        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_file "bad.txt" "file://{src}" "{dest}" "{"0" * 64}"\n'
        )
        assert r.returncode != 0
        assert "sha256 mismatch" in r.stderr
        assert not dest.exists()
        assert not (cache / "bad.txt").exists()

    @pytest.mark.parametrize(
        "mode,ext,flags",
        [("w:gz", ".tar.gz", "-xzf"), ("w:", ".tar", "-xf")],
    )
    def test_fetch_tar_caches_and_recovers_from_corrupt_cache(
        self, tmp_path: Path, mode: str, ext: str, flags: str
    ) -> None:
        payload = tmp_path / "src_file.txt"
        payload.write_text("inside archive\n")
        archive = tmp_path / f"pkg{ext}"
        with tarfile.open(archive, mode) as tf:
            tf.add(payload, arcname="pkg/src_file.txt")

        cache = tmp_path / "cache"
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out3 = tmp_path / "out3"

        # Miss -> extracts & caches
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_tar "pkg{ext}" "file://{archive}" "{flags}" "{out1}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert (out1 / "pkg" / "src_file.txt").read_text() == "inside archive\n"
        assert (cache / f"pkg{ext}").is_file()

        # Hit -> uses cache without invoking curl
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            'curl() { echo "unexpected curl" >&2; return 99; }\n'
            f'fetch_tar "pkg{ext}" "https://unused.example/x" "{flags}" "{out2}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert (out2 / "pkg" / "src_file.txt").read_text() == "inside archive\n"

        # Corrupt cache -> re-fetches and heals cache
        (cache / f"pkg{ext}").write_bytes(b"not a valid tar archive")
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_tar "pkg{ext}" "file://{archive}" "{flags}" "{out3}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert (out3 / "pkg" / "src_file.txt").read_text() == "inside archive\n"

    def test_fetch_git_tree_pinned_ref_and_cache_hit(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "origin"
        repo.mkdir()
        git_env = {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
        }
        subprocess.run(
            ["git", "init", "-q", str(repo)], check=True, env=git_env
        )
        (repo / "f.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "f.txt"], check=True, env=git_env
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "v1"],
            check=True,
            env=git_env,
        )
        ref1 = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            env=git_env,
        ).strip()
        (repo / "f.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-am", "v2"],
            check=True,
            env=git_env,
        )

        cache = tmp_path / "cache"
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"

        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            f'fetch_git_tree "demo-{ref1}" "{repo}" "{ref1}" "{out1}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert (out1 / "f.txt").read_text() == "v1\n"
        assert not (out1 / ".git").exists()
        assert (cache / f"demo-{ref1}.tar.gz").is_file()

        # Cache hit: git must not be called.
        r = _run_helper(
            f'TOOLS_CACHE_DIR="{cache}"\n'
            'git() { echo "unexpected git" >&2; return 99; }\n'
            f'fetch_git_tree "demo-{ref1}" "https://unused.example/x" "{ref1}" "{out2}"\n'
        )
        assert r.returncode == 0, r.stderr
        assert (out2 / "f.txt").read_text() == "v1\n"
