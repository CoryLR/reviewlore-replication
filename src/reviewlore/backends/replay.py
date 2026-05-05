"""Replay backend: serves saved ``raw.json`` instead of calling Claude Code.

Used by the CP6 replication artifact so a reader can re-run the pipeline end-
to-end without an Anthropic key, the Claude Code CLI, or any network access.

Design (two modes, same backend):

* **Mode A — raw replay (reviewer + judge).** The live reviewer and judge
  steps save ``raw.json`` next to ``review.json`` / ``verdicts.json`` for
  every per-revision invocation. This backend reads that ``raw.json``, runs
  the same envelope-parse + ``extract_json_from_text`` path the live backend
  runs, and returns an :class:`AgentResult` indistinguishable from a fresh
  call (same ``raw_output``, same ``cost_usd``, same token counts, same
  parsed structured output). Callers do not need to special-case replay.

* **Mode B — skip-and-load (filter, label, categorize, optimize-extract,
  optimize-consolidate).** These steps already check whether their parsed
  output exists and short-circuit before calling :meth:`invoke`. So in
  replay mode they normally do not enter this backend at all. If they do
  reach :meth:`invoke` (e.g. the parsed output was deleted but ``raw.json``
  is still on disk for a step that saves one), the same Mode A path applies.
  If neither parsed output nor ``raw.json`` is present, this backend raises
  :class:`ReplayMissingError` rather than falling back to a live LLM call —
  the artifact is meant to be reproducible without a key, so silent
  fall-throughs would defeat the contract.

The backend never launches a subprocess and never imports ``claude_code``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import AgentResult, extract_json_from_text, register_backend


class ReplayMissingError(RuntimeError):
    """Raised when the replay backend cannot find saved output for a call.

    Carries the ``replay_path`` the backend tried (if any) and a free-form
    ``hint`` describing the lookup context so the failure tells the reader
    exactly which step + key was missing.
    """

    def __init__(self, replay_path: Path | str | None, hint: str = ""):
        self.replay_path = replay_path
        self.hint = hint
        if replay_path is None:
            base = (
                "replay backend was invoked but the caller did not pass "
                "replay_path"
            )
        else:
            base = f"replay backend has no saved raw.json at {replay_path}"
        if hint:
            base += f" — {hint}"
        super().__init__(base)


class ReplayBackend:
    """Backend that returns previously-saved model output instead of calling
    a live model.

    Configuration keys (all optional):

    * ``strict`` (default ``True``): when ``True``, raise
      :class:`ReplayMissingError` on cache miss; when ``False``, return an
      empty :class:`AgentResult` so the caller's parser can decide whether to
      retry or skip. The strict default is recommended for the artifact zip
      so a reader sees a loud, actionable error rather than a silent miss.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.strict = bool(self.config.get("strict", True))

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
    ) -> AgentResult:
        # All live-mode arguments are accepted for protocol compatibility but
        # ignored here. The replay backend reads its answer from disk.
        del system_prompt, user_prompt, budget_usd, tools, repo_paths
        del effort_level, timeout_seconds

        if replay_path is None:
            if self.strict:
                raise ReplayMissingError(None, "caller did not pass replay_path")
            return AgentResult(raw_output="", parsed_json=None, model=model)

        path = Path(replay_path)
        if not path.exists():
            if self.strict:
                raise ReplayMissingError(
                    path, "saved raw.json not found on disk"
                )
            return AgentResult(raw_output="", parsed_json=None, model=model)

        try:
            with open(path) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            if self.strict:
                raise ReplayMissingError(
                    path, f"saved raw.json is unreadable: {e}"
                ) from e
            return AgentResult(raw_output="", parsed_json=None, model=model)

        raw_output = raw.get("raw_output", "")

        # Re-extract the structured JSON the same way claude_code does:
        # parse the --output-format json envelope to get `result_text`, then
        # apply extract_json_from_text against `result_text` with the call
        # site's output_marker.
        result_text = raw_output
        try:
            envelope = json.loads(raw_output)
            if isinstance(envelope, dict):
                result_text = envelope.get("result", raw_output)
        except (json.JSONDecodeError, TypeError):
            pass

        parsed = extract_json_from_text(result_text, output_marker)

        return AgentResult(
            raw_output=raw_output,
            parsed_json=parsed,
            cost_usd=raw.get("cost_usd", 0.0) or 0.0,
            duration_seconds=raw.get("duration_seconds", 0.0) or 0.0,
            model=raw.get("model") or model,
            input_tokens=raw.get("input_tokens", 0) or 0,
            output_tokens=raw.get("output_tokens", 0) or 0,
        )


register_backend("replay", ReplayBackend)
