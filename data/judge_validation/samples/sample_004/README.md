## Artifact note: worktree/ stripped

The replication artifact ships subject-repos/<project>/ as shallow bare
clones and strips the per-sample `worktree/` directory to keep the archive
small. The labeling task only needs `context.json`, `review.json`, and
`verdicts.json`, which are all present here. To recover the VS Code SCM
diff view, run from the artifact root:

```
bash scripts/reconstruct_sample_worktrees.sh
```

That helper provisions every sample's `worktree/` from the bundled bare
subject-repo at the recorded `head_sha`, after which the folder layout
matches the live validation workflow described below.

# Sample 004 — storybook/tuned_100 PR #33970

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
