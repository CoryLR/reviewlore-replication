"""Project discovery survey (deferred for CP4)."""

from __future__ import annotations

import click


def run_survey(platform: str | None = None) -> None:
    """Discover and assess candidate subject projects.

    Survey deferred for CP4. See docs/findings/005-systematic-project-survey.md
    for existing survey results (298 repos surveyed, 3 selected).
    """
    click.echo("Survey deferred for CP4.")
    click.echo("See docs/findings/005-systematic-project-survey.md for existing results.")
    click.echo("Selected projects: storybook, mermaid, ase")
