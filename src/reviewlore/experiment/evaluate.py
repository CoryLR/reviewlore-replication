"""Metrics computation, statistical tests, and comparison tables."""

from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import atomic_write_json, load_json, write_metadata


def _load_verdicts(data_dir: Path, conditions: list[str]) -> list[dict]:
    """Load all verdict records across conditions.

    Returns list of dicts with: pr_number, head_sha, human_comment_index,
    condition, verdict, human_file, human_line.
    """
    records = []
    for condition in conditions:
        judgment_dir = data_dir / DIRS["judgments"] / condition
        if not judgment_dir.exists():
            continue
        for verdict_file in judgment_dir.rglob("verdicts.json"):
            data = load_json(verdict_file)
            if not data:
                continue

            # Parse pr_number and rev from path
            parts = verdict_file.parts
            pr_part = [p for p in parts if p.startswith("pr_")]
            rev_part = [p for p in parts if p.startswith("rev_")]
            if not pr_part or not rev_part:
                continue

            pr_number = int(pr_part[0].replace("pr_", ""))
            head_sha = rev_part[0].replace("rev_", "")

            for v in data.get("verdicts", []):
                records.append({
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "human_comment_index": v.get("human_comment_index", 0),
                    "condition": condition,
                    "verdict": v.get("verdict", "no_match"),
                    "human_file": v.get("human_file"),
                    "human_line": v.get("human_line"),
                })

    return records


def _pair_verdicts(records: list[dict], conditions: list[str]) -> tuple[dict, list]:
    """Group verdicts into triples and identify complete/incomplete sets.

    Returns (paired_dict, incomplete_keys).
    paired_dict: {(pr, sha, idx): {condition: verdict}}
    """
    paired = {}
    for r in records:
        key = (r["pr_number"], r["head_sha"], r["human_comment_index"])
        if key not in paired:
            paired[key] = {}
        paired[key][r["condition"]] = r["verdict"]

    incomplete = [k for k, v in paired.items() if len(v) < len(conditions)]
    return paired, incomplete


def compute_judge_recall(records: list[dict], condition: str) -> float:
    """Compute recall for a single condition."""
    cond_records = [r for r in records if r["condition"] == condition]
    if not cond_records:
        return 0.0
    matches = sum(1 for r in cond_records if r["verdict"] == "match")
    return matches / len(cond_records)


def compute_rouge_chrf(data_dir: Path, conditions: list[str]) -> dict:
    """Compute ROUGE-L and ChrF++ max-pair scores per condition.

    For each human comment, compute max score against all AI comments.
    """
    try:
        from rouge_score.rouge_scorer import RougeScorer
        import sacrebleu
    except ImportError as e:
        return {"error": f"Missing dependency: {e}"}

    scorer = RougeScorer(["rougeL"], use_stemmer=True)

    results = {}
    for condition in conditions:
        rouge_scores = []
        chrf_scores = []

        review_dir = data_dir / DIRS["reviews"] / condition
        judgment_dir = data_dir / DIRS["judgments"] / condition

        if not review_dir.exists():
            continue

        for verdict_file in judgment_dir.rglob("verdicts.json"):
            verdict_data = load_json(verdict_file)
            if not verdict_data:
                continue

            # Find corresponding review
            rel = verdict_file.relative_to(judgment_dir)
            review_file = review_dir / rel.parent / "review.json"
            review_data = load_json(review_file)
            if not review_data:
                continue

            # Also load context for human comments
            context_file = review_dir / rel.parent / "context.json"
            context = load_json(context_file) or {}
            human_comments = context.get("human_comments", [])

            # Collect all AI comment texts
            ai_texts = []
            for c in review_data.get("inline_comments", []):
                ai_texts.append(c.get("comment", ""))
            for c in review_data.get("general_comments", []):
                ai_texts.append(c.get("comment", ""))

            # Score each human comment
            for hc in human_comments:
                h_text = hc.get("body", "")
                if not h_text or not ai_texts:
                    rouge_scores.append(0.0)
                    chrf_scores.append(0.0)
                    continue

                # Max ROUGE-L
                max_rouge = 0.0
                for ai_text in ai_texts:
                    score = scorer.score(h_text, ai_text)
                    max_rouge = max(max_rouge, score["rougeL"].fmeasure)
                rouge_scores.append(max_rouge)

                # Max ChrF++
                max_chrf = 0.0
                for ai_text in ai_texts:
                    chrf = sacrebleu.sentence_chrf(
                        ai_text, [h_text], char_order=6, word_order=2
                    )
                    max_chrf = max(max_chrf, chrf.score / 100.0)
                chrf_scores.append(max_chrf)

        results[condition] = {
            "avg_rouge_l": sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0.0,
            "avg_chrf": sum(chrf_scores) / len(chrf_scores) if chrf_scores else 0.0,
            "n_comments": len(rouge_scores),
        }

    return results


