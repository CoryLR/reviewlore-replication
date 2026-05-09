# Tailored vs. Generic LLM Code Review: Replication Package

Replication artifact for the SWE 699 final report (George Mason University, Spring 2026).

## Verification with saved data

Stdlib-only script (Python 3.8+, no `uv` or LLM key) that computes the paper's result numbers from `data/` in one script.

```bash
python3 compute_all_results.py
```

---

## Pipeline Prerequisites

- uv (Python version, package, and venv manager): `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux), `brew install uv`, or [docs](https://docs.astral.sh/uv/) for Windows.
- git 2.5+.

## Step 1: install dependencies

```bash
uv sync
```

To revert any local changes after a run, run `git restore .` or re-clone.

## Step 2: run the pipeline on ASE (~30 s)

Copy the whole codeblock to use as one command, or copy individual lines to see each step on its own. Stops on the first failure.

```bash
(
  set -e
  # Skip `relo collect`, it needs GITLAB_TOKEN, previous outputs are in data/ase/1_collected/.
  # uv run relo collect ase --merged-before 2026-04-17

  uv run relo filter ase
  uv run relo label ase
  uv run relox split ase
  uv run relo optimize ase --mr-range most_recent_100 -y
  uv run relo optimize ase --mr-range all -y
  uv run relo categorize ase --snapshot tuned_100
  uv run relox review ase
  uv run relox evaluate ase
  uv run python scripts/pooled_stats.py        # paired McNemar / Holm-Bonferroni / CMH
  uv run python scripts/pooled_aggregates.py   # weighted ROUGE-L / ChrF++
  uv run python scripts/rule_counts.py         # per-dimension rule counts
)
```

Compare the `relox evaluate ase` stdout to [EXPECTED-OUTPUT.md](EXPECTED-OUTPUT.md). Per-condition recall must match exactly; ROUGE-L / ChrF++ may differ in the 4th decimal. EXPECTED-OUTPUT.md also lists headline numbers for mermaid and storybook (the shipped data covers all 3 subjects; swap `ase` for `mermaid` or `storybook` to re-run the per-project commands on those).

## Running tests

```bash
uv run pytest tests/
```

## Live mode (optional, requires a Claude Code subscription)

```bash
# Install Claude Code: https://docs.claude.com/en/docs/claude-code
claude /login                                                    # one-time OAuth setup
export REVIEWLORE_BACKEND=claude-code

uv run relox review ase --condition tuned_100 --force            # makes real LLM calls
uv run relox evaluate ase

