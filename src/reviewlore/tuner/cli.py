"""ReviewLore tuning tool CLI (relo)."""

from __future__ import annotations

import functools
import os

import click

from .config import load_config, resolve_output_dir, resolve_project

# Ensure backends are registered on import
import reviewlore.backends.claude_code  # noqa: F401
import reviewlore.backends.replay  # noqa: F401
from reviewlore.backends.base import get_backend


def _resolve_backend_name(cli_flag: str | None, config) -> str:
    """Backend name precedence: --backend flag > REVIEWLORE_BACKEND env > config."""
    if cli_flag:
        return cli_flag
    env_val = os.environ.get("REVIEWLORE_BACKEND")
    if env_val:
        return env_val
    return config.backend.name


def _backend_config_for(name: str, config) -> dict:
    """Look up the backend-specific config dict by resolved backend name."""
    if name == "claude-code":
        return config.backend.claude_code
    if name == "replay":
        return config.backend.replay
    return {}


def shared_options(f):
    """Decorator that adds shared CLI options to a subcommand."""
    @click.option("-o", "--out", default=None, help="Data root directory")
    @click.option("--trial", is_flag=True, help="Use small limits for quick e2e testing")
    @click.option("--force", is_flag=True, help="Re-run even if step is already complete")
    @click.option("--yes", "-y", is_flag=True, help="Skip interactive confirmation prompts")
    @click.option("--dry-run", is_flag=True, help="Show what would be done without doing it")
    @click.option(
        "--backend",
        "backend_name",
        type=click.Choice(["claude-code", "replay"]),
        default=None,
        help="Override backend (precedence: --backend > REVIEWLORE_BACKEND env > config).",
    )
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper


def _setup(out, trial, force, yes, dry_run, backend_name=None):
    """Load config and resolve shared options."""
    config = load_config()
    out_dir = resolve_output_dir(out, config, trial=trial)
    if trial and out is None:
        click.echo(f"[relo] --trial auto-redirect: output root -> {out_dir}", err=True)
    resolved_backend = _resolve_backend_name(backend_name, config)
    backend = get_backend(resolved_backend, _backend_config_for(resolved_backend, config))
    return config, out_dir, backend


@click.group()
def cli():
    """ReviewLore: generate project-specific LLM code reviewer prompts."""
    pass


@cli.command()
@click.argument("project")
@click.option(
    "--merged-before",
    default=None,
    help="Collect only MRs merged on or before this ISO 8601 date/datetime "
         "(e.g. 2026-04-09 or 2026-04-09T23:59:59Z). Inclusive upper bound.",
)
@shared_options
def collect(project, merged_before, out, trial, force, yes, dry_run, backend_name):
    """Collect MR/PR review data from API."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .collect import collect as do_collect

    do_collect(
        project_name=project,
        config=config,
        out_dir=out_dir,
        trial=trial,
        force=force,
        merged_before=merged_before,
    )


@cli.command("filter")
@click.argument("project")
@shared_options
def filter_cmd(project, out, trial, force, yes, dry_run, backend_name):
    """Three-stage comment filtering."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .filter import filter_project

    filter_project(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        trial=trial,
        force=force,
    )


@cli.command()
@click.argument("project")
@shared_options
def label(project, out, trial, force, yes, dry_run, backend_name):
    """BitsAI-CR category labeling."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .label import label_project

    label_project(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        trial=trial,
        force=force,
    )


@cli.command()
@click.argument("project")
@click.option("--extract-only", is_flag=True, help="Extraction phase only")
@click.option("--consolidate-only", is_flag=True, help="Consolidation phase only")
@click.option(
    "--mr-range",
    type=click.Choice(["all", "most_recent_100", "both"]),
    default="both",
    help="Consolidation MR range",
)
@click.option(
    "--merged-before",
    default=None,
    help="Include only MRs merged on or before this ISO 8601 date/datetime "
         "(e.g. 2023-06-30 or 2023-06-30T12:34:56+00:00). Inclusive upper bound.",
)
@click.option(
    "--merged-after",
    default=None,
    help="Include only MRs merged on or after this ISO 8601 date/datetime. "
         "Inclusive lower bound.",
)
@shared_options
def optimize(project, extract_only, consolidate_only, mr_range, merged_before, merged_after, out, trial, force, yes, dry_run, backend_name):
    """Two-phase rule induction (extract + consolidate)."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .optimize import optimize as do_optimize

    do_optimize(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        extract_only=extract_only,
        consolidate_only=consolidate_only,
        mr_range=mr_range,
        merged_before=merged_before,
        merged_after=merged_after,
        trial=trial,
        force=force,
        yes=yes,
    )


@cli.command()
@click.argument("project")
@click.option(
    "--snapshot",
    default="tuned_100",
    help="Rule snapshot label to categorize (default: tuned_100). "
         "Reads data/<project>/5_optimization/snapshots/<snapshot>.json.",
)
@shared_options
def categorize(project, snapshot, out, trial, force, yes, dry_run, backend_name):
    """Post-hoc rule categorization (BitsAI-CR + generalizability + lintability)."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .categorize import categorize_project

    categorize_project(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        snapshot=snapshot,
        trial=trial,
        force=force,
    )


@cli.command()
@click.argument("project", required=False)
@shared_options
def status(project, out, trial, force, yes, dry_run, backend_name):
    """Show pipeline status."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from .status import show_status

    show_status(
        project_name=project,
        config=config,
        out_dir=out_dir,
    )


def main():
    """Entry point for relo CLI."""
    cli()


if __name__ == "__main__":
    main()