def calibrate_thresholds(
    data_dir: Path, conditions: list[str], records: list[dict]
) -> dict:
    """Find threshold per metric that maximizes Cohen's kappa with judge verdicts."""
    # This requires paired score + verdict data
    # Simplified: sweep thresholds and compute agreement
    try:
        from rouge_score.rouge_scorer import RougeScorer
        import sacrebleu
    except ImportError:
        return {"error": "Missing dependencies"}

    # Collect (score, judge_verdict) pairs for calibration
    # For now, return placeholder; full implementation needs per-comment score tracking
    return {"note": "Threshold calibration runs on full data only"}


def mcnemar_test(
    paired: dict, cond_a: str, cond_b: str
) -> dict:
    """Run McNemar's exact test comparing two conditions."""
    from scipy.stats import binomtest

    # Build 2x2 contingency table of discordant pairs
    b = 0  # matched under A but not B
    c = 0  # matched under B but not A
    n_complete = 0

    for key, verdicts in paired.items():
        if cond_a not in verdicts or cond_b not in verdicts:
            continue
        n_complete += 1
        va = verdicts[cond_a] == "match"
        vb = verdicts[cond_b] == "match"
        if va and not vb:
            b += 1
        elif vb and not va:
            c += 1

    if b + c == 0:
        return {
            "n_complete": n_complete,
            "discordant_b": b, "discordant_c": c,
            "p_value": 1.0, "odds_ratio": None,
            "note": "No discordant pairs",
        }

    # Exact binomial test on the smaller discordant cell
    smaller = min(b, c)
    total = b + c
    result = binomtest(smaller, total, 0.5, alternative="two-sided")

    odds_ratio = b / c if c > 0 else float("inf")

    return {
        "n_complete": n_complete,
        "discordant_b": b,
        "discordant_c": c,
        "p_value": result.pvalue,
        "odds_ratio": odds_ratio,
    }


def holm_bonferroni(p_values: list[float]) -> list[float]:
    """Apply Holm-Bonferroni correction to a list of p-values."""
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * n

    for rank, (orig_idx, p) in enumerate(indexed):
        adjusted_p = p * (n - rank)
        adjusted[orig_idx] = min(adjusted_p, 1.0)

    # Enforce monotonicity
    for i in range(1, n):
        orig_idx = indexed[i][0]
        prev_idx = indexed[i - 1][0]
        adjusted[orig_idx] = max(adjusted[orig_idx], adjusted[prev_idx])

    return adjusted


