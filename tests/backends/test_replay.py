"""Tests for the replay backend.

Covers: round-trip from saved raw.json, ReplayMissingError on miss, permissive
strict=False, registry presence, envelope-and-marker re-extraction parity with
the live backend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewlore.backends.base import AgentResult, get_backend
import reviewlore.backends.claude_code  # noqa: F401  (registers claude-code)
import reviewlore.backends.replay  # noqa: F401  (registers replay)
from reviewlore.backends.replay import ReplayBackend, ReplayMissingError


REVIEW_MARKER = "===FINAL_OUTPUT_BEGIN==="


def _make_envelope(result_text: str, *, cost: float = 0.07, in_toks: int = 1234, out_toks: int = 567) -> str:
    """Build a Claude Code --output-format json envelope string."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "total_cost_usd": cost,
        "input_tokens": in_toks,
        "output_tokens": out_toks,
    })


def _save_raw(path: Path, raw_output: str, *, cost: float = 0.07, duration: float = 12.5,
              model: str = "claude-sonnet-4-6", in_toks: int = 1234, out_toks: int = 567) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "raw_output": raw_output,
        "cost_usd": cost,
        "duration_seconds": duration,
        "model": model,
        "input_tokens": in_toks,
        "output_tokens": out_toks,
    }, indent=2))


def test_registry_exposes_replay():
    backend = get_backend("replay", {})
    assert isinstance(backend, ReplayBackend)


def test_registry_exposes_replay_via_strict_config_default():
    backend = get_backend("replay", {"strict": True})
    assert isinstance(backend, ReplayBackend)
    assert backend.strict is True


def test_replay_missing_replay_path_raises_when_strict(tmp_path: Path):
    backend = ReplayBackend({"strict": True})
    with pytest.raises(ReplayMissingError):
        backend.invoke(
            system_prompt="sys",
            user_prompt="usr",
            model="claude-sonnet-4-6",
            budget_usd=10.0,
            replay_path=None,
        )


def test_replay_missing_replay_path_returns_empty_when_permissive(tmp_path: Path):
    backend = ReplayBackend({"strict": False})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-sonnet-4-6",
        budget_usd=10.0,
        replay_path=None,
    )
    assert isinstance(result, AgentResult)
    assert result.parsed_json is None
    assert result.raw_output == ""
    assert result.model == "claude-sonnet-4-6"


def test_replay_missing_file_raises_when_strict(tmp_path: Path):
    backend = ReplayBackend({"strict": True})
    with pytest.raises(ReplayMissingError) as excinfo:
        backend.invoke(
            system_prompt="sys",
            user_prompt="usr",
            model="claude-sonnet-4-6",
            budget_usd=10.0,
            replay_path=tmp_path / "nope.json",
        )
    assert "nope.json" in str(excinfo.value)


def test_replay_unreadable_file_raises_when_strict(tmp_path: Path):
    raw_path = tmp_path / "raw.json"
    raw_path.write_text("{this is not json")
    backend = ReplayBackend({"strict": True})
    with pytest.raises(ReplayMissingError) as excinfo:
        backend.invoke(
            system_prompt="sys",
            user_prompt="usr",
            model="claude-sonnet-4-6",
            budget_usd=10.0,
            replay_path=raw_path,
        )
    assert "unreadable" in str(excinfo.value)


def test_replay_roundtrips_envelope_and_extracts_json(tmp_path: Path):
    """The replay backend must reproduce a parsed_json identical to live mode.

    Live mode parses the --output-format json envelope, pulls `result_text`,
    then runs extract_json_from_text on it. Replay must do the same.
    """
    inner_payload = {
        "verdicts": [
            {"human_comment_index": 0, "verdict": "match", "matched_ai_comment_index": 1},
            {"human_comment_index": 1, "verdict": "no_match", "matched_ai_comment_index": None},
        ]
    }
    result_text = (
        f"Reasoning preface...\n\n{REVIEW_MARKER}\n```json\n"
        f"{json.dumps(inner_payload)}\n```\n"
    )
    envelope = _make_envelope(result_text, cost=0.42, in_toks=4321, out_toks=765)

    raw_path = tmp_path / "raw.json"
    _save_raw(raw_path, envelope, cost=0.42, in_toks=4321, out_toks=765)

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-opus-4-6",
        budget_usd=50.0,
        output_marker=REVIEW_MARKER,
        replay_path=raw_path,
    )

    assert result.raw_output == envelope
    assert result.cost_usd == 0.42
    assert result.input_tokens == 4321
    assert result.output_tokens == 765
    # Parsed JSON should match the inner payload
    assert result.parsed_json == inner_payload


def test_replay_handles_inline_envelope_review_payload(tmp_path: Path):
    """Reviewer-shape payload (inline_comments / general_comments) round-trips."""
    inner = {
        "inline_comments": [
            {"file_path": "src/a.py", "line_number": 12, "comment": "something"},
        ],
        "general_comments": [],
    }
    result_text = f"prelude\n```json\n{json.dumps(inner)}\n```\n"
    envelope = _make_envelope(result_text)

    raw_path = tmp_path / "raw.json"
    _save_raw(raw_path, envelope)

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-sonnet-4-6",
        budget_usd=100.0,
        replay_path=raw_path,
    )

    assert result.parsed_json == inner


def test_replay_falls_back_when_envelope_is_unparseable(tmp_path: Path):
    """Pre-envelope raw_output (legacy bare result_text) still parses.

    If raw_output is not a valid JSON envelope, claude_code's live path falls
    back to using raw_output as result_text directly. Replay should mirror that.
    """
    inner = {"inline_comments": [], "general_comments": []}
    raw_output = f"```json\n{json.dumps(inner)}\n```"

    raw_path = tmp_path / "raw.json"
    _save_raw(raw_path, raw_output)

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-sonnet-4-6",
        budget_usd=100.0,
        replay_path=raw_path,
    )
    assert result.parsed_json == inner


def test_replay_returns_none_parsed_when_payload_invalid(tmp_path: Path):
    """If raw_output doesn't contain a parseable JSON, parsed_json is None."""
    envelope = _make_envelope("just prose, no json blocks here")
    raw_path = tmp_path / "raw.json"
    _save_raw(raw_path, envelope)

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-sonnet-4-6",
        budget_usd=100.0,
        replay_path=raw_path,
    )
    assert result.raw_output == envelope
    assert result.parsed_json is None
    # Cost / token metadata still passes through from saved raw.json
    assert result.cost_usd == 0.07


def test_replay_uses_saved_model_field(tmp_path: Path):
    """If saved raw.json records a model, the replay result uses it (not the
    caller's model arg). Falls back to the caller's arg if missing.
    """
    raw_path = tmp_path / "raw.json"
    _save_raw(raw_path, _make_envelope("nope"), model="claude-saved-model")

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-arg-model",
        budget_usd=100.0,
        replay_path=raw_path,
    )
    assert result.model == "claude-saved-model"


def test_replay_falls_back_to_caller_model_when_saved_blank(tmp_path: Path):
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(json.dumps({
        "raw_output": _make_envelope("nope"),
        "cost_usd": 0.0,
        "duration_seconds": 0.0,
        "model": "",
    }))

    backend = ReplayBackend({"strict": True})
    result = backend.invoke(
        system_prompt="sys",
        user_prompt="usr",
        model="claude-arg-model",
        budget_usd=100.0,
        replay_path=raw_path,
    )
    assert result.model == "claude-arg-model"
