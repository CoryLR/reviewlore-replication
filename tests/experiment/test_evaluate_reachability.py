"""Regression tests for ``_load_touched_files_per_rev``.

The replication artifact ships shallow bare bundles per subject repo: each
bundle contains the per-rev ``head_sha`` and ``merge_base_sha`` blobs and
trees but not the ancestry path that connects them, so ``git diff
<merge_base>...<head>`` (triple-dot) reports ``no merge base``. Reachability
must fall back to the two-dot form, which compares the two trees directly
and returns the same touched files because ``merge_base_sha`` is already
the pre-computed merge base in ``4_split/testable_revisions.json``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reviewlore.experiment.evaluate import _load_touched_files_per_rev
from reviewlore.tuner.dirs import DIRS


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_disconnected_history(repo: Path) -> tuple[str, str]:
    """Build a repo with two orphan roots that share no merge base.

    Mirrors what an artifact-shipped shallow bare bundle looks like to Git:
    both commits' trees are reachable, but the ancestry path between them
    is not, so triple-dot diff cannot compute a merge base.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "branch-b")
    (repo / "f.txt").write_text("v2\n")
    (repo / "g.txt").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return base_sha, head_sha


def _write_split(data_dir: Path, pr_number: int, head_sha: str, merge_base_sha: str) -> None:
    split_dir = data_dir / DIRS["split"]
    split_dir.mkdir(parents=True, exist_ok=True)
    (split_dir / "testable_revisions.json").write_text(json.dumps([
        {
            "pr_number": pr_number,
            "head_sha": head_sha,
            "merge_base_sha": merge_base_sha,
            "thread_count": 1,
            "is_final_revision": False,
        },
    ]))


def test_load_touched_files_falls_back_to_two_dot_when_no_merge_base(tmp_path: Path):
    repo = tmp_path / "shallow"
    base_sha, head_sha = _make_disconnected_history(repo)

    # Sanity: the scenario actually produces a "no merge base" failure under
    # triple-dot. If a future Git version changes this behavior the regression
    # framing needs revisiting.
    triple = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base_sha}...{head_sha}"],
        capture_output=True, text=True,
    )
    assert triple.returncode != 0
    assert "no merge base" in triple.stderr

    data_dir = tmp_path / "data"
    _write_split(data_dir, pr_number=1, head_sha=head_sha, merge_base_sha=base_sha)

    touched = _load_touched_files_per_rev(data_dir, repo)
    assert (1, head_sha) in touched
    assert touched[(1, head_sha)] == {"f.txt", "g.txt"}


def test_load_touched_files_uses_triple_dot_for_normal_history(tmp_path: Path):
    """Full repos with reachable ancestry stay on the triple-dot path; we
    rely on this as the default semantics so unrelated-branch edge cases
    cannot mask divergent histories. Two-dot is purely a shallow-bundle
    fallback.
    """
    repo = tmp_path / "full"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "g.txt").write_text("new\n")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")

    data_dir = tmp_path / "data"
    _write_split(data_dir, pr_number=2, head_sha=head_sha, merge_base_sha=base_sha)

    touched = _load_touched_files_per_rev(data_dir, repo)
    assert (2, head_sha) in touched
    assert touched[(2, head_sha)] == {"g.txt"}
