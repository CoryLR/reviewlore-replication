"""Agent backend abstraction: protocol, result type, JSON extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class AgentResult:
    """Result from an agent backend invocation."""

    raw_output: str
    parsed_json: dict | None = None
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class AgentBackend(Protocol):
    """Interface for headless LLM tool runners.

    ``replay_path`` is a per-call optional pointer to a previously-saved
    ``raw.json`` for this invocation. Live backends (claude_code) ignore it;
    the replay backend uses it to look up the saved output. Callers that wish
    to support the replay backend should pass the same path they intend to
    write the new ``raw.json`` to (the existing pattern of saving raw output
    after every call makes this trivial).
    """

    def invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        budget_usd: float,
        tools: list[str] | None = None,
        repo_paths: list[Path] | None = None,
        output_marker: str | None = None,
        effort_level: str = "high",
        timeout_seconds: int = 3600,
        replay_path: Path | None = None,
    ) -> AgentResult: ...


def extract_json_from_text(
    text: str, output_marker: str | None = None
) -> dict | None:
    """Extract structured JSON from agent output text.

    Three-strategy extraction (ported from PoC2 4_review.py):
    1. Find output_marker line, parse fenced JSON block after it.
    2. Fallback: find the last fenced ```json block.
    3. Fallback: find the last '{' and try to parse raw JSON.

    Returns parsed dict or None if all strategies fail.
    """
    if not text:
        return None

    # Strategy 1: output marker delimited
    if output_marker and output_marker in text:
        marker_pos = text.index(output_marker)
        after_marker = text[marker_pos + len(output_marker) :]
        fenced = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```", after_marker, re.DOTALL
        )
        if fenced:
            try:
                return json.loads(fenced.group(1).strip())
            except json.JSONDecodeError:
                pass

    # Strategy 2: last fenced json block
    fenced_blocks = re.findall(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL
    )
    for block in reversed(fenced_blocks):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Strategy 3: raw JSON via json.JSONDecoder().raw_decode, which respects
    # JSON string literals (unlike naive brace counting). Scan each `{` as a
    # potential start; the first one that yields a dict whose keys include a
    # known envelope key (inline_comments, general_comments, verdicts) wins.
    decoder = json.JSONDecoder()
    envelope_keys = ("inline_comments", "general_comments", "verdicts")
    for start in range(len(text)):
        if text[start] != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and any(k in parsed for k in envelope_keys):
            return parsed

    # Strategy 4: scavenger fallback. Used when the outer envelope is
    # unparseable due to invalid JSON escapes (e.g. `\d` regex metachars,
    # literal `\` + backtick) or unescaped quotes in comment strings — common
    # failure modes when the model's review comments contain code examples.
    # Collect every parseable sub-dict and classify as inline comment, general
    # comment, or judge verdict based on key shape, then reconstruct a
    # synthetic envelope. Partial recovery (some comments lost) beats total
    # parser collapse.
    inline_comments: list[dict] = []
    general_comments: list[dict] = []
    verdicts: list[dict] = []
    for start in range(len(text)):
        if text[start] != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if "verdict" in parsed and "human_comment_index" in parsed:
            verdicts.append(parsed)
        elif "file_path" in parsed and "line_number" in parsed:
            inline_comments.append(parsed)
        elif "dimension" in parsed and "comment" in parsed and "file_path" not in parsed:
            general_comments.append(parsed)

    if verdicts:
        return {"verdicts": verdicts}
    if inline_comments or general_comments:
        return {"inline_comments": inline_comments, "general_comments": general_comments}

    return None


# Backend registry: maps config name to backend class
BACKENDS: dict[str, type] = {}


def get_backend(name: str, config: dict | None = None) -> AgentBackend:
    """Instantiate a backend by name from the registry."""
    if name not in BACKENDS:
        available = ", ".join(BACKENDS.keys()) or "(none registered)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return BACKENDS[name](config or {})


def register_backend(name: str, cls: type) -> None:
    """Register a backend class in the registry."""
    BACKENDS[name] = cls
