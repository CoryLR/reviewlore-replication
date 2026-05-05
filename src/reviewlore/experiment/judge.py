"""Judge agent logic: comment formatting and invocation."""

from __future__ import annotations

from pathlib import Path

from reviewlore.tuner.config import Config, get_prompt
from reviewlore.tuner.state import atomic_write_json
from reviewlore.backends.base import AgentBackend

MAX_ATTEMPTS = 3
JUDGE_MARKER = "===FINAL_OUTPUT_BEGIN==="


def _validate_and_normalize_verdicts(
    parsed: dict,
    human_comments: list[dict],
    review: dict,
) -> dict:
    """Validate and normalize judge output before write.

    Drops accidental top-level `summary` block; verifies `verdicts[]` length
    and ordering; verifies `verdict` labels and `matched_ai_comment_index`
    range; canonicalizes `human_file` and `human_line` from input
    `human_comments`; requires `human_summary` and `reasoning` strings.

    Raises ValueError on any violation so the existing retry loop fires.
    """
    if not isinstance(parsed, dict):
        raise ValueError("parsed output is not a dict")

    parsed.pop("summary", None)

    verdicts = parsed.get("verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("verdicts is not a list")
    if len(verdicts) != len(human_comments):
        raise ValueError(
            f"verdicts length {len(verdicts)} != human_comments length {len(human_comments)}"
        )

    n_ai = len(review.get("inline_comments") or []) + len(review.get("general_comments") or [])

    for i, v in enumerate(verdicts):
        if not isinstance(v, dict):
            raise ValueError(f"verdict {i} is not a dict")
        idx = v.get("human_comment_index")
        if idx != i:
            raise ValueError(
                f"verdict {i} has human_comment_index={idx}, expected {i} (must be 0..n-1 in order)"
            )
        verdict_label = v.get("verdict")
        if verdict_label not in ("match", "no_match"):
            raise ValueError(f"verdict {i} has invalid verdict label: {verdict_label!r}")

        mai = v.get("matched_ai_comment_index")
        if verdict_label == "match":
            if not isinstance(mai, int):
                raise ValueError(f"verdict {i} match: matched_ai_comment_index must be int, got {mai!r}")
            if not (0 <= mai < n_ai):
                raise ValueError(
                    f"verdict {i} matched_ai_comment_index out of range: {mai} not in [0, {n_ai})"
                )
        else:
            if mai is not None:
                raise ValueError(
                    f"verdict {i} no_match: matched_ai_comment_index must be null, got {mai!r}"
                )

        # Canonicalize human_file and human_line from input
        src = human_comments[i]
        v["human_file"] = src.get("file_path")
        v["human_line"] = src.get("line_number")

        if not isinstance(v.get("human_summary"), str):
            raise ValueError(f"verdict {i} human_summary must be a string")
        if not isinstance(v.get("reasoning"), str) or not v["reasoning"].strip():
            raise ValueError(f"verdict {i} reasoning must be a non-empty string")

    return parsed


def format_comments_for_judge(
    human_comments: list[dict],
    review: dict,
) -> str:
    """Format human and AI comments for the judge prompt.

    Builds a markdown-formatted user prompt with numbered comments
    for index-based reference in verdicts.
    """
    parts = []

    # Human comments section
    parts.append("## Human Reviewer Comments")
    parts.append("")

    for i, comment in enumerate(human_comments):
        file_path = comment.get("file_path", "")
        line = comment.get("line_number", "")
        body = comment.get("body", "")
        author = comment.get("author", "")
        is_resolved = comment.get("is_resolved", None)
        category = comment.get("category", "")

        location = ""
        if file_path:
            location = f" [{file_path}"
            if line:
                location += f":{line}"
            location += "]"

        resolved_str = ""
        if is_resolved is True:
            resolved_str = " (resolved)"
        elif is_resolved is False:
            resolved_str = " (unresolved)"

        parts.append(f"### Human Comment {i}{location}{resolved_str}")
        if category:
            parts.append(f"Category: {category}")
        if author:
            parts.append(f"Author: {author}")
        parts.append(body)
        parts.append("")

    # AI comments section
    parts.append("## AI Reviewer Comments")
    parts.append("")

    inline = review.get("inline_comments", [])
    general = review.get("general_comments", [])

    if inline:
        parts.append("### Inline Comments")
        for i, comment in enumerate(inline):
            file_path = comment.get("file_path", "")
            line = comment.get("line_number", "")
            body = comment.get("comment", "")
            dimension = comment.get("dimension", "")
            importance = comment.get("importance", "")

            location = f"[{file_path}:{line}]" if file_path else ""
            ai_index = comment.get("index", i)
            parts.append(f"**AI Comment {ai_index}** {location}")
            if dimension:
                parts.append(f"Dimension: {dimension}")
            if importance:
                parts.append(f"Importance: {importance}")
            parts.append(body)
            parts.append("")

    if general:
        parts.append("### General Comments")
        offset = len(inline)
        for i, comment in enumerate(general):
            body = comment.get("comment", "")
            dimension = comment.get("dimension", "")
            importance = comment.get("importance", "")

            ai_index = comment.get("index", offset + i)
            parts.append(f"**AI Comment {ai_index}** (general)")
            if dimension:
                parts.append(f"Dimension: {dimension}")
            if importance:
                parts.append(f"Importance: {importance}")
            parts.append(body)
            parts.append("")

    if not inline and not general:
        parts.append("(No AI comments were generated)")
        parts.append("")

    return "\n".join(parts)


def run_judge(
    human_comments: list[dict],
    review: dict,
    config: Config,
    backend: AgentBackend,
    worktree_path: Path,
    output_dir: Path,
    trial: bool = False,
) -> dict | None:
    """Run the judge agent to evaluate reviewer output against human comments.

    Returns parsed verdicts dict or None on failure.
    """
    system_prompt = get_prompt("judge", config)
    user_prompt = format_comments_for_judge(human_comments, review)

    # Trial mode: disable tools for speed
    judge_tools = [] if trial else ["read", "grep", "glob", "bash"]
    judge_repos = [] if trial else [worktree_path]

    raw_path = output_dir / "raw.json"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = backend.invoke(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=config.models.get("judge", "claude-opus-4-6[1m]"),
            budget_usd=config.budgets.get("judge", 50.0),
            tools=judge_tools,
            repo_paths=judge_repos,
            output_marker=JUDGE_MARKER,
            effort_level="high",
            timeout_seconds=3600,
            replay_path=raw_path,
        )

        # Always save raw output
        raw_data = {
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
            try:
                verdicts = _validate_and_normalize_verdicts(
                    result.parsed_json, human_comments, review
                )
            except ValueError as e:
                print(f"      Judge validation failed (attempt {attempt}): {e}")
                if attempt < MAX_ATTEMPTS:
                    print(f"      Retrying...")
                    continue
                print(f"      Judge validation failed after {MAX_ATTEMPTS} attempts")
                return None
            verdicts["cost_usd"] = result.cost_usd
            verdicts["duration_seconds"] = result.duration_seconds
            atomic_write_json(output_dir / "verdicts.json", verdicts)
            return verdicts

        if attempt < MAX_ATTEMPTS:
            print(f"      Judge parse failed (attempt {attempt}), retrying...")

    print(f"      Judge parse failed after {MAX_ATTEMPTS} attempts")
    return None
