"""Pooled, weighted non-LLM metric aggregates across projects.

Reads each project's ``data/<project>/9_evaluation/metrics.json`` plus the
per-revision reviewer outputs under ``6_reviews/<condition>/pr_*/rev_*/``
and emits pooled comment-level averages weighted by the number of human
comments evaluated in each stratum.

The per-condition numbers reported are:

- ``n_eval``: human-comment pairs scored by the judge in this project/condition.
- ``recall``: judge recall (carried from ``metrics.json.judge_recall``).
- ``avg_rouge_l`` and ``avg_chrf``: comment-level max-pair averages from the
  non-LLM metrics block, weighted by ``n_eval``.
- ``ai_cmts``: total AI reviewer comments produced (inline + general,
  counted across all ``review.json`` files under the condition).
- ``n_revisions``: testable revisions that produced a ``review.json``.
- ``ai_per_rev``: ``ai_cmts / n_revisions``.

Pooled averages across projects weight ROUGE-L and ChrF++ by ``n_eval``.
AI comment totals sum directly; ``ai_per_rev`` is the summed comment count
divided by the summed revision count, not an average of per-project rates.

Usage::

    python3 scripts/pooled_aggregates.py
    python3 scripts/pooled_aggregates.py --projects storybook,mermaid --conditions generic,tuned_100
    python3 scripts/pooled_aggregates.py --json out/aggregates.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_metrics(data_dir: Path, project: str) -> dict | None:
    path = data_dir / project / "9_evaluation" / "metrics.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def count_ai_comments(data_dir: Path, project: str, condition: str) -> tuple[int, int]:
    """Return ``(total_ai_comments, n_revisions)`` for a condition.

    Each ``review.json`` contributes ``len(inline_comments) + len(general_comments)``.
    Reviews that fail to parse contribute zero comments but still count as a
    revision directory.
    """
    review_dir = data_dir / project / "6_reviews" / condition
    if not review_dir.is_dir():
        return (0, 0)
    total = 0
    n_revs = 0
    for rev_dir in review_dir.glob("pr_*/rev_*"):
        if not rev_dir.is_dir():
            continue
        n_revs += 1
        review_path = rev_dir / "review.json"
        if not review_path.is_file():
            continue
        try:
            review = json.loads(review_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        inline = review.get("inline_comments") or []
        general = review.get("general_comments") or []
        total += len(inline) + len(general)
    return (total, n_revs)


def discover_projects(data_dir: Path) -> list[str]:
    return sorted(
        p.name for p in data_dir.iterdir()
        if p.is_dir() and (p / "9_evaluation" / "metrics.json").is_file()
    )


def discover_conditions(metrics_by_project: dict[str, dict]) -> list[str]:
    seen: set[str] = set()
    for m in metrics_by_project.values():
        if not m:
            continue
        seen.update(m.get("judge_recall", {}).keys())
    return sorted(seen)


def compute(
    data_dir: Path,
    projects: list[str],
    conditions: list[str] | None,
) -> dict:
    metrics_by_project: dict[str, dict] = {}
    for proj in projects:
        m = load_metrics(data_dir, proj)
        if m is not None:
            metrics_by_project[proj] = m
    if conditions is None:
        conditions = discover_conditions(metrics_by_project)

    per_project: dict[str, dict[str, dict]] = {}
    for proj, m in metrics_by_project.items():
        judge_recall = m.get("judge_recall", {})
        reachable = m.get("reachable_recall", {})
        nlm = m.get("non_llm_metrics", {})
        per_project[proj] = {}
        for cond in conditions:
            # Require the condition to be present in judge_recall; otherwise skip.
            if cond not in judge_recall:
                continue
            n_eval = reachable.get(cond, {}).get("n_reachable") \
                     or nlm.get(cond, {}).get("n_comments") \
                     or 0
            ai_cmts, n_revs = count_ai_comments(data_dir, proj, cond)
            per_project[proj][cond] = {
                "n_eval": n_eval,
                "recall": judge_recall.get(cond, 0.0),
                "avg_rouge_l": nlm.get(cond, {}).get("avg_rouge_l", 0.0),
                "avg_chrf": nlm.get(cond, {}).get("avg_chrf", 0.0),
                "ai_cmts": ai_cmts,
                "n_revisions": n_revs,
                "ai_per_rev": (ai_cmts / n_revs) if n_revs else 0.0,
            }

    pooled: dict[str, dict] = {}
    for cond in conditions:
        contributing = [
            (proj, per_project[proj][cond])
            for proj in per_project
            if cond in per_project[proj]
        ]
        if not contributing:
            continue
        sum_n = sum(x["n_eval"] for _, x in contributing)
        if sum_n == 0:
            continue
        weighted_rouge = sum(x["n_eval"] * x["avg_rouge_l"] for _, x in contributing) / sum_n
        weighted_chrf = sum(x["n_eval"] * x["avg_chrf"] for _, x in contributing) / sum_n
        total_ai = sum(x["ai_cmts"] for _, x in contributing)
        total_revs = sum(x["n_revisions"] for _, x in contributing)
        # Weighted recall uses the same n_eval weighting for a direct analogue of the pooled McNemar recall.
        weighted_recall = sum(x["n_eval"] * x["recall"] for _, x in contributing) / sum_n
        pooled[cond] = {
            "contributing_projects": [p for p, _ in contributing],
            "n_eval": sum_n,
            "weighted_recall": weighted_recall,
            "weighted_rouge_l": weighted_rouge,
            "weighted_chrf": weighted_chrf,
            "ai_cmts": total_ai,
            "n_revisions": total_revs,
            "ai_per_rev": (total_ai / total_revs) if total_revs else 0.0,
        }

    return {
        "data_dir": str(data_dir),
        "projects": projects,
        "conditions": conditions,
        "per_project": per_project,
        "pooled": pooled,
    }


def render_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append("# Pooled weighted aggregates")
    lines.append("")
    lines.append(f"- Data directory: `{result['data_dir']}`")
    lines.append("")
    lines.append("## Per-project per-condition")
    lines.append("")
    for proj, by_cond in result["per_project"].items():
        lines.append(f"### {proj}")
        lines.append("")
        for cond, stats in by_cond.items():
            lines.append(
                f"- {cond}: n_eval={stats['n_eval']}  "
                f"recall={stats['recall']:.3f}  "
                f"ROUGE-L={stats['avg_rouge_l']:.3f}  "
                f"ChrF++={stats['avg_chrf']:.3f}  "
                f"AI cmts={stats['ai_cmts']}  "
                f"revs={stats['n_revisions']}  "
                f"AI/rev={stats['ai_per_rev']:.2f}"
            )
        lines.append("")
    lines.append("## Pooled across projects (weighted by n_eval)")
    lines.append("")
    for cond, stats in result["pooled"].items():
        contrib = ", ".join(stats["contributing_projects"])
        lines.append(
            f"- {cond} ({contrib}): n_eval={stats['n_eval']}  "
            f"recall={stats['weighted_recall']:.3f}  "
            f"ROUGE-L={stats['weighted_rouge_l']:.3f}  "
            f"ChrF++={stats['weighted_chrf']:.3f}  "
            f"AI cmts={stats['ai_cmts']}  "
            f"revs={stats['n_revisions']}  "
            f"AI/rev={stats['ai_per_rev']:.2f}"
        )
    lines.append("")
    return "\n".join(lines)


def parse_list(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--projects", help="Comma-separated project names (default: auto-discover)")
    parser.add_argument("--conditions", help="Comma-separated condition names (default: auto-discover)")
    parser.add_argument("--json", help="Optional JSON output path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    projects = parse_list(args.projects) or discover_projects(data_dir)
    if not projects:
        print("ERROR: no projects discovered under data dir", file=sys.stderr)
        return 1
    conditions = parse_list(args.conditions)

    result = compute(data_dir, projects, conditions)
    print(render_markdown(result))

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
