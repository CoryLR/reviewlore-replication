# Expected output

The grading-relevant output of Step 2 is the stdout of `uv run relox evaluate ase`. This file documents that, the files it writes, and the headline numbers reproduced for all three subjects.

## Stdout from `uv run relox evaluate ase`

```
Evaluating ase
  Step 1: Loading verdicts...
  22 human comments, 22 complete sets, 0 incomplete
  Reachability: 16 revs mapped, 0 verdict records flagged unreachable
  Step 2: Computing judge recall...
    generic: pooled=0.318  reachable=0.318  (n_reachable=22, unreachable_excluded=0)
    tuned_100: pooled=0.409  reachable=0.409  (n_reachable=22, unreachable_excluded=0)
    tuned_200: pooled=0.500  reachable=0.500  (n_reachable=22, unreachable_excluded=0)
  Step 3: Computing ROUGE-L and ChrF++...
    generic: ROUGE-L=0.102, ChrF++=0.192
    tuned_100: ROUGE-L=0.117, ChrF++=0.215
    tuned_200: ROUGE-L=0.124, ChrF++=0.219
  Step 4: Threshold calibration...
  Step 5: McNemar's test...
    generic_vs_tuned_100: p=0.6875, OR=0.5
    generic_vs_tuned_200: p=0.2188, OR=0.2
    overlap generic_vs_tuned_100: both=5 generic_only=2 tuned_100_only=4 neither=11 n=22 p=0.6875 OR=0.5
    overlap generic_vs_tuned_200: both=6 generic_only=1 tuned_200_only=5 neither=10 n=22 p=0.2188 OR=0.2
  Step 6: Per-category recall...
    per_category: 0/66 records without category match
  Step 7: Aggregating costs...
    Grand total: $55.46
    Tokens (review+judge): in=12,225,353, out=1,170,787, calls=96

  Evaluation complete. Results in <path>/data/ase/9_evaluation
```

The wall path (`<path>`), dollar total, token totals, and per-condition recall match the build machine bit-for-bit because the replay backend reads exact saved envelopes. ROUGE-L / ChrF++ rounding may differ in the 4th decimal across NLTK / sacrebleu version skew.

## Other Step 2 commands

The remaining Step 2 commands print short status messages: `relo filter` / `relo label` / `relox split` / `relo categorize` / `relo optimize` print `skipped_existing` (or, for the optimizer, `Replay backend: N MR(s) lack saved extractions; skipping...`); `relox review ase` prints `All revisions already processed.`; `pooled_stats.py` / `pooled_aggregates.py` / `rule_counts.py` print Markdown tables of cross-project aggregates. None of these recompute LLM output; they only re-derive parsed outputs from the shipped envelopes.

## Files written by `relox evaluate ase`

`uv run relox evaluate ase` writes (or rewrites) exactly three files:

- `data/ase/9_evaluation/metrics.json` — top-level metric keys (`judge_recall`, `reachable_recall`, `non_llm_metrics`, `threshold_calibration`, `mcnemar`, `paired_overlap`, `per_category_recall`, `costs`, `tokens`) plus a `conditions` list. Each metric maps to a per-condition sub-object keyed by `generic` / `tuned_100` / `tuned_200`; e.g. `metrics.judge_recall.tuned_100` holds the tuned-100 recall, `metrics.non_llm_metrics.tuned_100` holds `{avg_rouge_l, avg_chrf, n_comments}`.
- `data/ase/9_evaluation/comparison.md` — Markdown rendering of the same numbers.
- `data/ase/9_evaluation/metadata.json` — evaluate run timestamp + config provenance.

No other files in `data/` are modified. Every `raw.json` envelope, every `review.json`, every `verdicts.json`, and every input under `1_collected/` -- `5_optimization/` is read-only.

## Headline numbers per subject

Reading `data/<project>/9_evaluation/metrics.json` reproduces these recall + textual-similarity numbers (Table 1 of the paper). The shipped data covers all three subjects; running `uv run relox evaluate <project>` for `mermaid` or `storybook` re-derives these from their saved envelopes.

- storybook (`n_comments` = 161 per condition):
  - generic: recall = 38 / 161 = 23.6%, ROUGE-L = 0.117, ChrF++ = 0.190
  - tuned_100: recall = 44 / 161 = 27.3%, ROUGE-L = 0.117, ChrF++ = 0.188
  - tuned_200: recall = 42 / 161 = 26.1%, ROUGE-L = 0.119, ChrF++ = 0.191
- mermaid (`n_comments` = 59 per condition):
  - generic: recall = 28 / 59 = 47.5%, ROUGE-L = 0.126, ChrF++ = 0.206
  - tuned_100: recall = 29 / 59 = 49.2%, ROUGE-L = 0.134, ChrF++ = 0.223
  - tuned_200: recall = 27 / 59 = 45.8%, ROUGE-L = 0.124, ChrF++ = 0.212
- ase (`n_comments` = 22 per condition):
  - generic: recall = 7 / 22 = 31.8%, ROUGE-L = 0.102, ChrF++ = 0.192
  - tuned_100: recall = 9 / 22 = 40.9%, ROUGE-L = 0.117, ChrF++ = 0.215
  - tuned_200: recall = 11 / 22 = 50.0%, ROUGE-L = 0.124, ChrF++ = 0.219

Pooled cross-project numbers in the paper aggregate these per-project files; `scripts/pooled_stats.py` and `scripts/pooled_aggregates.py` (run as part of Step 2) emit the pooled McNemar / CMH / weighted ROUGE-L / ChrF++ tables.
