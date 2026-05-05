"""Regression tests for the GitHub review-comment normalization in collect.py.

These tests pin the contract that lets the judge prompt show correct line
numbers for comments on outdated review threads. GitHub returns ``line: null``
for those, with the original anchor preserved in ``originalLine``. The query
must request both fields, and the normalizer must fall back from ``line`` to
``originalLine`` when ``line`` is None.

If a future schema sweep silently drops ``originalLine`` from the GraphQL
query, ``test_query_includes_original_line`` fails. If a future refactor
changes the fallback semantics, ``test_normalize_falls_back_*`` fails.
"""
from reviewlore.tuner.collect import QUERY_PR_DETAILS, _normalize_github_comment


def _comment_node(**overrides):
    base = {
        "id": "RTC_kwDO000",
        "author": {"login": "alice"},
        "body": "comment body",
        "path": "src/foo.ts",
        "line": 42,
        "originalLine": 40,
        "startLine": None,
        "outdated": False,
        "createdAt": "2026-05-01T00:00:00Z",
        "commit": {"oid": "deadbeef"},
    }
    base.update(overrides)
    return base


def test_query_includes_original_line():
    """The GraphQL query must request originalLine for the outdated-thread fallback."""
    assert "originalLine" in QUERY_PR_DETAILS, (
        "QUERY_PR_DETAILS must request originalLine on PullRequestReviewComment "
        "or outdated-thread comments lose their anchor (line: null) during "
        "normalization. See cp6-judge-fix-plan.md for the bug history."
    )


def test_normalize_uses_line_when_present():
    out = _normalize_github_comment(_comment_node(line=42, originalLine=40))
    assert out["line_number"] == 42, (
        "When `line` is non-null, it must take precedence over `originalLine`."
    )


def test_normalize_falls_back_to_original_line():
    """The exact bug fix: line=null on outdated threads must use originalLine."""
    out = _normalize_github_comment(_comment_node(line=None, originalLine=405))
    assert out["line_number"] == 405, (
        "When `line` is null (typical for outdated threads) the normalizer "
        "must fall back to `originalLine`."
    )


def test_normalize_returns_none_when_both_missing():
    out = _normalize_github_comment(_comment_node(line=None, originalLine=None))
    assert out["line_number"] is None


def test_normalize_handles_missing_optional_fields():
    """A minimal comment node should normalize without raising."""
    out = _normalize_github_comment({
        "id": "x",
        "body": "b",
        "createdAt": "2026-05-01T00:00:00Z",
    })
    assert out["id"] == "x"
    assert out["body"] == "b"
    assert out["author"] == "ghost"
    assert out["file_path"] is None
    assert out["line_number"] is None
    assert out["commit_sha"] is None
    assert out["is_outdated"] is False
