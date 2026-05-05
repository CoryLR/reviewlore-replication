"""Pooled judge validation: verdict-level case-cohort sampling, manifest, agreement.

Stratification: (project, judge_verdict) -> 6 cells. Within each cell,
sampling is verdict-level: the verdict pool is stable-sorted and seeded-
shuffled, then the first N keys are recorded as the stratum's sample keys.
Revisions are the labeling UX unit -- each sample key's whole revision
folder is pulled into the sample, and the rater labels every verdict in
that folder. All labels contribute equally to the agreement rollup.

Census fallback: if a stratum's verdict pool is smaller than its per-cell
target, all verdicts in the pool become its sample keys. This is a strength
(zero sampling variance on the rare class), not a shortcoming.

Extension semantics: per-cell target is immutable-never-shrinks. On a larger
--target, the per-cell floor grows, the same per-cell-seeded shuffle is
re-derived, and the new prefix length is used. Existing sample keys must
remain a prefix of the re-shuffled pool (drift guard aborts if not).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reviewlore.tuner.config import Config, get_prompt, get_prompt_version
from reviewlore.tuner.dirs import DIRS
from reviewlore.tuner.state import load_json

SPEC_VERSION = "2.0"
SCHEME = "verdict_level_case_cohort"
DEFAULT_TARGET = 54
DEFAULT_SEED = 42
MANIFEST_NAME = "manifest.json"
VALIDATION_SUBDIR = "judge_validation"
SAMPLES_SUBDIR = "samples"

VERDICT_CLASSES = ("match", "no_match")


# ---------------------------------------------------------------------------
# Paths and trial redirect
# ---------------------------------------------------------------------------


def _trial_redirect(out_dir: Path, trial: bool) -> Path:
    if not trial:
        return out_dir
    if os.environ.get("RELO_OUT"):
        return out_dir
    tmp_root = Path(os.environ.get("TMPDIR", "/tmp")) / "reviewlore-trial"
    return tmp_root


def _validation_root(out_dir: Path) -> Path:
    return out_dir / VALIDATION_SUBDIR


def _samples_dir(out_dir: Path) -> Path:
    return _validation_root(out_dir) / SAMPLES_SUBDIR


def _manifest_path(out_dir: Path) -> Path:
    return _validation_root(out_dir) / MANIFEST_NAME


# ---------------------------------------------------------------------------
# Per-cell seeds (process-independent, sha256-derived)
# ---------------------------------------------------------------------------


def _per_cell_seed(master_seed: int, project: str, stratum_suffix: str) -> int:
    """sha256-derived seed keyed by (master_seed, project, stratum_suffix).

    stratum_suffix is the verdict class ("match" or "no_match") under the new
    case-cohort scheme. Retained as a generic string arg so the same helper
    can be exercised in tests without coupling to the 6-cell design.
    """
    key = f"{master_seed}|{project}|{stratum_suffix}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


# ---------------------------------------------------------------------------
# Verdict enumeration and shuffling
# ---------------------------------------------------------------------------


def _sample_key(project: str, condition: str, pr_dir: str, rev_dir: str, idx: int) -> str:
    pr_num = pr_dir.replace("pr_", "")
    short = rev_dir.replace("rev_", "")
    return f"{project}/{condition}/pr_{pr_num}/rev_{short}/idx_{idx}"


@dataclass(frozen=True)
class VerdictLocator:
    """Identifies one judge verdict in the data tree."""

    project: str
    condition: str
    pr_dir: str  # "pr_1234"
    rev_dir: str  # "rev_abc12345"
    pr_number: int
    head_sha: str
    merge_base_sha: str
    human_comment_index: int
    judge_verdict: str  # "match" | "no_match"
    human_file: str | None
    human_line: int | None
    human_summary: str
    matched_ai_comment_index: int | None
    reasoning: str

    @property
    def sample_key(self) -> str:
        return _sample_key(self.project, self.condition, self.pr_dir, self.rev_dir, self.human_comment_index)

    @property
    def folder_key(self) -> tuple[str, str, str, str]:
        return (self.project, self.condition, self.pr_dir, self.rev_dir)

    @property
    def sort_key(self) -> tuple:
        return (self.condition, self.pr_dir, self.rev_dir, self.human_comment_index)


def _enumerate_project_verdicts(
    out_dir: Path, config: Config, project: str, verdict_class: str
) -> list[VerdictLocator]:
    """Walk 7_judgments/<condition>/pr_*/rev_*/verdicts.json across configured
    conditions; return all verdicts whose `verdict` field matches verdict_class.

    head_sha / merge_base_sha are resolved from testable_revisions.json first,
    falling back to context.json in 6_reviews/.
    """
    testable = _testable_index(out_dir, project)
    out: list[VerdictLocator] = []
    jdir_root = out_dir / project / DIRS["judgments"]
    for condition in config.conditions:
        cdir = jdir_root / condition
        if not cdir.exists():
            continue
        for pr_dir in sorted(cdir.iterdir()):
            if not pr_dir.is_dir() or not pr_dir.name.startswith("pr_"):
                continue
            for rev_dir in sorted(pr_dir.iterdir()):
                if not rev_dir.is_dir() or not rev_dir.name.startswith("rev_"):
                    continue
                verdicts_path = rev_dir / "verdicts.json"
                if not verdicts_path.exists():
                    continue
                data = load_json(verdicts_path)
                if not data:
                    continue

                pr_num = int(pr_dir.name.replace("pr_", ""))
                short_head = rev_dir.name.replace("rev_", "")
                rev_info = testable.get((pr_num, short_head))
                if rev_info:
                    head_sha = rev_info["head_sha"]
                    merge_base_sha = rev_info["merge_base_sha"]
                else:
                    ctx = load_json(out_dir / project / DIRS["reviews"] / condition / pr_dir.name / rev_dir.name / "context.json")
                    if not ctx or "head_sha" not in ctx or "merge_base_sha" not in ctx:
                        continue
                    head_sha = ctx["head_sha"]
                    merge_base_sha = ctx["merge_base_sha"]

                for v in data.get("verdicts", []):
                    if v.get("verdict") != verdict_class:
                        continue
                    idx = v.get("human_comment_index")
                    if idx is None:
                        continue
                    out.append(
                        VerdictLocator(
                            project=project,
                            condition=condition,
                            pr_dir=pr_dir.name,
                            rev_dir=rev_dir.name,
                            pr_number=pr_num,
                            head_sha=head_sha,
                            merge_base_sha=merge_base_sha,
                            human_comment_index=int(idx),
                            judge_verdict=verdict_class,
                            human_file=v.get("human_file"),
                            human_line=v.get("human_line"),
                            human_summary=v.get("human_summary", ""),
                            matched_ai_comment_index=v.get("matched_ai_comment_index"),
                            reasoning=v.get("reasoning", ""),
                        )
                    )
    return out


def _shuffled_pool(
    master_seed: int, project: str, verdict_class: str, pool: list[VerdictLocator]
) -> list[VerdictLocator]:
    """Stable sort + seeded shuffle. Deterministic across processes."""
    sorted_pool = sorted(pool, key=lambda v: v.sort_key)
    seed = _per_cell_seed(master_seed, project, verdict_class)
    rnd = random.Random(seed)
    shuffled = list(sorted_pool)
    rnd.shuffle(shuffled)
    return shuffled


# ---------------------------------------------------------------------------
# Blank verdicts.json construction (rater-facing)
# ---------------------------------------------------------------------------


def _build_blank_verdicts(judge_verdicts: dict) -> dict:
    """Mirror judge's verdicts.json schema; null out rater decision fields.

    Drops runtime metadata (cost_usd, duration_seconds) and the top-level
    summary block (removed from the judge output schema in 2026-05-04).
    """
    blank_entries = []
    for v in judge_verdicts.get("verdicts", []):
        blank_entries.append({
            "human_comment_index": v.get("human_comment_index"),
            "human_file": v.get("human_file"),
            "human_line": v.get("human_line"),
            "human_summary": v.get("human_summary", ""),
            "verdict": None,
            "matched_ai_comment_index": None,
            "reasoning": None,
        })
    return {"verdicts": blank_entries}


# ---------------------------------------------------------------------------
# Worktree provisioning
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _resolve_subject_repo(config: Config, project: str) -> Path:
    proj_cfg = config.projects[project]
    subject = proj_cfg.subject_repo
    if not subject:
        raise RuntimeError(f"Project {project} has no subject_repo configured in reviewlore.yaml")
    p = Path(subject)
    if not p.is_absolute():
        p = (config.config_dir / p).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Subject repo not found: {p}")
    return p


def _provision_worktree(
    subject_repo: Path, worktree_path: Path, head_sha: str, merge_base_sha: str
) -> None:
    """Provision a worktree at head_sha with HEAD reset-mixed to merge_base_sha.

    Result: HEAD=merge_base_sha, index=merge_base_sha, working tree files=head_sha.
    `git status` reports the PR diff as unstaged changes (including deletions).

    Runs `git worktree prune` first so previously-deleted-on-disk worktrees
    whose metadata lingers in the subject repo don't block re-registration.
    """
    worktree_path = worktree_path.resolve()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        return
    _run_git(["worktree", "prune"], cwd=subject_repo, check=False)
    _run_git(["worktree", "add", "--detach", str(worktree_path), head_sha], cwd=subject_repo)
    _run_git(["reset", "--mixed", merge_base_sha], cwd=worktree_path)


# ---------------------------------------------------------------------------
# README / workspace / sample_info generation
# ---------------------------------------------------------------------------


_RATER_README = """# Sample {sample_id} — {project}/{condition} PR #{pr_number}

