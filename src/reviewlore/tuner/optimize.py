"""Two-phase rule induction: parallel extraction + sequential consolidation."""

from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click

from .config import Config, get_prompt, get_prompt_version
from .dirs import DIRS
from .state import atomic_write_json, load_json, write_metadata
from reviewlore.backends.base import AgentBackend
from reviewlore.backends.replay import ReplayBackend


# ---------------------------------------------------------------------------
# Date parsing helpers for --merged-before / --merged-after CLI flags
# ---------------------------------------------------------------------------


def _parse_cli_date(s: str) -> datetime:
    """Parse an ISO 8601 date or datetime from a CLI argument.

    Accepts date-only (e.g. "2023-06-30"), datetime without timezone
    (e.g. "2023-06-30T12:34:56"), and ISO 8601 with Z or explicit
    offset (e.g. "2023-06-30T12:34:56Z" or "2023-06-30T12:34:56+00:00").
    Naive datetimes are treated as UTC. Returns a timezone-aware UTC
    datetime so comparisons with parsed `merged_at` values are sound.
    """
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_mr_date(s: str) -> datetime | None:
    """Parse an MR's `merged_at` field. Returns a tz-aware UTC datetime
    or None if the field is missing or unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Prompt assembly helpers
# ---------------------------------------------------------------------------

DIMENSIONS = [
    "Code Defect",
    "Maintainability and Readability",
    "Performance Issue",
    "Code Style",
]


def empty_rules_json() -> dict:
    """Return an empty rule set with all four dimensions present."""
    return {dim: [] for dim in DIMENSIONS}


def _coerce_rule_entry(entry: object) -> str:
    """Turn a rule-list entry into a clean string, tolerating legacy dict
    shapes like {"text": "..."} or {"rule": "..."}. Returns "" if the entry
    cannot be interpreted."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("text", "rule"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def sanitize_rules_json(obj: object) -> dict:
    """Coerce a parsed JSON object into the canonical rule-set schema.

    Accepts: {dimension: [str | {text|rule: str}, ...]}. Drops unknown
    top-level keys, ensures all four dimensions exist in canonical order,
    filters out non-string rule entries, and deduplicates while
    preserving insertion order.
    """
    result = empty_rules_json()
    if not isinstance(obj, dict):
        return result
    for dim in DIMENSIONS:
        raw = obj.get(dim)
        if not isinstance(raw, list):
            continue
        seen: set[str] = set()
        clean: list[str] = []
        for entry in raw:
            text = _coerce_rule_entry(entry)
            if not text or text in seen:
                continue
            seen.add(text)
            clean.append(text)
        result[dim] = clean
    return result


def count_rules(rules_json: dict) -> int:
    """Total number of rules across all dimensions."""
    return sum(len(rules_json.get(dim, [])) for dim in DIMENSIONS)


