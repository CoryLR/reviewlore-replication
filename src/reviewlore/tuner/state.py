"""Pipeline state tracking: atomic writes, metadata, staleness detection."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import Config


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: write to temp file, then rename.

    This prevents partial writes from corrupting output files if
    the process is interrupted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=path.stem
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_metadata(output_dir: Path, metadata: dict) -> None:
    """Write metadata.json to an output directory."""
    atomic_write_json(output_dir / "metadata.json", metadata)


def load_metadata(output_dir: Path) -> dict | None:
    """Read metadata.json from an output directory, or None if missing."""
    meta_path = output_dir / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def check_staleness(metadata: dict, config: Config) -> list[str]:
    """Compare prompt versions in metadata against current config.

    Returns a list of prompt names that are stale (version mismatch).
    """
    stale = []
    recorded_versions = metadata.get("prompt_versions", {})
    for prompt_name, recorded_version in recorded_versions.items():
        if prompt_name in config.prompts:
            current_version = config.prompts[prompt_name].version
            if recorded_version != current_version:
                stale.append(prompt_name)
    return stale


def load_json(path: Path) -> dict | None:
    """Load a JSON file, returning None on missing or invalid files."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