You are labeling the judge's verdicts blind: rate every entry in `verdicts.json`
as `match` or `no_match` without reference to the judge's own decisions (those are
stored in the locked manifest at `data/judge_validation/manifest.json` and not
consulted until `compare` runs).

## Steps

1. Open `workspace.code-workspace` in VS Code. You'll see this sample folder and
   the `worktree/` root side-by-side. The worktree's `git status` (and the VS Code
   SCM panel) shows the PR diff: modifications, additions, and deletions exactly
   as the judge saw.
2. Read `context.json` (full human comments that were input to the judge) and
   `review.json` (the AI reviewer output the judge compared against). Navigate
   the worktree to inspect code at review time.
3. For every entry in `verdicts.json` (indices listed in `sample_info.json >
   target_indices`), fill:
   - `verdict`: `"match"` or `"no_match"`.
   - `matched_ai_comment_index`: required when `verdict` is `"match"`, else leave
     null. See the "matched_ai_comment_index convention" note below.
   - `reasoning`: brief free-text; records why you labeled as you did. Helps
     later disagreement analysis.
4. Save `verdicts.json` and move to the next sample folder.

## human_summary caveat

Each entry's `human_summary` is the judge's own summary of the human comment
(kept verbatim for schema parity with the judge output). The full untruncated
human comment body is in `context.json`; consult it rather than relying on
`human_summary`, which is the judge's framing.