def render_rules_markdown(rules_json: dict) -> str:
    """Render the JSON rule set as 2-tier nested-bullet markdown.

    Tier 1: BitsAI-CR dimension (bold).
    Tier 2: rule text.

    Dimensions are emitted in canonical order; empty dimensions are
    omitted. Returns "(none)" when no rules are present, matching the
    legacy placeholder used by `assemble_reviewer_prompt`.
    """
    if count_rules(rules_json) == 0:
        return "(none)"
    lines: list[str] = []
    for dim in DIMENSIONS:
        rules = rules_json.get(dim, [])
        if not rules:
            continue
        lines.append(f"- **{dim}**")
        for rule in rules:
            lines.append(f"  - {rule}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase A: Extraction
# ---------------------------------------------------------------------------

EXTRACT_MARKER = "===FINAL_OUTPUT_BEGIN==="


def fetch_mr_diff(
    merge_sha: str,
    subject_repo: str,
    max_chars: int = 60000,
) -> str:
    """Return the MR's diff as a string.

    Uses `git diff <merge_sha>^N <merge_sha>` and walks parents ^1..^4 to
    find the first parent whose diff is non-empty. This handles normal
    merges, author-run "git merge base" commits with flipped parent
    ordering (e.g. Mermaid PR 5665), and octopus merges. Returns "" if
    no diff can be produced or if subject_repo/merge_sha are missing.
    The result is truncated to max_chars with a visible trailer.
    """
    if not merge_sha or not subject_repo:
        return ""
    for parent_n in range(1, 5):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    subject_repo,
                    "diff",
                    f"{merge_sha}^{parent_n}",
                    merge_sha,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        if result.returncode != 0:
            # `^N` does not exist (most merges have 2 parents, stop at ^3)
            if "unknown revision" in (result.stderr or "") or "bad revision" in (result.stderr or ""):
                break
            continue
        diff = result.stdout
        if diff.strip():
            if len(diff) > max_chars:
                diff = diff[:max_chars] + f"\n\n[... diff truncated at {max_chars} chars ...]"
            return diff
    return ""


def build_extraction_prompt(mr_data: dict, diff: str = "") -> str:
    """Build the user prompt for rule extraction from a single MR."""
    parts = []

    parts.append(f"## Merge Request: {mr_data.get('title', 'Untitled')}")
    parts.append("")

    body = mr_data.get("body", "")
    if body:
        parts.append("### Description")
        parts.append(body[:2000])  # Reasonable limit for description
        parts.append("")

    if diff:
        parts.append("### Merge Request Diff")
        parts.append("```diff")
        parts.append(diff)
        parts.append("```")
        parts.append("")

    parts.append("### Human Review Comments")
    parts.append("")

    threads = mr_data.get("review_threads", [])
    for i, thread in enumerate(threads):
        comments = thread.get("comments", [])
        if not comments:
            continue
        first = comments[0]
        file_path = first.get("file_path", "")
        line = first.get("line_number", "")
        author = first.get("author", "")
        body = first.get("body", "")

        location = ""
        if file_path:
            location = f"[{file_path}"
            if line:
                location += f":{line}"
            location += "]"

        parts.append(f"**Comment {i}** {location} (by {author}):")
        parts.append(body)

        # Include follow-up comments for context
        for reply in comments[1:]:
            reply_author = reply.get("author", "")
            reply_body = reply.get("body", "")
            parts.append(f"  > Reply ({reply_author}): {reply_body}")

        parts.append("")

    return "\n".join(parts)


def extract_from_mr(
    mr_path: Path,
    config: Config,
    backend: AgentBackend,
    subject_repo: str = "",
) -> dict | None:
    """Extract candidate rules from a single MR.

    Returns extraction result dict or None on failure.
    """
    mr_data = load_json(mr_path)
    if mr_data is None:
        return None

    merge_sha = mr_data.get("merge_commit_sha", "") or ""
    diff = fetch_mr_diff(merge_sha, subject_repo) if subject_repo else ""

    system_prompt = get_prompt("optimizer_extract", config)
    user_prompt = build_extraction_prompt(mr_data, diff=diff)

    result = backend.invoke(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=config.models.get("optimizer_extract", "claude-sonnet-4-6"),
        budget_usd=config.budgets.get("optimizer_extract", 25.0),
        tools=[],
        output_marker=EXTRACT_MARKER,
        effort_level="high",
        timeout_seconds=600,
    )

    extraction = {
        "pr_number": mr_data.get("number"),
        "source_mr_date": mr_data.get("merged_at", ""),
        "candidates": [],
        "analysis": [],
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
    }

    if result.parsed_json:
        extraction["candidates"] = result.parsed_json.get("candidates", [])
        extraction["analysis"] = result.parsed_json.get("analysis", [])
    else:
        extraction["parse_error"] = True
        extraction["raw_output"] = result.raw_output[:5000]

    return extraction


def extract_rules(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    mr_list: list[int] | None = None,
    merged_before: str | None = None,
    merged_after: str | None = None,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Extract candidate rules from labeled MRs in parallel.

    If mr_list is provided, extract only from those MR numbers.
    If merged_before / merged_after is provided, additionally filter
    to MRs whose `merged_at` timestamp is within the bounds (inclusive
    at both ends). Filters compose: mr_list intersected with date range.
    """
    labeled_dir = out_dir / project_name / DIRS["labeled"]
    extract_dir = out_dir / project_name / DIRS["optimization"] / "extraction"

    if not labeled_dir.exists():
        print(f"No labeled data for {project_name}.")
        return

    # Resolve subject_repo so extraction can hydrate per-MR diffs via git.
    project_config = config.projects.get(project_name)
    subject_repo = ""
    if project_config and getattr(project_config, "subject_repo", ""):
        raw_sr = project_config.subject_repo
        candidate = (config.config_dir / raw_sr).resolve()
        if candidate.is_dir():
            subject_repo = str(candidate)
        else:
            print(f"  WARN: subject_repo {candidate} not found; extraction will run without diffs")
    else:
        print(f"  WARN: subject_repo not set for {project_name}; extraction will run without diffs")

    # Find MR files to process
    all_files = sorted(labeled_dir.glob("pr_*.json"))
    if mr_list is not None:
        mr_set = set(mr_list)
        all_files = [f for f in all_files if _pr_number_from_path(f) in mr_set]

    # Apply --merged-before / --merged-after date-range filter on MR merged_at.
    # Inclusive at both ends. MRs with missing or unparseable merged_at are
    # excluded when any date bound is set (conservative: can't prove it's in
    # the window). This composes with mr_list (already applied above).
    if merged_before or merged_after:
        before_dt = _parse_cli_date(merged_before) if merged_before else None
        after_dt = _parse_cli_date(merged_after) if merged_after else None
        before_date_count = len(all_files)
        kept: list[Path] = []
        unparseable = 0
        for f in all_files:
            data = load_json(f)
            if not data:
                continue
            mr_dt = _parse_mr_date(data.get("merged_at", ""))
            if mr_dt is None:
                unparseable += 1
                continue
            if before_dt is not None and mr_dt > before_dt:
                continue
            if after_dt is not None and mr_dt < after_dt:
                continue
            kept.append(f)
        dropped = before_date_count - len(kept)
        if dropped > 0:
            msg = f"  Date filter: kept {len(kept)}/{before_date_count} MRs"
            if merged_before:
                msg += f" (merged_before={merged_before})"
            if merged_after:
                msg += f" (merged_after={merged_after})"
            if unparseable:
                msg += f" [{unparseable} dropped for unparseable merged_at]"
            print(msg)
        all_files = kept

    # Skip MRs with zero actionable human comments (post-Stage-3 filter).
    # An extraction call on an empty-comment MR costs ~$0.016 and returns
    # {"candidates": []}, so filter them up front.
    before_skip = len(all_files)
    filtered: list[Path] = []
    for f in all_files:
        data = load_json(f)
        if not data:
            continue
        threads = data.get("review_threads", [])
        has_comments = any(t.get("comments") for t in threads)
        if has_comments:
            filtered.append(f)
    skipped = before_skip - len(filtered)
    if skipped > 0:
        print(f"  Skipping {skipped} MRs with no actionable human comments")
    all_files = filtered

    # Trial mode: cap to the first few MRs for quick e2e testing. This
    # keeps `--trial` actually small rather than a silent full run.
    if trial:
        all_files = all_files[:2]
        print(f"  TRIAL MODE: capped to {len(all_files)} MRs")

    # Filter to unprocessed (resume support)
    to_process = []
    for f in all_files:
        pr_num = _pr_number_from_path(f)
        output_path = extract_dir / f"pr_{pr_num}.json"
        if not force and output_path.exists():
            continue
        to_process.append(f)

    if not to_process:
        print(f"  Extraction: all {len(all_files)} MRs already processed")
        return

    # Replay backend cannot regenerate per-MR extractions: the original
    # experiment did not save raw LLM envelopes for the extraction step
    # (only the parsed candidate-rule lists landed on disk). Fast-skip
    # missing MRs rather than wasting wall time on git-diff fetches that
    # would only feed ReplayMissingError. Pre-extracted MRs remain
    # available for consolidation.
    if isinstance(backend, ReplayBackend):
        already = len(all_files) - len(to_process)
        print(
            f"  Replay backend: {len(to_process)} MR(s) lack saved "
            f"extractions; skipping (the {already} already-extracted "
            f"MR(s) will still feed consolidation)."
        )
        return

    print(f"  Extracting rules from {len(to_process)} MRs ({len(all_files) - len(to_process)} already done)")

    workers = config.concurrency.extraction_workers
    stagger = config.concurrency.worker_stagger_delay
    total_cost = 0.0
    completed = 0
    failed = 0
    start = time.time()

    def _process_mr(mr_path: Path) -> dict | None:
        result = extract_from_mr(mr_path, config, backend, subject_repo=subject_repo)
        if result:
            pr_num = result["pr_number"]
            output_path = extract_dir / f"pr_{pr_num}.json"
            atomic_write_json(output_path, result)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for i, mr_path in enumerate(to_process):
            if i > 0 and stagger > 0:
                time.sleep(stagger)
            futures.append(executor.submit(_process_mr, mr_path))

        for future in futures:
            try:
                result = future.result()
                if result:
                    total_cost += result.get("cost_usd", 0.0)
                    completed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"    Extraction error: {e}")
                failed += 1

    elapsed = time.time() - start
    print(f"  Extraction complete: {completed} done, {failed} failed, ${total_cost:.2f} cost, {elapsed:.0f}s")

    # Write metadata
    total_extracted = len(list(extract_dir.glob("pr_*.json"))) if extract_dir.exists() else 0
    write_metadata(extract_dir, {
        "step": "extraction",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "prompt_versions": {
            "optimizer_extract": get_prompt_version("optimizer_extract", config),
        },
        "outputs": {
            "total_mrs": total_extracted,
        },
        "cost": {"total_usd": total_cost},
        "duration_seconds": elapsed,
    })


# ---------------------------------------------------------------------------
# Phase B: Consolidation
# ---------------------------------------------------------------------------

CONSOLIDATE_MARKER = "===FINAL_OUTPUT_BEGIN==="


def build_consolidation_prompt(
    current_rules: dict,
    batch_candidates: list[str],
    batch_info: str,
) -> str:
    """Build the user prompt for rule consolidation.

    `current_rules` is the canonical rule-set JSON (see `empty_rules_json`).
    `batch_candidates` is a flat list of rule strings from the current
    batch of MRs.
    """
    parts = []

    parts.append("## Current Rule Set")
    parts.append("")
    parts.append("```json")
    parts.append(json.dumps(current_rules, indent=2))
    parts.append("```")
    parts.append("")

    parts.append(f"## New Candidate Rules ({batch_info})")
    parts.append("")
    for i, candidate in enumerate(batch_candidates):
        parts.append(f"{i + 1}. {candidate}")
    parts.append("")

    parts.append("Return the updated rule set as a single JSON object following the schema in the system prompt.")

    return "\n".join(parts)


def consolidate_rules(
    project_name: str,
    mr_range: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Consolidate extracted candidates into a tuned prompt.

    mr_range: "all" (consolidation_200) or "most_recent_100" (consolidation_100)
    """
    extract_dir = out_dir / project_name / DIRS["optimization"] / "extraction"

    if not extract_dir.exists():
        print(f"  No extraction data. Run extraction first.")
        return

    # Load all extraction outputs
    ext_files = sorted(extract_dir.glob("pr_*.json"))
    if not ext_files:
        print(f"  No extraction outputs found.")
        return

    # Load and sort by merge date
    extractions = []
    for f in ext_files:
        data = load_json(f)
        if data and data.get("candidates"):
            extractions.append(data)

    # Treat missing/null source_mr_date as "" so the sort is total. Seen on ASE
    # MRs !187 and !276, which GitLab returned without a merged_at timestamp.
    extractions.sort(key=lambda e: e.get("source_mr_date") or "")

    # Filter by range
    if mr_range == "most_recent_100":
        extractions = extractions[-100:] if len(extractions) > 100 else extractions
        range_label = "100"
        consolidation_name = "consolidation_100"
    else:
        range_label = "200"
        consolidation_name = "consolidation_200"

    print(f"  Consolidating {len(extractions)} MRs ({consolidation_name})")

    cons_dir = out_dir / project_name / DIRS["optimization"] / consolidation_name
    snapshot_dir = out_dir / project_name / DIRS["optimization"] / "snapshots"

    # Batch by config.optimizer.batch_size
    batch_size = config.optimizer.batch_size
    if trial:
        batch_size = max(len(extractions), 1)  # one batch in trial

    batches = []
    for i in range(0, len(extractions), batch_size):
        batches.append(extractions[i : i + batch_size])

    # Replay backend cannot regenerate consolidation batches (no raw
    # envelopes saved for this step). If any batch is missing or
    # --force is requested, abort cleanly so the shipped snapshot is
    # not partially overwritten.
    if isinstance(backend, ReplayBackend):
        missing = [
            i + 1 for i in range(len(batches))
            if not (cons_dir / f"batch_{i + 1:02d}.json").exists()
        ]
        if force:
            print(
                "  Replay backend: --force is not supported for "
                "consolidation (no raw envelopes saved). Skipping; "
                "remove --force or switch to live mode."
            )
            return
        if missing:
            print(
                f"  Replay backend: {len(missing)} consolidation "
                f"batch(es) missing ({missing[:5]}{'...' if len(missing) > 5 else ''}); "
                f"skipping. The shipped snapshot is preserved."
            )
            return

    system_prompt = get_prompt("optimizer_consolidate", config)
    current_rules: dict = empty_rules_json()
    total_cost = 0.0
    start = time.time()

    def _extract_candidate_strings(ext: dict) -> list[str]:
        """Pull candidate rule strings from an extraction file, tolerating
        both the new flat-string schema and the legacy dict-based schema."""
        out: list[str] = []
        for cand in ext.get("candidates", []):
            if isinstance(cand, str):
                if cand.strip():
                    out.append(cand.strip())
            elif isinstance(cand, dict):
                text = cand.get("text") or cand.get("rule") or ""
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
        return out

    for batch_idx, batch in enumerate(batches):
        batch_num = batch_idx + 1
        batch_file = cons_dir / f"batch_{batch_num:02d}.json"

        # Resume: skip completed batches
        if not force and batch_file.exists():
            existing = load_json(batch_file)
            if existing:
                saved = existing.get("rules_after")
                if isinstance(saved, dict):
                    current_rules = sanitize_rules_json(saved)
                print(f"    Batch {batch_num}: already done, resuming...")
                continue

        # Collect all candidates from this batch
        batch_candidates: list[str] = []
        for ext in batch:
            batch_candidates.extend(_extract_candidate_strings(ext))

        if not batch_candidates:
            batch_data = {
                "batch_number": batch_num,
                "mr_count": len(batch),
                "candidate_count": 0,
                "rules_before": current_rules,
                "rules_after": current_rules,
                "summary": "No candidates in this batch",
                "cost_usd": 0.0,
            }
            atomic_write_json(batch_file, batch_data)
            continue

        # Build date range for context
        dates = [e.get("source_mr_date", "") for e in batch if e.get("source_mr_date")]
        date_range = f"{dates[0][:10]} to {dates[-1][:10]}" if dates else "unknown"

        user_prompt = build_consolidation_prompt(
            current_rules, batch_candidates,
            f"batch {batch_num}/{len(batches)}, {date_range}",
        )

        result = backend.invoke(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=config.models.get("optimizer_consolidate", "claude-opus-4-6"),
            budget_usd=config.budgets.get("optimizer_consolidate", 50.0),
            tools=[],
            output_marker=CONSOLIDATE_MARKER,
            effort_level="high",
            timeout_seconds=900,
        )

        total_cost += result.cost_usd

        batch_data = {
            "batch_number": batch_num,
            "mr_count": len(batch),
            "candidate_count": len(batch_candidates),
            "batch_date_range": date_range,
            "rules_before": current_rules,
            "cost_usd": result.cost_usd,
        }

        if result.parsed_json:
            new_rules = sanitize_rules_json(result.parsed_json)
            if count_rules(new_rules) == 0 and count_rules(current_rules) > 0:
                # Model returned an empty-looking object; likely a schema
                # drift or a parse that caught a sub-object. Keep previous
                # state rather than wiping real rules.
                batch_data["rules_after"] = current_rules
                batch_data["parse_error"] = True
                batch_data["raw_output"] = result.raw_output[:5000]
            else:
                batch_data["rules_after"] = new_rules
                current_rules = new_rules
        else:
            batch_data["rules_after"] = current_rules
            batch_data["parse_error"] = True
            batch_data["raw_output"] = result.raw_output[:5000]

        atomic_write_json(batch_file, batch_data)
        print(
            f"    Batch {batch_num}: {count_rules(current_rules)} rules "
            f"({len(batch_candidates)} candidates)"
        )

        # Save intermediate snapshots at intervals (only for full pass)
        if mr_range == "all" and config.optimizer.snapshot_interval > 0:
            mrs_processed = (batch_idx + 1) * batch_size
            if mrs_processed % config.optimizer.snapshot_interval == 0:
                snap_dir_fp = snapshot_dir / "full_pass"
                snap_dir_fp.mkdir(parents=True, exist_ok=True)
                snap_json = snap_dir_fp / f"snapshot_{mrs_processed:03d}.json"
                snap_md = snap_dir_fp / f"snapshot_{mrs_processed:03d}.md"
                snap_json.write_text(json.dumps(current_rules, indent=2))
                snap_md.write_text(render_rules_markdown(current_rules))

    # Save final snapshot: JSON source of truth + rendered markdown
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    final_json = snapshot_dir / f"tuned_{range_label}.json"
    final_md = snapshot_dir / f"tuned_{range_label}.md"
    final_json.write_text(json.dumps(current_rules, indent=2))
    final_md.write_text(render_rules_markdown(current_rules))
    print(f"  Rules saved: {final_md} ({count_rules(current_rules)} rules)")

    # For full pass, also copy final to snapshot_200 in full_pass/
    if mr_range == "all":
        snap_dir_fp = snapshot_dir / "full_pass"
        snap_dir_fp.mkdir(parents=True, exist_ok=True)
        (snap_dir_fp / f"snapshot_{len(extractions):03d}.json").write_text(
            json.dumps(current_rules, indent=2)
        )
        (snap_dir_fp / f"snapshot_{len(extractions):03d}.md").write_text(
            render_rules_markdown(current_rules)
        )

    elapsed = time.time() - start
    if total_cost == 0.0 and (cons_dir / "metadata.json").exists():
        # Skip-existing replay run: preserve historical cost + provenance.
        return
    write_metadata(cons_dir, {
        "step": "consolidation",
        "project": project_name,
        "consolidation_run": consolidation_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "prompt_versions": {
            "optimizer_consolidate": get_prompt_version("optimizer_consolidate", config),
        },
        "params": {
            "mr_range": mr_range,
            "batch_size": batch_size,
            "source_mr_count": len(extractions),
        },
        "outputs": {
            "batches_processed": len(batches),
            "consolidated_rules": count_rules(current_rules),
            "prompt_file": str(final_md),
            "rules_json": str(final_json),
        },
        "cost": {"total_usd": total_cost},
        "duration_seconds": elapsed,
    })


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def optimize(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    mr_list: list[int] | None = None,
    extract_only: bool = False,
    consolidate_only: bool = False,
    mr_range: str = "both",
    merged_before: str | None = None,
    merged_after: str | None = None,
    trial: bool = False,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Run the full optimization pipeline (extract + consolidate).

    Smart prerequisite checking runs collect/filter/label if needed
    (when called via relo, not relox which handles its own prerequisites).
    """
    print(f"\nOptimizing {project_name}")
    if trial:
        print("  TRIAL MODE")

    # Extract phase
    if not consolidate_only:
        if not yes:
            labeled_dir = out_dir / project_name / DIRS["labeled"]
            mr_count = len(list(labeled_dir.glob("pr_*.json"))) if labeled_dir.exists() else 0
            if mr_count == 0:
                print("  No labeled data found. Run collect/filter/label first.")
                return
            click.echo(f"\n  Ready to extract rules from {mr_count} MRs.")
            if not click.confirm("  Proceed with extraction?", default=True):
                return

        extract_rules(
            project_name, config, backend, out_dir,
            mr_list=mr_list,
            merged_before=merged_before,
            merged_after=merged_after,
            trial=trial, force=force,
        )

    if extract_only:
        return

    # Consolidate phase
    if trial:
        # Trial: single consolidation run
        consolidate_rules(
            project_name, "most_recent_100", config, backend, out_dir,
            trial=trial, force=force,
        )
    elif mr_range == "both":
        # Default: run both
        consolidate_rules(
            project_name, "all", config, backend, out_dir,
            trial=trial, force=force,
        )
        consolidate_rules(
            project_name, "most_recent_100", config, backend, out_dir,
            trial=trial, force=force,
        )
    else:
        consolidate_rules(
            project_name, mr_range, config, backend, out_dir,
            trial=trial, force=force,
        )

    print(f"\nOptimization complete for {project_name}")


def _pr_number_from_path(path: Path) -> int:
    """Extract PR number from a filename like pr_12345.json."""
    match = re.search(r"pr_(\d+)\.json", path.name)
    return int(match.group(1)) if match else 0
