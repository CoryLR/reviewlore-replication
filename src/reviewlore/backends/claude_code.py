"""Claude Code CLI backend implementation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import AgentResult, extract_json_from_text, register_backend


# Generic tool name to Claude Code tool name mapping
TOOL_NAME_MAP = {
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "bash": "Bash",
}


class ClaudeCodeBackend:
    """Backend that invokes Claude Code CLI (claude -p) as a subprocess."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

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
        """Invoke Claude Code CLI and parse the result.

        ``replay_path`` is accepted for protocol compatibility with the replay
        backend and ignored here (live calls always hit the model).
        """
        del replay_path  # unused: live mode always invokes the CLI
        cmd = ["claude", "-p", "--output-format", "json"]
        cmd.extend(["--model", model])
        cmd.extend(["--max-budget-usd", str(budget_usd)])

        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        if effort_level:
            cmd.extend(["--effort", effort_level])

        # Tool configuration
        if tools is not None:
            if len(tools) == 0:
                # Empty list: disable all tools
                cmd.extend(["--tools", ""])
            else:
                # Map generic names to Claude Code names
                mapped = [TOOL_NAME_MAP.get(t, t) for t in tools]
                cmd.extend(["--allowed-tools", ",".join(mapped)])

        # Repository access
        if repo_paths:
            for repo_path in repo_paths:
                cmd.extend(["--add-dir", str(repo_path)])

        # Backend-specific options from config
        if self.config.get("dangerously_skip_permissions"):
            cmd.append("--dangerously-skip-permissions")

        # Build clean environment (strip CLAUDECODE to prevent nesting)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        # Headless auth via OAuth token file. Mirrors the shell-side
        # ``claudessh`` wrapper (env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN
        # CLAUDE_CODE_OAUTH_TOKEN=<token> claude ...) so SSH sessions and
        # other non-interactive shells can drive ``claude -p`` without a
        # logged-in CLI. OPT-IN: precedence is REVIEWLORE_OAUTH_TOKEN_FILE
        # env var (per-session override) > backend.claude_code.oauth_token_file
        # in the yaml > nothing (use ambient auth).
        token_file = (
            os.environ.get("REVIEWLORE_OAUTH_TOKEN_FILE")
            or self.config.get("oauth_token_file")
        )
        if token_file:
            token_path = os.path.expanduser(str(token_file))
            try:
                with open(token_path) as f:
                    token = f.read().strip()
            except OSError as e:
                return AgentResult(
                    raw_output=f"oauth_token_file unreadable ({token_path}): {e}",
                    parsed_json=None,
                    cost_usd=0.0,
                    duration_seconds=0.0,
                    model=model,
                )
            if not token:
                return AgentResult(
                    raw_output=f"oauth_token_file is empty: {token_path}",
                    parsed_json=None,
                    cost_usd=0.0,
                    duration_seconds=0.0,
                    model=model,
                )
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        start_time = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
            duration = time.time() - start_time
            raw_output = proc.stdout
            stderr = proc.stderr

            if proc.returncode != 0:
                return AgentResult(
                    raw_output=raw_output or stderr,
                    parsed_json=None,
                    cost_usd=0.0,
                    duration_seconds=duration,
                    model=model,
                )

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return AgentResult(
                raw_output=f"TIMEOUT after {timeout_seconds}s",
                parsed_json=None,
                cost_usd=0.0,
                duration_seconds=duration,
                model=model,
            )

        # Parse the --output-format json envelope
        cost_usd = 0.0
        input_tokens = 0
        output_tokens = 0
        result_text = raw_output

        try:
            envelope = json.loads(raw_output)
            result_text = envelope.get("result", raw_output)
            cost_usd = envelope.get("total_cost_usd", 0.0) or 0.0
            input_tokens = envelope.get("input_tokens", 0) or 0
            output_tokens = envelope.get("output_tokens", 0) or 0
        except (json.JSONDecodeError, TypeError):
            # If the output isn't valid JSON envelope, use raw output
            pass

        # Extract structured JSON from the result text
        parsed = extract_json_from_text(result_text, output_marker)

        return AgentResult(
            raw_output=raw_output,
            parsed_json=parsed,
            cost_usd=cost_usd,
            duration_seconds=duration,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# Register this backend
register_backend("claude-code", ClaudeCodeBackend)