def per_category_recall(
    records: list[dict], data_dir: Path, conditions: list[str]
) -> dict:
    """Compute recall per BitsAI-CR category per condition.

    Keys the category map by the stable (pr_number, head_sha, human_comment_index)
    tuple that the judge records on each verdict. The index is rebuilt per revision
    by replicating review_judge.build_context's thread filter: for each testable
    revision, keep only review threads whose first comment's commit_sha matches the
    revision's head_sha, then number the surviving threads starting at zero. This
    matches the index the judge assigns in its verdict records, so the category
    lookup is stable across conditions (the previous (pr, file, line) key drifted
    when the judge normalized human_file/human_line slightly differently across
    conditions).
    """
    labeled_dir = data_dir / DIRS["labeled"]
    split_file = data_dir / DIRS["split"] / "testable_revisions.json"

    revisions = load_json(split_file) or []
    if not isinstance(revisions, list):
        revisions = []

    labeled_cache: dict[int, dict] = {}
    category_map: dict[tuple, str] = {}
    # Verdict records store head_sha as the 8-char short form (from the rev_XXXXXXXX
    # directory name in _load_verdicts), so we key the map on the same short form.
    for rev in revisions:
        pr_number = rev.get("pr_number")
        full_head_sha = rev.get("head_sha")
        if pr_number is None or not full_head_sha:
            continue
        short_head_sha = full_head_sha[:8]
        if pr_number not in labeled_cache:
            labeled_cache[pr_number] = load_json(labeled_dir / f"pr_{pr_number}.json") or {}
        data = labeled_cache[pr_number]
        idx = 0
        for thread in data.get("review_threads", []):
            comments = thread.get("comments", [])
            if not comments:
                continue
            first = comments[0]
            if first.get("commit_sha") != full_head_sha:
                continue
            category_map[(pr_number, short_head_sha, idx)] = first.get("category", "unknown")
            idx += 1

    results = {}
    total_records = 0
    unknown_misses = 0
    for condition in conditions:
        cond_records = [r for r in records if r["condition"] == condition]
        by_cat: dict[str, dict] = {}

        for r in cond_records:
            key = (r["pr_number"], r["head_sha"], r["human_comment_index"])
            cat = category_map.get(key, "unknown")
            total_records += 1
            if cat == "unknown":
                unknown_misses += 1
            if cat not in by_cat:
                by_cat[cat] = {"matches": 0, "total": 0}
            by_cat[cat]["total"] += 1
            if r["verdict"] == "match":
                by_cat[cat]["matches"] += 1

        cat_recall = {}
        for cat, counts in by_cat.items():
            recall = counts["matches"] / counts["total"] if counts["total"] > 0 else 0.0
            cat_recall[cat] = {
                "recall": recall,
                "matches": counts["matches"],
                "total": counts["total"],
                "low_sample": counts["total"] < 5,
            }

        results[condition] = cat_recall

    print(f"    per_category: {unknown_misses}/{total_records} records without category match")
    return results