unset REVIEWLORE_BACKEND                                         # back to replay
```

Backend precedence (highest wins): `--backend` flag > `REVIEWLORE_BACKEND` env var > `reviewlore.yaml` (`backend.name`).

Live mode overwrites the shipped envelopes under `data/<project>/{6_reviews,7_judgments}/`; `git restore data/` to revert. To target SHAs outside the original test set, run `git -C subject-repos/<project> fetch origin <sha>` first. A full 3-project x 3-condition live re-run is ~25 hours wall and ~$480 in Claude Code subscription model-usage.

## File index

Every file and directory in this artifact, with a 1-2 sentence description.

### Top-level files

- `.gitattributes` -- marks bundled bare-repo pack and index files as binary so git does not try to diff them.
- `.gitignore` -- excludes local runtime scratch (caches, virtualenvs), regenerated sample worktrees, trial / live output trees, and platform metadata.
- `README.md` -- this file.
- `EXPECTED-OUTPUT.md` -- literal expected stdout for `relox evaluate ase` (the grading-relevant output of Step 2), plus per-subject headline recall numbers for storybook, mermaid, and ase.
- `compute_all_results.py` -- no-dependency saved-data verification; walks `data/` and prints the paper's result numbers with labels. Stdlib only, runs on Python 3.8+.
- `pyproject.toml` -- Python project metadata: Python 3.13 floor, runtime deps (httpx, click, pyyaml, NLTK, sacrebleu, etc.), dev deps (pytest), and the `[project.scripts]` entries that expose `relo` (tuning CLI) and `relox` (experiment CLI).
- `uv.lock` -- fully-pinned dependency lockfile consumed by `uv sync` for bit-identical environments.
- `reviewlore.yaml` -- single source of truth for runtime config: default backend (`replay`), per-stage models / budgets / prompt paths, optimizer batch size, concurrency limits, filter rules, conditions list, and per-project (platform, repo, language, subject_repo) entries.

### `prompts/` -- system prompts loaded by each LLM step

- `prompts/reviewer_system.md` (v3) -- generic reviewer system prompt. The tuned conditions append the project's tuned snapshot (`tuned_100.md` or `tuned_200.md`) at runtime.
- `prompts/judge_system.md` (v3) -- judge system prompt. v3 implements coverage-based matching (many-to-one and one-to-many both allowed) and emits `verdicts.json` with per-comment `match` / `no_match` decisions plus reasoning.
- `prompts/optimizer_extract.md` (v2) -- per-MR extraction prompt; one call per tuning MR produces candidate rules from human review comments + diff.
- `prompts/optimizer_consolidate.md` (v2) -- batch consolidation prompt; merges, dedupes, and prunes candidate rules in chronological batches of 10 to produce the running rule set.
- `prompts/filter_actionability.md` (v1) -- Stage 3 actionability filter prompt run by `relo filter`; classifies each surviving comment as actionable or not.
- `prompts/label_bitsai.md` (v1) -- BitsAI-CR taxonomy labeling prompt; tags each filtered comment with one of {Code Defect, Code Style, Maintenance & Readability, Performance}.
- `prompts/categorize_rules.md` (v1) -- post-hoc categorizer prompt; assigns BitsAI-CR sub-category, generalizability (project-specific vs generalizable), and lintability (lintable / partially_lintable / requires_llm) to each rule in a tuned snapshot.

### `src/reviewlore/` -- ReviewLore source tree

- `__init__.py` -- exposes `__version__`.
- `backends/__init__.py` -- empty package marker.
- `backends/base.py` -- agent backend abstraction: `AgentResult` dataclass, `AgentBackend` protocol, JSON-from-text extraction (envelope + 4-strategy fallback parser), and the in-process backend registry (`register_backend`, `get_backend`).
- `backends/claude_code.py` -- live `claude-code` backend: spawns the Claude Code CLI as a subprocess, passes prompts via stdin, captures the `--output-format json` envelope, and returns an `AgentResult`.
- `backends/replay.py` -- replay backend used by the artifact: reads saved `raw.json` envelopes, runs the same parser the live backend runs, returns indistinguishable `AgentResult`s, raises `ReplayMissingError` on cache miss when `strict=true` (the artifact default).
- `experiment/__init__.py` -- empty package marker.
- `experiment/cli.py` -- `relox` Click entry point (review, judge, evaluate, validate, status, case-studies).
- `experiment/review.py` -- reviewer agent driver: condition-aware prompt assembly (generic vs tuned with the rule snapshot appended) and per-revision invocation with retry/recovery.
- `experiment/judge.py` -- judge agent driver: AI-comment formatting with explicit `index` fields, output validation, and per-revision invocation with retry.
- `experiment/review_judge.py` -- review + judge orchestrator: per-revision worktree lifecycle, in-process worktree lock, thread-pooled parallel execution, skip-existing semantics, force-rerun support.
- `experiment/evaluate.py` -- recall computation, ROUGE-L / ChrF++ scoring, threshold calibration against judge agreement, McNemar's exact tests, paired-overlap tables, per-category recall, cost + token aggregation. Includes the shallow-bundle two-dot fallback for `_load_touched_files_per_rev` so the artifact's bare repos resolve reachability.
- `experiment/case_studies.py` -- mines `recall_vs_similarity.json` (cases where judge verdict and ROUGE-L disagree, bucketed A/B/C/D) and `lost_comments.json` (the c-cell of the McNemar table: comments that generic caught but tuned-100 missed).
- `experiment/validate.py` -- verdict-level case-cohort sampling for blind judge validation: stratifies on (project, judge_verdict), seeded-shuffles each pool, builds the manifest, supports immutable-floor extension and census fallback for rare classes.
- `experiment/validate_audit.py` -- judge-validation audit helpers: same-line no-match auto-confirm, rater-label rollup, per-class agreement, Cohen's kappa, M-of-N counts.
- `experiment/split.py` -- temporal split into tuning/test sets with per-revision testability assessment (non-squash merge + reachable commit + non-final-revision comments).
- `experiment/status.py` -- pipeline status reporting for `relox status`; combines tuner status with experiment-specific stages (split, review, judge, evaluate).
- `experiment/survey.py` -- stub for the project-discovery survey step; CP4 deferred this in favor of the existing 298-repo survey results documented in the paper.
- `tuner/__init__.py` -- empty package marker.
- `tuner/cli.py` -- `relo` Click entry point (collect, filter, label, split, optimize, categorize, status).
- `tuner/config.py` -- YAML config loader, dataclass models for `Config` / `BackendConfig` / `PromptConfig` / `ProjectConfig`, `resolve_output_dir` (CLI flag > `RELO_OUT` env var > yaml `output_dir`), `resolve_project`, `get_prompt`.
- `tuner/dirs.py` -- single source of truth for the pipeline's numbered output directory names (`1_collected`, ..., `9_evaluation`).
- `tuner/state.py` -- atomic JSON writes (write-temp-then-rename), metadata read/write, staleness detection.
- `tuner/collect.py` -- MR/PR collection from GitHub GraphQL and GitLab REST APIs; pins `originalLine` in the GraphQL query so outdated review threads keep their anchor line numbers.
- `tuner/filter.py` -- three-stage comment filter: bot/structural removal, resolution-state tagging, and the LLM Stage 3 actionability classifier.
- `tuner/label.py` -- BitsAI-CR taxonomy labeling pass over the filtered comments.
- `tuner/optimize.py` -- two-phase rule induction: parallel per-MR extraction (Phase 2a) followed by sequential, batch-of-10 consolidation (Phase 2b) that emits the tuned snapshots.
- `tuner/categorize.py` -- post-hoc categorizer: per-rule BitsAI sub-category + generalizability + lintability axes, run once per project per snapshot for the CP6 RQ2 expansion and lint-vs-LLM Discussion section.
- `tuner/status.py` -- pipeline status reporting for `relo status`.

### `tests/` -- pytest suite

- `tests/__init__.py`, `tests/backends/__init__.py`, `tests/experiment/__init__.py`, `tests/tuner/__init__.py` -- empty package markers.
- `tests/backends/test_replay.py` -- replay backend round-trip, `ReplayMissingError` on cache miss, permissive (`strict=false`) mode, registry presence, envelope-and-marker parity with the live backend.
- `tests/experiment/conftest.py` -- shared fixtures: synthetic data/ trees and synthetic git repos so validate.py tests run without touching real subject clones.
- `tests/experiment/test_evaluate_reachability.py` -- regression test for the shallow-bundle two-dot fallback in `_load_touched_files_per_rev`; reproduces the orphan-history scenario the artifact's bare bundles trigger.
- `tests/experiment/test_evaluate_tokens.py` -- pins the token aggregation fallback that re-derives `input_tokens` / `output_tokens` from the nested envelope `usage` block (the CP4/CP5 top-level fields were always 0).
- `tests/experiment/test_judge.py` -- judge output validation / normalization and index-aware comment formatting.
- `tests/experiment/test_validate.py` -- 18 tests covering case-cohort sampling: seed determinism, verdict-pool shuffle stability, census fallback, target-raise extension semantics, drift guards, manifest atomic write, CLI smoke test.
- `tests/tuner/test_collect_normalize.py` -- pins `originalLine` into the GitHub GraphQL query and asserts the `line -> originalLine` fallback in the comment normalizer; a regression here would resurface the outdated-line-number bug the judge-fix track addressed.

### `scripts/` -- analysis helpers shipped with the artifact

- `scripts/pooled_stats.py` -- pooled and stratified statistical tests for paired conditions: per-project pairwise McNemar with Holm-Bonferroni correction, pooled McNemar, Cochran-Mantel-Haenszel test, ensemble recall promotion thresholds.
- `scripts/pooled_aggregates.py` -- pooled, weighted non-LLM metric aggregates (ROUGE-L, ChrF++) across projects, weighted by per-stratum `n_comments`.
- `scripts/rule_counts.py` -- per-BitsAI-dimension rule counts per project per volume; reads tuned snapshots under `5_optimization/snapshots/`.
- `scripts/reconstruct_sample_worktrees.sh` -- bash helper that provisions each `data/judge_validation/samples/sample_NN/worktree/` directory from the corresponding bundled bare subject-repo at the recorded `head_sha`; also patches each `workspace.code-workspace` to re-add the worktree folder entry. Run from the artifact root.

### `data/` -- pipeline outputs (the replication corpus)

For each project `<P>` in {`storybook`, `mermaid`, `ase`}:

- `data/<P>/1_collected/` -- raw GitHub/GitLab MR/PR data, one `pr_<N>.json` per merged PR (250 per project; 220 for ASE, since the project did not have 250 qualifying merged MRs in the collection window). `metadata.json` records the collection timestamp + collection config.
- `data/<P>/2_filtered/` -- output of the three-stage filter (bot / resolution / actionability), one `pr_<N>.json` per surviving PR.
- `data/<P>/3_labeled/` -- BitsAI-CR taxonomy labels per surviving thread, mirroring the `2_filtered/` layout.
- `data/<P>/4_split/` -- temporal tuning/test split: `tuning.txt` / `test.txt` are flat PR-number lists, `testable_revisions.json` lists the per-revision testability metadata (head_sha, merge_base_sha, comment counts) the reviewer + judge pipeline iterates over, `metadata.json` records the split timestamp and cutoff.
- `data/<P>/5_optimization/` -- optimizer state and outputs:
  - `extraction/` -- per-MR candidate rules from Phase 2a (one `pr_<N>.json` per tuning MR plus `metadata.json`).
  - `consolidation_100/` -- Phase 2b batch outputs for the tuned-100 snapshot (`batch_01.json` through `batch_NN.json` plus `metadata.json`).
  - `consolidation_200/` -- Phase 2b batch outputs for the tuned-200 snapshot.
  - `snapshots/tuned_100.{json,md}`, `snapshots/tuned_200.{json,md}` -- final consolidated rule sets in machine and human form.
  - `snapshots/tuned_100_categorized.{json,md}` plus `_metadata.json` and `_raw.json` -- output of `relo categorize` with the lintability / generalizability axes.
  - `snapshots/full_pass/snapshot_NNN.{json,md}` -- intermediate snapshots taken every 25 MRs during the full-200 consolidation pass.
- `data/<P>/6_reviews/<condition>/pr_<N>/rev_<sha>/` -- reviewer outputs per (condition, PR, revision):
  - `raw.json` -- the saved Claude Code envelope (input to the replay backend; never modified by replication runs).
  - `review.json` -- parsed reviewer output: comments with `file_path`, `line_number`, `severity`, `message`, and the explicit `index` field added during judge-fix-2.
  - `context.json` -- the full reviewer prompt context (file diff, existing human comments, etc.).
  - `system_prompt.md` -- the exact reviewer system prompt used for this run (generic or tuned snapshot).
- `data/<P>/7_judgments/<condition>/pr_<N>/rev_<sha>/` -- judge outputs per (condition, PR, revision):
  - `raw.json` -- saved judge envelope.
  - `verdicts.json` -- per-human-comment verdict (`match` or `no_match`), `matched_ai_comment_index`, `reasoning`.
- `data/<P>/9_evaluation/` -- aggregated metrics:
  - `metrics.json` -- recall, ROUGE-L, ChrF++, McNemar, threshold calibration, paired overlap, per-category recall, cost, tokens. This is the file the quick-start regenerates.
  - `comparison.md` -- Markdown rendering of the same numbers.
  - `metadata.json` -- evaluate run timestamp + provenance.

Plus the cross-project judge validation tree:

- `data/judge_validation/manifest.json` -- sampling manifest: 18 sample folders, 50 verdicts across 6 strata ((project, judge_verdict)).
- `data/judge_validation/agreement.json`, `data/judge_validation/agreement_report.md` -- rater-label rollup: per-class agreement, Cohen's kappa, M-of-N counts.
- `data/judge_validation/samples/sample_NN/` -- one folder per sampled revision:
  - `README.md` -- per-sample human-rater instructions, prefixed with the artifact-note banner explaining the stripped `worktree/`.
  - `sample_info.json` -- which (project, judge_verdict) cells this sample contributes to and the inclusion-driving sample keys.
  - `context.json` -- copy of the reviewer context for this revision.
  - `review.json` -- copy of the reviewer output (with the explicit `index` field) for this revision.
  - `verdicts.json` -- the rater-fillable verdicts file, frozen at `judge_verdict_at_sampling`.
  - `judge_system_prompt.md` -- copy of the judge prompt used at sampling time.
  - `workspace.code-workspace` -- VS Code multi-root workspace pre-configured for the rater task; the artifact strips the worktree folder entry, so run `scripts/reconstruct_sample_worktrees.sh` first if you want the SCM diff view.

### `subject-repos/` -- shallow bare subject-repo bundles

- `subject-repos/storybook/`, `subject-repos/mermaid/`, `subject-repos/ase/` -- shallow bare git clones containing only the head and merge-base SHAs of each test revision. Each SHA is reachable via `refs/replication/<sha>` so `git gc` cannot prune it. The `origin` remote points at the upstream URL (`https://github.com/storybookjs/storybook.git`, `https://github.com/mermaid-js/mermaid.git`, `https://gitlab.com/ase/ase.git`) so live-mode reruns can `git fetch` additional history on demand.

## Troubleshooting

- `ReplayMissingError`: a step tried to replay a saved LLM output that does not exist on disk. This usually means the command is being run on a project, condition, or revision that was not part of the original experiment. Use the canned commands in this README (which target only revisions present in the test set), or switch to live mode.
- `uv: command not found`: install uv via the curl/powershell/brew instructions in the Prerequisites section, then re-open the shell.
- Subject repo missing SHA in live mode: the bundled bare repositories contain only the test SHAs. To target a SHA outside the original test set, run `git -C subject-repos/<project> fetch origin <sha>` first.
- `relox evaluate` reports `Reachability: 0 revs mapped`: the shipped artifact uses a two-dot diff fallback in `_load_touched_files_per_rev` so the shallow bare bundles resolve; if you see zero mapped revs, you are running the artifact against a modified `evaluate.py` that has lost the fallback. Run `uv run pytest tests/experiment/test_evaluate_reachability.py` to confirm.
- VS Code workspace flashes "missing folder" on a sample folder: the artifact strips `data/judge_validation/samples/sample_NN/worktree/` and removes the corresponding entry from `workspace.code-workspace`. Run `bash scripts/reconstruct_sample_worktrees.sh` from the artifact root before opening the workspace.
