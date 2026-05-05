"""Combined review + judge orchestration with worktree lifecycle."""

from __future__ import annotations

import json
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import atomic_write_json, load_json, write_metadata

from .review import assemble_reviewer_prompt, run_review
from .judge import run_judge
from reviewlore.backends.base import AgentBackend


def _run_git(args: list[str], repo_path: str) -> subprocess.CompletedProcess:
    """Run a git command in the given repo."""
    return subprocess.run(
        ["git", "-C", repo_path] + args,
        capture_output=True, text=True, timeout=60,
    )


def build_context(
    revision: dict,
    labeled_dir: Path,
    collected_dir: Path,
    worktree_path: Path,
) -> dict:
    """Build the shared context for a revision.

    Computes diff, loads MR metadata and human comments.
    """
    pr_number = revision["pr_number"]
    head_sha = revision["head_sha"]
    merge_base_sha = revision["merge_base_sha"]

    # Compute diff in worktree
    diff_result = subprocess.run(
        ["git", "-C", str(worktree_path), "diff",
         f"{merge_base_sha}...{head_sha}",
         "--", ":!*.png", ":!*.jpg", ":!*.gif", ":!*.ico",
         ":!*.woff", ":!*.woff2", ":!*.eot", ":!*.ttf"],
        capture_output=True, text=True, timeout=120,
    )
    diff = diff_result.stdout if diff_result.returncode == 0 else ""

    # Load MR metadata from collected data
    collected_file = collected_dir / f"pr_{pr_number}.json"
    collected_data = load_json(collected_file) or {}

    # Load human comments for this revision from labeled data
    labeled_file = labeled_dir / f"pr_{pr_number}.json"
    labeled_data = load_json(labeled_file) or {}
    threads = labeled_data.get("review_threads", [])

    human_comments = []
    for thread in threads:
        comments = thread.get("comments", [])
        if not comments:
            continue
        first = comments[0]
        if first.get("commit_sha") == head_sha:
            human_comments.append({
                "body": first.get("body", ""),
                "author": first.get("author", ""),
                "file_path": first.get("file_path"),
                "line_number": first.get("line_number"),
                "is_resolved": thread.get("is_resolved"),
                "category": first.get("category", "unknown"),
                "dimension": first.get("dimension", "unknown"),
            })

    context = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "title": collected_data.get("title", ""),
        "description": collected_data.get("body", ""),
        "diff": diff,
        "human_comments": human_comments,
    }

    return context


