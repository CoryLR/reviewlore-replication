"""ReviewLore experiment harness CLI (relox)."""

from __future__ import annotations

import functools
import os

import click

from reviewlore.tuner.config import load_config, resolve_output_dir, resolve_project
from reviewlore.tuner.dirs import DIRS

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
        click.echo(f"[relox] --trial auto-redirect: output root -> {out_dir}", err=True)
    resolved_backend = _resolve_backend_name(backend_name, config)
    backend = get_backend(resolved_backend, _backend_config_for(resolved_backend, config))
    return config, out_dir, backend


@click.group()
def cli():
    """ReviewLore experiment harness: controlled evaluation of tuned vs. generic prompts."""
    pass


@cli.command()
@click.argument("project")
@shared_options
def split(project, out, trial, force, yes, dry_run, backend_name):
    """Create temporal tuning/test split with testability assessment."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .split import split_project

    split_project(
        project_name=project,
        config=config,
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
@shared_options
def optimize(project, extract_only, consolidate_only, mr_range, out, trial, force, yes, dry_run, backend_name):
    """Extract + consolidate using tuning-set MRs only."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    # Load tuning-set MR list from split
    tuning_path = out_dir / project / DIRS["split"] / "tuning.txt"
    if not tuning_path.exists():
        raise click.ClickException(
            f"No tuning set found at {tuning_path}. Run 'relox split {project}' first."
        )
    mr_list = [int(line.strip()) for line in tuning_path.read_text().splitlines() if line.strip()]

    from reviewlore.tuner.optimize import optimize as do_optimize

    do_optimize(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        mr_list=mr_list,
        extract_only=extract_only,
        consolidate_only=consolidate_only,
        mr_range=mr_range,
        trial=trial,
        force=force,
        yes=yes,
    )


@cli.command()
@click.argument("project")
@click.option("--condition", default=None, help="Run only one condition")
@click.option("--review-only", is_flag=True, help="Run reviewer only, skip judge")
@click.option("--judge-only", is_flag=True, help="Run judge only on existing reviews")
@shared_options
def review(project, condition, review_only, judge_only, out, trial, force, yes, dry_run, backend_name):
    """Run reviewer + judge on test set (all conditions)."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .review_judge import review_and_judge

    conditions = [condition] if condition else None

    review_and_judge(
        project_name=project,
        config=config,
        backend=backend,
        out_dir=out_dir,
        conditions=conditions,
        review_only=review_only,
        judge_only=judge_only,
        trial=trial,
        force=force,
    )


@cli.group()
def validate():
    """Judge validation: blind human labeling under verdict-level case-cohort (project × judge_verdict) stratification."""
    pass


@validate.command("setup")
@click.option("--target", type=int, default=None, help="Pooled verdict target across 6 cells (3 projects × {match, no_match}); derives per-cell floor via ceil(target / 6). Default 54; 3 in --trial mode.")
@click.option("--seed", type=int, default=None, help="Master seed, locked to manifest after first setup.")
@shared_options
def validate_setup(target, seed, out, trial, force, yes, dry_run, backend_name):
    """Build or extend the pooled validation manifest, sample folders, worktrees."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from .validate import setup_validation

    setup_validation(
        config=config,
        out_dir=out_dir,
        target=target,
        seed=seed,
        trial=trial,
        dry_run=dry_run,
    )


@validate.command("compare")
@click.option("--json", "json_out", default=None, help="Also write machine-readable summary to this path.")
@shared_options
def validate_compare(json_out, out, trial, force, yes, dry_run, backend_name):
    """Compare rater labels vs. judge labels (primary + sensitivity agreement + kappa, per-stratum)."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from pathlib import Path as _Path
    from .validate import compare_validation

    compare_validation(
        config=config,
        out_dir=out_dir,
        trial=trial,
        json_out=_Path(json_out) if json_out else None,
    )


@validate.command("status")
@shared_options
def validate_status(out, trial, force, yes, dry_run, backend_name):
    """Report labeling progress: labeled/total verdicts and folders, per-stratum breakdown."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from .validate import status_validation

    status_validation(
        config=config,
        out_dir=out_dir,
        trial=trial,
    )


@validate.command("audit")
@click.option("--n", type=int, default=None, help="Cap rater labels at N (manifest order).")
@click.option("--json", "json_out", default=None, help="Write the structured audit JSON to this path.")
@shared_options
def validate_audit(n, json_out, out, trial, force, yes, dry_run, backend_name):
    """CP6 audit: same-line no_match check + M-of-N rater-vs-judge agreement."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from pathlib import Path as _Path
    from .validate_audit import cli_validate_audit

    cli_validate_audit(
        config=config,
        out_dir=out_dir,
        n=n,
        write_json=_Path(json_out) if json_out else None,
    )


@cli.command()
@click.argument("project")
@shared_options
def evaluate(project, out, trial, force, yes, dry_run, backend_name):
    """Compute metrics, statistical tests, and comparison tables."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .evaluate import evaluate_project

    evaluate_project(
        project_name=project,
        config=config,
        out_dir=out_dir,
        trial=trial,
    )


@cli.group("case-studies")
def case_studies():
    """CP6 case-study mining (pure analysis, no LLM calls)."""
    pass


@case_studies.command("recall-similarity")
@click.argument("project")
@shared_options
def case_studies_recall(project, out, trial, force, yes, dry_run, backend_name):
    """Bucket per-verdict cases by (judge_verdict, top-ROUGE-L) for Wing's CP4 ask."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .case_studies import case_studies_recall_similarity

    case_studies_recall_similarity(
        project_name=project,
        config=config,
        out_dir=out_dir,
    )


@case_studies.command("lost-comments")
@click.argument("project")
@click.option("--cond-a", default="generic", help="Condition that matched (default: generic).")
@click.option("--cond-b", default="tuned_100", help="Condition that did not match (default: tuned_100).")
@shared_options
def case_studies_lost(project, cond_a, cond_b, out, trial, force, yes, dry_run, backend_name):
    """Pull the c-cell of the (cond_a, cond_b) paired comparison for Wing's Apr 28 ask."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)
    resolve_project(project, config)

    from .case_studies import case_studies_lost_comments

    case_studies_lost_comments(
        project_name=project,
        config=config,
        out_dir=out_dir,
        cond_a=cond_a,
        cond_b=cond_b,
    )


@cli.command()
@click.argument("project", required=False)
@shared_options
def status(project, out, trial, force, yes, dry_run, backend_name):
    """Show full pipeline status including review/judge/eval."""
    config, out_dir, backend = _setup(out, trial, force, yes, dry_run, backend_name)

    from .status import show_experiment_status

    show_experiment_status(
        project_name=project,
        config=config,
        out_dir=out_dir,
    )


@cli.command()
@click.option("--platform", type=click.Choice(["github", "gitlab"]), default=None)
def survey(platform):
    """Discover candidate subject projects (resumable)."""
    from .survey import run_survey

    run_survey(platform=platform)


def main():
    """Entry point for relox CLI."""
    cli()


if __name__ == "__main__":
    main()
