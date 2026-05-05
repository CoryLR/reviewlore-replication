"""Three-stage comment filtering: bot removal, resolution tagging, LLM actionability."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, get_prompt
from .dirs import DIRS
from .state import atomic_write_json, load_json, write_metadata
from reviewlore.backends.base import AgentBackend

# ---------------------------------------------------------------------------
# Stage 1: Bot/structural removal
# ---------------------------------------------------------------------------

# Additional heuristic patterns for bot detection
BOT_SUBSTRINGS = ["bot", "[bot]", "-bot", "_bot"]

# Pre-compiled non-substantive patterns
NON_SUBSTANTIVE_DEFAULTS = [
    re.compile(r"^lgtm\.?$", re.IGNORECASE),
    re.compile(r"^\+1$"),
    re.compile(r"^:\+1:$"),
    re.compile(r"^:thumbsup:$", re.IGNORECASE),
    re.compile(r"^looks good", re.IGNORECASE),
    re.compile(r"^ship it", re.IGNORECASE),
    re.compile(r"^approved\.?$", re.IGNORECASE),
    re.compile(r"^ack\.?$", re.IGNORECASE),
    re.compile(r"^nack\.?$", re.IGNORECASE),
    re.compile(r"^thanks!?\.?$", re.IGNORECASE),
    re.compile(r"^done\.?$", re.IGNORECASE),
    re.compile(r"^fixed\.?$", re.IGNORECASE),
    re.compile(r"^addressed\.?$", re.IGNORECASE),
    re.compile(r"^will do\.?$", re.IGNORECASE),
    re.compile(r"^good catch\.?$", re.IGNORECASE),
    re.compile(r"^nice!?\.?$", re.IGNORECASE),
    re.compile(r"^great\.?$", re.IGNORECASE),
    re.compile(r"^[\U0001f300-\U0001f9ff\s]+$"),  # pure emoji
]


def is_bot_author(username: str, config: Config) -> bool:
    """Check if a username is a known bot."""
    lower = username.lower()
    if lower in [b.lower() for b in config.filter.bot_usernames]:
        return True
    return any(pat in lower for pat in BOT_SUBSTRINGS)


def is_non_substantive(body: str, config: Config) -> bool:
    """Check if comment body matches non-substantive patterns."""
    stripped = body.strip()
    # Check config patterns
    for pattern_str in config.filter.exclude_patterns:
        if re.match(pattern_str, stripped, re.IGNORECASE):
            return True
    # Check default patterns
    for pattern in NON_SUBSTANTIVE_DEFAULTS:
        if pattern.match(stripped):
            return True
    return False


def stage1_filter_thread(
    thread: dict, pr_author: str, config: Config
) -> tuple[dict | None, str | None]:
    """Evaluate a thread's first comment for Stage 1 filtering.

    Returns (thread, None) if kept, or (None, reason) if filtered.
    """
    comments = thread.get("comments", [])
    if not comments:
        return None, "empty"

    first_comment = comments[0]
    author = first_comment.get("author", "")
    body = first_comment.get("body", "").strip()

    if is_bot_author(author, config):
        return None, "bot"

    if author.lower() == pr_author.lower():
        return None, "self_comment"

    word_count = len(body.split())
    if word_count < config.filter.min_word_count:
        return None, "too_short"

    if is_non_substantive(body, config):
        return None, "non_substantive"

    return thread, None


# ---------------------------------------------------------------------------
# Stage 2: Resolution tagging (metadata enrichment, not removal)
# ---------------------------------------------------------------------------

ACKNOWLEDGMENT_PHRASES = [
    "fixed", "done", "addressed", "good catch", "will do",
    "applied", "updated", "resolved", "changed",
]


def stage2_tag_resolution(thread: dict) -> dict:
    """Add resolution metadata to a thread."""
    is_resolved = thread.get("is_resolved")
    is_outdated = thread.get("is_outdated")

    if is_resolved is True:
        signal = "resolved"
    elif is_outdated is True:
        signal = "outdated"
    elif is_resolved is False:
        signal = "unresolved"
    else:
        signal = "unknown"

    # Check replies for acknowledgment phrases
    has_ack = False
    comments = thread.get("comments", [])
    for comment in comments[1:]:  # skip first (the review comment)
        body_lower = comment.get("body", "").lower()
        if any(phrase in body_lower for phrase in ACKNOWLEDGMENT_PHRASES):
            has_ack = True
            break

    thread["resolution_signal"] = signal
    thread["has_acknowledgment"] = has_ack
    return thread


# ---------------------------------------------------------------------------
# Stage 3: LLM actionability filtering
# ---------------------------------------------------------------------------

def stage3_filter_actionability(
    threads: list[dict],
    pr_data: dict,
    config: Config,
    backend: AgentBackend,
) -> tuple[list[dict], float]:
    """Use LLM to classify thread-starting comments as actionable/non-actionable.

    One backend call per MR with all threads. Returns ``(annotated_threads,
    cost_usd)`` so the caller can accumulate per-MR cost into a project-level
    total persisted in the filter step's metadata.
    """
    if not threads:
        return threads, 0.0

    system_prompt = get_prompt("filter_actionability", config)

    # Build user prompt with all thread-starting comments
    comments_for_llm = []
    for i, thread in enumerate(threads):
        first = thread["comments"][0]
        comments_for_llm.append({
            "index": i,
            "author": first.get("author", ""),
            "body": first.get("body", ""),
            "file_path": first.get("file_path"),
            "line_number": first.get("line_number"),
        })

    user_prompt = json.dumps({
        "pr_title": pr_data.get("title", ""),
        "comments": comments_for_llm,
    }, indent=2)

    result = backend.invoke(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=config.filter.actionability.model,
        budget_usd=config.filter.actionability.budget,
        tools=[],  # no tools for classification
        effort_level="low",
        timeout_seconds=300,
    )

    cost_usd = float(getattr(result, "cost_usd", 0.0) or 0.0)

    if result.parsed_json is None:
        # On parse failure, keep all threads (conservative)
        for thread in threads:
            thread["actionable"] = True
            thread["actionability_reasoning"] = "parse_failure"
        return threads, cost_usd

    # Apply classifications
    classifications = result.parsed_json.get("classifications", [])
    actionable_map = {}
    for cls in classifications:
        idx = cls.get("index", -1)
        actionable_map[idx] = {
            "actionable": cls.get("actionable", True),
            "reasoning": cls.get("reasoning", ""),
        }

    for i, thread in enumerate(threads):
        info = actionable_map.get(i, {"actionable": True, "reasoning": "not_classified"})
        thread["actionable"] = info["actionable"]
        thread["actionability_reasoning"] = info["reasoning"]

    return threads, cost_usd


# ---------------------------------------------------------------------------
# Main filter orchestration
# ---------------------------------------------------------------------------

def filter_mr(
    input_path: Path,
    output_path: Path,
    config: Config,
    backend: AgentBackend,
) -> dict:
    """Run all 3 filter stages on a single MR file.

    Returns stats dict.
    """
    pr_data = load_json(input_path)
    if pr_data is None:
        return {"error": f"Could not load {input_path}"}

    pr_author = pr_data.get("author", "")
    threads = pr_data.get("review_threads", [])

    stats = {
        "total_threads": len(threads),
        "stage1_kept": 0,
        "stage1_filtered_bot": 0,
        "stage1_filtered_self_comment": 0,
        "stage1_filtered_too_short": 0,
        "stage1_filtered_non_substantive": 0,
        "stage1_filtered_empty": 0,
        "stage3_filtered_non_actionable": 0,
        "stage3_cost_usd": 0.0,
    }

    # Stage 1
    kept_threads = []
    removed_threads = []

    for thread in threads:
        kept, reason = stage1_filter_thread(thread, pr_author, config)
        if kept:
            kept_threads.append(kept)
            stats["stage1_kept"] += 1
        else:
            removed_threads.append({
                "thread": thread,
                "removal_reason": f"stage1_{reason}",
            })
            key = f"stage1_filtered_{reason}"
            if key in stats:
                stats[key] += 1

    # Stage 2: tag resolution on kept threads
    for thread in kept_threads:
        stage2_tag_resolution(thread)

    # Stage 3: LLM actionability
    if kept_threads:
        kept_threads, stage3_cost = stage3_filter_actionability(
            kept_threads, pr_data, config, backend
        )
        stats["stage3_cost_usd"] = stage3_cost

        # Separate actionable from non-actionable
        final_kept = []
        for thread in kept_threads:
            if thread.get("actionable", True):
                final_kept.append(thread)
            else:
                removed_threads.append({
                    "thread": thread,
                    "removal_reason": "stage3_non_actionable",
                })
                stats["stage3_filtered_non_actionable"] += 1
        kept_threads = final_kept

    # Build output
    output_data = dict(pr_data)
    output_data["review_threads"] = kept_threads
    output_data["removed_threads"] = removed_threads
    output_data["filter_stats"] = stats

    atomic_write_json(output_path, output_data)
    return stats


def filter_project(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Filter all collected MRs for a project."""
    collected_dir = out_dir / project_name / DIRS["collected"]
    filtered_dir = out_dir / project_name / DIRS["filtered"]

    if not collected_dir.exists():
        print(f"No collected data for {project_name}. Run 'relo collect' first.")
        return

    input_files = sorted(collected_dir.glob("pr_*.json"))
    if not input_files:
        print(f"No PR files found in {collected_dir}")
        return

    print(f"Filtering {project_name}: {len(input_files)} MRs")
    if trial:
        print("  TRIAL MODE")

    start = time.time()
    aggregate_stats: dict[str, float] = {}
    processed = 0
    skipped = 0
    total_cost = 0.0

    for input_path in input_files:
        output_path = filtered_dir / input_path.name

        if not force and output_path.exists():
            skipped += 1
            continue

        stats = filter_mr(input_path, output_path, config, backend)
        processed += 1

        # Aggregate stats (including stage3_cost_usd). float tolerance handled below.
        for key, val in stats.items():
            if isinstance(val, (int, float)):
                aggregate_stats[key] = aggregate_stats.get(key, 0) + val

        total_cost += float(stats.get("stage3_cost_usd", 0.0) or 0.0)

        if processed % 25 == 0:
            print(f"  Processed {processed}/{len(input_files) - skipped}...")

    elapsed = time.time() - start
    total_filtered = len(list(filtered_dir.glob("pr_*.json")))

    print(f"  Filtered: {total_filtered} MRs ({processed} new, {skipped} skipped)")
    if aggregate_stats:
        total = aggregate_stats.get("total_threads", 0)
        kept = aggregate_stats.get("stage1_kept", 0) - aggregate_stats.get("stage3_filtered_non_actionable", 0)
        print(f"  Kept threads: {kept} / {total}")
        print(f"    Stage 1: bot={aggregate_stats.get('stage1_filtered_bot', 0)}, "
              f"self={aggregate_stats.get('stage1_filtered_self_comment', 0)}, "
              f"short={aggregate_stats.get('stage1_filtered_too_short', 0)}, "
              f"non-subst={aggregate_stats.get('stage1_filtered_non_substantive', 0)}")
        s3 = aggregate_stats.get("stage3_filtered_non_actionable", 0)
        if s3:
            print(f"    Stage 3: non-actionable={s3}")
    print(f"  Stage 3 cost: ${total_cost:.4f}")

    if processed == 0 and (filtered_dir / "metadata.json").exists():
        # Skip-existing replay run: preserve historical cost + provenance.
        return

    write_metadata(filtered_dir, {
        "step": "filter",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": {"trial": trial},
        "prompt_versions": {
            "filter_actionability": config.prompts["filter_actionability"].version,
        },
        "outputs": {
            "total_mrs": total_filtered,
        },
        "aggregate_stats": aggregate_stats,
        "cost": {"total_usd": total_cost},
        "duration_seconds": elapsed,
    })
