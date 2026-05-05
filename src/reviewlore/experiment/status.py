"""Experiment pipeline status reporting for relox."""

from __future__ import annotations

from pathlib import Path

import click

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import load_metadata
from reviewlore.tuner.status import show_status as show_tuner_status


def show_experiment_status(
    project_name: str | None,
    config: Config,
    out_dir: Path,
) -> None:
    """Show full pipeline status including experiment-specific steps."""
    # Show tuner steps first
    show_tuner_status(project_name, config, out_dir)

    projects = [project_name] if project_name else list(config.projects.keys())

    for name in projects:
        proj_dir = out_dir / name

        # Split
        split_dir = proj_dir / DIRS["split"]
        if split_dir.exists():
            tuning_path = split_dir / "tuning.txt"
            test_path = split_dir / "test.txt"
            testable_path = split_dir / "testable_revisions.json"
            tuning_count = _count_lines(tuning_path)
            test_count = _count_lines(test_path)
            testable_count = _count_json_array(testable_path)
            click.echo(f"  split: {tuning_count}/{test_count} (tuning/test), {testable_count} testable revisions")
        else:
            click.echo(f"  split: pending")

        # Reviews and judgments per condition
        for step in ["reviews", "judgments"]:
            step_dir = proj_dir / DIRS[step]
            if step_dir.exists():
                for cond_dir in sorted(step_dir.iterdir()):
                    if cond_dir.is_dir():
                        rev_dirs = list(cond_dir.rglob("rev_*"))
                        click.echo(f"  {step}/{cond_dir.name}: {len(rev_dirs)} revisions")
            else:
                click.echo(f"  {step}: pending")

        # Validation
        val_dir = proj_dir / DIRS["validation"]
        if val_dir.exists():
            human_dir = val_dir / "human_labels"
            if human_dir.exists():
                label_files = list(human_dir.rglob("label_*.json"))
                filled = sum(1 for f in label_files if _verdict_filled(f))
                click.echo(f"  validation: {filled}/{len(label_files)} labeled")
            else:
                click.echo(f"  validation: setup pending")
        else:
            click.echo(f"  validation: pending")

        # Evaluation
        eval_dir = proj_dir / DIRS["evaluation"]
        if (eval_dir / "metrics.json").exists():
            click.echo(f"  evaluation: complete")
        else:
            click.echo(f"  evaluation: pending")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len([l for l in path.read_text().splitlines() if l.strip()])


def _count_json_array(path: Path) -> int:
    import json
    if not path.exists():
        return 0
    try:
        with open(path) as f:
            data = json.load(f)
        return len(data) if isinstance(data, list) else 0
    except (json.JSONDecodeError, OSError):
        return 0


def _verdict_filled(path: Path) -> bool:
    import json
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("verdict") is not None
    except (json.JSONDecodeError, OSError):
        return False
