"""
Compute the paper's result numbers from saved LLM outputs in data/.
Requirements: Python 3.8+
Run `python3 compute_all_results.py` from repo root to execute
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

PROJECTS = ["storybook", "mermaid", "ase"]
CONDITIONS = ["generic", "tuned_100", "tuned_200"]
DIMENSIONS = ["Code Defect", "Maintainability and Readability",
              "Performance Issue", "Code Style"]

PROJECT_DISPLAY = {"storybook": "Storybook", "mermaid": "Mermaid", "ase": "ASE"}
CONDITION_DISPLAY = {"generic": "Generic", "tuned_100": "Tuned-100",
                     "tuned_200": "Tuned-200"}

PIPELINE_STEPS = ["filter", "label", "extraction", "consolidation", "review", "judge"]
PIPELINE_STEP_DISPLAY = {
    "filter": "Filter (Stage 3)",
    "label": "Labeling",
    "extraction": "Extraction",
    "consolidation": "Consolidation",
    "review": "Reviewer",
    "judge": "Judge",
}

# Storybook + Mermaid Stage 3 filter cost is back-estimated per-thread (the
# raw per-invocation logs do not capture Stage 3 separately for those two
# projects). ASE Stage 3 is per-invocation so its cost is in metadata.
FILTER_STAGE3_BACKFILL = {"storybook": 7.07, "mermaid": 4.42, "ase": 0.0}

CATEGORIZATION_AXIS_KEYS = ("n", "lintable", "partial", "requires_llm",
                            "project_specific", "generalizable")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data", help="Root data directory.")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data directory not found: {data_dir}", file=sys.stderr)
        return 1
    if not validate_artifact(data_dir):
        return 1

    paired_verdicts = load_paired_verdicts(data_dir)
    metrics_by_project = {project: load_json(data_dir / project / "9_evaluation"
                                             / "metrics.json")
                          for project in PROJECTS}
    review_judge_costs = load_review_and_judge_costs(data_dir)
    metadata_costs = load_metadata_costs(data_dir)
    rule_counts_by_volume = load_rule_counts_per_volume(data_dir)
    categorization_axes = load_categorization_axes(data_dir)
    subcategory_counts = load_pooled_subcategory_counts(data_dir)
    test_set_counts = load_test_set_counts(data_dir)
    same_line_counts = load_same_line_audit(data_dir)

    print_report_header(data_dir)
    print_recall_by_condition(paired_verdicts)
    print_paired_comparisons_vs_generic(paired_verdicts)
    print_significance_tests(paired_verdicts)
    print_ensemble_recall(paired_verdicts)
    print_text_similarity(metrics_by_project)
    print_per_category_recall(metrics_by_project)
    print_rule_counts_and_categorization(rule_counts_by_volume,
                                         categorization_axes, subcategory_counts)
    print_pipeline_costs(review_judge_costs, metadata_costs)
    print_test_setup(test_set_counts)
    print_judge_validation(data_dir, same_line_counts)
    return 0


# ===== Loaders =====

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def validate_artifact(data_dir):
    required_paths = [
        data_dir / "judge_validation" / "agreement.json",
    ]
    for project in PROJECTS:
        snapshots_dir = data_dir / project / "5_optimization" / "snapshots"
        required_paths += [
            data_dir / project / "9_evaluation" / "metrics.json",
            snapshots_dir / "tuned_100.md",
            snapshots_dir / "tuned_200.md",
            snapshots_dir / "tuned_100_categorized.json",
        ]
        for condition in CONDITIONS:
            required_paths += [
                data_dir / project / "7_judgments" / condition,
                data_dir / project / "6_reviews" / condition,
            ]
    missing_paths = [path for path in required_paths if not path.exists()]
    if missing_paths:
        print("ERROR: required artifact paths missing:", file=sys.stderr)
        for path in missing_paths:
            print(f"  - {path.relative_to(data_dir)}", file=sys.stderr)
        return False
    return True


def load_paired_verdicts(data_dir):
    paired = {}
    for project in PROJECTS:
        for condition in CONDITIONS:
            verdicts_dir = data_dir / project / "7_judgments" / condition
            for verdicts_path in sorted(verdicts_dir.rglob("verdicts.json")):
                verdicts_data = load_json(verdicts_path)
                if not isinstance(verdicts_data, dict):
                    continue
                rev_segment = verdicts_path.parent.name
                pr_segment = verdicts_path.parent.parent.name
                if not rev_segment.startswith("rev_") or not pr_segment.startswith("pr_"):
                    continue
                for verdict_entry in verdicts_data.get("verdicts", []):
                    key = (project, pr_segment, rev_segment,
                           verdict_entry.get("human_comment_index", 0))
                    paired.setdefault(key, {})[condition] = verdict_entry.get(
                        "verdict", "no_match")
    return paired


def load_review_and_judge_costs(data_dir):
    costs = {project: {} for project in PROJECTS}
    for project in PROJECTS:
        for step_name, step_subdir in (("review", "6_reviews"),
                                        ("judge", "7_judgments")):
            cost_total = 0.0
            for condition in CONDITIONS:
                step_dir = data_dir / project / step_subdir / condition
                for raw_path in step_dir.rglob("raw.json"):
                    raw_envelope = load_json(raw_path)
                    if not isinstance(raw_envelope, dict):
                        continue
                    cost_total += float(raw_envelope.get("cost_usd", 0.0) or 0.0)
            costs[project][step_name] = cost_total
    return costs


def _extract_metadata_cost_total(cost_field):
    if isinstance(cost_field, dict):
        return float(cost_field.get("total_usd", 0.0) or 0.0)
    if isinstance(cost_field, (int, float)):
        return float(cost_field)
    return 0.0


def load_metadata_costs(data_dir):
    pipeline_subdirs = ("1_collected", "2_filtered", "3_labeled", "4_split",
                        "5_optimization", "8_categorization", "9_evaluation")
    costs = {project: {} for project in PROJECTS}
    for project in PROJECTS:
        for subdir_name in pipeline_subdirs:
            subdir = data_dir / project / subdir_name
            if not subdir.is_dir():
                continue
            for metadata_path in subdir.rglob("metadata.json"):
                metadata = load_json(metadata_path) or {}
                step_name = metadata.get("step", "")
                if step_name and step_name not in ("review", "judge"):
                    costs[project][step_name] = (
                        costs[project].get(step_name, 0.0)
                        + _extract_metadata_cost_total(metadata.get("cost", 0.0)))
    return costs


_DIMENSION_HEADER_RE = re.compile(r"^- \*\*(.+?)\*\*\s*$")


def _count_rules_by_dimension(snapshot_text):
    counts = {dimension: 0 for dimension in DIMENSIONS}
    current_dimension = None
    for line in snapshot_text.splitlines():
        header_match = _DIMENSION_HEADER_RE.match(line)
        if header_match:
            current_dimension = header_match.group(1).strip()
        elif line.startswith("  - ") and current_dimension in counts:
            counts[current_dimension] += 1
    return counts


def load_rule_counts_per_volume(data_dir):
    counts_by_project = {}
    for project in PROJECTS:
        counts_by_project[project] = {}
        snapshots_dir = data_dir / project / "5_optimization" / "snapshots"
        if not snapshots_dir.is_dir():
            continue
        for snapshot_path in snapshots_dir.glob("tuned_*.md"):
            volume_match = re.match(r"tuned_(\d+)\.md$", snapshot_path.name)
            if volume_match:
                volume = int(volume_match.group(1))
                counts_by_project[project][volume] = _count_rules_by_dimension(
                    snapshot_path.read_text(encoding="utf-8"))
    return counts_by_project


def load_categorization_axes(data_dir):
    LINTABILITY_VALUES = {"lintable", "partially_lintable", "requires_llm"}
    GENERALIZABILITY_VALUES = {"project_specific", "generalizable"}
    axes_by_project = {}
    for project in PROJECTS:
        per_dimension = {dimension: {"n": 0, "lintable": 0, "partial": 0,
                                      "requires_llm": 0,
                                      "project_specific": 0, "generalizable": 0}
                         for dimension in DIMENSIONS}
        categorization_path = (data_dir / project / "5_optimization"
                               / "snapshots" / "tuned_100_categorized.json")
        categorization = load_json(categorization_path) or {}
        for entry in categorization.get("categorizations") or []:
            if not isinstance(entry, dict):
                continue
            dim_axes = per_dimension.get(entry.get("dimension"))
            if dim_axes is None:
                continue
            dim_axes["n"] += 1
            lint = entry.get("lintability")
            if lint in LINTABILITY_VALUES:
                dim_axes["partial" if lint == "partially_lintable" else lint] += 1
            gen = entry.get("generalizability")
            if gen in GENERALIZABILITY_VALUES:
                dim_axes[gen] += 1
        axes_by_project[project] = per_dimension
    return axes_by_project


def load_pooled_subcategory_counts(data_dir):
    counts = {}
    for project in PROJECTS:
        categorization_path = (data_dir / project / "5_optimization"
                               / "snapshots" / "tuned_100_categorized.json")
        categorization = load_json(categorization_path)
        if not isinstance(categorization, dict):
            continue
        for entry in categorization.get("categorizations") or []:
            if isinstance(entry, dict):
                subcategory_name = entry.get("category", "")
                counts[subcategory_name] = counts.get(subcategory_name, 0) + 1
    return counts


def load_test_set_counts(data_dir):
    counts = {}
    for project in PROJECTS:
        pr_count = rev_count = 0
        for pr_dir in sorted((data_dir / project / "7_judgments" / "generic")
                             .glob("pr_*")):
            rev_dirs = [rev_dir for rev_dir in pr_dir.glob("rev_*") if rev_dir.is_dir()]
            if rev_dirs:
                pr_count += 1
                rev_count += len(rev_dirs)
        counts[project] = (pr_count, rev_count)
    return counts


def load_same_line_audit(data_dir):
    counts = {}
    for project in PROJECTS:
        same_line_count = no_match_total = 0
        for condition in CONDITIONS:
            verdicts_dir = data_dir / project / "7_judgments" / condition
            for verdicts_path in sorted(verdicts_dir.rglob("verdicts.json")):
                verdicts_data = load_json(verdicts_path) or {}
                rev_segment = verdicts_path.parent.name
                pr_segment = verdicts_path.parent.parent.name
                if not rev_segment.startswith("rev_") or not pr_segment.startswith("pr_"):
                    continue
                review_dir = (data_dir / project / "6_reviews"
                              / condition / pr_segment / rev_segment)
                human_comments = ((load_json(review_dir / "context.json") or {})
                                  .get("human_comments") or [])
                ai_inline_keys = set()
                review_data = load_json(review_dir / "review.json") or {}
                for inline_comment in review_data.get("inline_comments") or []:
                    if not isinstance(inline_comment, dict):
                        continue
                    file_path = inline_comment.get("file_path")
                    line_number = inline_comment.get("line_number")
                    if file_path is not None and line_number is not None:
                        ai_inline_keys.add((file_path, line_number))
                for verdict_entry in verdicts_data.get("verdicts") or []:
                    if (not isinstance(verdict_entry, dict)
                            or verdict_entry.get("verdict") != "no_match"):
                        continue
                    no_match_total += 1
                    human_index = verdict_entry.get("human_comment_index", -1)
                    if isinstance(human_index, int) and 0 <= human_index < len(human_comments):
                        human = human_comments[human_index]
                        if (isinstance(human, dict)
                                and (human.get("file_path"),
                                     human.get("line_number")) in ai_inline_keys):
                            same_line_count += 1
        counts[project] = (same_line_count, no_match_total)
    return counts


# ===== Stats helpers =====

def count_matches(paired_verdicts, condition, project=None):
    matches = total = 0
    for key, verdict_by_condition in paired_verdicts.items():
        if (project is None or key[0] == project) and condition in verdict_by_condition:
            total += 1
            matches += verdict_by_condition[condition] == "match"
    return matches, total


def paired_overlap_counts(paired_verdicts, first_condition, second_condition,
                          project=None):
    counts = {"both": 0, "first_only": 0, "second_only": 0, "paired": 0}
    for key, verdict_by_condition in paired_verdicts.items():
        if project is not None and key[0] != project:
            continue
        if (first_condition not in verdict_by_condition
                or second_condition not in verdict_by_condition):
            continue
        counts["paired"] += 1
        first_match = verdict_by_condition[first_condition] == "match"
        second_match = verdict_by_condition[second_condition] == "match"
        if first_match and second_match:
            counts["both"] += 1
        elif first_match:
            counts["first_only"] += 1
        elif second_match:
            counts["second_only"] += 1
    return counts


def ensemble_recall_counts(paired_verdicts, conditions, project=None):
    matches = total = 0
    for key, verdict_by_condition in paired_verdicts.items():
        if project is not None and key[0] != project:
            continue
        if all(condition in verdict_by_condition for condition in conditions):
            total += 1
            matches += any(verdict_by_condition[condition] == "match"
                           for condition in conditions)
    return matches, total


def mcnemar_exact_p_value(first_only, second_only):
    discordant_total = first_only + second_only
    if discordant_total == 0:
        return 1.0
    pmf_values = [math.comb(discordant_total, successes) * 0.5 ** discordant_total
                  for successes in range(discordant_total + 1)]
    observed_tail = pmf_values[second_only]
    return min(1.0, sum(probability for probability in pmf_values
                        if probability <= observed_tail + 1e-15))


def cmh_paired_test(per_project_overlaps):
    first_only_sum = sum(overlap["first_only"] for overlap in per_project_overlaps)
    second_only_sum = sum(overlap["second_only"] for overlap in per_project_overlaps)
    discordant_total = first_only_sum + second_only_sum
    if discordant_total == 0:
        return 1.0, 1.0
    z_score = ((second_only_sum - discordant_total / 2.0)
               / math.sqrt(discordant_total / 4.0))
    p_value = math.erfc(abs(z_score) / math.sqrt(2.0))
    if first_only_sum:
        common_odds_ratio = second_only_sum / first_only_sum
    else:
        common_odds_ratio = float("inf") if second_only_sum else 1.0
    return p_value, common_odds_ratio


# ===== Format helpers =====

def format_percent_with_count(matches, total):
    return "n/a" if not total else f"{matches / total * 100:.1f}% ({matches}/{total})"


def format_percent(matches, total):
    return "n/a" if not total else f"{matches / total * 100:.1f}%"


def format_signed(value):
    return f"{value:+.1f}"


def format_dollars(amount):
    return f"${amount:.2f}"


def display_pct(matches, total):
    return round(matches / total * 100, 1) if total else 0.0


def format_table(rows, widths, aligns, indent="  ", gap="  "):
    """Format rows as fixed-width aligned columns. aligns is a per-cell string of 'l'/'r'."""
    lines = []
    for row in rows:
        formatted = []
        for cell, width, align in zip(row, widths, aligns):
            text = str(cell)
            formatted.append(text.ljust(width) if align == "l" else text.rjust(width))
        lines.append(indent + gap.join(formatted))
    return lines


def print_table(rows, widths, aligns, indent="  ", gap="  "):
    for line in format_table(rows, widths, aligns, indent=indent, gap=gap):
        print(line)


# ===== print_* helpers =====

def print_report_header(data_dir):
    print()
    print("Paper result numbers computed from saved data")
    print()
    project_names = ", ".join(PROJECT_DISPLAY[project] for project in PROJECTS)
    condition_names = ", ".join(CONDITION_DISPLAY[condition] for condition in CONDITIONS)
    print(f"Data: {data_dir}")
    print(f"Projects: {project_names}")
    print(f"Conditions: {condition_names}")


def print_recall_by_condition(paired_verdicts):
    print()
    print("Recall by condition (per project + pooled)")
    print()
    rows = [["Project"] + [CONDITION_DISPLAY[condition] for condition in CONDITIONS]]
    for project in PROJECTS:
        rows.append([PROJECT_DISPLAY[project]] + [
            format_percent_with_count(*count_matches(paired_verdicts, condition,
                                                      project=project))
            for condition in CONDITIONS
        ])
    rows.append(["Pooled"] + [
        format_percent_with_count(*count_matches(paired_verdicts, condition))
        for condition in CONDITIONS
    ])
    print_table(rows, [9, 14, 14, 14], "lrrr")


def print_paired_comparisons_vs_generic(paired_verdicts):
    print()
    print("Paired comparisons vs Generic (gained, lost, delta in pp)")
    print()
    pooled_generic = count_matches(paired_verdicts, "generic")

    def vs_generic_rows(tuned_condition):
        rows = [["Project", "delta pp", "gained", "lost"]]
        for project in PROJECTS:
            generic_matches, generic_total = count_matches(
                paired_verdicts, "generic", project=project)
            tuned_matches, tuned_total = count_matches(
                paired_verdicts, tuned_condition, project=project)
            overlap = paired_overlap_counts(paired_verdicts, "generic",
                                              tuned_condition, project=project)
            delta_pp = (display_pct(tuned_matches, tuned_total)
                        - display_pct(generic_matches, generic_total))
            rows.append([PROJECT_DISPLAY[project],
                         format_signed(delta_pp),
                         str(overlap["second_only"]),
                         str(overlap["first_only"])])
        pooled_tuned = count_matches(paired_verdicts, tuned_condition)
        pooled_overlap = paired_overlap_counts(paired_verdicts, "generic", tuned_condition)
        pooled_delta = (display_pct(*pooled_tuned)
                        - display_pct(*pooled_generic))
        rows.append(["Pooled",
                     format_signed(pooled_delta),
                     str(pooled_overlap["second_only"]),
                     str(pooled_overlap["first_only"])])
        return rows

    for i, tuned_condition in enumerate(("tuned_100", "tuned_200")):
        if i:
            print()
        print(f"{CONDITION_DISPLAY[tuned_condition]} vs Generic:")
        print_table(vs_generic_rows(tuned_condition), [9, 8, 6, 4], "lrrr")

    print()
    rows = [["Project", "gained", "lost"]]
    for project in PROJECTS:
        overlap = paired_overlap_counts(paired_verdicts, "tuned_100", "tuned_200",
                                          project=project)
        if not overlap["paired"]:
            continue
        rows.append([PROJECT_DISPLAY[project],
                     str(overlap["second_only"]),
                     str(overlap["first_only"])])
    print("Tuned-200 vs Tuned-100 (per project):")
    print_table(rows, [9, 6, 4], "lrr")


def print_significance_tests(paired_verdicts):
    print()
    print("Statistical significance tests (CMH + McNemar exact)")
    print()
    for i, tuned_condition in enumerate(("tuned_100", "tuned_200")):
        if i:
            print()
        tuned_label = CONDITION_DISPLAY[tuned_condition]
        per_project_overlaps = [paired_overlap_counts(paired_verdicts, "generic",
                                                       tuned_condition,
                                                       project=project)
                                 for project in PROJECTS]
        per_project_overlaps = [overlap for overlap in per_project_overlaps
                                 if overlap["paired"]]
        cmh_p_value, cmh_common_odds_ratio = cmh_paired_test(per_project_overlaps)
        first_only_sum = sum(overlap["first_only"] for overlap in per_project_overlaps)
        second_only_sum = sum(overlap["second_only"] for overlap in per_project_overlaps)
        mcnemar_p = mcnemar_exact_p_value(first_only_sum, second_only_sum)

        print(f"- Pooled {tuned_label} vs Generic:")
        print(f"  - CMH p-value: {cmh_p_value:.3f}")
        print(f"  - CMH common odds ratio: {cmh_common_odds_ratio:.2f}")
        print(f"  - McNemar exact p-value: {mcnemar_p:.3f}")


def print_ensemble_recall(paired_verdicts):
    print()
    print("Ensemble recall")
    print()
    pooled_generic_matches, pooled_generic_total = count_matches(paired_verdicts,
                                                                   "generic")
    pooled_generic_pct = display_pct(pooled_generic_matches, pooled_generic_total)

    ensemble_results = {}
    rows = [["Ensemble", "Recall", "Lift"]]
    for ensemble_label, ensemble_conditions in (
        ("Generic + Tuned-100", ("generic", "tuned_100")),
        ("Generic + Tuned-200", ("generic", "tuned_200")),
        ("Generic + Tuned-100 + Tuned-200", CONDITIONS),
    ):
        ensemble_matches, ensemble_total = ensemble_recall_counts(
            paired_verdicts, ensemble_conditions)
        ensemble_results[ensemble_label] = (ensemble_matches, ensemble_total)
        delta_pp = display_pct(ensemble_matches, ensemble_total) - pooled_generic_pct
        rows.append([ensemble_label,
                     format_percent_with_count(ensemble_matches, ensemble_total),
                     f"{format_signed(delta_pp)} pp"])
    print_table(rows, [31, 15, 8], "lrr")
    print()

    incremental_pp = (display_pct(*ensemble_results["Generic + Tuned-100 + Tuned-200"])
                      - display_pct(*ensemble_results["Generic + Tuned-100"]))
    print(f"- Triple ensemble incremental lift over (Generic + Tuned-100): "
          f"{format_signed(incremental_pp)} pp")


def print_text_similarity(metrics_by_project):
    print()
    print("Text similarity averages (ROUGE-L + ChrF++)")
    print()
    pooled_similarity = {}
    for condition in CONDITIONS:
        comment_total = 0
        rouge_weighted_sum = chrf_weighted_sum = 0.0
        for project_metrics in metrics_by_project.values():
            non_llm = (((project_metrics or {}).get("non_llm_metrics") or {})
                       .get(condition) or {})
            comment_count = int(non_llm.get("n_comments", 0) or 0)
            comment_total += comment_count
            rouge_weighted_sum += comment_count * float(non_llm.get("avg_rouge_l", 0.0) or 0.0)
            chrf_weighted_sum += comment_count * float(non_llm.get("avg_chrf", 0.0) or 0.0)
        if comment_total:
            pooled_similarity[condition] = (rouge_weighted_sum / comment_total,
                                            chrf_weighted_sum / comment_total)
        else:
            pooled_similarity[condition] = (0.0, 0.0)

    def cell(metrics_block, key):
        return f"{float(metrics_block.get(key, 0.0) or 0.0):.3f}"

    for i, (metric_label, metric_key) in enumerate((("ROUGE-L (avg)", "avg_rouge_l"),
                                                     ("ChrF++ (avg)", "avg_chrf"))):
        if i:
            print()
        rows = [["Project"] + [CONDITION_DISPLAY[condition] for condition in CONDITIONS]]
        for project in PROJECTS:
            row = [PROJECT_DISPLAY[project]]
            for condition in CONDITIONS:
                block = (((metrics_by_project.get(project) or {})
                          .get("non_llm_metrics") or {}).get(condition, {}))
                row.append(cell(block, metric_key))
            rows.append(row)
        rows.append(["Pooled"] + [
            f"{pooled_similarity[condition][i]:.3f}"
            for condition in CONDITIONS
        ])
        print(f"{metric_label}:")
        print_table(rows, [9, 7, 9, 9], "lrrr")


def print_per_category_recall(metrics_by_project):
    print()
    print("Per-category recall (n>=5 pooled)")
    print()
    print("Cells show recall % (matches); n is the pooled denominator.")
    print()
    pooled_per_subcategory = {}
    for project in PROJECTS:
        per_category = (metrics_by_project.get(project) or {}).get("per_category_recall", {})
        for condition in CONDITIONS:
            for subcategory_name, stats in per_category.get(condition, {}).items():
                bucket = pooled_per_subcategory.setdefault(
                    (subcategory_name, condition), [0, 0])
                bucket[0] += int(stats.get("matches", 0) or 0)
                bucket[1] += int(stats.get("total", 0) or 0)

    subcategory_max_total = {}
    for (subcategory_name, _), (_, total) in pooled_per_subcategory.items():
        subcategory_max_total[subcategory_name] = max(
            subcategory_max_total.get(subcategory_name, 0), total)
    qualifying_subcategories = sorted(
        [(name, total) for name, total in subcategory_max_total.items() if total >= 5],
        key=lambda name_total: (-name_total[1], name_total[0]))

    rows = [["Sub-category", "n"] + [CONDITION_DISPLAY[condition]
                                       for condition in CONDITIONS]]
    for subcategory_name, _ in qualifying_subcategories:
        n = subcategory_max_total[subcategory_name]
        cells = [subcategory_name, str(n)]
        for condition in CONDITIONS:
            matches, total = pooled_per_subcategory.get(
                (subcategory_name, condition), [0, 0])
            cells.append(
                f"{matches/total*100:.1f}% ({matches})" if total else "n/a")
        rows.append(cells)
    print_table(rows, [37, 2, 10, 10, 10], "lrrrr")


def print_rule_counts_and_categorization(rule_counts_by_volume,
                                          categorization_axes, subcategory_counts):
    print()
    print("Rule counts and categorization axes")
    print()

    pooled_subcategory_total = sum(subcategory_counts.values())
    print(f"- Pooled Tuned-100 rule count (sum of sub-categories): "
          f"{pooled_subcategory_total}")
    print()

    print("Top sub-categories by pooled count (Tuned-100; share = count")
    print("as percent of pooled total).")
    sorted_subcategories = sorted(subcategory_counts.items(),
                                    key=lambda item: (-item[1], item[0]))
    for subcategory_name, count in sorted_subcategories[:10]:
        share_pct = (count / pooled_subcategory_total * 100
                      if pooled_subcategory_total else 0.0)
        print(f"- {subcategory_name}: {count} ({share_pct:.1f}%)")
    if pooled_subcategory_total:
        top10_total = sum(count for _, count in sorted_subcategories[:10])
        print(f"- Top 10 sub-categories pooled share: "
              f"{top10_total / pooled_subcategory_total * 100:.1f}%")
    print()

    print("Per-project rule counts by snapshot volume")
    print("(Tuned-100 = most recent 100 tuning MRs, Tuned-200 = all 200).")
    print()
    for i, volume in enumerate((100, 200)):
        if i:
            print()
        rows = [["Dimension"] + [PROJECT_DISPLAY[project] for project in PROJECTS]]
        total_row = ["Total"]
        for project in PROJECTS:
            observed = rule_counts_by_volume.get(project, {}).get(volume) or {}
            total_row.append(str(sum(observed.values())))
        rows.append(total_row)
        for dimension in DIMENSIONS:
            row = [dimension]
            for project in PROJECTS:
                observed = rule_counts_by_volume.get(project, {}).get(volume) or {}
                row.append(str(observed.get(dimension, 0)))
            rows.append(row)
        print(f"Tuned-{volume}:")
        print_table(rows, [31, 9, 7, 3], "lrrr")
    print()

    def axis_value(dimension, axis_key):
        return sum(categorization_axes.get(project, {})
                   .get(dimension, {}).get(axis_key, 0)
                   for project in PROJECTS)

    pooled_axes = {key: sum(axis_value(dim, key) for dim in DIMENSIONS)
                   for key in CATEGORIZATION_AXIS_KEYS}

    rows = [["Dimension", "n", "Lintable", "Partial", "Requires-LLM"]]
    for dimension in DIMENSIONS:
        rows.append([dimension,
                     str(axis_value(dimension, "n")),
                     str(axis_value(dimension, "lintable")),
                     str(axis_value(dimension, "partial")),
                     str(axis_value(dimension, "requires_llm"))])
    print("Tuned-100 categorization, lintability axis:")
    print_table(rows, [31, 3, 8, 7, 12], "lrrrr")
    print()

    rows = [["Dimension", "n", "Project-specific", "Generalizable"]]
    for dimension in DIMENSIONS:
        rows.append([dimension,
                     str(axis_value(dimension, "n")),
                     str(axis_value(dimension, "project_specific")),
                     str(axis_value(dimension, "generalizable"))])
    print("Tuned-100 categorization, generalizability axis:")
    print_table(rows, [31, 3, 16, 13], "lrrr")
    print()

    pooled_n = pooled_axes["n"]
    print(f"- Pooled categorization (n={pooled_n}):")
    for axis_key, share_label in (
        ("lintable", "Lintable"),
        ("partial", "Partially-lintable"),
        ("requires_llm", "Requires-LLM"),
        ("project_specific", "Project-specific"),
        ("generalizable", "Generalizable"),
    ):
        count = pooled_axes[axis_key]
        share_pct = count / pooled_n * 100 if pooled_n else 0.0
        print(f"  - {share_label}: {count} ({share_pct:.1f}%)")
    print()

    print("- Requires-LLM share by dimension:")
    for dimension in DIMENSIONS:
        dim_total = axis_value(dimension, "n")
        dim_requires_llm = axis_value(dimension, "requires_llm")
        share_pct = dim_requires_llm / dim_total * 100 if dim_total else 0.0
        print(f"  - {dimension}: {share_pct:.1f}% "
              f"({dim_requires_llm}/{dim_total})")


def print_pipeline_costs(review_judge_costs, metadata_costs):
    print()
    print("Pipeline cost (USD):")
    print()

    def step_cost(step, project):
        if step in ("review", "judge"):
            return float(review_judge_costs[project][step])
        observed = float(metadata_costs.get(project, {}).get(step, 0.0) or 0.0)
        if step == "filter":
            observed += FILTER_STAGE3_BACKFILL.get(project, 0.0)
        return observed

    rows = [["Step"] + [PROJECT_DISPLAY[project] for project in PROJECTS]
            + ["Combined"]]
    for step in PIPELINE_STEPS:
        per_project_cells = [format_dollars(step_cost(step, project))
                              for project in PROJECTS]
        combined_cell = format_dollars(sum(step_cost(step, project)
                                            for project in PROJECTS))
        rows.append([PIPELINE_STEP_DISPLAY[step]] + per_project_cells
                    + [combined_cell])
    project_totals = [sum(step_cost(step, project) for step in PIPELINE_STEPS)
                       for project in PROJECTS]
    grand_total = sum(project_totals)
    rows.append(["Total"]
                + [format_dollars(total) for total in project_totals]
                + [format_dollars(grand_total)])
    print_table(rows, [16, 9, 7, 6, 8], "lrrrr")
    print()
    print("(Storybook + Mermaid Filter (Stage 3) costs are back-estimated)")

    print()

    review_judge_combined = sum(review_judge_costs[project]["review"]
                                 + review_judge_costs[project]["judge"]
                                 for project in PROJECTS)
    review_judge_share_pct = (review_judge_combined / grand_total * 100
                              if grand_total else 0.0)
    per_pass_cost = review_judge_combined / 3.0
    print("- Reviewer + Judge:")
    print(f"  - combined: {format_dollars(review_judge_combined)}")
    print(f"  - share of pipeline total: {review_judge_share_pct:.1f}%")
    print(f"  - per-condition pass cost (combined / 3 conditions): "
          f"{format_dollars(per_pass_cost)}")


def print_test_setup(test_set_counts):
    print()
    print("Test setup (held-out PRs and test revisions)")
    print()
    rows = [["Project", "Held-out PRs", "Test revisions"]]
    for project in PROJECTS:
        prs, revs = test_set_counts.get(project, (0, 0))
        rows.append([PROJECT_DISPLAY[project], str(prs), str(revs)])
    print_table(rows, [9, 12, 14], "lrr")


def print_judge_validation(data_dir, same_line_counts):
    print()
    print("Judge validation (rater-vs-judge agreement, same-file-and-line audit)")
    print()

    agreement = load_json(data_dir / "judge_validation" / "agreement.json") or {}
    pooled = agreement.get("pooled", {})
    pooled_total = int(pooled.get("n", 0) or 0)
    pooled_agreements = int(pooled.get("agreements", 0) or 0)
    pooled_kappa = float(pooled.get("kappa", 0.0) or 0.0)

    by_judge_class = {"match": [0, 0], "no_match": [0, 0]}
    for stratum_key, stratum_stats in (agreement.get("per_stratum") or {}).items():
        if not isinstance(stratum_stats, dict) or "/" not in stratum_key:
            continue
        _, judge_class = stratum_key.split("/", 1)
        if judge_class not in by_judge_class:
            continue
        by_judge_class[judge_class][0] += int(stratum_stats.get("agreements", 0) or 0)
        by_judge_class[judge_class][1] += int(stratum_stats.get("labeled", 0) or 0)

    print("- Pooled rater-vs-judge agreement:")
    print(f"  - overall: {pooled_agreements}/{pooled_total} = "
          f"{format_percent(pooled_agreements, pooled_total)}")
    print(f"  - Cohen's kappa: {pooled_kappa:.3f}")
    print(f"  - when judge said match: "
          f"{by_judge_class['match'][0]}/{by_judge_class['match'][1]}")
    print(f"  - when judge said no_match: "
          f"{by_judge_class['no_match'][0]}/{by_judge_class['no_match'][1]}")
    print()

    print("Same-file-and-line audit: judge no_match verdicts whose human comment shares")
    print("(file, line) with any AI inline comment, pooled across projects x conditions.")
    pooled_same_line = pooled_no_match = 0
    rows = [["Project", "same file+line", "all no_match"]]
    for project in PROJECTS:
        same_line, no_match = same_line_counts.get(project, (0, 0))
        pooled_same_line += same_line
        pooled_no_match += no_match
        rows.append([PROJECT_DISPLAY[project], str(same_line), str(no_match)])
    rows.append(["Pooled", str(pooled_same_line), str(pooled_no_match)])
    print_table(rows, [9, 14, 12], "lrr")
    print(f"- Pooled same file+line / all no_match: "
          f"{format_percent(pooled_same_line, pooled_no_match)}")


if __name__ == "__main__":
    sys.exit(main())