def process_revision(
    revision: dict,
    conditions: list[str],
    config: Config,
    backend: AgentBackend,
    project_name: str,
    data_dir: Path,
    worktree_lock: threading.Lock,
    review_only: bool = False,
    judge_only: bool = False,
    trial: bool = False,
    force: bool = False,
) -> dict:
    """Process a single revision: create worktree, run review + judge, cleanup.

    Returns summary dict with results.
    """
    pr_number = revision["pr_number"]
    head_sha = revision["head_sha"]
    short_sha = head_sha[:8]

    project_config = config.projects.get(project_name)
    if not project_config:
        return {"error": f"Unknown project: {project_name}"}

    # Resolve subject repo and worktree paths
    subject_repo = str((config.config_dir / project_config.subject_repo).resolve())
    worktree_base = Path(subject_repo).parent / ".worktrees"
    worktree_path = worktree_base / f"{project_name}-{short_sha}"

    labeled_dir = data_dir / DIRS["labeled"]
    collected_dir = data_dir / DIRS["collected"]
    snapshot_dir = data_dir / DIRS["optimization"] / "snapshots"

    summary = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "conditions": {},
    }

    # Fast path: if every (condition, phase) is already cached and not force,
    # skip worktree creation entirely. Saves ~1-2s of git work per revision
    # under the replay backend on a fully populated data dir.
    cached_context_paths = [
        data_dir / DIRS["reviews"] / cond / f"pr_{pr_number}" / f"rev_{short_sha}" / "context.json"
        for cond in conditions
    ]
    all_outputs_cached = (not force) and all(
        ((judge_only or (data_dir / DIRS["reviews"] / cond / f"pr_{pr_number}" / f"rev_{short_sha}" / "review.json").exists())
         and (review_only or (data_dir / DIRS["judgments"] / cond / f"pr_{pr_number}" / f"rev_{short_sha}" / "verdicts.json").exists()))
        for cond in conditions
    )
    if all_outputs_cached and any(p.exists() for p in cached_context_paths):
        for cond in conditions:
            cond_result = {"condition": cond}
            if not judge_only:
                cond_result["review"] = "skipped_existing"
            if not review_only:
                cond_result["judge"] = "skipped_existing"
            summary["conditions"][cond] = cond_result
        return summary

    try:
        # Create worktree (serialized)
        with worktree_lock:
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            result = _run_git(
                ["worktree", "add", "--detach", str(worktree_path), head_sha],
                subject_repo,
            )
            if result.returncode != 0:
                return {"error": f"Worktree creation failed: {result.stderr}"}

        # Build shared context
        context = build_context(
            revision, labeled_dir, collected_dir, worktree_path
        )

        # Save context.json (shared across conditions)
        # Use the first condition's review dir to save context
        for condition in conditions:
            rev_dir = data_dir / DIRS["reviews"] / condition / f"pr_{pr_number}" / f"rev_{short_sha}"
            context_path = rev_dir / "context.json"
            if not context_path.exists():
                rev_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_json(context_path, context)

        human_comments = context.get("human_comments", [])
        if not human_comments:
            summary["skipped"] = "no_human_comments"
            return summary

        # Process each condition
        for condition in conditions:
            review_dir = data_dir / DIRS["reviews"] / condition / f"pr_{pr_number}" / f"rev_{short_sha}"
            judgment_dir = data_dir / DIRS["judgments"] / condition / f"pr_{pr_number}" / f"rev_{short_sha}"

            cond_result = {"condition": condition}

            # Review phase
            if not judge_only:
                review_path = review_dir / "review.json"
                if review_path.exists() and not force:
                    cond_result["review"] = "skipped_existing"
                else:
                    system_prompt = assemble_reviewer_prompt(
                        condition, config, snapshot_dir
                    )
                    print(f"    Reviewing PR #{pr_number} rev {short_sha} [{condition}]...")
                    review = run_review(
                        context, condition, system_prompt,
                        config, backend, worktree_path, review_dir,
                        trial=trial,
                    )
                    cond_result["review"] = "success" if review else "failed"

            # Judge phase
            if not review_only:
                verdict_path = judgment_dir / "verdicts.json"
                if verdict_path.exists() and not force:
                    cond_result["judge"] = "skipped_existing"
                else:
                    # Load review output
                    review_data = load_json(review_dir / "review.json")
                    if review_data:
                        print(f"    Judging PR #{pr_number} rev {short_sha} [{condition}]...")
                        verdicts = run_judge(
                            human_comments, review_data,
                            config, backend, worktree_path, judgment_dir,
                            trial=trial,
                        )
                        cond_result["judge"] = "success" if verdicts else "failed"
                    else:
                        cond_result["judge"] = "skipped_no_review"

            summary["conditions"][condition] = cond_result

    finally:
        # Cleanup worktree (serialized)
        with worktree_lock:
            _run_git(
                ["worktree", "remove", "--force", str(worktree_path)],
                subject_repo,
            )

    return summary


