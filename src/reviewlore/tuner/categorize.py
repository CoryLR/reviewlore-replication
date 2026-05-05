"""Post-hoc categorization of project-specific rules.

Reads a rule snapshot (e.g. tuned_100.json), assigns each rule three labels via
one Sonnet pass per project: BitsAI-CR sub-category (within the rule's existing
dimension), generalizability (project-specific vs generalizable), and
lintability (lintable / partially_lintable / requires_llm). Feeds the CP6 RQ2
expansion and the lint-vs-LLM Discussion section.

Output paths (next to the input snapshot):
- ``<label>_categorized.json``: structured per-rule axis labels
- ``<label>_categorized.md``: human-readable rendering with axis annotations
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, get_prompt
from .dirs import DIRS
from .state import atomic_write_json, load_json
from reviewlore.backends.base import AgentBackend


DIMENSIONS = (
    "Code Defect",
    "Maintainability and Readability",
    "Performance Issue",
    "Code Style",
)

GENERALIZABILITY_LABELS = ("project_specific", "generalizable")
LINTABILITY_LABELS = ("lintable", "partially_lintable", "requires_llm")


def _flatten_snapshot(snapshot: dict) -> list[dict]:
    """Walk a snapshot dict ``{dimension: [rule_str, ...]}`` into a flat list.

    Each entry carries a stable ``index`` (its position in the flattened list),
    its enclosing ``dimension``, the rule's index within the dimension, and the
    raw rule text. The flat list preserves insertion order so the markdown
    output can be regenerated grouped back by dimension.
    """
    flat: list[dict] = []
    for dimension in DIMENSIONS:
        rules = snapshot.get(dimension, [])
        if not isinstance(rules, list):
            continue
        for within_idx, rule in enumerate(rules):
            if not isinstance(rule, str) or not rule.strip():
                continue
            flat.append(
                {
                    "index": len(flat),
                    "dimension": dimension,
                    "within_dimension_index": within_idx,
                    "rule": rule,
                }
            )
    # Sweep up any non-canonical dimensions that may appear in older snapshots
    seen = set(DIMENSIONS)
    for dimension, rules in snapshot.items():
        if dimension in seen or not isinstance(rules, list):
            continue
        for within_idx, rule in enumerate(rules):
            if not isinstance(rule, str) or not rule.strip():
                continue
            flat.append(
                {
                    "index": len(flat),
                    "dimension": dimension,
                    "within_dimension_index": within_idx,
                    "rule": rule,
                }
            )
    return flat


def _build_user_prompt(project_name: str, flat_rules: list[dict]) -> str:
    payload = {
        "project": project_name,
        "rules": [
            {
                "index": r["index"],
                "dimension": r["dimension"],
                "rule": r["rule"],
            }
            for r in flat_rules
        ],
    }
    return json.dumps(payload, indent=2)


def _normalize_categorizations(
    parsed: dict | None, flat_rules: list[dict]
) -> list[dict]:
    """Convert the model output into a per-rule list aligned to ``flat_rules``.

    Missing entries get ``unknown`` placeholders so downstream consumers always
    see one record per rule. Out-of-range indices and unknown labels also
    collapse to ``unknown`` rather than silently dropping rules.
    """
    by_index: dict[int, dict] = {}
    if isinstance(parsed, dict):
        for entry in parsed.get("categorizations", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index", -1))
            except (TypeError, ValueError):
                continue
            by_index[idx] = entry

    out: list[dict] = []
    for r in flat_rules:
        entry = by_index.get(r["index"], {})
        category = entry.get("category", "unknown")
        gen = entry.get("generalizability", "unknown")
        lint = entry.get("lintability", "unknown")
        if gen not in GENERALIZABILITY_LABELS and gen != "unknown":
            gen = "unknown"
        if lint not in LINTABILITY_LABELS and lint != "unknown":
            lint = "unknown"
        out.append(
            {
                "index": r["index"],
                "dimension": r["dimension"],
                "within_dimension_index": r["within_dimension_index"],
                "rule": r["rule"],
                "category": category if isinstance(category, str) else "unknown",
                "generalizability": gen,
                "lintability": lint,
                "reasoning": entry.get("reasoning", "") if isinstance(entry, dict) else "",
            }
        )
    return out


def _summarize(categorized: list[dict]) -> dict:
    by_category: dict[str, int] = {}
    by_generalizability: dict[str, int] = {}
    by_lintability: dict[str, int] = {}
    by_dim_lint: dict[str, dict[str, int]] = {}
    by_dim_gen: dict[str, dict[str, int]] = {}
    for r in categorized:
        cat = r.get("category", "unknown") or "unknown"
        gen = r.get("generalizability", "unknown") or "unknown"
        lint = r.get("lintability", "unknown") or "unknown"
        dim = r.get("dimension", "unknown") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
        by_generalizability[gen] = by_generalizability.get(gen, 0) + 1
        by_lintability[lint] = by_lintability.get(lint, 0) + 1
        by_dim_lint.setdefault(dim, {})[lint] = by_dim_lint.setdefault(dim, {}).get(lint, 0) + 1
        by_dim_gen.setdefault(dim, {})[gen] = by_dim_gen.setdefault(dim, {}).get(gen, 0) + 1
    return {
        "n_rules": len(categorized),
        "by_category": by_category,
        "by_generalizability": by_generalizability,
        "by_lintability": by_lintability,
        "by_dimension_x_lintability": by_dim_lint,
        "by_dimension_x_generalizability": by_dim_gen,
    }


def _render_markdown(
    project_name: str,
    snapshot_label: str,
    categorized: list[dict],
    summary: dict,
) -> str:
    lines: list[str] = []
    lines.append(f"# Categorized rules: {project_name} / {snapshot_label}")
    lines.append("")
    lines.append(
        "Each rule is annotated with its BitsAI-CR sub-category (within its existing dimension), "
        "generalizability (project_specific / generalizable), and lintability "
        "(lintable / partially_lintable / requires_llm). Generated via one Sonnet pass per project."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total rules: {summary['n_rules']}")
    lines.append("- Generalizability:")
    for k in (*GENERALIZABILITY_LABELS, "unknown"):
        v = summary["by_generalizability"].get(k, 0)
        if v:
            lines.append(f"    - `{k}`: {v}")
    lines.append("- Lintability:")
    for k in (*LINTABILITY_LABELS, "unknown"):
        v = summary["by_lintability"].get(k, 0)
        if v:
            lines.append(f"    - `{k}`: {v}")
    lines.append("- Top categories:")
    top = sorted(summary["by_category"].items(), key=lambda x: -x[1])[:10]
    for cat, n in top:
        lines.append(f"    - {cat}: {n}")
    lines.append("")
    lines.append("## Rules by dimension")
    lines.append("")

    # Group categorized rules by dimension, in the canonical dimension order
    by_dim: dict[str, list[dict]] = {}
    for r in categorized:
        by_dim.setdefault(r["dimension"], []).append(r)

    ordered_dims = list(DIMENSIONS) + [d for d in by_dim if d not in DIMENSIONS]
    for dim in ordered_dims:
        rules = by_dim.get(dim)
        if not rules:
            continue
        lines.append(f"### {dim}")
        lines.append("")
        for r in rules:
            lines.append(
                f"- **[{r['category']}]** _(gen: `{r['generalizability']}`, "
                f"lint: `{r['lintability']}`)_"
            )
            rule_text = r["rule"].replace("\n", " ").strip()
            lines.append(f"    - {rule_text}")
            reasoning = (r.get("reasoning") or "").strip()
            if reasoning:
                lines.append(f"    - _reasoning:_ {reasoning}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def categorize_snapshot(
    snapshot_json_path: Path,
    output_json_path: Path,
    output_md_path: Path,
    project_name: str,
    snapshot_label: str,
    config: Config,
    backend: AgentBackend,
    rule_limit: int | None = None,
) -> dict:
    """Categorize one rule snapshot end-to-end with a single LLM call.

    ``rule_limit`` slices the flattened rule list to the first N rules; used by
    --trial mode to validate the prompt + parse round-trip before paying for a
    full pass. ``None`` means "all rules" (production behavior).
    """
    snapshot = load_json(snapshot_json_path)
    if snapshot is None:
        return {"error": f"Could not load snapshot: {snapshot_json_path}"}

    flat_rules = _flatten_snapshot(snapshot)
    if not flat_rules:
        return {"error": f"No rules found in snapshot: {snapshot_json_path}"}
    if rule_limit is not None and rule_limit > 0:
        flat_rules = flat_rules[:rule_limit]

    system_prompt = get_prompt("categorize_rules", config)
    user_prompt = _build_user_prompt(project_name, flat_rules)

    # Path the replay backend will read from on a re-run; we also write to it
    # at the end of this function. Same path serves as both lookup and save
    # location, so live-mode and replay-mode are file-symmetric.
    raw_path = output_json_path.with_name(output_json_path.stem + "_raw.json")

    # Use the label config: same Sonnet model, same per-call budget. Empty tools
    # because the categorizer reasons over text only — no repo access needed.
    result = backend.invoke(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=config.label.model,
        budget_usd=config.label.budget,
        tools=[],
        effort_level="low",
        timeout_seconds=900,
        replay_path=raw_path,
    )

    categorized = _normalize_categorizations(result.parsed_json, flat_rules)
    summary = _summarize(categorized)

    output: dict = {
        "project": project_name,
        "snapshot_label": snapshot_label,
        "n_rules": len(categorized),
        "categorizations": categorized,
        "summary": summary,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "parse_ok": result.parsed_json is not None,
    }
    atomic_write_json(output_json_path, output)

    output_md_path.parent.mkdir(parents=True, exist_ok=True)
    output_md_path.write_text(
        _render_markdown(project_name, snapshot_label, categorized, summary)
    )

    # Also persist the raw model output for replay-backend roundtripping.
    # raw_path was defined above the invoke call; reuse it.
    raw_data = {
        "project": project_name,
        "snapshot_label": snapshot_label,
        "raw_output": result.raw_output[:10_000_000],
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    atomic_write_json(raw_path, raw_data)

    return {
        "n_rules": len(categorized),
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "parse_ok": result.parsed_json is not None,
        "summary": summary,
    }


def categorize_project(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    snapshot: str = "tuned_100",
    trial: bool = False,
    force: bool = False,
) -> None:
    """Categorize a project's rule snapshot. Skips if outputs already exist.

    The default snapshot is ``tuned_100``: per the CP6 plan, only the tuned-100
    rule set needs categorization for the RQ2 + lint-vs-LLM narrative; tuned-200
    adds nothing the rule-dilution discussion does not already get from the
    tuned-100 categorization plus pooled rule counts.
    """
    snapshots_dir = (
        out_dir / project_name / DIRS["optimization"] / "snapshots"
    )
    snapshot_json = snapshots_dir / f"{snapshot}.json"
    snapshot_md = snapshots_dir / f"{snapshot}.md"

    if not snapshot_json.exists():
        print(
            f"No snapshot for {project_name} at {snapshot_json}. "
            f"Run 'relo optimize' first."
        )
        return
    if not snapshot_md.exists():
        # Not strictly required by the categorizer, but the consolidator emits
        # both side by side; warn so a missing .md does not silently propagate.
        print(f"  Note: {snapshot_md.name} not found; only the .json was loaded.")

    # In trial mode, write to a sibling path with a "_trial" suffix so the
    # cheap-and-fast verification pass does not collide with (or block) the
    # production output. Trial mode also slices to the first 5 rules.
    out_label = f"{snapshot}_trial" if trial else snapshot
    rule_limit = 5 if trial else None
    output_json = snapshots_dir / f"{out_label}_categorized.json"
    output_md = snapshots_dir / f"{out_label}_categorized.md"

    if not force and output_json.exists() and output_md.exists():
        print(
            f"Categorize {project_name}/{out_label}: outputs already exist, "
            f"skipping (use --force to re-run)."
        )
        return

    print(f"Categorizing {project_name}/{out_label}")
    if trial:
        print(f"  TRIAL MODE (first {rule_limit} rules only)")

    start = time.time()
    stats = categorize_snapshot(
        snapshot_json_path=snapshot_json,
        output_json_path=output_json,
        output_md_path=output_md,
        project_name=project_name,
        snapshot_label=out_label,
        config=config,
        backend=backend,
        rule_limit=rule_limit,
    )
    elapsed = time.time() - start

    if "error" in stats:
        print(f"  ERROR: {stats['error']}")
        return

    summary = stats.get("summary", {})
    print(
        f"  Categorized {stats.get('n_rules', 0)} rules "
        f"in {elapsed:.1f}s, cost ${stats.get('cost_usd', 0.0):.3f}"
    )
    print(f"  parse_ok: {stats.get('parse_ok', False)}")
    print(f"  Generalizability: {summary.get('by_generalizability', {})}")
    print(f"  Lintability: {summary.get('by_lintability', {})}")

    # Snapshot-scoped metadata file (so multiple snapshots can coexist and we
    # don't collide with any other step's metadata.json in the snapshots dir).
    meta_path = snapshots_dir / f"{out_label}_categorized_metadata.json"
    atomic_write_json(meta_path, {
        "step": "categorize",
        "project": project_name,
        "snapshot": out_label,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if stats.get("parse_ok") else "parse_failure",
        "params": {"trial": trial, "snapshot": snapshot},
        "prompt_versions": {
            "categorize_rules": config.prompts["categorize_rules"].version,
        },
        "outputs": {
            "n_rules": stats.get("n_rules", 0),
            "summary": summary,
        },
        "cost": {"total_usd": stats.get("cost_usd", 0.0)},
        "duration_seconds": elapsed,
    })