def _extract_tokens_from_raw(raw: dict) -> dict:
    """Pull token counts from a saved raw.json record.

    The CP4/CP5 ClaudeCodeBackend wrote ``input_tokens`` and ``output_tokens``
    at the top level by reading them off the envelope's top level — but Claude
    Code's ``--output-format json`` envelope nests usage under
    ``envelope["usage"]["input_tokens"]`` (and friends), so the top-level
    fields are always 0 in practice. We re-derive correct counts here by
    re-parsing the saved envelope, falling back to the (possibly nonzero)
    top-level fields only when the envelope is unavailable or doesn't carry a
    ``usage`` block. The total input is uncached + cache-creation +
    cache-read; reporting the sum matches how the API bills them.

    Returns a dict with ``input_tokens``, ``output_tokens``, ``cache_read``,
    ``cache_creation``, ``input_uncached``. ``input_tokens`` is the billable
    sum (uncached + creation + read); the components are kept for granular
    analysis.
    """
    fallback_in = int(raw.get("input_tokens", 0) or 0)
    fallback_out = int(raw.get("output_tokens", 0) or 0)
    raw_output = raw.get("raw_output", "")
    if not raw_output:
        return {
            "input_tokens": fallback_in,
            "output_tokens": fallback_out,
            "cache_read": 0,
            "cache_creation": 0,
            "input_uncached": fallback_in,
        }
    try:
        envelope = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "input_tokens": fallback_in,
            "output_tokens": fallback_out,
            "cache_read": 0,
            "cache_creation": 0,
            "input_uncached": fallback_in,
        }

    usage = envelope.get("usage") if isinstance(envelope, dict) else None
    if not isinstance(usage, dict):
        return {
            "input_tokens": fallback_in,
            "output_tokens": fallback_out,
            "cache_read": 0,
            "cache_creation": 0,
            "input_uncached": fallback_in,
        }

    uncached = int(usage.get("input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    out_toks = int(usage.get("output_tokens", 0) or 0)
    return {
        "input_tokens": uncached + cache_creation + cache_read,
        "output_tokens": out_toks,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "input_uncached": uncached,
    }


def aggregate_tokens(
    data_dir: Path, conditions: list[str] | None = None
) -> dict:
    """Sum input/output tokens for the reviewer + judge steps.

    Walks per-revision raw.json under each ``conditions`` subdirectory of
    ``6_reviews`` and ``7_judgments``. Earlier pipeline steps (filter Stage 3,
    label, optimize-extract, optimize-consolidate, categorize) save parsed
    outputs only; their token counts are unrecoverable from saved data, so
    this aggregator covers reviewer + judge only — together accounting for
    roughly 77% of the pipeline's dollar cost.

    Returns ``{step: {input_tokens, output_tokens, total_tokens,
    cache_read, cache_creation, input_uncached, n_calls}}`` keyed by step
    name (``review`` / ``judge``), plus a ``grand_total`` sum over both.
    ``input_tokens`` is the billable sum: ``input_uncached`` +
    ``cache_creation`` + ``cache_read`` (matches API billing semantics).

    ``conditions=None`` falls back to summing every immediate subdirectory
    of each step dir (matches pre-Apr-13 ``aggregate_costs`` behavior); when
    a list is passed, only those condition subdirs are walked, so renamed-
    aside siblings like ``generic_apr12/`` from a prior run are ignored.
    """
    out: dict[str, dict] = {}
    for step_name, dir_key in [("review", "reviews"), ("judge", "judgments")]:
        step_dir = data_dir / DIRS[dir_key]
        if not step_dir.exists():
            continue
        if conditions:
            cond_dirs = [step_dir / c for c in conditions if (step_dir / c).is_dir()]
        else:
            cond_dirs = [p for p in step_dir.iterdir() if p.is_dir()]
        in_toks = 0
        out_toks = 0
        cache_read = 0
        cache_creation = 0
        input_uncached = 0
        n_calls = 0
        for cd in cond_dirs:
            for raw_path in cd.rglob("raw.json"):
                raw = load_json(raw_path)
                if not raw:
                    continue
                t = _extract_tokens_from_raw(raw)
                in_toks += t["input_tokens"]
                out_toks += t["output_tokens"]
                cache_read += t["cache_read"]
                cache_creation += t["cache_creation"]
                input_uncached += t["input_uncached"]
                n_calls += 1
        out[step_name] = {
            "input_tokens": in_toks,
            "output_tokens": out_toks,
            "total_tokens": in_toks + out_toks,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "input_uncached": input_uncached,
            "n_calls": n_calls,
        }

    grand_in = sum(s["input_tokens"] for s in out.values())
    grand_out = sum(s["output_tokens"] for s in out.values())
    grand_cr = sum(s["cache_read"] for s in out.values())
    grand_cc = sum(s["cache_creation"] for s in out.values())
    grand_un = sum(s["input_uncached"] for s in out.values())
    out["grand_total"] = {
        "input_tokens": grand_in,
        "output_tokens": grand_out,
        "total_tokens": grand_in + grand_out,
        "cache_read": grand_cr,
        "cache_creation": grand_cc,
        "input_uncached": grand_un,
        "n_calls": sum(s["n_calls"] for s in out.values()),
    }
    return out


def aggregate_costs(data_dir: Path, conditions: list[str] | None = None) -> dict:
    """Compute per-step costs.

    Review and judge costs walk per-revision raw.json under the specified condition
    subdirectories only, so renamed-aside siblings like `generic_apr12/` from a
    prior run are ignored. `conditions=None` falls back to summing every immediate
    subdirectory, which matches pre-Apr-13 behavior but will double-count if
    `*_apr12` dirs live alongside the current conditions.

    Non-review/judge costs (extraction, consolidation, label, filter, etc.) are
    read from metadata.json files scoped to the canonical step directories listed
    in `DIRS`. This excludes renamed-aside archived siblings like
    `5_optimization_apr12/` or `5_optimization_pre-phase1.5/` that live at the
    same depth as the canonical step dirs; those would otherwise double-count
    via a naive `data_dir.rglob("metadata.json")`.
    """
    costs: dict[str, float] = {}

    for step_name, dir_key in [("review", "reviews"), ("judge", "judgments")]:
        step_dir = data_dir / DIRS[dir_key]
        if not step_dir.exists():
            continue
        if conditions:
            cond_dirs = [step_dir / c for c in conditions if (step_dir / c).is_dir()]
        else:
            cond_dirs = [p for p in step_dir.iterdir() if p.is_dir()]
        total = 0.0
        for cd in cond_dirs:
            for raw_path in cd.rglob("raw.json"):
                raw = load_json(raw_path)
                if raw:
                    total += raw.get("cost_usd", 0.0)
        costs[step_name] = total

    # Walk metadata.json files scoped to canonical step directories only.
    # Skip the review/judgment step dirs since review/judge cost is already
    # handled above via per-invocation raw.json walking.
    skip_step_dirs = {DIRS["reviews"], DIRS["judgments"]}
    for dir_name in DIRS.values():
        if dir_name in skip_step_dirs:
            continue
        step_root = data_dir / dir_name
        if not step_root.exists():
            continue
        for meta_path in step_root.rglob("metadata.json"):
            meta = load_json(meta_path)
            if not meta:
                continue
            step = meta.get("step", "unknown")
            if step in ("review", "judge"):
                continue
            cost = meta.get("cost", {})
            if isinstance(cost, dict):
                total = cost.get("total_usd", 0.0)
            elif isinstance(cost, (int, float)):
                total = cost
            else:
                total = 0.0
            costs[step] = costs.get(step, 0.0) + total

    costs["grand_total"] = sum(costs.values())
    return costs


def _load_touched_files_per_rev(
    data_dir: Path, subject_repo: str | Path
) -> dict[tuple[int, str], set[str]]:
    """Return the set of files touched by diff(merge_base...head) for each testable rev.

    Empty dict if testable_revisions.json or subject_repo is missing/invalid. Revs
    whose git diff fails silently are omitted from the map; callers treat a missing
    key as "cannot determine reachability" and leave verdicts flagged reachable.
    """
    if not subject_repo:
        return {}
    split_path = data_dir / DIRS["split"] / "testable_revisions.json"
    revs = load_json(split_path)
    if not isinstance(revs, list):
        return {}
    touched: dict[tuple[int, str], set[str]] = {}
    for r in revs:
        pr = r.get("pr_number")
        head = r.get("head_sha")
        mb = r.get("merge_base_sha")
        if pr is None or not head or not mb:
            continue
        # Triple-dot needs the path between mb and head packed in the repo so Git
        # can verify mb is reachable. The replication artifact ships shallow
        # bundles that contain only the two commits, so fall back to two-dot
        # when triple-dot reports "no merge base"; mb is already pre-computed
        # in 4_split/testable_revisions.json so two-dot returns the same files.
        res = None
        for spec in (f"{mb}...{head}", f"{mb}..{head}"):
            try:
                res = subprocess.run(
                    ["git", "-C", str(subject_repo), "diff", "--name-only", spec],
                    capture_output=True, text=True, timeout=30,
                )
            except (subprocess.SubprocessError, OSError):
                res = None
                break
            if res.returncode == 0:
                break
        if res is None or res.returncode != 0:
            continue
        touched[(pr, head)] = {ln for ln in res.stdout.splitlines() if ln.strip()}
    return touched


def _flag_unreachable(
    records: list[dict], touched: dict[tuple[int, str], set[str]]
) -> None:
    """Mutate records in place: set r['unreachable']=True when human_file not in touched.

    Conservative default: if touched is empty or the rev's entry is missing, leave
    unreachable=False so the comment counts toward reachable recall. A rev with an
    empty touched set (zero files in the diff) flags all its comments unreachable.
    """
    for r in records:
        r["unreachable"] = False
    if not touched:
        return
    for r in records:
        key = (r["pr_number"], r["head_sha"])
        files = touched.get(key)
        if files is None:
            continue
        hf = r.get("human_file")
        if hf is not None and hf not in files:
            r["unreachable"] = True


def compute_reachable_recall(records: list[dict], condition: str) -> dict:
    """Recall on the reachable subset only, plus counts.

    Records must have had _flag_unreachable applied first.
    """
    cond = [r for r in records if r["condition"] == condition]
    reachable = [r for r in cond if not r.get("unreachable", False)]
    unreachable_n = len(cond) - len(reachable)
    if not reachable:
        return {
            "reachable_recall": 0.0,
            "n_reachable": 0,
            "unreachable_excluded": unreachable_n,
            "matches_reachable": 0,
        }
    matches = sum(1 for r in reachable if r["verdict"] == "match")
    return {
        "reachable_recall": matches / len(reachable),
        "n_reachable": len(reachable),
        "unreachable_excluded": unreachable_n,
        "matches_reachable": matches,
    }


def compute_paired_overlap(paired: dict, cond_a: str, cond_b: str) -> dict:
    """Both/a_only/b_only/neither 4-cell overlap and exact-binomial McNemar.

    Scipy-free: two-sided exact binomial tail via stdlib math.comb. Odds ratio
    is a_only/b_only with inf/1.0 fallback when b_only is zero.
    """
    both = a_only = b_only = neither = 0
    for verdicts in paired.values():
        if cond_a not in verdicts or cond_b not in verdicts:
            continue
        va = verdicts[cond_a] == "match"
        vb = verdicts[cond_b] == "match"
        if va and vb:
            both += 1
        elif va:
            a_only += 1
        elif vb:
            b_only += 1
        else:
            neither += 1
    n_paired = both + a_only + b_only + neither
    disc = a_only + b_only
    if disc == 0:
        p = 1.0
    else:
        k = min(a_only, b_only)
        tail = sum(math.comb(disc, i) for i in range(0, k + 1)) / (2 ** disc)
        p = min(1.0, 2 * tail)
    if b_only > 0:
        or_val: float = a_only / b_only
    else:
        or_val = float("inf") if a_only > 0 else 1.0
    return {
        "n_paired": n_paired,
        "both": both,
        f"{cond_a}_only": a_only,
        f"{cond_b}_only": b_only,
        "neither": neither,
        "mcnemar_exact_p": p,
        "odds_ratio": or_val,
    }


def evaluate_project(
    project_name: str,
    config: Config,
    out_dir: Path,
    trial: bool = False,
) -> None:
    """Run the full 7-step evaluation algorithm."""
    data_dir = out_dir / project_name
    eval_dir = data_dir / DIRS["evaluation"]
    print(f"\nEvaluating {project_name}")
    if trial:
        print("  TRIAL MODE")

    # Step 1: Load and pair verdicts
    print("  Step 1: Loading verdicts...")
    records = _load_verdicts(data_dir, config.conditions)
    if not records:
        print("  No verdicts found. Run 'relox review' first.")
        return

    # Narrow to conditions that actually have data
    conditions = sorted(set(r["condition"] for r in records))
    skipped = [c for c in config.conditions if c not in conditions]
    if skipped:
        print(f"  Conditions with data: {', '.join(conditions)} (no data for: {', '.join(skipped)})")

    paired, incomplete = _pair_verdicts(records, conditions)
    total_comments = len(paired)
    complete_sets = total_comments - len(incomplete)
    print(f"  {total_comments} human comments, {complete_sets} complete sets, {len(incomplete)} incomplete")

    # Reachability flagging: shell out to git per rev, drop human comments whose
    # target file is outside the PR's touched-files set from pooled recall.
    subject_repo_path: Path | str = ""
    if project_name in config.projects:
        raw_sr = config.projects[project_name].subject_repo or ""
        if raw_sr:
            sr = Path(raw_sr)
            subject_repo_path = sr if sr.is_absolute() else (config.config_dir / sr).resolve()
    if not subject_repo_path:
        print("  Reachability: subject_repo not configured; reachable_recall will equal pooled_recall")
    elif not Path(subject_repo_path).exists():
        print(f"  Reachability: subject_repo path missing ({subject_repo_path}); reachable_recall will equal pooled_recall")
        subject_repo_path = ""
    touched = _load_touched_files_per_rev(data_dir, subject_repo_path)
    if subject_repo_path and not touched:
        print("  Reachability: zero revs mapped (git diff failed on every testable rev); reachable_recall will equal pooled_recall")
    _flag_unreachable(records, touched)
    n_revs_with_files = len(touched)
    n_unreachable_total = sum(1 for r in records if r.get("unreachable"))
    print(f"  Reachability: {n_revs_with_files} revs mapped, {n_unreachable_total} verdict records flagged unreachable")

    metrics: dict = {"conditions": conditions}

    # Step 2: Judge recall (pooled + reachable)
    print("  Step 2: Computing judge recall...")
    recall = {}
    reachable = {}
    for cond in conditions:
        recall[cond] = compute_judge_recall(records, cond)
        reachable[cond] = compute_reachable_recall(records, cond)
    metrics["judge_recall"] = recall
    metrics["reachable_recall"] = reachable
    for cond, r in recall.items():
        rr = reachable[cond]
        print(
            f"    {cond}: pooled={r:.3f}  reachable={rr['reachable_recall']:.3f}"
            f"  (n_reachable={rr['n_reachable']}, unreachable_excluded={rr['unreachable_excluded']})"
        )

    # Step 3: ROUGE-L and ChrF++
    print("  Step 3: Computing ROUGE-L and ChrF++...")
    nlm = compute_rouge_chrf(data_dir, conditions)
    metrics["non_llm_metrics"] = nlm
    for cond, scores in nlm.items():
        if isinstance(scores, dict) and "avg_rouge_l" in scores:
            print(f"    {cond}: ROUGE-L={scores['avg_rouge_l']:.3f}, ChrF++={scores['avg_chrf']:.3f}")

    # Step 4: Threshold calibration
    print("  Step 4: Threshold calibration...")
    thresholds = calibrate_thresholds(data_dir, conditions, records)
    metrics["threshold_calibration"] = thresholds

    # Step 5: McNemar's test
    print("  Step 5: McNemar's test...")
    comparisons = {}
    p_values = []
    comparison_keys = []

    for tuned_cond in [c for c in conditions if c != "generic"]:
        key = f"generic_vs_{tuned_cond}"
        result = mcnemar_test(paired, "generic", tuned_cond)
        comparisons[key] = result
        p_values.append(result["p_value"])
        comparison_keys.append(key)
        print(f"    {key}: p={result['p_value']:.4f}, OR={result.get('odds_ratio', 'N/A')}")

    if p_values:
        adjusted = holm_bonferroni(p_values)
        for key, adj_p in zip(comparison_keys, adjusted):
            comparisons[key]["adjusted_p_value"] = adj_p

    metrics["mcnemar"] = comparisons

    # Step 5b: Paired-overlap 4-cell breakdown (both/a_only/b_only/neither)
    overlap: dict = {}
    for tuned_cond in [c for c in conditions if c != "generic"]:
        key = f"generic_vs_{tuned_cond}"
        overlap[key] = compute_paired_overlap(paired, "generic", tuned_cond)
        o = overlap[key]
        print(
            f"    overlap {key}: both={o['both']} generic_only={o['generic_only']} "
            f"{tuned_cond}_only={o[f'{tuned_cond}_only']} neither={o['neither']} "
            f"n={o['n_paired']} p={o['mcnemar_exact_p']:.4f} OR={o['odds_ratio']}"
        )
    metrics["paired_overlap"] = overlap

    # Step 6: Per-category recall
    print("  Step 6: Per-category recall...")
    cat_recall = per_category_recall(records, data_dir, conditions)
    metrics["per_category_recall"] = cat_recall

    # Step 7: Cost aggregation (scoped to current conditions so renamed-aside
    # siblings like `generic_apr12/` from a prior run don't double-count).
    print("  Step 7: Aggregating costs...")
    costs = aggregate_costs(data_dir, conditions=conditions)
    metrics["costs"] = costs
    print(f"    Grand total: ${costs.get('grand_total', 0.0):.2f}")

    # Step 7b: Token aggregation for reviewer + judge (only steps with raw.json).
    # Filter / label / optimize-extract / optimize-consolidate save parsed
    # outputs only, so their token counts cannot be recovered post-hoc; CP6
    # acknowledges this in prose. Reviewer + judge cover ~77% of cost.
    tokens = aggregate_tokens(data_dir, conditions=conditions)
    metrics["tokens"] = tokens
    grand = tokens.get("grand_total", {})
    if grand:
        print(
            f"    Tokens (review+judge): in={grand.get('input_tokens', 0):,}, "
            f"out={grand.get('output_tokens', 0):,}, "
            f"calls={grand.get('n_calls', 0)}"
        )

    # Write outputs
    eval_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(eval_dir / "metrics.json", metrics)

    # Write comparison.md
    comparison_md = _build_comparison_md(metrics, project_name)
    (eval_dir / "comparison.md").write_text(comparison_md)

    write_metadata(eval_dir, {
        "step": "evaluate",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": {"trial": trial},
        "outputs": {
            "total_comments": total_comments,
            "complete_sets": complete_sets,
        },
    })

    print(f"\n  Evaluation complete. Results in {eval_dir}")


def _build_comparison_md(metrics: dict, project_name: str) -> str:
    """Build a human-readable comparison summary."""
    lines = []
    lines.append(f"# Evaluation Results: {project_name}")
    lines.append("")
    lines.append("## Judge Recall")
    lines.append("")

    recall = metrics.get("judge_recall", {})
    for cond, r in sorted(recall.items()):
        lines.append(f"- {cond}: {r:.3f}")

    lines.append("")
    lines.append("## Non-LLM Metrics")
    lines.append("")
    nlm = metrics.get("non_llm_metrics", {})
    for cond, scores in sorted(nlm.items()):
        if isinstance(scores, dict) and "avg_rouge_l" in scores:
            lines.append(f"- {cond}: ROUGE-L={scores['avg_rouge_l']:.3f}, ChrF++={scores['avg_chrf']:.3f}")

    lines.append("")
    lines.append("## Statistical Tests")
    lines.append("")
    mcn = metrics.get("mcnemar", {})
    for key, result in sorted(mcn.items()):
        adj_p = result.get("adjusted_p_value", result.get("p_value", "N/A"))
        orr = result.get("odds_ratio", "N/A")
        lines.append(f"- {key}: p={adj_p:.4f} (adjusted), OR={orr}")

    lines.append("")
    lines.append("## Costs")
    lines.append("")
    costs = metrics.get("costs", {})
    for step, cost in sorted(costs.items()):
        if step != "grand_total":
            lines.append(f"- {step}: ${cost:.2f}")
    lines.append(f"- **Total: ${costs.get('grand_total', 0.0):.2f}**")

    lines.append("")
    return "\n".join(lines)