def review_and_judge(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    conditions: list[str] | None = None,
    review_only: bool = False,
    judge_only: bool = False,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Run reviewer + judge on all testable revisions."""
    data_dir = out_dir / project_name
    split_dir = data_dir / DIRS["split"]

    # Load testable revisions
    testable_path = split_dir / "testable_revisions.json"
    if not testable_path.exists():
        print(f"No testable revisions for {project_name}. Run 'relox split' first.")
        return

    with open(testable_path) as f:
        revisions = json.load(f)

    if not revisions:
        print(f"No testable revisions found for {project_name}.")
        return

    # Determine conditions
    if conditions is None:
        if trial:
            conditions = ["generic"]
        else:
            conditions = config.conditions

    # Verify tuned conditions have snapshots; skip those that don't
    snapshot_dir = data_dir / DIRS["optimization"] / "snapshots"
    verified = []
    for cond in conditions:
        if cond == "generic":
            verified.append(cond)
        else:
            snapshot_path = snapshot_dir / f"{cond}.md"
            if snapshot_path.exists():
                verified.append(cond)
            else:
                print(f"  Warning: no snapshot for condition '{cond}' ({snapshot_path}), skipping")
    conditions = verified
    if not conditions:
        print(f"No runnable conditions for {project_name}.")
        return

    # Trial: limit revisions
    if trial:
        revisions = revisions[:1]

    print(f"\nReview+Judge: {project_name}")
    print(f"  Revisions: {len(revisions)}")
    print(f"  Conditions: {', '.join(conditions)}")
    if review_only:
        print(f"  Mode: review only")
    elif judge_only:
        print(f"  Mode: judge only")
    if trial:
        print(f"  TRIAL MODE")
    print()

    # Filter to revisions that need processing
    to_process = []
    for rev in revisions:
        needs_work = False
        pr_number = rev["pr_number"]
        short_sha = rev["head_sha"][:8]

        for cond in conditions:
            if not judge_only:
                review_path = data_dir / DIRS["reviews"] / cond / f"pr_{pr_number}" / f"rev_{short_sha}" / "review.json"
                if force or not review_path.exists():
                    needs_work = True
                    break
            if not review_only:
                verdict_path = data_dir / DIRS["judgments"] / cond / f"pr_{pr_number}" / f"rev_{short_sha}" / "verdicts.json"
                if force or not verdict_path.exists():
                    needs_work = True
                    break

        if needs_work:
            to_process.append(rev)

    if not to_process:
        print("  All revisions already processed.")
        return

    print(f"  Processing {len(to_process)} revisions ({len(revisions) - len(to_process)} already done)")

    worktree_lock = threading.Lock()
    workers = config.concurrency.review_workers
    stagger = config.concurrency.worker_stagger_delay
    start = time.time()
    results = []

    def _process(rev):
        return process_revision(
            rev, conditions, config, backend,
            project_name, data_dir, worktree_lock,
            review_only=review_only, judge_only=judge_only,
            trial=trial, force=force,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for i, rev in enumerate(to_process):
            if i > 0 and stagger > 0:
                time.sleep(stagger)
            futures.append(executor.submit(_process, rev))

        for future in futures:
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                print(f"    Error: {e}")
                results.append({"error": str(e)})

    elapsed = time.time() - start
    successes = sum(1 for r in results if "error" not in r)
    print(f"\n  Complete: {successes}/{len(to_process)} revisions in {elapsed:.0f}s")

    # Aggregate costs from per-revision raw.json files
    review_cost = 0.0
    judge_cost = 0.0
    for cond in conditions:
        for raw_path in (data_dir / DIRS["reviews"] / cond).rglob("raw.json"):
            raw = load_json(raw_path)
            if raw:
                review_cost += raw.get("cost_usd", 0.0)
        for raw_path in (data_dir / DIRS["judgments"] / cond).rglob("raw.json"):
            raw = load_json(raw_path)
            if raw:
                judge_cost += raw.get("cost_usd", 0.0)

    print(f"  Review cost: ${review_cost:.2f}, Judge cost: ${judge_cost:.2f}")

    # Write metadata
    write_metadata(data_dir / DIRS["reviews"], {
        "step": "review",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": {
            "conditions": conditions,
            "trial": trial,
            "review_only": review_only,
            "judge_only": judge_only,
        },
        "prompt_versions": {
            "reviewer_generic": config.prompts["reviewer_generic"].version,
        },
        "outputs": {
            "revisions_processed": successes,
            "revisions_failed": len(to_process) - successes,
        },
        "cost": {"total_usd": review_cost},
        "duration_seconds": elapsed,
    })

    if not review_only:
        write_metadata(data_dir / DIRS["judgments"], {
            "step": "judge",
            "project": project_name,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete",
            "prompt_versions": {
                "judge": config.prompts["judge"].version,
            },
            "cost": {"total_usd": judge_cost},
            "duration_seconds": elapsed,
        })
