"""Shared test fixtures for the validate module.

Builds synthetic data/ roots and synthetic git repos so unit tests can exercise
the full validate.py code path without touching real subject-project clones or
real pipeline outputs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from reviewlore.tuner.config import (
    ActionabilityConfig,
    BackendConfig,
    CollectionConfig,
    Config,
    ConcurrencyConfig,
    FilterConfig,
    LabelConfig,
    OptimizerConfig,
    ProjectConfig,
    PromptConfig,
    SplitConfig,
)


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


@dataclass
class SyntheticRev:
    pr_number: int
    head_sha_short: str  # 8 char
    verdicts: list[tuple[int, str]]  # (human_comment_index, verdict)


def _make_judge_verdicts(revs_verdicts: list[tuple[int, str]]) -> dict:
    return {
        "verdicts": [
            {
                "human_comment_index": idx,
                "human_file": f"src/file_{idx}.py",
                "human_line": idx * 10,
                "human_summary": f"Concern about thing {idx}",
                "verdict": v,
                "matched_ai_comment_index": 0 if v == "match" else None,
                "reasoning": f"reasoning {idx}",
            }
            for idx, v in revs_verdicts
        ],
        "summary": {
            "total_human_comments": len(revs_verdicts),
            "matches": sum(1 for _, v in revs_verdicts if v == "match"),
            "no_matches": sum(1 for _, v in revs_verdicts if v == "no_match"),
            "recall": None,
            "unmatched_ai_comments": 0,
        },
        "cost_usd": 0.123,
        "duration_seconds": 42.0,
    }


def _make_review(n_inline: int = 2, n_general: int = 1) -> dict:
    return {
        "inline_comments": [
            {"file_path": f"src/file_{i}.py", "line_number": i * 10, "dimension": "D", "importance": "minor", "comment": f"AI inline {i}"}
            for i in range(n_inline)
        ],
        "general_comments": [
            {"file_path": None, "line_number": None, "dimension": "D", "importance": "minor", "comment": f"AI general {i}"}
            for i in range(n_general)
        ],
        "cost_usd": 0.5,
        "duration_seconds": 60.0,
    }


def _make_context(pr_number: int, head_sha: str, merge_base_sha: str, n_humans: int) -> dict:
    return {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "title": f"PR {pr_number}",
        "description": "",
        "diff": "",
        "human_comments": [
            {"body": f"full body for comment {i}", "author": "bob", "file_path": f"src/file_{i}.py", "line_number": i * 10, "is_resolved": False, "category": "CC", "dimension": "D"}
            for i in range(n_humans)
        ],
    }


@pytest.fixture
def synthetic_subject_repo(tmp_path_factory) -> Path:
    """A bare-minimum git repo with two commits; the second adds, modifies, deletes files.

    Used to exercise the worktree provisioning recipe in test_validate.py.
    """
    repo = tmp_path_factory.mktemp("subject_repo")
    _run_git(["init", "-q", "-b", "main"], cwd=repo)
    _run_git(["config", "user.email", "t@t.com"], cwd=repo)
    _run_git(["config", "user.name", "tester"], cwd=repo)
    _run_git(["config", "commit.gpgsign", "false"], cwd=repo)

    # Base commit: keep.txt (will be modified), remove.txt (will be deleted)
    (repo / "keep.txt").write_text("base\n")
    (repo / "remove.txt").write_text("delete me\n")
    _run_git(["add", "keep.txt", "remove.txt"], cwd=repo)
    _run_git(["commit", "-q", "-m", "base"], cwd=repo)
    base_sha = _run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Head commit: modify keep.txt, delete remove.txt, add new.txt
    (repo / "keep.txt").write_text("head\n")
    (repo / "remove.txt").unlink()
    (repo / "new.txt").write_text("added by PR\n")
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", "head"], cwd=repo)
    head_sha = _run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Stash SHAs as a sidecar file next to the repo so tests can read them
    (repo.parent / f"{repo.name}.shas.json").write_text(json.dumps({"base": base_sha, "head": head_sha}))
    return repo


@pytest.fixture
def mini_config(tmp_path: Path) -> Config:
    """Minimal Config dataclass pointing at tmp_path/reviewlore.yaml."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    prompt_path = config_dir / "prompts" / "judge_system.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("# judge system prompt\nv1 content\n")
    cfg = Config(
        output_dir="data/",
        backend=BackendConfig(name="claude-code", claude_code={}),
        models={},
        budgets={},
        prompts={
            "judge": PromptConfig(path="prompts/judge_system.md", version=2),
        },
        optimizer=OptimizerConfig(),
        concurrency=ConcurrencyConfig(),
        filter=FilterConfig(actionability=ActionabilityConfig()),
        label=LabelConfig(),
        collection=CollectionConfig(),
        split=SplitConfig(),
        conditions=["generic", "tuned_100", "tuned_200"],
        projects={},  # filled by specific tests
        config_dir=config_dir,
    )
    return cfg


