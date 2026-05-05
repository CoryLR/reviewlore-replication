"""Tests for judge validation/normalization and index-aware comment formatting.

Phase 1.7 of cp6-judge-fix-2-plan.md.
"""
from __future__ import annotations

import pytest

from reviewlore.experiment import judge as judge_mod


def _human(idx: int, file_path: str | None = "src/x.py", line: int | None = 10) -> dict:
    return {"file_path": file_path, "line_number": line, "body": f"human {idx}"}


def _good_verdict(i: int, verdict: str = "match", mai: int | None = 0) -> dict:
    return {
        "human_comment_index": i,
        "human_file": f"src/file_{i}.py",
        "human_line": i * 10,
        "human_summary": f"summary {i}",
        "verdict": verdict,
        "matched_ai_comment_index": mai if verdict == "match" else None,
        "reasoning": f"reasoning {i}",
    }


def _review_with(n_inline: int = 2, n_general: int = 1) -> dict:
    return {
        "inline_comments": [
            {"index": i, "file_path": "src/a.py", "line_number": i + 1, "comment": f"ai inline {i}"}
            for i in range(n_inline)
        ],
        "general_comments": [
            {"index": n_inline + j, "comment": f"ai general {j}", "dimension": "Code Style"}
            for j in range(n_general)
        ],
    }


# ---------------------------------------------------------------------------
# _validate_and_normalize_verdicts
# ---------------------------------------------------------------------------


def test_validate_drops_top_level_summary():
    parsed = {
        "verdicts": [_good_verdict(0)],
        "summary": {"total_human_comments": 1, "matches": 1, "no_matches": 0},
    }
    out = judge_mod._validate_and_normalize_verdicts(parsed, [_human(0)], _review_with(2, 1))
    assert "summary" not in out
    assert len(out["verdicts"]) == 1


def test_validate_canonicalizes_human_file_and_line_from_input():
    parsed = {"verdicts": [_good_verdict(0)]}
    # Judge wrote bogus human_file/line; validator must canonicalize from input.
    parsed["verdicts"][0]["human_file"] = "WRONG.py"
    parsed["verdicts"][0]["human_line"] = 999
    out = judge_mod._validate_and_normalize_verdicts(
        parsed, [_human(0, file_path="src/correct.py", line=42)], _review_with(2, 1)
    )
    assert out["verdicts"][0]["human_file"] == "src/correct.py"
    assert out["verdicts"][0]["human_line"] == 42


def test_validate_keeps_per_verdict_human_summary_and_reasoning():
    parsed = {"verdicts": [_good_verdict(0)]}
    out = judge_mod._validate_and_normalize_verdicts(parsed, [_human(0)], _review_with(2, 1))
    assert out["verdicts"][0]["human_summary"] == "summary 0"
    assert out["verdicts"][0]["reasoning"] == "reasoning 0"


def test_validate_rejects_count_mismatch():
    parsed = {"verdicts": [_good_verdict(0)]}
    with pytest.raises(ValueError, match="length"):
        judge_mod._validate_and_normalize_verdicts(
            parsed, [_human(0), _human(1)], _review_with(2, 1)
        )


def test_validate_rejects_out_of_order_human_comment_index():
    parsed = {"verdicts": [_good_verdict(0), _good_verdict(0)]}  # both index 0
    with pytest.raises(ValueError, match="human_comment_index"):
        judge_mod._validate_and_normalize_verdicts(
            parsed, [_human(0), _human(1)], _review_with(2, 1)
        )


def test_validate_rejects_bad_verdict_label():
    bad = _good_verdict(0)
    bad["verdict"] = "partial"
    with pytest.raises(ValueError, match="invalid verdict label"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_rejects_match_with_invalid_matched_ai_comment_index():
    bad = _good_verdict(0, verdict="match", mai=999)
    with pytest.raises(ValueError, match="out of range"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_rejects_match_with_null_matched_ai_comment_index():
    bad = _good_verdict(0, verdict="match")
    bad["matched_ai_comment_index"] = None
    with pytest.raises(ValueError, match="must be int"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_rejects_no_match_with_non_null_matched_ai_comment_index():
    bad = _good_verdict(0, verdict="no_match")
    bad["matched_ai_comment_index"] = 0
    with pytest.raises(ValueError, match="must be null"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_rejects_missing_human_summary():
    bad = _good_verdict(0)
    del bad["human_summary"]
    with pytest.raises(ValueError, match="human_summary"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_rejects_empty_reasoning():
    bad = _good_verdict(0)
    bad["reasoning"] = "   "
    with pytest.raises(ValueError, match="reasoning"):
        judge_mod._validate_and_normalize_verdicts({"verdicts": [bad]}, [_human(0)], _review_with(2, 1))


def test_validate_accepts_many_to_one_coverage_match():
    """Two human comments matched to the same AI comment is legitimate
    under coverage-based matching (2026-05-04 judge-fix-2)."""
    parsed = {"verdicts": [
        _good_verdict(0, verdict="match", mai=0),
        _good_verdict(1, verdict="match", mai=0),  # same AI comment covers both
    ]}
    out = judge_mod._validate_and_normalize_verdicts(
        parsed, [_human(0), _human(1)], _review_with(2, 1)
    )
    assert out["verdicts"][0]["matched_ai_comment_index"] == 0
    assert out["verdicts"][1]["matched_ai_comment_index"] == 0


# ---------------------------------------------------------------------------
# format_comments_for_judge index handling
# ---------------------------------------------------------------------------


def test_format_uses_explicit_index_when_present():
    review = {
        "inline_comments": [
            {"index": 0, "file_path": "x.py", "line_number": 1, "comment": "first"},
            {"index": 1, "file_path": "x.py", "line_number": 2, "comment": "second"},
        ],
        "general_comments": [
            {"index": 2, "comment": "third"},
        ],
    }
    text = judge_mod.format_comments_for_judge([_human(0)], review)
    assert "**AI Comment 0**" in text
    assert "**AI Comment 1**" in text
    assert "**AI Comment 2**" in text


def test_format_falls_back_to_positional_when_index_missing():
    review = {
        "inline_comments": [
            {"file_path": "x.py", "line_number": 1, "comment": "first"},
            {"file_path": "x.py", "line_number": 2, "comment": "second"},
        ],
        "general_comments": [
            {"comment": "third"},
        ],
    }
    text = judge_mod.format_comments_for_judge([_human(0)], review)
    assert "**AI Comment 0**" in text
    assert "**AI Comment 1**" in text
    # general fallback uses len(inline) + i = 2
    assert "**AI Comment 2**" in text
