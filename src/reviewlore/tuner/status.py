"""Pipeline status reporting for relo."""

from __future__ import annotations

from pathlib import Path

import click

from .config import Config
from .dirs import DIRS
from .state import load_metadata


def show_status(
    project_name: str | None,
    config: Config,
    out_dir: Path,
) -> None:
    """Show pipeline status for all or one project."""
    projects = [project_name] if project_name else list(config.projects.keys())

    for name in projects:
        proj_dir = out_dir / name
        click.echo(f"\n{name}:")

        # Check each step
        for step in ["collected", "filtered", "labeled"]:
            step_dir = proj_dir / DIRS[step]
            if step_dir.exists():
                files = list(step_dir.glob("pr_*.json"))
                meta = load_metadata(step_dir)
                status_str = f"{len(files)} MRs"
                if meta:
                    stale = _check_stale(meta, config)
                    if stale:
                        status_str += f" (STALE: {', '.join(stale)})"
                click.echo(f"  {step}: {status_str}")
            else:
                click.echo(f"  {step}: pending")

        # Optimization
        opt_dir = proj_dir / DIRS["optimization"]
        ext_dir = opt_dir / "extraction"
        if ext_dir.exists():
            ext_files = list(ext_dir.glob("pr_*.json"))
            click.echo(f"  extraction: {len(ext_files)} MRs")
        else:
            click.echo(f"  extraction: pending")

        for run_name in ["consolidation_100", "consolidation_200"]:
            run_dir = opt_dir / run_name
            if run_dir.exists():
                batches = list(run_dir.glob("batch_*.json"))
                click.echo(f"  {run_name}: {len(batches)} batches")
            else:
                click.echo(f"  {run_name}: pending")

        # Snapshots
        snap_dir = opt_dir / "snapshots"
        if snap_dir.exists():
            snaps = list(snap_dir.glob("tuned_*.md"))
            snap_names = [s.stem for s in snaps]
            if snap_names:
                click.echo(f"  snapshots: {', '.join(snap_names)}")

    # Cost aggregation
    _show_costs(projects, out_dir)


def _check_stale(metadata: dict, config: Config) -> list[str]:
    """Check for stale prompt versions."""
    from .state import check_staleness
    return check_staleness(metadata, config)


def _show_costs(projects: list[str], out_dir: Path) -> None:
    """Aggregate and show costs from metadata files."""
    total = 0.0
    per_project = {}
    for name in projects:
        proj_dir = out_dir / name
        proj_cost = 0.0
        for meta_path in proj_dir.rglob("metadata.json"):
            meta = load_metadata(meta_path.parent)
            if meta:
                cost = meta.get("cost", {})
                if isinstance(cost, dict):
                    proj_cost += cost.get("total_usd", 0.0)
                elif isinstance(cost, (int, float)):
                    proj_cost += cost
        per_project[name] = proj_cost
        total += proj_cost

    if total > 0:
        parts = ", ".join(f"{n}: ${c:.2f}" for n, c in per_project.items() if c > 0)
        click.echo(f"\nTotal cost: ${total:.2f}  ({parts})")