@pytest.fixture
def synthetic_data_root(tmp_path: Path, synthetic_subject_repo: Path, mini_config: Config) -> tuple[Path, Config]:
    """Populate tmp_path/data/ with 3 synthetic projects (storybook/mermaid/ase
    shapes, small rev counts) and wire them into mini_config.

    Every project uses the same synthetic_subject_repo (simplifies worktree tests
    since all head_sha / merge_base_sha are the same).
    """
    data_root = tmp_path / "data"
    shas = json.loads((synthetic_subject_repo.parent / f"{synthetic_subject_repo.name}.shas.json").read_text())
    head = shas["head"]
    base = shas["base"]

    # 3 projects x 3 conditions; each cell has 5 revisions with varying verdict density
    project_names = ["proj_a", "proj_b", "proj_c"]
    rev_density = {
        "proj_a": [1, 2, 1, 3, 1],  # 8 verdicts per condition
        "proj_b": [2, 2, 2, 2, 2],  # 10 verdicts per condition
        "proj_c": [1, 1, 1, 1, 1],  # 5 verdicts per condition
    }
    pr_numbers_by_project = {
        "proj_a": [100, 101, 102, 103, 104],
        "proj_b": [200, 201, 202, 203, 204],
        "proj_c": [300, 301, 302, 303, 304],
    }

    for proj in project_names:
        mini_config.projects[proj] = ProjectConfig(
            platform="github",
            repo=f"synthetic/{proj}",
            language="python",
            subject_repo=str(synthetic_subject_repo),
        )
        pr_numbers = pr_numbers_by_project[proj]
        densities = rev_density[proj]

        # testable_revisions.json (used for head/base SHA lookup in validate.py)
        testable = []
        for pr in pr_numbers:
            testable.append({
                "pr_number": pr,
                "head_sha": head,
                "merge_base_sha": base,
                "thread_count": 1,
                "is_final_revision": False,
            })
        _write_json(data_root / proj / "4_split" / "testable_revisions.json", testable)

        for cond in mini_config.conditions:
            for pr, dens in zip(pr_numbers, densities):
                pr_dir = f"pr_{pr}"
                rev_dir = f"rev_{head[:8]}"
                # Verdicts: alternate match/no_match deterministically per (cond, pr, idx)
                verdicts = [(i, "match" if (pr + i) % 2 == 0 else "no_match") for i in range(dens)]
                _write_json(
                    data_root / proj / "7_judgments" / cond / pr_dir / rev_dir / "verdicts.json",
                    _make_judge_verdicts(verdicts),
                )
                _write_json(
                    data_root / proj / "6_reviews" / cond / pr_dir / rev_dir / "review.json",
                    _make_review(n_inline=2, n_general=1),
                )
                _write_json(
                    data_root / proj / "6_reviews" / cond / pr_dir / rev_dir / "context.json",
                    _make_context(pr, head, base, n_humans=dens),
                )
            # 7_judgments/<condition>/ metadata is not per-condition; instead write per-project:
        _write_json(
            data_root / proj / "7_judgments" / "metadata.json",
            {
                "step": "judge",
                "project": proj,
                "status": "complete",
                "prompt_versions": {"judge": 2},
                "cost": {"total_usd": 1.0},
                "duration_seconds": 100.0,
            },
        )

    return data_root, mini_config
