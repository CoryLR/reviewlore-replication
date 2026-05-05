"""Case-study mining over per-revision review + judge data.

Two CP6 deliverables live here:

* :func:`mine_recall_vs_similarity` — for Wing's CP4 ask, find concrete cases
  where the LLM-judge verdict and the textual-similarity metric (ROUGE-L /
  ChrF++) disagree. Buckets the joint per-comment (judge_verdict, top
  ROUGE-L) distribution into:

  * **A** — judge match + low ROUGE-L (default <0.15). Strongest evidence
    that recall via judge captures semantic agreement that surface metrics
    miss; these are the headline paper examples.
  * **B** — judge no_match + high ROUGE-L (default >0.30). Counter-direction:
    surface-similar but semantically different. Limits the "judge >
    textual-sim" framing.
  * **C** — judge match + high ROUGE-L (>=0.30). Sanity check (both signals
    agree positively).
  * **D** — judge no_match + low ROUGE-L (<0.15). The unsurprising negatives.

* :func:`extract_lost_comments` — for Wing's Apr 28 ask, surface the c-cell
  of the Generic-vs-Tuned-100 paired comparison: human comments where the
  generic prompt produced a match and the tuned-100 prompt did not. Pooled
  across the 3 projects this is 15 verdict records per the CP5 pooled-overlap
  table; the per-project breakdown clusters trivially into a Discussion
  paragraph.

Both functions are pure analysis: zero LLM calls, zero subprocess use beyond
loading saved JSON. Outputs land under ``data/<project>/8_case_studies/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import atomic_write_json, load_json


# Default thresholds for bucketing. ROUGE-L is in [0, 1] (we treat its f-measure
# directly). ChrF++ is rescaled to [0, 1] by dividing by 100, matching the
# evaluate.py convention.
DEFAULT_LOW_ROUGE = 0.15
DEFAULT_HIGH_ROUGE = 0.30


@dataclass
class _ScoredCase:
    pr_number: int
    head_sha: str
    human_comment_index: int
    human_file: str | None
    human_line: int | None
    human_text: str
    judge_verdict: str
    matched_ai_comment_index: int | None
    judge_reasoning: str
    matched_ai_text: str
    matched_ai_file: str | None
    matched_ai_line: int | None
    top_rouge_score: float
    top_rouge_ai_index: int | None
    top_rouge_ai_text: str
    top_rouge_ai_file: str | None
    top_rouge_ai_line: int | None
    top_chrf_score: float
    top_chrf_ai_index: int | None
    top_chrf_ai_text: str
    n_ai_comments: int

    def to_dict(self) -> dict:
        return {
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "human_comment_index": self.human_comment_index,
            "human_file": self.human_file,
            "human_line": self.human_line,
            "human_text": self.human_text,
            "judge_verdict": self.judge_verdict,
            "matched_ai_comment_index": self.matched_ai_comment_index,
            "judge_reasoning": self.judge_reasoning,
            "matched_ai": {
                "ai_text": self.matched_ai_text,
                "ai_file": self.matched_ai_file,
                "ai_line": self.matched_ai_line,
            },
            "top_rouge": {
                "score": self.top_rouge_score,
                "ai_index": self.top_rouge_ai_index,
                "ai_text": self.top_rouge_ai_text,
                "ai_file": self.top_rouge_ai_file,
                "ai_line": self.top_rouge_ai_line,
            },
            "top_chrf": {
                "score": self.top_chrf_score,
                "ai_index": self.top_chrf_ai_index,
                "ai_text": self.top_chrf_ai_text,
            },
            "n_ai_comments": self.n_ai_comments,
        }


def _flatten_ai_comments(review: dict) -> list[dict]:
    """Concatenate inline + general AI comments into a flat indexed list.

    Mirrors the indexing the judge sees in ``format_comments_for_judge``:
    inline first, then general, both numbered from 0.
    """
    flat = []
    for c in review.get("inline_comments", []) or []:
        flat.append({
            "kind": "inline",
            "file_path": c.get("file_path"),
            "line_number": c.get("line_number"),
            "comment": c.get("comment", ""),
            "dimension": c.get("dimension", ""),
            "importance": c.get("importance", ""),
        })
    for c in review.get("general_comments", []) or []:
        flat.append({
            "kind": "general",
            "file_path": c.get("file_path"),
            "line_number": c.get("line_number"),
            "comment": c.get("comment", ""),
            "dimension": c.get("dimension", ""),
            "importance": c.get("importance", ""),
        })
    return flat


def _score_pair_rouge(scorer, h_text: str, ai_text: str) -> float:
    """ROUGE-L f-measure for a single (human, ai) pair. 0.0 on empty input."""
    if not h_text or not ai_text:
        return 0.0
    s = scorer.score(h_text, ai_text)
    return float(s["rougeL"].fmeasure)


def _score_pair_chrf(sacrebleu_mod, h_text: str, ai_text: str) -> float:
    """ChrF++ score rescaled to [0, 1]. 0.0 on empty input."""
    if not h_text or not ai_text:
        return 0.0
    chrf = sacrebleu_mod.sentence_chrf(
        ai_text, [h_text], char_order=6, word_order=2
    )
    return float(chrf.score) / 100.0


def _bucket_label(verdict: str, rouge: float, low: float, high: float) -> str:
    """Map (verdict, rouge_score) to one of A / B / C / D."""
    if verdict == "match":
        return "A" if rouge < low else "C"
    return "B" if rouge > high else "D"


def mine_recall_vs_similarity(
    project_name: str,
    out_dir: Path,
    conditions: list[str] | None = None,
    low_rouge: float = DEFAULT_LOW_ROUGE,
    high_rouge: float = DEFAULT_HIGH_ROUGE,
) -> dict:
    """Bucket per-verdict cases by (judge_verdict, top-ROUGE-L) for one project.

    Walks ``data/<project>/7_judgments/<condition>/.../verdicts.json`` and the
    sibling ``review.json`` + ``context.json`` per revision. For every verdict
    record, computes the max-ROUGE-L AI text (and the max-ChrF++ AI text) and
    assigns a bucket label. Writes the structured dump to
    ``data/<project>/8_case_studies/recall_vs_similarity.json``.

    Returns a summary dict with bucket counts and the output path.
    """
    try:
        from rouge_score.rouge_scorer import RougeScorer
        import sacrebleu
    except ImportError as e:
        return {"error": f"Missing dependency: {e}"}
    scorer = RougeScorer(["rougeL"], use_stemmer=True)

    if conditions is None:
        conditions = ["tuned_100"]

    data_dir = out_dir / project_name
    out_path = (
        data_dir / DIRS["case_studies"] / "recall_vs_similarity.json"
    )

    records_by_condition: dict[str, list[dict]] = {}
    bucket_counts_by_condition: dict[str, dict] = {}

    for condition in conditions:
        review_root = data_dir / DIRS["reviews"] / condition
        judge_root = data_dir / DIRS["judgments"] / condition
        if not judge_root.exists():
            continue

        cases: list[_ScoredCase] = []
        for verdict_path in judge_root.rglob("verdicts.json"):
            verdict_data = load_json(verdict_path)
            if not verdict_data:
                continue
            rel = verdict_path.relative_to(judge_root)
            review_path = review_root / rel.parent / "review.json"
            context_path = review_root / rel.parent / "context.json"
            review = load_json(review_path) or {}
            context = load_json(context_path) or {}
            human_comments = context.get("human_comments", []) or []

            ai_comments = _flatten_ai_comments(review)

            # Parse pr_number / short head_sha from path: condition/pr_X/rev_Y
            parts = rel.parts
            pr_part = next((p for p in parts if p.startswith("pr_")), None)
            rev_part = next((p for p in parts if p.startswith("rev_")), None)
            if pr_part is None or rev_part is None:
                continue
            try:
                pr_number = int(pr_part.replace("pr_", ""))
            except ValueError:
                continue
            head_sha = rev_part.replace("rev_", "")

            for v in verdict_data.get("verdicts", []) or []:
                hci = v.get("human_comment_index", 0)
                if hci is None or hci < 0 or hci >= len(human_comments):
                    # Index out of range vs. saved context: skip rather than
                    # crash. Could happen if context.json was regenerated
                    # against a different filter pass.
                    continue
                hc = human_comments[hci]
                h_text = hc.get("body", "")

                # Compute max-ROUGE-L and max-ChrF++ AI texts
                top_rouge_score = 0.0
                top_rouge_idx: int | None = None
                top_rouge_text = ""
                top_chrf_score = 0.0
                top_chrf_idx: int | None = None
                top_chrf_text = ""
                for idx, ai in enumerate(ai_comments):
                    ai_text = ai.get("comment", "")
                    rscore = _score_pair_rouge(scorer, h_text, ai_text)
                    if rscore > top_rouge_score:
                        top_rouge_score = rscore
                        top_rouge_idx = idx
                        top_rouge_text = ai_text
                    cscore = _score_pair_chrf(sacrebleu, h_text, ai_text)
                    if cscore > top_chrf_score:
                        top_chrf_score = cscore
                        top_chrf_idx = idx
                        top_chrf_text = ai_text

                top_rouge_file = (
                    ai_comments[top_rouge_idx]["file_path"]
                    if top_rouge_idx is not None and top_rouge_idx < len(ai_comments)
                    else None
                )
                top_rouge_line = (
                    ai_comments[top_rouge_idx]["line_number"]
                    if top_rouge_idx is not None and top_rouge_idx < len(ai_comments)
                    else None
                )

                # The judge's matched AI comment may differ from the
                # top-ROUGE-L AI: max-pair ROUGE-L is a surface-similarity
                # signal, while the judge's match is a semantic-equivalence
                # signal. Surfacing both lets the case study contrast them.
                matched_idx = v.get("matched_ai_comment_index")
                matched_text = ""
                matched_file: str | None = None
                matched_line: int | None = None
                if isinstance(matched_idx, int) and 0 <= matched_idx < len(ai_comments):
                    matched_text = ai_comments[matched_idx].get("comment", "")
                    matched_file = ai_comments[matched_idx].get("file_path")
                    matched_line = ai_comments[matched_idx].get("line_number")

                cases.append(_ScoredCase(
                    pr_number=pr_number,
                    head_sha=head_sha,
                    human_comment_index=hci,
                    human_file=hc.get("file_path"),
                    human_line=hc.get("line_number"),
                    human_text=h_text,
                    judge_verdict=v.get("verdict", "no_match"),
                    matched_ai_comment_index=matched_idx,
                    judge_reasoning=v.get("reasoning", "") or "",
                    matched_ai_text=matched_text,
                    matched_ai_file=matched_file,
                    matched_ai_line=matched_line,
                    top_rouge_score=top_rouge_score,
                    top_rouge_ai_index=top_rouge_idx,
                    top_rouge_ai_text=top_rouge_text,
                    top_rouge_ai_file=top_rouge_file,
                    top_rouge_ai_line=top_rouge_line,
                    top_chrf_score=top_chrf_score,
                    top_chrf_ai_index=top_chrf_idx,
                    top_chrf_ai_text=top_chrf_text,
                    n_ai_comments=len(ai_comments),
                ))

        # Sort A by ascending ROUGE-L (most striking divergences first); B
        # by descending ROUGE-L; C by descending ROUGE-L; D by descending too.
        # Within-bucket ordering picks the strongest paper-example candidates
        # at the top.
        bucketed: dict[str, list[_ScoredCase]] = {"A": [], "B": [], "C": [], "D": []}
        for case in cases:
            bucket = _bucket_label(case.judge_verdict, case.top_rouge_score, low_rouge, high_rouge)
            bucketed[bucket].append(case)
        bucketed["A"].sort(key=lambda c: c.top_rouge_score)  # most striking match-with-low-rouge
        bucketed["B"].sort(key=lambda c: -c.top_rouge_score)  # surface-similar but no match
        bucketed["C"].sort(key=lambda c: -c.top_rouge_score)
        bucketed["D"].sort(key=lambda c: c.top_rouge_score)

        records_by_condition[condition] = [c.to_dict() for c in cases]
        bucket_counts_by_condition[condition] = {
            bucket: [c.to_dict() for c in items]
            for bucket, items in bucketed.items()
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "project": project_name,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {"low_rouge": low_rouge, "high_rouge": high_rouge},
        "conditions": list(records_by_condition.keys()),
        "all_records": records_by_condition,
        "buckets": bucket_counts_by_condition,
    }
    atomic_write_json(out_path, output)

    summary = {
        "project": project_name,
        "output_path": str(out_path),
        "n_records_by_condition": {
            cond: len(records) for cond, records in records_by_condition.items()
        },
        "bucket_counts_by_condition": {
            cond: {bucket: len(items) for bucket, items in buckets.items()}
            for cond, buckets in bucket_counts_by_condition.items()
        },
    }
    return summary


def extract_lost_comments(
    project_name: str,
    out_dir: Path,
    cond_a: str = "generic",
    cond_b: str = "tuned_100",
) -> dict:
    """Pull the c-cell of the (cond_a, cond_b) paired-overlap comparison.

    The c-cell is "human comments where ``cond_a`` matched and ``cond_b`` did
    not" — i.e., recall the tuned prompt lost relative to the baseline. Wing
    asked at Apr 28 to characterize these qualitatively. Each record carries
    full human-comment text + both conditions' top-ROUGE-L AI comment so the
    qualitative cluster is reproducible.

    Output: ``data/<project>/8_case_studies/lost_comments.json``.
    """
    try:
        from rouge_score.rouge_scorer import RougeScorer
    except ImportError as e:
        return {"error": f"Missing dependency: {e}"}
    scorer = RougeScorer(["rougeL"], use_stemmer=True)

    data_dir = out_dir / project_name
    out_path = data_dir / DIRS["case_studies"] / "lost_comments.json"

    # Build map of (pr, head_sha, hci) -> verdict per condition
    paired: dict[tuple[int, str, int], dict[str, dict]] = {}
    for condition in (cond_a, cond_b):
        judge_root = data_dir / DIRS["judgments"] / condition
        if not judge_root.exists():
            continue
        for verdict_path in judge_root.rglob("verdicts.json"):
            verdict_data = load_json(verdict_path)
            if not verdict_data:
                continue
            rel = verdict_path.relative_to(judge_root)
            parts = rel.parts
            pr_part = next((p for p in parts if p.startswith("pr_")), None)
            rev_part = next((p for p in parts if p.startswith("rev_")), None)
            if pr_part is None or rev_part is None:
                continue
            try:
                pr_number = int(pr_part.replace("pr_", ""))
            except ValueError:
                continue
            head_sha = rev_part.replace("rev_", "")
            for v in verdict_data.get("verdicts", []) or []:
                hci = v.get("human_comment_index", 0)
                key = (pr_number, head_sha, hci)
                paired.setdefault(key, {})[condition] = {
                    "verdict": v.get("verdict", "no_match"),
                    "matched_ai_comment_index": v.get("matched_ai_comment_index"),
                    "reasoning": v.get("reasoning", "") or "",
                    "human_file": v.get("human_file"),
                    "human_line": v.get("human_line"),
                }

    # c-cell: cond_a matched, cond_b did not
    lost_cases = []
    for (pr_number, head_sha, hci), verdicts in paired.items():
        a_v = verdicts.get(cond_a)
        b_v = verdicts.get(cond_b)
        if not a_v or not b_v:
            continue
        if a_v["verdict"] != "match":
            continue
        if b_v["verdict"] != "no_match":
            continue

        # Pull human text from cond_a's context (any condition's context.json
        # carries the same human_comments since build_context derives them
        # before condition branching).
        context_path = (
            data_dir / DIRS["reviews"] / cond_a / f"pr_{pr_number}"
            / f"rev_{head_sha}" / "context.json"
        )
        context = load_json(context_path) or {}
        human_comments = context.get("human_comments", []) or []
        if hci is None or hci < 0 or hci >= len(human_comments):
            continue
        hc = human_comments[hci]
        h_text = hc.get("body", "")

        # Top-ROUGE-L AI text per condition (helps a reader see what each
        # condition actually said about this code, even when no judge match).
        top_per_condition: dict[str, dict] = {}
        for condition in (cond_a, cond_b):
            review_path = (
                data_dir / DIRS["reviews"] / condition / f"pr_{pr_number}"
                / f"rev_{head_sha}" / "review.json"
            )
            review = load_json(review_path) or {}
            ai_comments = _flatten_ai_comments(review)
            best_score = 0.0
            best_text = ""
            best_file = None
            best_line = None
            for ai in ai_comments:
                rscore = _score_pair_rouge(scorer, h_text, ai.get("comment", ""))
                if rscore > best_score:
                    best_score = rscore
                    best_text = ai.get("comment", "")
                    best_file = ai.get("file_path")
                    best_line = ai.get("line_number")
            top_per_condition[condition] = {
                "top_rouge_score": best_score,
                "top_rouge_ai_text": best_text,
                "top_rouge_ai_file": best_file,
                "top_rouge_ai_line": best_line,
                "n_ai_comments": len(ai_comments),
            }

        lost_cases.append({
            "pr_number": pr_number,
            "head_sha": head_sha,
            "human_comment_index": hci,
            "human_file": hc.get("file_path"),
            "human_line": hc.get("line_number"),
            "human_text": h_text,
            "category": hc.get("category", "unknown"),
            "dimension": hc.get("dimension", "unknown"),
            cond_a: {
                **top_per_condition.get(cond_a, {}),
                "judge_verdict": a_v.get("verdict"),
                "matched_ai_comment_index": a_v.get("matched_ai_comment_index"),
                "judge_reasoning": a_v.get("reasoning", ""),
            },
            cond_b: {
                **top_per_condition.get(cond_b, {}),
                "judge_verdict": b_v.get("verdict"),
                "matched_ai_comment_index": b_v.get("matched_ai_comment_index"),
                "judge_reasoning": b_v.get("reasoning", ""),
            },
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "project": project_name,
        "comparison": f"{cond_a}_vs_{cond_b}",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_lost": len(lost_cases),
        "lost_cases": lost_cases,
    }
    atomic_write_json(out_path, output)

    return {
        "project": project_name,
        "output_path": str(out_path),
        "n_lost": len(lost_cases),
    }


def case_studies_recall_similarity(
    project_name: str,
    config: Config,
    out_dir: Path,
) -> None:
    """CLI entry point for ``relox case-studies recall-similarity <project>``."""
    print(f"Mining recall-vs-similarity case studies for {project_name}")
    summary = mine_recall_vs_similarity(project_name, out_dir)
    if "error" in summary:
        print(f"  ERROR: {summary['error']}")
        return
    print(f"  Output: {summary['output_path']}")
    for cond, n in summary["n_records_by_condition"].items():
        buckets = summary["bucket_counts_by_condition"][cond]
        bucket_str = " ".join(f"{k}={v}" for k, v in buckets.items())
        print(f"  {cond}: {n} verdict records ({bucket_str})")


def case_studies_lost_comments(
    project_name: str,
    config: Config,
    out_dir: Path,
    cond_a: str = "generic",
    cond_b: str = "tuned_100",
) -> None:
    """CLI entry point for ``relox case-studies lost-comments <project>``."""
    print(f"Extracting lost comments ({cond_a} vs {cond_b}) for {project_name}")
    summary = extract_lost_comments(project_name, out_dir, cond_a, cond_b)
    if "error" in summary:
        print(f"  ERROR: {summary['error']}")
        return
    print(f"  Output: {summary['output_path']}")
    print(f"  c-cell ({cond_a} matched, {cond_b} no_match): {summary['n_lost']} cases")
