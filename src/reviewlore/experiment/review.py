"""Reviewer agent logic: prompt assembly and invocation."""

from __future__ import annotations

from pathlib import Path

from reviewlore.tuner.config import Config, get_prompt
from reviewlore.tuner.state import atomic_write_json
from reviewlore.backends.base import AgentBackend

MAX_ATTEMPTS = 3
REVIEW_MARKER = "===FINAL_OUTPUT_BEGIN==="


def assemble_reviewer_prompt(
    condition: str,
    config: Config,
    snapshot_dir: Path,
) -> str:
    """Assemble the system prompt for a reviewer condition.

    For 'generic': base prompt with '(none)' left in place at the rules
    placeholder. For 'tuned_N': the base prompt's
    '## Project-Specific Rules' section is populated with the rendered
    2-tier markdown (dimension header + flat rule bullets) from the
    matching snapshot.
    """
    base_prompt = get_prompt("reviewer_generic", config)

    if condition == "generic":
        return base_prompt

    snapshot_path = snapshot_dir / f"{condition}.md"
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Snapshot not found: {snapshot_path}. Run 'relox optimize' first."
        )

    rules_text = snapshot_path.read_text().strip()
    if not rules_text or rules_text == "(none)":
        return base_prompt

    marker = "## Project-Specific Rules"
    preface = (
        "These rules supplement general review guidelines. "
        "Do not limit your review scope to only these rules."
    )

    if marker not in base_prompt:
        return f"{base_prompt}\n\n{marker}\n\n{preface}\n\n{rules_text}\n"

    # Replace the section starting at `marker` and continuing until the
    # next top-level section (or end of file) with the rendered rules.
    head, _, tail = base_prompt.partition(marker)
    # Drop the old body (everything up to the next "## " or end)
    rest_lines = tail.split("\n")
    # Skip the marker line itself (empty string from partition tail[0])
    idx = 1
    while idx < len(rest_lines):
        if rest_lines[idx].startswith("## "):
            break
        idx += 1
    trailing = "\n".join(rest_lines[idx:])

    assembled = (
        f"{head}{marker}\n\n{preface}\n\n{rules_text}\n"
    )
    if trailing.strip():
        assembled += f"\n{trailing}"
    return assembled


def build_review_user_prompt(context: dict) -> str:
    """Build the user prompt from context data."""
    parts = []

    title = context.get("title", "Untitled")
    description = context.get("description", "")
    diff = context.get("diff", "")

    parts.append(f"Review the following merge request diff.")
    parts.append("")
    parts.append(f"## PR Title: {title}")
    parts.append("")

    if description:
        parts.append("## PR Description")
        parts.append(description[:3000])
        parts.append("")

    parts.append("The project codebase is available via your file tools for browsing context.")
    parts.append("")
    parts.append("## Diff")
    parts.append("```diff")
    parts.append(diff)
    parts.append("```")

    return "\n".join(parts)


def run_review(
    context: dict,
    condition: str,
    system_prompt: str,
    config: Config,
    backend: AgentBackend,
    worktree_path: Path,
    output_dir: Path,
    trial: bool = False,
) -> dict | None:
    """Run the reviewer agent for a single condition on a single revision.

    Returns parsed review dict or None on failure.
    Saves review.json and raw.json to output_dir.
    """
    user_prompt = build_review_user_prompt(context)

    # Save the system prompt used for this review
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "system_prompt.md").write_text(system_prompt)

    # Trial mode: disable tools for speed (worktree still created, diff in prompt)
    review_tools = [] if trial else ["read", "grep", "glob", "bash"]
    review_repos = [] if trial else [worktree_path]

    raw_path = output_dir / "raw.json"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = backend.invoke(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=config.models.get("reviewer", "claude-sonnet-4-6[1m]"),
            budget_usd=config.budgets.get("reviewer", 100.0),
            tools=review_tools,
            repo_paths=review_repos,
            output_marker=REVIEW_MARKER,
            effort_level="high",
            timeout_seconds=3600,
            replay_path=raw_path,
        )

        # Always save raw output
        raw_data = {
            "condition": condition,
            "attempt": attempt,
            "raw_output": result.raw_output[:10_000_000],
            "cost_usd": result.cost_usd,
            "duration_seconds": result.duration_seconds,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(raw_path, raw_data)

        if result.parsed_json:
            review = result.parsed_json
            review["cost_usd"] = result.cost_usd
            review["duration_seconds"] = result.duration_seconds
            atomic_write_json(output_dir / "review.json", review)
            return review

        if attempt < MAX_ATTEMPTS:
            print(f"      Parse failed (attempt {attempt}), retrying...")

    print(f"      Review parse failed after {MAX_ATTEMPTS} attempts")
    return None