## matched_ai_comment_index convention

Use the explicit `index` integer field on each AI comment in `review.json`. The
`index` field is present on every AI comment (both `inline_comments[]` and
`general_comments[]`); inline comments are numbered first, then general
comments continue the numbering.

## Coverage-based matching

A human comment is `match` if any AI comment, or any combination of AI
comments, substantively covers the concern. Many-to-one matches (one AI
comment covering several human comments) and one-to-many matches (multiple AI
comments collectively covering one human comment) are both allowed; coverage
is the criterion, not bipartite assignment. When several AI comments could
cover the same human concern, pick the closest or strongest covering AI
comment for `matched_ai_comment_index` and mention the others in `reasoning`.

## Judge prompt

`judge_system_prompt.md` is the reference prompt as of setup time. The manifest
records the judge's actual at-run prompt version for drift detection.

## Labeling scope

Label every verdict in this folder. All labels contribute equally to the
M-of-N agreement rollup that `relox validate audit` produces.
"""


def _write_rater_readme(sample_dir: Path, sample_id: int, project: str, condition: str, pr_number: int) -> None:
    text = _RATER_README.format(
        sample_id=f"{sample_id:03d}",
        project=project,
        condition=condition,
        pr_number=pr_number,
    )
    (sample_dir / "README.md").write_text(text)


def _write_workspace(sample_dir: Path, worktree_path: Path) -> None:
    workspace = {
        "folders": [
            {"path": "."},
            {"path": str(worktree_path.relative_to(sample_dir)) if worktree_path.is_relative_to(sample_dir) else str(worktree_path)},
        ],
        "settings": {
            "files.autoSave": "afterDelay",
            "editor.formatOnSave": False,
        },
    }
    (sample_dir / "workspace.code-workspace").write_text(json.dumps(workspace, indent=2) + "\n")


def _write_sample_info(
    sample_dir: Path,
    sample_id: int,
    project: str,
    condition: str,
    pr_number: int,
    head_sha: str,
    merge_base_sha: str,
    target_indices: list[int],
    verdict_sample_keys: list[str],
    worktree_relpath: str,
) -> None:
    info = {
        "sample_id": sample_id,
        "sample_folder": f"sample_{sample_id:03d}",
        "project": project,
        "condition": condition,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "target_indices": target_indices,
        "verdict_sample_keys": verdict_sample_keys,
        "worktree_path": worktree_relpath,
    }
    (sample_dir / "sample_info.json").write_text(json.dumps(info, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Testability revisions lookup
# ---------------------------------------------------------------------------


def _testable_index(out_dir: Path, project: str) -> dict[tuple[int, str], dict]:
    path = out_dir / project / DIRS["split"] / "testable_revisions.json"
    data = load_json(path) or []
    idx: dict[tuple[int, str], dict] = {}
    for rev in data:
        pr = rev.get("pr_number")
        head = rev.get("head_sha", "")
        if pr is None or not head:
            continue
        idx[(int(pr), head[:8])] = rev
    return idx


# ---------------------------------------------------------------------------
# Atomic manifest write
# ---------------------------------------------------------------------------


def _atomic_write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _verdict_entry_dict(v: VerdictLocator,
                        reviewer_output_path: str, judge_raw_output_path: str, context_path: str) -> dict:
    return {
        "sample_key": v.sample_key,
        "human_comment_index": v.human_comment_index,
        "human_file": v.human_file,
        "human_line": v.human_line,
        "human_summary": v.human_summary,
        "judge_verdict_at_sampling": v.judge_verdict,
        "matched_ai_comment_index_at_sampling": v.matched_ai_comment_index,
        "reasoning_at_sampling": v.reasoning,
        "reviewer_output_path": reviewer_output_path,
        "judge_raw_output_path": judge_raw_output_path,
        "context_path": context_path,
    }


def _relpath_under(out_dir: Path, p: Path) -> str:
    try:
        return str(p.relative_to(out_dir.parent))
    except ValueError:
        return str(p)


def _enumerate_folder_verdicts(
    out_dir: Path, project: str, condition: str, pr_dir: str, rev_dir: str
) -> tuple[list[VerdictLocator], dict]:
    """Load every verdict in a single (project, condition, pr, rev) folder.

    Returns (locators, raw_judge_verdicts_json_dict). Raises if verdicts.json
    is absent.
    """
    testable = _testable_index(out_dir, project)
    pr_num = int(pr_dir.replace("pr_", ""))
    short = rev_dir.replace("rev_", "")
    rev_info = testable.get((pr_num, short))
    if rev_info:
        head_sha = rev_info["head_sha"]
        merge_base_sha = rev_info["merge_base_sha"]
    else:
        ctx = load_json(out_dir / project / DIRS["reviews"] / condition / pr_dir / rev_dir / "context.json")
        if not ctx or "head_sha" not in ctx or "merge_base_sha" not in ctx:
            raise RuntimeError(f"No testable record and no context SHAs for {project}/{condition}/{pr_dir}/{rev_dir}")
        head_sha = ctx["head_sha"]
        merge_base_sha = ctx["merge_base_sha"]

    verdicts_path = out_dir / project / DIRS["judgments"] / condition / pr_dir / rev_dir / "verdicts.json"
    data = load_json(verdicts_path)
    if not data:
        raise FileNotFoundError(f"Missing verdicts.json at {verdicts_path}")

    locators: list[VerdictLocator] = []
    for v in data.get("verdicts", []):
        idx = v.get("human_comment_index")
        if idx is None:
            continue
        verdict_class = v.get("verdict", "")
        if verdict_class not in VERDICT_CLASSES:
            continue  # skip unlabeled / weird entries
        locators.append(
            VerdictLocator(
                project=project,
                condition=condition,
                pr_dir=pr_dir,
                rev_dir=rev_dir,
                pr_number=pr_num,
                head_sha=head_sha,
                merge_base_sha=merge_base_sha,
                human_comment_index=int(idx),
                judge_verdict=verdict_class,
                human_file=v.get("human_file"),
                human_line=v.get("human_line"),
                human_summary=v.get("human_summary", ""),
                matched_ai_comment_index=v.get("matched_ai_comment_index"),
                reasoning=v.get("reasoning", ""),
            )
        )
    return locators, data


def setup_validation(
    config: Config,
    out_dir: Path,
    target: int | None = None,
    seed: int | None = None,
    trial: bool = False,
    dry_run: bool = False,
) -> None:
    """Build or extend the verdict-level case-cohort validation manifest.

    - Cells: (project, judge_verdict) -> 3 x 2 = 6.
    - Pooled --target; per-cell floor = ceil(target / 6).
    - Within each cell: enumerate project verdicts of that class, stable sort,
      seeded shuffle, take first N as the stratum's sample keys.
    - Census fallback: if pool < target, take all.
    - Rev-level dedupe: same rev selected from multiple strata yields one
      sample folder; the rater labels every verdict in the rev folder.
    - Atomic manifest write via os.replace at end.
    """
    if trial and target is None:
        target = 3
    if target is None:
        target = DEFAULT_TARGET
    if seed is None:
        seed = DEFAULT_SEED

    effective_out = _trial_redirect(out_dir, trial)
    if effective_out != out_dir:
        print(f"[validate] --trial auto-redirect: output root -> {effective_out}")

    validation_root = _validation_root(effective_out)
    samples_dir = _samples_dir(effective_out)
    manifest_path = _manifest_path(effective_out)

    # 1. In-scope cells: (project, verdict_class) = 6 cells
    projects_in_scope = list(config.projects.keys())
    in_scope = [(p, v) for p in projects_in_scope for v in VERDICT_CLASSES]
    num_cells = len(in_scope)
    if num_cells == 0:
        raise RuntimeError("No in-scope cells: config has no projects")

    # 2. Validate every project has at least one verdicts.json on disk for at
    #    least one condition (disk gate).
    missing_projects: list[str] = []
    for p in projects_in_scope:
        any_data = False
        for c in config.conditions:
            cdir = out_dir / p / DIRS["judgments"] / c
            if cdir.exists():
                any_data = True
                break
        if not any_data:
            missing_projects.append(p)
    if missing_projects:
        print("ERROR: projects have no 7_judgments/ data on disk:", file=sys.stderr)
        for m in missing_projects:
            print(f"  - {m}", file=sys.stderr)
        raise SystemExit(2)

    # 3. Load or init manifest
    existing = load_json(manifest_path)
    if existing:
        if existing.get("scheme") and existing["scheme"] != SCHEME:
            raise RuntimeError(
                f"scheme mismatch: manifest has scheme={existing.get('scheme')}, code expects {SCHEME}. "
                "Delete the manifest to re-setup under the new scheme."
            )
        if existing.get("master_seed") != seed and seed != DEFAULT_SEED:
            raise RuntimeError(
                f"seed mismatch: manifest has master_seed={existing['master_seed']}, "
                f"CLI passed --seed {seed}. Master seed is locked after first setup."
            )
        seed = existing["master_seed"]
        if target < existing.get("pooled_target", 0):
            raise RuntimeError(
                f"target shrink refused: manifest has pooled_target={existing['pooled_target']}, "
                f"CLI passed --target {target}. Extension-only; to reduce scope, delete the manifest."
            )
        manifest = existing
    else:
        manifest = {
            "spec_version": SPEC_VERSION,
            "scheme": SCHEME,
            "master_seed": seed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pooled_target": 0,
            "derived_per_cell_target_at_latest_setup": 0,
            "judge_prompt_version_at_setup": 0,
            "projects": {},
            "strata": {},
            "samples": [],
        }

    # Derived per-cell floor for THIS invocation
    derived_floor = math.ceil(target / num_cells)
    if target % num_cells != 0:
        eq = derived_floor * num_cells
        print(
            f"[validate] WARNING: target {target} does not divide evenly into {num_cells} strata; "
            f"rounded up to {derived_floor} per stratum (effective pooled floor: {eq}). "
            f"Use --target {eq} for exactly equal strata."
        )

    manifest["pooled_target"] = target
    manifest["derived_per_cell_target_at_latest_setup"] = derived_floor
    manifest["judge_prompt_version_at_setup"] = get_prompt_version("judge", config)
    manifest.setdefault("scheme", SCHEME)
    manifest.setdefault("spec_version", SPEC_VERSION)

    # 4. Per-project judge_prompt_version_at_run
    for proj in projects_in_scope:
        meta = load_json(out_dir / proj / DIRS["judgments"] / "metadata.json")
        if meta:
            at_run = meta.get("prompt_versions", {}).get("judge")
            if at_run is not None:
                if proj not in manifest["projects"]:
                    manifest["projects"][proj] = {"judge_prompt_version_at_run": at_run}
                stored = manifest["projects"][proj].get("judge_prompt_version_at_run")
                if stored != at_run:
                    print(
                        f"[validate] WARNING: {proj} judge_prompt_version_at_run changed: "
                        f"manifest has {stored}, 7_judgments/metadata.json now has {at_run}"
                    )
                setup_ver = manifest["judge_prompt_version_at_setup"]
                if at_run != setup_ver:
                    print(
                        f"[validate] WARNING: {proj} judge_prompt_version_at_run={at_run} "
                        f"differs from judge_prompt_version_at_setup={setup_ver}."
                    )

    # 5. Per-stratum sample-key selection (verdict-level case-cohort)
    #    For each (project, verdict_class) cell:
    #      - Enumerate the project's verdict pool of that class.
    #      - Stable sort + seeded shuffle.
    #      - New floor = max(existing floor, derived_floor). Immutable.
    #      - Sample keys = shuffled_pool[: min(floor, len(pool))].
    #      - Verify existing sample keys remain a prefix of the new shuffle.

    # Existing sample keys per stratum (from prior setup)
    existing_keys_by_stratum: dict[str, list[str]] = {
        k: list(s.get("sample_keys", [])) for k, s in manifest["strata"].items()
    }

    # Full cumulative locators (keyed by sample_key) across all strata
    selected_locators_by_key: dict[str, VerdictLocator] = {}

    for (project, verdict_class) in in_scope:
        stratum_key = f"{project}/{verdict_class}"

        pool = _enumerate_project_verdicts(out_dir, config, project, verdict_class)
        shuffled = _shuffled_pool(seed, project, verdict_class, pool)
        pool_size = len(shuffled)

        prior = manifest["strata"].get(stratum_key, {})
        prior_target = int(prior.get("per_cell_target", 0))
        prior_keys = existing_keys_by_stratum.get(stratum_key, [])

        new_floor = max(prior_target, derived_floor)
        n_select = min(new_floor, pool_size)

        # Drift guard: prior keys must remain a prefix of the reshuffled pool.
        shuffled_keys = [v.sample_key for v in shuffled]
        if prior_keys:
            if len(prior_keys) > n_select:
                raise RuntimeError(
                    f"stratum {stratum_key}: prior sample-key count {len(prior_keys)} exceeds new floor {n_select}"
                )
            if shuffled_keys[: len(prior_keys)] != prior_keys:
                raise RuntimeError(
                    f"stratum {stratum_key}: prior sample keys are not a prefix of the re-shuffled pool. "
                    "This means the verdict pool changed between setups (e.g., a rev was re-judged with "
                    "a different verdict). Inspect prior sample keys in the manifest and the current "
                    "7_judgments/ tree; manual intervention required."
                )

        # New sample keys = shuffled_keys[:n_select]
        sample_keys = shuffled_keys[:n_select]
        selected_locator_list = shuffled[:n_select]

        for loc in selected_locator_list:
            selected_locators_by_key[loc.sample_key] = loc

        manifest["strata"][stratum_key] = {
            "per_cell_seed": _per_cell_seed(seed, project, verdict_class),
            "per_cell_target": new_floor,
            "pool_size": pool_size,
            "census": new_floor >= pool_size,
            "sample_keys": sample_keys,
        }

    # 6. Group selected verdicts by folder_key; determine which folders already
    #    exist on disk. A folder_key = (project, condition, pr_dir, rev_dir).
    selected_folder_keys: dict[tuple[str, str, str, str], list[VerdictLocator]] = {}
    for loc in selected_locators_by_key.values():
        selected_folder_keys.setdefault(loc.folder_key, []).append(loc)

    # Existing samples indexed by folder_key
    existing_samples_by_folder: dict[tuple[str, str, str, str], dict] = {}
    for s in manifest["samples"]:
        fk = (s["project"], s["condition"], f"pr_{s['pr_number']}", f"rev_{s['head_sha'][:8]}")
        existing_samples_by_folder[fk] = s

    # 7. Drift guard on existing samples: compare frozen verdict fields against live
    drift_warnings: list[str] = []
    for sample in manifest["samples"]:
        proj = sample["project"]
        cond = sample["condition"]
        pr_dir = f"pr_{sample['pr_number']}"
        rev_dir = f"rev_{sample['head_sha'][:8]}"
        try:
            live_locs, _ = _enumerate_folder_verdicts(out_dir, proj, cond, pr_dir, rev_dir)
        except (FileNotFoundError, RuntimeError):
            drift_warnings.append(f"{proj}/{cond}/{pr_dir}/{rev_dir}: live verdicts.json missing")
            continue
        live_by_idx = {loc.human_comment_index: loc for loc in live_locs}
        for frozen in sample.get("verdicts", []):
            idx = frozen["human_comment_index"]
            live = live_by_idx.get(idx)
            if not live:
                drift_warnings.append(f"{frozen['sample_key']}: live entry missing")
                continue
            checks = [
                ("judge_verdict_at_sampling", live.judge_verdict, frozen.get("judge_verdict_at_sampling")),
                ("matched_ai_comment_index_at_sampling", live.matched_ai_comment_index, frozen.get("matched_ai_comment_index_at_sampling")),
                ("reasoning_at_sampling", live.reasoning, frozen.get("reasoning_at_sampling")),
                ("human_file", live.human_file, frozen.get("human_file")),
                ("human_line", live.human_line, frozen.get("human_line")),
                ("human_summary", live.human_summary, frozen.get("human_summary")),
            ]
            for name, live_val, frozen_val in checks:
                if live_val != frozen_val:
                    drift_warnings.append(f"{frozen['sample_key']}: {name} changed")
                    break
    if drift_warnings:
        print("[validate] DRIFT WARNING: live judge output differs from manifest-frozen fields")
        for w in drift_warnings[:20]:
            print(f"  - {w}")
        if len(drift_warnings) > 20:
            print(f"  ... and {len(drift_warnings) - 20} more")

    # 8. Build plan: folders that need to be provisioned fresh.
    #    Existing rev folders are reused as-is; sample-key set growth in a
    #    stratum is tracked at the stratum level only, not per-verdict.
    new_folders: list[tuple[tuple[str, str, str, str], list[VerdictLocator]]] = []
    for folder_key, selected_locs in selected_folder_keys.items():
        if folder_key in existing_samples_by_folder:
            continue
        new_folders.append((folder_key, selected_locs))

    # 9. Dry-run: print plan and exit
    if dry_run:
        print("[validate] --dry-run: plan")
        print(f"  pooled_target: {target}")
        print(f"  derived per-stratum floor: {derived_floor}")
        print(f"  strata (pool_size / floor / census):")
        for (p, vc) in in_scope:
            sk = f"{p}/{vc}"
            st = manifest["strata"][sk]
            print(f"    {sk}: pool={st['pool_size']}, floor={st['per_cell_target']}, census={st['census']}")
        print(f"  new folders to provision: {len(new_folders)}")
        return

    # 10. Shuffle new_folders' sample_id assignment across strata for rater variety
    existing_sample_ids = {s["sample_id"] for s in manifest["samples"]}
    next_id = max(existing_sample_ids) + 1 if existing_sample_ids else 1
    assignment_rng = random.Random(seed ^ 0xABCD)
    new_folders_shuffled = list(new_folders)
    assignment_rng.shuffle(new_folders_shuffled)

    judge_prompt_text = get_prompt("judge", config)
    new_samples_records: list[dict] = []
    provisioned = 0

    for folder_key, selected_locs in new_folders_shuffled:
        project, condition, pr_dir, rev_dir = folder_key
        sample_id = next_id
        next_id += 1
        sample_folder = f"sample_{sample_id:03d}"
        sample_dir = samples_dir / sample_folder
        sample_dir.mkdir(parents=True, exist_ok=True)

        # Enumerate ALL verdicts in this rev folder
        all_locs, raw_judge_verdicts = _enumerate_folder_verdicts(out_dir, project, condition, pr_dir, rev_dir)
        pr_number = selected_locs[0].pr_number
        head_sha = selected_locs[0].head_sha
        merge_base_sha = selected_locs[0].merge_base_sha

        # Copy context.json and review.json
        src_review = out_dir / project / DIRS["reviews"] / condition / pr_dir / rev_dir / "review.json"
        src_context = out_dir / project / DIRS["reviews"] / condition / pr_dir / rev_dir / "context.json"
        if src_review.exists():
            (sample_dir / "review.json").write_text(src_review.read_text())
        if src_context.exists():
            (sample_dir / "context.json").write_text(src_context.read_text())

        # Write blank verdicts.json
        blank = _build_blank_verdicts(raw_judge_verdicts)
        (sample_dir / "verdicts.json").write_text(json.dumps(blank, indent=2) + "\n")

        # Bundle judge prompt
        (sample_dir / "judge_system_prompt.md").write_text(judge_prompt_text)

        # Provision worktree
        subject_repo = _resolve_subject_repo(config, project)
        worktree_path = sample_dir / "worktree"
        try:
            _provision_worktree(subject_repo, worktree_path, head_sha, merge_base_sha)
        except subprocess.CalledProcessError as e:
            print(f"[validate] worktree provisioning failed for {sample_folder}: {e.stderr}", file=sys.stderr)
            raise

        # Verdict entries: every verdict in the rev folder
        review_path = out_dir / project / DIRS["reviews"] / condition / pr_dir / rev_dir / "review.json"
        context_path = out_dir / project / DIRS["reviews"] / condition / pr_dir / rev_dir / "context.json"
        judge_path = out_dir / project / DIRS["judgments"] / condition / pr_dir / rev_dir / "verdicts.json"
        rel_review = _relpath_under(out_dir, review_path)
        rel_context = _relpath_under(out_dir, context_path)
        rel_judge = _relpath_under(out_dir, judge_path)

        verdict_entries = []
        for loc in all_locs:
            verdict_entries.append(_verdict_entry_dict(
                loc,
                reviewer_output_path=rel_review,
                judge_raw_output_path=rel_judge,
                context_path=rel_context,
            ))

        target_indices = [loc.human_comment_index for loc in all_locs]
        verdict_keys = [loc.sample_key for loc in all_locs]

        _write_rater_readme(sample_dir, sample_id, project, condition, pr_number)
        _write_workspace(sample_dir, worktree_path)
        _write_sample_info(
            sample_dir, sample_id, project, condition, pr_number, head_sha, merge_base_sha,
            target_indices, verdict_keys, worktree_relpath="worktree",
        )

        new_samples_records.append({
            "sample_id": sample_id,
            "sample_folder": sample_folder,
            "project": project,
            "condition": condition,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "merge_base_sha": merge_base_sha,
            "worktree_path": f"data/{VALIDATION_SUBDIR}/{SAMPLES_SUBDIR}/{sample_folder}/worktree",
            "target_indices": target_indices,
            "verdicts": verdict_entries,
        })
        provisioned += 1

    # 11. Stitch + atomic write
    manifest["samples"].extend(new_samples_records)
    _atomic_write_manifest(manifest_path, manifest)

    # Pooled actual = sum of stratum sample keys (== labels expected for those strata)
    pooled_keys = sum(len(s["sample_keys"]) for s in manifest["strata"].values())
    total_samples = len(manifest["samples"])
    total_verdict_entries = sum(len(s["verdicts"]) for s in manifest["samples"])
    print(f"[validate] setup complete")
    print(f"  pooled_target: {target} (per-cell floor: {derived_floor})")
    print(f"  pooled stratum sample keys: {pooled_keys} across {total_samples} sample folders")
    print(f"  total verdicts to label: {total_verdict_entries}")
    print(f"  new folders this run: {provisioned}")
    print(f"  per-stratum (project/verdict_class: keys / pool / census):")
    for k in sorted(manifest["strata"].keys()):
        st = manifest["strata"][k]
        print(f"    {k}: {len(st['sample_keys'])} / {st['pool_size']}"
              f"{' (CENSUS)' if st['census'] else ''}")
    print(f"  manifest: {manifest_path}")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    n = len(pairs)
    a = sum(1 for h, j in pairs if h == "match" and j == "match")
    b = sum(1 for h, j in pairs if h == "match" and j == "no_match")
    c = sum(1 for h, j in pairs if h == "no_match" and j == "match")
    d = sum(1 for h, j in pairs if h == "no_match" and j == "no_match")
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def compare_validation(
    config: Config,
    out_dir: Path,
    trial: bool = False,
    json_out: Path | None = None,
) -> None:
    """Join rater verdicts with manifest-frozen judge verdicts.

    Per (project, verdict_class) stratum agreement + kappa, plus a pooled
    roll-up across all 6 strata. Every labeled verdict in every visited
    sample folder contributes equally.
    """
    effective_out = _trial_redirect(out_dir, trial)
    validation_root = _validation_root(effective_out)
    manifest_path = _manifest_path(effective_out)
    samples_dir = _samples_dir(effective_out)

    manifest = load_json(manifest_path)
    if not manifest:
        print(f"[validate] no manifest at {manifest_path}; run 'relox validate setup' first")
        return

    pairs: list[tuple[str, str]] = []
    by_stratum: dict[str, list[tuple[str, str]]] = {}

    n_expected = 0
    n_labeled = 0

    for sample in manifest["samples"]:
        sample_folder = sample["sample_folder"]
        verdicts_path = samples_dir / sample_folder / "verdicts.json"
        live = load_json(verdicts_path)
        n_expected += len(sample.get("verdicts", []))
        if not live:
            continue
        live_by_idx = {lv.get("human_comment_index"): lv for lv in live.get("verdicts", [])}

        for v in sample.get("verdicts", []):
            idx = v["human_comment_index"]
            live_entry = live_by_idx.get(idx)
            if not live_entry:
                continue
            rater_verdict = live_entry.get("verdict")
            if rater_verdict is None:
                continue
            n_labeled += 1

            judge_verdict = v["judge_verdict_at_sampling"]
            pair = (rater_verdict, judge_verdict)
            pairs.append(pair)
            stratum_key = f"{sample['project']}/{judge_verdict}"
            by_stratum.setdefault(stratum_key, []).append(pair)

    def _agree(p: list[tuple[str, str]]) -> tuple[int, int, float]:
        if not p:
            return 0, 0, 0.0
        agree = sum(1 for h, j in p if h == j)
        return agree, len(p), agree / len(p)

    pooled_agree, pooled_n, pooled_rate = _agree(pairs)
    pooled_kappa = _cohens_kappa(pairs)

    per_stratum: dict[str, dict] = {}
    for k, group in by_stratum.items():
        ag, n, rate = _agree(group)
        per_stratum[k] = {
            "labeled": n,
            "agreements": ag,
            "rate": rate,
            "kappa": _cohens_kappa(group),
        }

    # Report
    lines: list[str] = []
    lines.append("# Judge Validation Agreement Report")
    lines.append("")
    lines.append(f"- Scheme: {manifest.get('scheme', 'unknown')}")
    lines.append(f"- Verdicts labeled: {n_labeled} / {n_expected}")
    lines.append(f"- Total sample folders: {len(manifest['samples'])}")
    if n_labeled < n_expected:
        lines.append("- **Preliminary**: compare run mid-labeling; stats reflect the labeled subset only.")
    lines.append("")
    lines.append("## Pooled agreement")
    lines.append("")
    if pairs:
        lines.append(f"- Pooled: {pooled_agree}/{pooled_n} = {pooled_rate:.1%}")
        lines.append(f"- Cohen's kappa: {pooled_kappa:.3f}")
    else:
        lines.append("- Pooled: n/a (no labels yet)")
    lines.append("")
    lines.append("## Per-stratum (project/verdict_class)")
    lines.append("")
    for k in sorted(per_stratum.keys()):
        st = per_stratum[k]
        lines.append(f"- {k}: {st['agreements']}/{st['labeled']} = {st['rate']:.1%}, kappa={st['kappa']:.3f}")
    lines.append("")

    (validation_root / "agreement_report.md").write_text("\n".join(lines))

    agreement_json = {
        "spec_version": manifest.get("spec_version"),
        "scheme": manifest.get("scheme"),
        "n_expected": n_expected,
        "n_labeled": n_labeled,
        "pooled": {
            "agreements": pooled_agree,
            "n": pooled_n,
            "agreement": pooled_rate,
            "kappa": pooled_kappa,
        },
        "per_stratum": per_stratum,
    }
    (validation_root / "agreement.json").write_text(json.dumps(agreement_json, indent=2) + "\n")
    if json_out:
        Path(json_out).write_text(json.dumps(agreement_json, indent=2) + "\n")

    print("[validate compare]")
    print(f"  labeled: {n_labeled}/{n_expected}")
    if pairs:
        print(f"  pooled: {pooled_agree}/{pooled_n} = {pooled_rate:.1%}, kappa={pooled_kappa:.3f}")
    for k in sorted(per_stratum.keys()):
        st = per_stratum[k]
        print(f"    {k}: {st['agreements']}/{st['labeled']} = {st['rate']:.1%}, k={st['kappa']:.3f}")
    print(f"  report: {validation_root / 'agreement_report.md'}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status_validation(
    config: Config,
    out_dir: Path,
    trial: bool = False,
) -> None:
    """Report labeling progress: total labeled/expected and per-stratum breakdown."""
    effective_out = _trial_redirect(out_dir, trial)
    manifest_path = _manifest_path(effective_out)
    samples_dir = _samples_dir(effective_out)

    manifest = load_json(manifest_path)
    if not manifest:
        print(f"[validate] no manifest at {manifest_path}")
        return

    total_expected = 0
    total_labeled = 0
    folders_fully = 0
    folders_partial = 0
    folders_empty = 0

    # Per (project, verdict_class) stratum counts: every verdict in every
    # sample folder is bucketed by its judge_verdict_at_sampling.
    per_stratum: dict[str, dict] = {}

    for sample in manifest["samples"]:
        sample_folder = sample["sample_folder"]
        verdicts_path = samples_dir / sample_folder / "verdicts.json"
        live = load_json(verdicts_path)

        folder_expected = len(sample["verdicts"])
        folder_labeled = 0

        total_expected += folder_expected

        live_by_idx = {lv.get("human_comment_index"): lv for lv in (live.get("verdicts", []) if live else [])}

        for v in sample["verdicts"]:
            idx = v["human_comment_index"]
            lv = live_by_idx.get(idx)
            labeled = lv is not None and lv.get("verdict") is not None

            stratum_key = f"{sample['project']}/{v['judge_verdict_at_sampling']}"
            st = per_stratum.setdefault(stratum_key, {"expected": 0, "labeled": 0})
            st["expected"] += 1
            if labeled:
                folder_labeled += 1
                st["labeled"] += 1

        total_labeled += folder_labeled

        if folder_labeled == folder_expected and folder_expected > 0:
            folders_fully += 1
        elif folder_labeled > 0:
            folders_partial += 1
        else:
            folders_empty += 1

    folders_total = len(manifest["samples"])
    print(f"[validate status]")
    print(f"  scheme: {manifest.get('scheme', 'unknown')}")
    print(f"  verdicts: {total_labeled} / {total_expected} labeled")
    print(f"  folders: {folders_fully} fully / {folders_partial} partial / {folders_empty} empty "
          f"(total {folders_total})")
    print(f"  per-stratum (project/verdict_class):")
    for k in sorted(per_stratum.keys()):
        v = per_stratum[k]
        print(f"    {k}: {v['labeled']}/{v['expected']}")
