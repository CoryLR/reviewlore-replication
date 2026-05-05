"""Tests for the token-aggregation half of evaluate.py.

The CP4/CP5 ClaudeCodeBackend persisted ``input_tokens`` / ``output_tokens``
at the saved-raw.json top level by reading them off the envelope's top level —
but Claude Code's ``--output-format json`` envelope nests usage under
``envelope["usage"]["input_tokens"]`` (and friends), so the top-level fields
are always 0 in practice. CP6's ``aggregate_tokens`` re-derives correct
counts by re-parsing the saved envelope; this test pins that fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

from reviewlore.experiment.evaluate import (
    _extract_tokens_from_raw,
    aggregate_tokens,
)
from reviewlore.tuner.dirs import DIRS


def _make_envelope_raw(usage: dict, *, total_cost: float = 0.5) -> dict:
    return {
        "raw_output": json.dumps({
            "type": "result",
            "result": "irrelevant",
            "total_cost_usd": total_cost,
            "usage": usage,
        }),
        "input_tokens": 0,  # mirrors the buggy CP4/CP5 top-level zeros
        "output_tokens": 0,
        "cost_usd": total_cost,
        "duration_seconds": 12.0,
        "model": "claude-sonnet-4-6",
    }


def test_extract_recovers_usage_from_envelope():
    raw = _make_envelope_raw({
        "input_tokens": 7,
        "cache_creation_input_tokens": 11_449,
        "cache_read_input_tokens": 91_949,
        "output_tokens": 7_778,
    })
    t = _extract_tokens_from_raw(raw)
    assert t["input_uncached"] == 7
    assert t["cache_creation"] == 11_449
    assert t["cache_read"] == 91_949
    # Billable input total is the sum of all three components
    assert t["input_tokens"] == 7 + 11_449 + 91_949
    assert t["output_tokens"] == 7_778


def test_extract_falls_back_to_top_level_when_envelope_unparseable():
    raw = {
        "raw_output": "not json at all",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    t = _extract_tokens_from_raw(raw)
    assert t["input_tokens"] == 100
    assert t["output_tokens"] == 50
    # No usage block to derive cache breakdown
    assert t["cache_read"] == 0
    assert t["cache_creation"] == 0
    assert t["input_uncached"] == 100


def test_extract_falls_back_when_envelope_missing_usage():
    raw = {
        "raw_output": json.dumps({"result": "ok", "total_cost_usd": 0.1}),
        "input_tokens": 99,
        "output_tokens": 11,
    }
    t = _extract_tokens_from_raw(raw)
    assert t["input_tokens"] == 99
    assert t["output_tokens"] == 11


def test_extract_handles_missing_usage_subkeys():
    """A partial usage block shouldn't crash; missing fields default to 0."""
    raw = _make_envelope_raw({"input_tokens": 5, "output_tokens": 9})
    t = _extract_tokens_from_raw(raw)
    assert t["input_uncached"] == 5
    assert t["cache_creation"] == 0
    assert t["cache_read"] == 0
    assert t["input_tokens"] == 5
    assert t["output_tokens"] == 9


def test_aggregate_tokens_walks_review_and_judge(tmp_path: Path):
    """aggregate_tokens sums per-revision raw.json across both step dirs."""
    data_dir = tmp_path / "data"
    review_root = data_dir / DIRS["reviews"]
    judge_root = data_dir / DIRS["judgments"]

    def _write(step_root: Path, condition: str, pr: int, rev: str, usage: dict):
        target = step_root / condition / f"pr_{pr}" / f"rev_{rev}" / "raw.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_make_envelope_raw(usage)))

    # Two review calls and two judge calls under "generic"
    _write(review_root, "generic", 1, "abcdef01", {
        "input_tokens": 10, "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30, "output_tokens": 5,
    })
    _write(review_root, "generic", 2, "abcdef02", {
        "input_tokens": 100, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 300, "output_tokens": 50,
    })
    _write(judge_root, "generic", 1, "abcdef01", {
        "input_tokens": 1, "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3, "output_tokens": 4,
    })
    _write(judge_root, "generic", 2, "abcdef02", {
        "input_tokens": 11, "cache_creation_input_tokens": 22,
        "cache_read_input_tokens": 33, "output_tokens": 44,
    })
    # An unrelated stale "generic_apr12/" sibling that should be excluded by
    # the conditions filter
    _write(review_root, "generic_apr12", 99, "deadbeef", {
        "input_tokens": 99_999, "cache_creation_input_tokens": 99_999,
        "cache_read_input_tokens": 99_999, "output_tokens": 99_999,
    })

    out = aggregate_tokens(data_dir, conditions=["generic"])

    # Review step: (10+20+30) + (100+200+300) = 60 + 600 = 660 input billable
    assert out["review"]["input_tokens"] == 660
    assert out["review"]["output_tokens"] == 55
    assert out["review"]["n_calls"] == 2
    # Judge step: (1+2+3) + (11+22+33) = 6 + 66 = 72 input billable
    assert out["judge"]["input_tokens"] == 72
    assert out["judge"]["output_tokens"] == 48
    assert out["judge"]["n_calls"] == 2
    # Grand total
    assert out["grand_total"]["input_tokens"] == 660 + 72
    assert out["grand_total"]["output_tokens"] == 55 + 48
    assert out["grand_total"]["n_calls"] == 4
    # Stale apr12 sibling must be excluded (would have added 100K+ tokens)
    assert out["grand_total"]["input_tokens"] < 1000


def test_aggregate_tokens_returns_zeros_when_no_data(tmp_path: Path):
    out = aggregate_tokens(tmp_path / "empty", conditions=["generic"])
    # No step dirs exist, so review/judge entries aren't populated. The grand
    # total still emits a zeroed bucket so callers can rely on the key being
    # present.
    assert "review" not in out
    assert "judge" not in out
    assert out["grand_total"]["input_tokens"] == 0
    assert out["grand_total"]["output_tokens"] == 0
    assert out["grand_total"]["n_calls"] == 0
