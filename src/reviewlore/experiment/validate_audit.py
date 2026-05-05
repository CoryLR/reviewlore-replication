"""Judge-validation audit helpers for the CP6 case-study subsection.

Two halves, both Wing's CP5 feedback asks:

* :func:`audit_same_line_no_matches` — Wing's "auto-confirm" rule: for every
  judge ``no_match`` verdict, check whether any AI comment in the same
  reviewer output sits at the same ``(file_path, line_number)`` as the
  human comment. When there is a same-line AI comment, the no-match verdict
  is suspicious (the AI noticed the same code but the judge said the
  concerns differed) and warrants manual re-inspection.

* :func:`pull_rater_subset` and :func:`summarize_agreement` — pull the
  rater's filled labels from the sample folders and report explicit M-of-N
  agreement counts. Rater labels every verdict in every sampled revision;
  all labels contribute equally to the headline rollup.

All functions are pure analysis: no LLM calls, no subprocess use beyond
loading saved JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import atomic_write_json, load_json


VALIDATION_SUBDIR = "judge_validation"
SAMPLES_SUBDIR = "samples"
MANIFEST_NAME = "manifest.json"


# ---------------------------------------------------------------------------
# Same-line auto-confirm audit
# ---------------------------------------------------------------------------


def audit_same_line_no_matches(
    project_name: str,
    out_dir: Path,
    conditions: list[str] | None = None,
) -> dict:
    """Flag judge ``no_match`` verdicts where the AI commented on the same line.

    For each ``verdicts.json`` under ``data/<project>/7_judgments/<condition>``,
    walks the verdicts list; for any ``verdict == "no_match"`` we look up the
    sibling ``review.json`` and check whether any AI inline comment shares
    ``(file_path, line_number)`` with the human's ``(human_file, human_line)``.
    A match-on-line in the review while the judge said no-match is the
    auto-confirm-candidate signal Wing asked for in CP5 feedback.

    Returns a structured dict suitable for the paper's case-study table:
    ``{conditions: {<cond>: {n_no_match, n_same_line_candidates,
    candidates: [...]}}, pooled: {...}}``. Saves the same dict to
    ``data/<project>/8_case_studies/same_line_no_match_audit.json``.
    """
    if conditions is None:
        conditions = ["generic", "tuned_100", "tuned_200"]

    data_dir = out_dir / project_name
    out_path = (
        data_dir / DIRS["case_studies"] / "same_line_no_match_audit.json"
    )

    by_condition: dict[str, dict] = {}
    pooled_n_nm = 0
    pooled_n_candidates = 0

    for condition in conditions:
        review_root = data_dir / DIRS["reviews"] / condition
        judge_root = data_dir / DIRS["judgments"] / condition
        if not judge_root.exists():
            continue

        n_no_match = 0
        candidates: list[dict] = []

        for verdict_path in judge_root.rglob("verdicts.json"):
            verdict_data = load_json(verdict_path)
            if not verdict_data:
                continue
            rel = verdict_path.relative_to(judge_root)
            review_path = review_root / rel.parent / "review.json"
            review = load_json(review_path) or {}

            # Build a (file_path, line_number) -> [ai_comment_index] index for
            # the AI's inline comments. General comments don't carry a line
            # so they're excluded from this same-line heuristic.
            line_index: dict[tuple[str, int], list[int]] = {}
            for ai_idx, ai in enumerate(review.get("inline_comments", []) or []):
                fp = ai.get("file_path")
                ln = ai.get("line_number")
                if fp is None or ln is None:
                    continue
                try:
                    ln_int = int(ln)
                except (TypeError, ValueError):
                    continue
                line_index.setdefault((fp, ln_int), []).append(ai_idx)

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
                if v.get("verdict") != "no_match":
                    continue
                n_no_match += 1
                hf = v.get("human_file")
                hl = v.get("human_line")
                if hf is None or hl is None:
                    continue
                try:
                    hl_int = int(hl)
                except (TypeError, ValueError):
                    continue
                same_line_ai = line_index.get((hf, hl_int))
                if not same_line_ai:
                    continue
                # Pull the AI comment text(s) for the case study record
                ai_texts = []
                for ai_idx in same_line_ai:
                    ai = review.get("inline_comments", [])[ai_idx]
                    ai_texts.append({
                        "ai_comment_index": ai_idx,
                        "ai_comment": ai.get("comment", ""),
                    })
                candidates.append({
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "human_comment_index": v.get("human_comment_index"),
                    "human_file": hf,
                    "human_line": hl_int,
                    "human_summary": v.get("human_summary", ""),
                    "judge_reasoning": v.get("reasoning", "") or "",
                    "same_line_ai_comments": ai_texts,
                })

        by_condition[condition] = {
            "n_no_match": n_no_match,
            "n_same_line_candidates": len(candidates),
            "candidates": candidates,
        }
        pooled_n_nm += n_no_match
        pooled_n_candidates += len(candidates)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "project": project_name,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "conditions": list(by_condition.keys()),
        "by_condition": by_condition,
        "pooled": {
            "n_no_match": pooled_n_nm,
            "n_same_line_candidates": pooled_n_candidates,
        },
    }
    atomic_write_json(out_path, output)
    return {
        "project": project_name,
        "output_path": str(out_path),
        "pooled_n_no_match": pooled_n_nm,
        "pooled_n_same_line_candidates": pooled_n_candidates,
    }


# ---------------------------------------------------------------------------
# Rater-label pull and agreement summary
# ---------------------------------------------------------------------------


@dataclass
class RaterLabel:
    sample_id: int
    sample_folder: str
    sample_key: str
    project: str
    condition: str
    pr_number: int
    head_sha: str
    human_comment_index: int
    judge_verdict: str
    rater_verdict: str
    judge_matched_ai_index: int | None
    rater_matched_ai_index: int | None
    rater_reasoning: str

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "sample_folder": self.sample_folder,
            "sample_key": self.sample_key,
            "project": self.project,
            "condition": self.condition,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "human_comment_index": self.human_comment_index,
            "judge_verdict": self.judge_verdict,
            "rater_verdict": self.rater_verdict,
            "judge_matched_ai_index": self.judge_matched_ai_index,
            "rater_matched_ai_index": self.rater_matched_ai_index,
            "rater_reasoning": self.rater_reasoning,
        }


def _validation_root(out_dir: Path) -> Path:
    return out_dir / VALIDATION_SUBDIR


def _load_manifest(out_dir: Path) -> dict | None:
    manifest_path = _validation_root(out_dir) / MANIFEST_NAME
    return load_json(manifest_path)


def pull_rater_subset(
    out_dir: Path,
    n: int | None = None,
) -> list[RaterLabel]:
    """Walk sample folders and return labeled rater verdicts.

    Order: manifest sample order, then verdict order within each sample.
    Caps at ``n`` if provided.

    A verdict is "labeled" iff its rater ``verdict`` field is non-null in the
    sample folder's ``verdicts.json``. Skips folders without a verdicts.json
    (a sample that was never visited).
    """
    manifest = _load_manifest(out_dir)
    if not manifest:
        return []
    samples = manifest.get("samples", []) or []
    samples_root = _validation_root(out_dir) / SAMPLES_SUBDIR

    out: list[RaterLabel] = []

    for sample in samples:
        sample_id = sample.get("sample_id")
        sample_folder = sample.get("sample_folder")
        if sample_id is None or sample_folder is None:
            continue
        rater_path = samples_root / sample_folder / "verdicts.json"
        rater_data = load_json(rater_path)
        if not rater_data:
            continue
        rater_verdicts = rater_data.get("verdicts", []) or []
        manifest_verdicts = sample.get("verdicts", []) or []
        manifest_by_hci = {
            mv.get("human_comment_index"): mv for mv in manifest_verdicts
        }

        for rater_v in rater_verdicts:
            rater_verdict = rater_v.get("verdict")
            if rater_verdict is None:
                continue  # not labeled yet
            hci = rater_v.get("human_comment_index")
            mv = manifest_by_hci.get(hci)
            if mv is None:
                continue
            label = RaterLabel(
                sample_id=sample_id,
                sample_folder=sample_folder,
                sample_key=mv.get("sample_key", ""),
                project=sample.get("project", ""),
                condition=sample.get("condition", ""),
                pr_number=int(sample.get("pr_number", 0) or 0),
                head_sha=sample.get("head_sha", ""),
                human_comment_index=int(hci) if hci is not None else -1,
                judge_verdict=mv.get("judge_verdict_at_sampling", ""),
                rater_verdict=rater_verdict,
                judge_matched_ai_index=mv.get("matched_ai_comment_index_at_sampling"),
                rater_matched_ai_index=rater_v.get("matched_ai_comment_index"),
                rater_reasoning=rater_v.get("reasoning", "") or "",
            )
            out.append(label)

    if n is not None and n > 0:
        out = out[:n]
    return out


def summarize_agreement(labels: list[RaterLabel]) -> dict:
    """Compute Wing's "M of N" headline counts plus per-class agreement.

    Returns a dict with:

    * ``n_total``, ``n_match_judge``, ``n_no_match_judge``: totals.
    * ``judge_correct`` / ``judge_wrong``: rater-vs-judge agreement.
    * ``per_class``: {match: P(rater match | judge match), no_match: P(rater
      no_match | judge no_match), n_match, n_no_match}.
    * ``per_project``: same as above, keyed by project.
    """
    n_total = len(labels)
    if n_total == 0:
        return {"n_total": 0, "note": "no rater labels found"}

    n_match_judge = sum(1 for l in labels if l.judge_verdict == "match")
    n_no_match_judge = sum(1 for l in labels if l.judge_verdict == "no_match")
    judge_correct = sum(1 for l in labels if l.judge_verdict == l.rater_verdict)
    judge_wrong = n_total - judge_correct

    p_match_given_match = sum(
        1 for l in labels if l.judge_verdict == "match" and l.rater_verdict == "match"
    )
    p_no_given_no = sum(
        1 for l in labels if l.judge_verdict == "no_match" and l.rater_verdict == "no_match"
    )

    def _agree_pct(num: int, den: int) -> float | None:
        return (num / den) if den > 0 else None

    per_class = {
        "match": {
            "n_judge_match": n_match_judge,
            "n_rater_agreed": p_match_given_match,
            "p_rater_match_given_judge_match": _agree_pct(p_match_given_match, n_match_judge),
        },
        "no_match": {
            "n_judge_no_match": n_no_match_judge,
            "n_rater_agreed": p_no_given_no,
            "p_rater_no_match_given_judge_no_match": _agree_pct(p_no_given_no, n_no_match_judge),
        },
    }

    # Per-project rollup
    per_project: dict[str, dict] = {}
    for proj in sorted({l.project for l in labels if l.project}):
        proj_labels = [l for l in labels if l.project == proj]
        n = len(proj_labels)
        correct = sum(1 for l in proj_labels if l.judge_verdict == l.rater_verdict)
        nj_m = sum(1 for l in proj_labels if l.judge_verdict == "match")
        nj_nm = sum(1 for l in proj_labels if l.judge_verdict == "no_match")
        ra_m = sum(1 for l in proj_labels if l.judge_verdict == "match" and l.rater_verdict == "match")
        ra_nm = sum(1 for l in proj_labels if l.judge_verdict == "no_match" and l.rater_verdict == "no_match")
        per_project[proj] = {
            "n_total": n,
            "n_judge_correct": correct,
            "agreement_overall": correct / n if n > 0 else None,
            "p_rater_match_given_judge_match": _agree_pct(ra_m, nj_m),
            "p_rater_no_match_given_judge_no_match": _agree_pct(ra_nm, nj_nm),
        }

    return {
        "n_total": n_total,
        "n_match_judge": n_match_judge,
        "n_no_match_judge": n_no_match_judge,
        "judge_correct": judge_correct,
        "judge_wrong": judge_wrong,
        "agreement_overall": judge_correct / n_total,
        "per_class": per_class,
        "per_project": per_project,
    }


def run_validate_audit(
    out_dir: Path,
    n: int | None = None,
    audit_same_line_projects: list[str] | None = None,
    write_json: Path | None = None,
) -> dict:
    """End-to-end audit: same-line check + rater-pull + M-of-N summary.

    ``audit_same_line_projects`` controls which projects get the same-line
    audit (defaults to all projects in the manifest's ``projects`` array if
    available; falls back to no audit when the manifest is missing).
    """
    manifest = _load_manifest(out_dir)
    projects = []
    if manifest:
        projects = manifest.get("projects", []) or []
    if audit_same_line_projects is not None:
        projects = audit_same_line_projects

    same_line: dict[str, dict] = {}
    for project in projects:
        same_line[project] = audit_same_line_no_matches(project, out_dir)

    labels = pull_rater_subset(out_dir, n=n)
    summary = summarize_agreement(labels)

    audit_out = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_requested": n,
        "n_labels_found": len(labels),
        "labels": [l.to_dict() for l in labels],
        "summary": summary,
        "same_line_audit": same_line,
    }
    if write_json:
        write_json.parent.mkdir(parents=True, exist_ok=True)
        write_json.write_text(json.dumps(audit_out, indent=2) + "\n")
    return audit_out


def cli_validate_audit(
    config: Config,
    out_dir: Path,
    n: int | None = None,
    write_json: Path | None = None,
) -> None:
    """CLI entry: ``relox validate audit`` driver."""
    audit_out = run_validate_audit(
        out_dir=out_dir,
        n=n,
        write_json=write_json,
    )
    n_labels = audit_out["n_labels_found"]
    summary = audit_out["summary"]
    print(f"Rater labels found: {n_labels}")
    if n_labels == 0:
        print("  (No rater labels yet. Fill verdicts.json files in "
              "data/judge_validation/samples/sample_*/ to populate.)")
    else:
        print(f"  Judge correct: {summary['judge_correct']} / {summary['n_total']} "
              f"({summary['agreement_overall']:.1%} overall agreement)")
        pc = summary["per_class"]
        m = pc["match"]
        nm = pc["no_match"]
        if m["n_judge_match"] > 0:
            print(
                f"  Of {m['n_judge_match']} judge match: rater agreed on "
                f"{m['n_rater_agreed']} ({m['p_rater_match_given_judge_match']:.1%})"
            )
        if nm["n_judge_no_match"] > 0:
            print(
                f"  Of {nm['n_judge_no_match']} judge no_match: rater agreed on "
                f"{nm['n_rater_agreed']} ({nm['p_rater_no_match_given_judge_no_match']:.1%})"
            )
        for proj, stats in summary.get("per_project", {}).items():
            print(
                f"    {proj}: {stats['n_judge_correct']}/{stats['n_total']} "
                f"({stats['agreement_overall']:.1%})"
            )

    same_line = audit_out.get("same_line_audit", {})
    for proj, stats in same_line.items():
        print(
            f"Same-line audit {proj}: "
            f"{stats['pooled_n_same_line_candidates']} of "
            f"{stats['pooled_n_no_match']} no_match verdicts have a "
            f"same-line AI inline comment ({stats['output_path']})"
        )
