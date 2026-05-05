"""Temporal split into tuning/test sets with testability assessment."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import atomic_write_json, load_json, write_metadata


def _run_git(args: list[str], repo_path: str) -> subprocess.CompletedProcess:
    """Run a git command in the given repo."""
    return subprocess.run(
        ["git", "-C", repo_path] + args,
        capture_output=True, text=True, timeout=30,
    )


def temporal_split(
    labeled_dir: Path,
    config: Config,
    trial: bool = False,
) -> tuple[list[int], list[int]]:
    """Split labeled MRs into tuning and test sets by merge date.

    Returns (tuning_prs, test_prs) as lists of PR numbers.
    Trial mode uses 50/50 split instead of 80/20.
    """
    # Load all labeled MR files with their merge dates
    mr_files = sorted(labeled_dir.glob("pr_*.json"))
    if not mr_files:
        raise ValueError(f"No labeled files found in {labeled_dir}")

    mrs = []
    missing_merged_at: list[int] = []
    for f in mr_files:
        data = load_json(f)
        if data:
            merged_at_raw = data.get("merged_at")
            # Some platforms omit or null out merged_at on very old merged MRs
            # (observed on GitLab for ASE MRs !187 and !276). Treat these as
            # the earliest available merges so they land in the tuning set
            # rather than crashing the sort; log so the caller can inspect.
            if not merged_at_raw:
                missing_merged_at.append(data["number"])
                merged_at_raw = ""
            mrs.append({
                "number": data["number"],
                "merged_at": merged_at_raw,
                "path": f,
            })

    if missing_merged_at:
        print(
            f"  Note: {len(missing_merged_at)} MR(s) had missing merged_at "
            f"and were treated as earliest for temporal ordering: "
            f"{sorted(missing_merged_at)[:10]}{' ...' if len(missing_merged_at) > 10 else ''}"
        )

    # Sort by merge date (oldest first); missing dates sort as empty string, i.e. first.
    mrs.sort(key=lambda m: m["merged_at"])

    # Split
    if trial:
        # 50/50 for trial so both paths are exercised
        split_idx = len(mrs) // 2
    else:
        split_idx = int(len(mrs) * config.split.tuning_ratio)

    tuning = [m["number"] for m in mrs[:split_idx]]
    test = [m["number"] for m in mrs[split_idx:]]

    return tuning, test


def assess_testability(
    test_prs: list[int],
    labeled_dir: Path,
    project_config,
    config: Config,
) -> list[dict]:
    """Assess which test MR revisions are testable for evaluation.

    For each test MR:
    1. Check non-squash merge (2+ parents)
    2. Group threads by first comment commit_sha to identify revisions
    3. Check commit reachability and non-final status
    4. Compute merge base for usable revisions

    Returns list of testable revision dicts.
    """
    repo_path = str(
        (config.config_dir / project_config.subject_repo).resolve()
    )
    testable = []

    for pr_number in test_prs:
        pr_file = labeled_dir / f"pr_{pr_number}.json"
        data = load_json(pr_file)
        if data is None:
            continue

        # Check non-squash merge
        parent_count = data.get("merge_commit_parent_count", 0)
        if parent_count < 2:
            continue

        merge_sha = data.get("merge_commit_sha", "")
        base_ref = data.get("base_ref", "")

        # Get final branch commit (second parent of merge commit)
        final_sha = None
        result = _run_git(["rev-parse", f"{merge_sha}^2"], repo_path)
        if result.returncode == 0:
            final_sha = result.stdout.strip()

        # Group threads by first comment's commit_sha
        threads = data.get("review_threads", [])
        revisions: dict[str, list[dict]] = {}

        for thread in threads:
            comments = thread.get("comments", [])
            if not comments:
                continue
            first = comments[0]
            sha = first.get("commit_sha")
            if not sha:
                continue
            if sha not in revisions:
                revisions[sha] = []
            revisions[sha].append(thread)

        # Assess each revision
        for head_sha, rev_threads in revisions.items():
            # Skip final revision
            if final_sha and head_sha == final_sha:
                continue

            # Check reachability
            result = _run_git(["cat-file", "-t", head_sha], repo_path)
            if result.returncode != 0:
                continue

            # Try base_ref first (cheap when it works, handles the normal case).
            merge_base_sha = ""
            if base_ref:
                result = _run_git(
                    ["merge-base", base_ref, head_sha], repo_path
                )
                if result.returncode == 0:
                    candidate = result.stdout.strip()
                    # Reject candidate == head_sha: happens when head_sha is already
                    # an ancestor of base_ref after the PR merged, which makes
                    # git diff <candidate>...<head_sha> empty.
                    if candidate and candidate != head_sha:
                        merge_base_sha = candidate

            # Fallback: walk parents of the merge commit and pick the first parent
            # whose merge-base with head_sha yields a non-self, non-empty SHA. This
            # handles (a) normal GitHub merges where ^1 is the base-side parent,
            # (b) author-run "git merge base" commits where ^1 is the feature side
            # (observed on Mermaid PR 5665), and (c) octopus merges with 3+ parents.
            if not merge_base_sha and merge_sha:
                for parent_n in range(1, 5):  # ^1..^4, typically stops at ^2
                    result = _run_git(
                        ["rev-parse", f"{merge_sha}^{parent_n}"], repo_path
                    )
                    if result.returncode != 0:
                        break  # no more parents on this merge commit
                    parent_sha = result.stdout.strip()
                    if not parent_sha or parent_sha == head_sha:
                        continue  # this parent IS head_sha; try the next one
                    result = _run_git(
                        ["merge-base", parent_sha, head_sha], repo_path
                    )
                    if result.returncode != 0:
                        continue
                    candidate = result.stdout.strip()
                    if candidate and candidate != head_sha:
                        merge_base_sha = candidate
                        break

            if not merge_base_sha:
                continue  # no usable fork point; silently skip this rev

            testable.append({
                "pr_number": pr_number,
                "head_sha": head_sha,
                "merge_base_sha": merge_base_sha,
                "thread_count": len(rev_threads),
                "is_final_revision": False,
            })

    return testable


def split_project(
    project_name: str,
    config: Config,
    out_dir: Path,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Create temporal tuning/test split with testability assessment."""
    labeled_dir = out_dir / project_name / DIRS["labeled"]
    split_dir = out_dir / project_name / DIRS["split"]

    if not labeled_dir.exists():
        print(f"No labeled data for {project_name}. Run 'relo label' first.")
        return

    if not force and (split_dir / "testable_revisions.json").exists():
        print(f"Split already exists for {project_name}. Use --force to re-split.")
        return

    project_config = config.projects[project_name]

    print(f"Splitting {project_name}")
    if trial:
        print("  TRIAL MODE (50/50 split)")

    # Temporal split
    tuning_prs, test_prs = temporal_split(labeled_dir, config, trial)
    print(f"  Split: {len(tuning_prs)} tuning / {len(test_prs)} test")

    # Save split files
    split_dir.mkdir(parents=True, exist_ok=True)

    tuning_path = split_dir / "tuning.txt"
    tuning_path.write_text("\n".join(str(n) for n in tuning_prs) + "\n")

    test_path = split_dir / "test.txt"
    test_path.write_text("\n".join(str(n) for n in test_prs) + "\n")

    # Testability assessment
    print("  Assessing testability...")
    testable = assess_testability(
        test_prs, labeled_dir, project_config, config
    )
    atomic_write_json(split_dir / "testable_revisions.json", testable)

    testable_mrs = len(set(r["pr_number"] for r in testable))
    print(f"  Testable: {len(testable)} revisions across {testable_mrs} MRs")

    # Metadata
    write_metadata(split_dir, {
        "step": "split",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": {
            "tuning_ratio": 0.5 if trial else config.split.tuning_ratio,
            "trial": trial,
        },
        "outputs": {
            "tuning_count": len(tuning_prs),
            "test_count": len(test_prs),
            "testable_revisions": len(testable),
            "testable_mrs": testable_mrs,
        },
    })
