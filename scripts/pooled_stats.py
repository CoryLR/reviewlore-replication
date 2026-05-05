"""Pooled and stratified statistical tests for paired reviewer conditions.

Walks ``data/<project>/7_judgments/<condition>/pr_*/rev_*/verdicts.json`` and
pairs verdicts across conditions for the same human comment. Emits:

- A per-project pairwise McNemar exact test (``generic`` vs each tuned
  condition) with Holm-Bonferroni correction across the set of per-project
  tests for each pairwise comparison.
- A pooled McNemar exact test across projects (summed discordant cells), kept
  for comparability with any two-project preliminary report.
- A Cochran-Mantel-Haenszel (CMH) stratified test for matched pairs, the
  primary global statistic for a multi-project design.  For paired data, this
  reduces to the normal-approximation test on the summed discordant cells,
  computed here via ``math.erfc`` so the script stays stdlib-only.
- Common Mantel-Haenszel odds ratio for the tuned-vs-generic contrast
  (``sum_c / sum_b`` where ``b`` counts generic-only matches and ``c`` counts
  tuned-only matches).
- Ensemble union recall for every non-empty subset of the present conditions,
  per project and pooled.  "Union" counts a human comment as matched if at
  least one condition in the subset matched it; only comments paired across
  every condition in the subset are included in the denominator.  No
  significance test is attached; ensemble recall is a deterministic set-union
  over existing verdicts, framed as a deployment option rather than an
  experimental condition.

Breslow-Day homogeneity is intentionally *not* computed: the chi-square tail
probability required for a p-value is not in the standard library.  Add it
when ``scipy`` is acceptable.

Usage::

    python3 scripts/pooled_stats.py                       # default: all projects, all conditions
    python3 scripts/pooled_stats.py --projects storybook,mermaid
    python3 scripts/pooled_stats.py --conditions generic,tuned_100
    python3 scripts/pooled_stats.py --data-dir data --json out/stats.json

Only ``(project, condition)`` cells that actually have ``verdicts.json``
output on disk enter the analysis; missing cells are logged and skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Verdict discovery and pairing
# ---------------------------------------------------------------------------


def load_verdicts(data_dir: Path, project: str, condition: str) -> list[dict]:
    """Return a list of verdict records for ``(project, condition)``.

    Each record carries the keys needed to pair with verdicts for the same
    human comment under a different condition: ``project``, ``pr_number``,
    ``head_sha_short`` (the short rev SHA used in the output directory name),
    ``human_comment_index``, and ``verdict``.
    """
    jdir = data_dir / project / "7_judgments" / condition
    if not jdir.is_dir():
        return []
    out: list[dict] = []
    for vpath in sorted(jdir.rglob("verdicts.json")):
        try:
            data = json.loads(vpath.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        parts = vpath.parts
        pr_parts = [p for p in parts if p.startswith("pr_")]
        rev_parts = [p for p in parts if p.startswith("rev_")]
        if not pr_parts or not rev_parts:
            continue
        pr_number = int(pr_parts[0].removeprefix("pr_"))
        rev_sha_short = rev_parts[0].removeprefix("rev_")
        for v in data.get("verdicts", []):
            out.append({
                "project": project,
                "condition": condition,
                "pr_number": pr_number,
                "head_sha_short": rev_sha_short,
                "human_comment_index": v.get("human_comment_index", 0),
                "verdict": v.get("verdict", "no_match"),
            })
    return out


def pair_by_condition(records: list[dict]) -> dict[tuple, dict[str, str]]:
    """Group verdict records by ``(project, pr, rev, human_idx)``.

    Returns a dict whose values are per-comment maps from condition name to
    verdict string.  Entries missing one or more conditions are retained but
    can be filtered by the caller.
    """
    out: dict[tuple, dict[str, str]] = {}
    for r in records:
        key = (r["project"], r["pr_number"], r["head_sha_short"], r["human_comment_index"])
        out.setdefault(key, {})[r["condition"]] = r["verdict"]
    return out


# ---------------------------------------------------------------------------
# Paired 2x2 cell counts
# ---------------------------------------------------------------------------


def paired_cells(
    paired: dict[tuple, dict[str, str]],
    cond_a: str,
    cond_b: str,
    project: str | None = None,
) -> dict[str, int]:
    """Return ``{"both", "a_only", "b_only", "neither", "n_paired"}`` counts.

    ``a_only`` counts comments matched under ``cond_a`` but not ``cond_b``;
    ``b_only`` is the reverse.  When ``project`` is given, restricts to keys
    whose project component equals it.
    """
    both = a_only = b_only = neither = 0
    for key, v in paired.items():
        if project is not None and key[0] != project:
            continue
        if cond_a not in v or cond_b not in v:
            continue
        va = v[cond_a] == "match"
        vb = v[cond_b] == "match"
        if va and vb:
            both += 1
        elif va:
            a_only += 1
        elif vb:
            b_only += 1
        else:
            neither += 1
    return {
        "both": both,
        "a_only": a_only,
        "b_only": b_only,
        "neither": neither,
        "n_paired": both + a_only + b_only + neither,
    }


# ---------------------------------------------------------------------------
# Ensemble union recall
# ---------------------------------------------------------------------------


def ensemble_recall(
    paired: dict[tuple, dict[str, str]],
    subset: tuple[str, ...],
    project: str | None = None,
) -> dict[str, int | float]:
    """Return union-match recall for an ensemble of conditions.

    A human comment is counted as matched by the ensemble when at least one
    condition in ``subset`` returned ``match`` on it.  The denominator counts
    only comments paired across every condition in ``subset`` so that the
    numerator and denominator refer to the same fixed comment set across all
    ensembles of a given size.

    Returns ``{"matched", "n_paired", "recall"}``.  ``recall`` is ``0.0`` when
    ``n_paired`` is zero.
    """
    matched = 0
    n_paired = 0
    for key, v in paired.items():
        if project is not None and key[0] != project:
            continue
        if not all(c in v for c in subset):
            continue
        n_paired += 1
        if any(v[c] == "match" for c in subset):
            matched += 1
    recall = matched / n_paired if n_paired else 0.0
    return {"matched": matched, "n_paired": n_paired, "recall": recall}


def _subsets(items: list[str], min_size: int = 1) -> list[tuple[str, ...]]:
    """Enumerate non-empty subsets of ``items`` in stable order.

    Order: by subset size ascending, then by input order within each size.
    Keeps the output deterministic for diff-friendly logs.
    """
    out: list[tuple[str, ...]] = []
    n = len(items)
    for size in range(min_size, n + 1):
        # Iterate all bitmasks of the given popcount in input order.
        for mask in range(1 << n):
            if bin(mask).count("1") != size:
                continue
            subset = tuple(items[i] for i in range(n) if (mask >> i) & 1)
            out.append(subset)
    return out


# ---------------------------------------------------------------------------
# Exact and asymptotic test helpers
# ---------------------------------------------------------------------------


def binomial_two_sided_p(successes: int, trials: int, p: float = 0.5) -> float:
    """Two-sided exact binomial p-value under H0: probability = ``p``.

    Uses the "minimum likelihood" convention: the sum of probabilities of
    outcomes at least as unlikely as the observed one.  For the paired
    McNemar case where ``p = 0.5`` (symmetric binomial) this reduces to
    ``2 * tail`` where ``tail`` is the one-sided probability on the smaller
    arm.  Implemented with ``math.comb`` so no SciPy is needed.
    """
    if trials <= 0:
        return 1.0
    def pmf(x: int) -> float:
        return math.comb(trials, x) * (p ** x) * ((1 - p) ** (trials - x))
    observed_p = pmf(successes)
    # A tiny slack avoids floating-point strictness dropping an outcome with
    # pmf numerically equal to the observed one.
    two_sided = sum(pmf(x) for x in range(trials + 1) if pmf(x) <= observed_p + 1e-15)
    return min(1.0, two_sided)


def mcnemar_exact(b_only_generic: int, b_only_tuned: int) -> dict:
    """McNemar's exact test on a pair of discordant counts.

    ``b_only_generic`` counts pairs matched under the control (generic) only;
    ``b_only_tuned`` counts pairs matched under the treatment (tuned) only.
    Odds ratio is reported with the convention ``tuned_only / generic_only``
    so an OR > 1 means tuning helped.
    """
    disc = b_only_generic + b_only_tuned
    if disc == 0:
        return {
            "discordant_generic_only": b_only_generic,
            "discordant_tuned_only": b_only_tuned,
            "discordant_total": 0,
            "p_value": 1.0,
            "odds_ratio": 1.0,
            "note": "No discordant pairs; test is undefined.",
        }
    # Two-sided exact binomial on the smaller cell, H0: each discordant pair
    # is equally likely to fall on either side.
    p = binomial_two_sided_p(b_only_tuned, disc, 0.5)
    if b_only_generic > 0:
        or_val: float = b_only_tuned / b_only_generic
    else:
        or_val = float("inf") if b_only_tuned > 0 else 1.0
    return {
        "discordant_generic_only": b_only_generic,
        "discordant_tuned_only": b_only_tuned,
        "discordant_total": disc,
        "p_value": p,
        "odds_ratio": or_val,
    }


def holm_bonferroni(p_values: list[float]) -> list[float]:
    """Return Holm-Bonferroni-adjusted p-values, preserving input order.

    Input-order preservation matters because the caller usually wants to zip
    the result back against a list of comparison names.
    """
    n = len(p_values)
    if n == 0:
        return []
    ranked = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * n
    running_max = 0.0
    for rank, (orig_idx, p) in enumerate(ranked):
        raw = p * (n - rank)
        running_max = max(running_max, min(raw, 1.0))
        adjusted[orig_idx] = running_max
    return adjusted


def cmh_paired_normal(strata_cells: list[dict[str, int]]) -> dict:
    """Cochran-Mantel-Haenszel test for stratified paired binary data.

    ``strata_cells`` is a list of ``paired_cells`` outputs, one per stratum.
    Under H0 (no tuning effect), each stratum's count of tuned-only matches
    is Binomial(discordant_k, 0.5).  The stratified test statistic is

        Z = (sum_k tuned_only_k - 0.5 * sum_k discordant_k)
            / sqrt(0.25 * sum_k discordant_k)

    with ``p = erfc(|Z| / sqrt(2))``.  The Mantel-Haenszel common odds ratio
    for matched pairs is ``sum_k tuned_only_k / sum_k generic_only_k``.
    """
    sum_a = sum(s["a_only"] for s in strata_cells)
    sum_b = sum(s["b_only"] for s in strata_cells)
    disc = sum_a + sum_b
    if disc == 0:
        return {
            "strata": len(strata_cells),
            "pooled_discordant_total": 0,
            "chi_square": 0.0,
            "p_value": 1.0,
            "common_odds_ratio": 1.0,
            "note": "No discordant pairs in any stratum; test is undefined.",
        }
    expected = disc / 2.0
    variance = disc / 4.0
    z = (sum_b - expected) / math.sqrt(variance)
    p = math.erfc(abs(z) / math.sqrt(2.0))
    or_val = sum_b / sum_a if sum_a > 0 else (float("inf") if sum_b > 0 else 1.0)
    return {
        "strata": len(strata_cells),
        "pooled_generic_only": sum_a,
        "pooled_tuned_only": sum_b,
        "pooled_discordant_total": disc,
        "z_statistic": z,
        "chi_square": z * z,
        "p_value": p,
        "common_odds_ratio": or_val,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def discover(data_dir: Path, projects: list[str] | None, conditions: list[str] | None) -> tuple[list[str], list[str]]:
    """Return the set of projects and conditions actually present on disk.

    When ``projects`` is ``None``, every immediate subdirectory of
    ``data_dir`` that contains a ``7_judgments/`` tree is included.  When
    ``conditions`` is ``None``, conditions are the union across all included
    projects.
    """
    if projects is None:
        projects = sorted(
            p.name for p in data_dir.iterdir()
            if p.is_dir() and (p / "7_judgments").is_dir()
        )
    if conditions is None:
        seen: set[str] = set()
        for proj in projects:
            jroot = data_dir / proj / "7_judgments"
            if jroot.is_dir():
                for child in jroot.iterdir():
                    if child.is_dir():
                        seen.add(child.name)
        conditions = sorted(seen)
    return projects, conditions


def compute_all(
    data_dir: Path,
    projects: list[str],
    conditions: list[str],
) -> dict:
    """Run all statistical comparisons and return a single result dict."""
    # Load verdicts for every (project, condition) cell that exists.
    records: list[dict] = []
    cells_present: list[tuple[str, str]] = []
    cells_missing: list[tuple[str, str]] = []
    for proj in projects:
        for cond in conditions:
            cell_records = load_verdicts(data_dir, proj, cond)
            if cell_records:
                records.extend(cell_records)
                cells_present.append((proj, cond))
            else:
                cells_missing.append((proj, cond))

    paired = pair_by_condition(records)

    # Sanity: which conditions have data for each project?
    project_conditions: dict[str, list[str]] = {}
    for proj, cond in cells_present:
        project_conditions.setdefault(proj, []).append(cond)

    # Pairwise comparisons: generic vs each tuned condition that is present.
    pairwise_results: dict[str, dict] = {}
    if "generic" in conditions:
        tuned_conditions = [c for c in conditions if c != "generic"]
        for tc in tuned_conditions:
            comparison_label = f"generic_vs_{tc}"
            # Per-project McNemar, restricted to projects that have BOTH conditions.
            per_project: dict[str, dict] = {}
            p_values: list[float] = []
            project_keys: list[str] = []
            for proj in projects:
                pcs = project_conditions.get(proj, [])
                if "generic" not in pcs or tc not in pcs:
                    continue
                cells = paired_cells(paired, "generic", tc, project=proj)
                test = mcnemar_exact(cells["a_only"], cells["b_only"])
                recall_generic = (cells["both"] + cells["a_only"]) / cells["n_paired"] if cells["n_paired"] else 0.0
                recall_tuned = (cells["both"] + cells["b_only"]) / cells["n_paired"] if cells["n_paired"] else 0.0
                per_project[proj] = {
                    "cells": cells,
                    "recall_generic": recall_generic,
                    "recall_tuned": recall_tuned,
                    "delta_pp": (recall_tuned - recall_generic) * 100.0,
                    "mcnemar": test,
                }
                p_values.append(test["p_value"])
                project_keys.append(proj)

            # Holm-Bonferroni across projects for this comparison.
            adjusted = holm_bonferroni(p_values)
            for k, adj_p in zip(project_keys, adjusted):
                per_project[k]["mcnemar"]["p_value_holm"] = adj_p

            # Pooled McNemar (sum discordant across projects).
            strata_cells = [per_project[k]["cells"] for k in project_keys]
            sum_a = sum(s["a_only"] for s in strata_cells)
            sum_b = sum(s["b_only"] for s in strata_cells)
            sum_both = sum(s["both"] for s in strata_cells)
            sum_n = sum(s["n_paired"] for s in strata_cells)
            pooled_test = mcnemar_exact(sum_a, sum_b)
            pooled_recall_generic = (sum_both + sum_a) / sum_n if sum_n else 0.0
            pooled_recall_tuned = (sum_both + sum_b) / sum_n if sum_n else 0.0

            # CMH stratified paired test.
            cmh = cmh_paired_normal(strata_cells)

            pairwise_results[comparison_label] = {
                "per_project": per_project,
                "pooled": {
                    "cells": {
                        "both": sum_both,
                        "a_only": sum_a,
                        "b_only": sum_b,
                        "n_paired": sum_n,
                    },
                    "recall_generic": pooled_recall_generic,
                    "recall_tuned": pooled_recall_tuned,
                    "delta_pp": (pooled_recall_tuned - pooled_recall_generic) * 100.0,
                    "mcnemar_exact": pooled_test,
                },
                "cmh_stratified": cmh,
            }

    # Ensemble union recall per (non-empty subset, project) and pooled.
    # Only subsets of conditions actually loaded enter the enumeration.
    loaded_conditions = sorted({c for _, c in cells_present})
    ensembles: list[dict] = []
    for subset in _subsets(loaded_conditions, min_size=1):
        per_project = {
            proj: ensemble_recall(paired, subset, project=proj)
            for proj in projects
            if all(c in project_conditions.get(proj, []) for c in subset)
        }
        pooled = ensemble_recall(paired, subset, project=None)
        # Restrict pooled denominator to projects that have every subset member,
        # so the pooled n matches the per-project sum.
        pooled_matched = sum(v["matched"] for v in per_project.values())
        pooled_n = sum(v["n_paired"] for v in per_project.values())
        pooled_recall = pooled_matched / pooled_n if pooled_n else 0.0
        ensembles.append({
            "subset": list(subset),
            "per_project": per_project,
            "pooled": {
                "matched": pooled_matched,
                "n_paired": pooled_n,
                "recall": pooled_recall,
            },
            "pooled_unrestricted": pooled,
        })

    return {
        "data_dir": str(data_dir),
        "projects_requested": projects,
        "conditions_requested": conditions,
        "cells_present": [{"project": p, "condition": c} for p, c in cells_present],
        "cells_missing": [{"project": p, "condition": c} for p, c in cells_missing],
        "paired_comment_count": len(paired),
        "pairwise": pairwise_results,
        "ensembles": ensembles,
    }


def render_markdown(result: dict) -> str:
    """Produce a short, human-readable summary."""
    lines: list[str] = []
    lines.append("# Pooled and stratified paired statistics")
    lines.append("")
    lines.append(f"- Data directory: `{result['data_dir']}`")
    lines.append(f"- Cells present: {len(result['cells_present'])}  missing: {len(result['cells_missing'])}")
    if result["cells_missing"]:
        missing = ", ".join(f"{c['project']}/{c['condition']}" for c in result["cells_missing"])
        lines.append(f"  - Missing: {missing}")
    lines.append(f"- Unique paired comments across conditions: {result['paired_comment_count']}")
    lines.append("")

    for label, block in result["pairwise"].items():
        lines.append(f"## {label}")
        lines.append("")
        lines.append("### Per-project McNemar (Holm-Bonferroni adjusted across projects)")
        lines.append("")
        for proj, pp in block["per_project"].items():
            c = pp["cells"]
            m = pp["mcnemar"]
            lines.append(
                f"- **{proj}**: n={c['n_paired']}  "
                f"both={c['both']}  generic-only={c['a_only']}  tuned-only={c['b_only']}  "
                f"neither={c['neither']}  "
                f"recall {pp['recall_generic']:.3f} -> {pp['recall_tuned']:.3f}  "
                f"delta {pp['delta_pp']:+.1f}pp  "
                f"p_exact={m['p_value']:.4f}  p_holm={m['p_value_holm']:.4f}  "
                f"OR={m['odds_ratio']}"
            )
        lines.append("")
        p = block["pooled"]
        pm = p["mcnemar_exact"]
        c = p["cells"]
        lines.append("### Pooled McNemar (summed discordant cells)")
        lines.append(
            f"- n={c['n_paired']}  both={c['both']}  generic-only={c['a_only']}  "
            f"tuned-only={c['b_only']}  "
            f"recall {p['recall_generic']:.3f} -> {p['recall_tuned']:.3f}  "
            f"delta {p['delta_pp']:+.1f}pp  "
            f"p_exact={pm['p_value']:.4f}  OR={pm['odds_ratio']}"
        )
        lines.append("")
        cmh = block["cmh_stratified"]
        lines.append("### CMH stratified paired (normal approximation, primary global statistic)")
        lines.append(
            f"- strata={cmh['strata']}  discordant total={cmh.get('pooled_discordant_total', 0)}  "
            f"Z={cmh.get('z_statistic', float('nan')):.3f}  "
            f"chi^2={cmh.get('chi_square', 0.0):.3f}  "
            f"p={cmh['p_value']:.4f}  common OR={cmh['common_odds_ratio']}"
        )
        if "note" in cmh:
            lines.append(f"  - Note: {cmh['note']}")
        lines.append("")

    if result.get("ensembles"):
        lines.append("## Ensemble union recall")
        lines.append("")
        lines.append("Union of catches across the listed conditions; a comment counts as matched if at least one member condition matched it. No significance test attached (deterministic set union over existing verdicts).")
        lines.append("")
        for ens in result["ensembles"]:
            subset_label = " U ".join(ens["subset"])
            lines.append(f"### {subset_label}")
            for proj, pp in ens["per_project"].items():
                lines.append(
                    f"- **{proj}**: {pp['matched']}/{pp['n_paired']} "
                    f"= {pp['recall']*100:.1f}%"
                )
            p = ens["pooled"]
            lines.append(
                f"- **pooled**: {p['matched']}/{p['n_paired']} "
                f"= {p['recall']*100:.1f}%"
            )
            lines.append("")
    return "\n".join(lines)


def parse_list(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Root data directory (default: ./data)",
    )
    parser.add_argument(
        "--projects",
        help="Comma-separated project names (default: auto-discover)",
    )
    parser.add_argument(
        "--conditions",
        help="Comma-separated condition names (default: auto-discover)",
    )
    parser.add_argument(
        "--json",
        help="Optional path to also write the result as JSON",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    projects, conditions = discover(
        data_dir, parse_list(args.projects), parse_list(args.conditions)
    )
    if not projects:
        print("ERROR: no projects discovered under data dir", file=sys.stderr)
        return 1
    if not conditions:
        print("ERROR: no conditions discovered", file=sys.stderr)
        return 1

    result = compute_all(data_dir, projects, conditions)
    print(render_markdown(result))

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
