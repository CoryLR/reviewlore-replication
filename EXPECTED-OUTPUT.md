# Expected output

This file documents the canonical stdout for the two ways of reproducing the paper's numbers from saved data. The primary path is `python3 compute_all_results.py` (no dependencies); the secondary path is the full `uv run relox` pipeline.

## Stdout from `python3 compute_all_results.py`

The script reads only data committed to `data/` and prints the paper's main result numbers. It requires Python 3.8+ and uses only the standard library — no `uv sync`, no API key. Output is deterministic across Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14 (verified on macOS and Linux).

The `Data:` line near the top shows the absolute path to your local `data/` directory and therefore varies by environment; everything else is bit-identical.

```

Paper result numbers computed from saved data

Data: <absolute-path-to-your-clone>/data
Projects: Storybook, Mermaid, ASE
Conditions: Generic, Tuned-100, Tuned-200

Recall by condition (per project + pooled)

  Project           Generic       Tuned-100       Tuned-200
  Storybook  23.6% (38/161)  27.3% (44/161)  26.1% (42/161)
  Mermaid     47.5% (28/59)   49.2% (29/59)   45.8% (27/59)
  ASE          31.8% (7/22)    40.9% (9/22)   50.0% (11/22)
  Pooled     30.2% (73/242)  33.9% (82/242)  33.1% (80/242)

Paired comparisons vs Generic (gained, lost, delta in pp)

Tuned-100 vs Generic:
  Project    delta pp  gained  lost
  Storybook      +3.7      17    11
  Mermaid        +1.7       6     5
  ASE            +9.1       4     2
  Pooled         +3.7      27    18

Tuned-200 vs Generic:
  Project    delta pp  gained  lost
  Storybook      +2.5      14    10
  Mermaid        -1.7       4     5
  ASE           +18.2       5     1
  Pooled         +2.9      23    16

Tuned-200 vs Tuned-100 (per project):
  Project    gained  lost
  Storybook      12    14
  Mermaid         4     6
  ASE             2     0

Statistical significance tests (CMH + McNemar exact)

- Pooled Tuned-100 vs Generic:
  - CMH p-value: 0.180
  - CMH common odds ratio: 1.50
  - McNemar exact p-value: 0.233

- Pooled Tuned-200 vs Generic:
  - CMH p-value: 0.262
  - CMH common odds ratio: 1.44
  - McNemar exact p-value: 0.337

Ensemble recall

  Ensemble                                  Recall      Lift
  Generic + Tuned-100              41.3% (100/242)  +11.1 pp
  Generic + Tuned-200               39.7% (96/242)   +9.5 pp
  Generic + Tuned-100 + Tuned-200  44.6% (108/242)  +14.4 pp

- Triple ensemble incremental lift over (Generic + Tuned-100): +3.3 pp

Text similarity averages (ROUGE-L + ChrF++)

ROUGE-L (avg):
  Project    Generic  Tuned-100  Tuned-200
  Storybook    0.117      0.117      0.119
  Mermaid      0.126      0.134      0.124
  ASE          0.102      0.117      0.124
  Pooled       0.118      0.121      0.121

ChrF++ (avg):
  Project    Generic  Tuned-100  Tuned-200
  Storybook    0.190      0.188      0.191
  Mermaid      0.206      0.223      0.212
  ASE          0.192      0.215      0.219
  Pooled       0.194      0.199      0.199

Per-category recall (n>=5 pooled)

Cells show recall % (matches); n is the pooled denominator.

  Sub-category                            n     Generic   Tuned-100   Tuned-200
  Logic Error                            54  20.4% (11)  31.5% (17)  31.5% (17)
  Structural Issues                      23   34.8% (8)   34.8% (8)   39.1% (9)
  Dead-Code Related Issues               18   50.0% (9)  66.7% (12)   50.0% (9)
  Documentation                          18   22.2% (4)   33.3% (6)   33.3% (6)
  Semantic Deviation                     17   29.4% (5)   47.1% (8)   35.3% (6)
  Unclear Code Descriptions              16   43.8% (7)   31.2% (5)   37.5% (6)
  API Misuse                             14   28.6% (4)   28.6% (4)   28.6% (4)
  Code Duplication                        9   55.6% (5)   55.6% (5)   55.6% (5)
  Language-Specific Standards             8   12.5% (1)   12.5% (1)   12.5% (1)
  Unchecked Return                        8   75.0% (6)   50.0% (4)   62.5% (5)
  Computation Efficiency                  6   50.0% (3)   33.3% (2)   16.7% (1)
  Redundancy Handling                     6   33.3% (2)    0.0% (0)   16.7% (1)
  Trailing/Unused/Incorrect Formattings   6    0.0% (0)   16.7% (1)   16.7% (1)
  Comment Requirements                    5    0.0% (0)   40.0% (2)   20.0% (1)
  Function Consistency                    5   20.0% (1)   20.0% (1)   40.0% (2)
  Naming Convention                       5   20.0% (1)   20.0% (1)   20.0% (1)

Rule counts and categorization axes

- Pooled Tuned-100 rule count (sum of sub-categories): 673

Top sub-categories by pooled count (Tuned-100; share = count
as percent of pooled total).
- Logic Error: 208 (30.9%)
- Structural Issues: 86 (12.8%)
- Unclear Code Descriptions: 75 (11.1%)
- Language-Specific Standards: 53 (7.9%)
- API Misuse: 35 (5.2%)
- Documentation: 20 (3.0%)
- Naming Standards: 18 (2.7%)
- Naming Convention: 17 (2.5%)
- Code Duplication: 16 (2.4%)
- Redundancy Handling: 16 (2.4%)
- Top 10 sub-categories pooled share: 80.8%

Per-project rule counts by snapshot volume
(Tuned-100 = most recent 100 tuning MRs, Tuned-200 = all 200).

Tuned-100:
  Dimension                        Storybook  Mermaid  ASE
  Total                                  249      200  224
  Code Defect                            143       74   60
  Maintainability and Readability         92      107   96
  Performance Issue                        2        6    8
  Code Style                              12       13   60

Tuned-200:
  Dimension                        Storybook  Mermaid  ASE
  Total                                  284      271  230
  Code Defect                            164      100   56
  Maintainability and Readability        100      110   95
  Performance Issue                        4        5    8
  Code Style                              16       56   71

Tuned-100 categorization, lintability axis:
  Dimension                          n  Lintable  Partial  Requires-LLM
  Code Defect                      277         4       40           233
  Maintainability and Readability  295         0       38           257
  Performance Issue                 16         0        3            13
  Code Style                        85        14       33            38

Tuned-100 categorization, generalizability axis:
  Dimension                          n  Project-specific  Generalizable
  Code Defect                      277               139            138
  Maintainability and Readability  295               164            131
  Performance Issue                 16                 7              9
  Code Style                        85                37             48

- Pooled categorization (n=673):
  - Lintable: 18 (2.7%)
  - Partially-lintable: 114 (16.9%)
  - Requires-LLM: 541 (80.4%)
  - Project-specific: 347 (51.6%)
  - Generalizable: 326 (48.4%)

- Requires-LLM share by dimension:
  - Code Defect: 84.1% (233/277)
  - Maintainability and Readability: 87.1% (257/295)
  - Performance Issue: 81.2% (13/16)
  - Code Style: 44.7% (38/85)

Pipeline cost (USD):

  Step              Storybook  Mermaid     ASE  Combined
  Filter (Stage 3)      $7.07    $4.42   $2.69    $14.18
  Labeling              $6.94    $4.30   $1.38    $12.62
  Extraction           $18.48   $19.59   $7.05    $45.12
  Consolidation        $14.11   $15.45   $9.50    $39.07
  Reviewer            $166.80  $101.64  $28.44   $296.88
  Judge                $50.98   $14.67   $6.39    $72.04
  Total               $264.39  $160.08  $55.46   $479.92

(Storybook + Mermaid Filter (Stage 3) costs are back-estimated)

- Reviewer + Judge:
  - combined: $368.93
  - share of pipeline total: 76.9%
  - per-condition pass cost (combined / 3 conditions): $122.98

Test setup (held-out PRs and test revisions)

  Project    Held-out PRs  Test revisions
  Storybook            30              76
  Mermaid              19              36
  ASE                  10              16

Judge validation (rater-vs-judge agreement, same-file-and-line audit)

- Pooled rater-vs-judge agreement:
  - overall: 50/50 = 100.0%
  - Cohen's kappa: 1.000
  - when judge said match: 25/25
  - when judge said no_match: 25/25

Same-file-and-line audit: judge no_match verdicts whose human comment shares
(file, line) with any AI inline comment, pooled across projects x conditions.
  Project    same file+line  all no_match
  Storybook               9           359
  Mermaid                 5            93
  ASE                     1            39
  Pooled                 15           491
- Pooled same file+line / all no_match: 3.1%
```

Determinism check: stripping the `Data:` line and piping the rest to `md5sum` yields `8f7600c0ecc2272347412e6ca690330c` on every supported Python version on both macOS and Linux. Equivalent: `python3 compute_all_results.py | grep -v '^Data:' | md5sum`.

---

## Additional reference: `uv run relox evaluate ase`

The full `relox` pipeline path (Step 2 of the README) requires `uv sync` and exercises the same saved envelopes via the replay backend. It is not needed to verify the paper's numbers — the script above already covers that — but is preserved here for anyone running the full pipeline end-to-end.

### Stdout from `uv run relox evaluate ase`

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

### Other Step 2 commands

The remaining Step 2 commands print short status messages: `relo filter` / `relo label` / `relox split` / `relo categorize` / `relo optimize` print `skipped_existing` (or, for the optimizer, `Replay backend: N MR(s) lack saved extractions; skipping...`); `relox review ase` prints `All revisions already processed.`; `pooled_stats.py` / `pooled_aggregates.py` / `rule_counts.py` print Markdown tables of cross-project aggregates. None of these recompute LLM output; they only re-derive parsed outputs from the shipped envelopes.

### Files written by `relox evaluate ase`

`uv run relox evaluate ase` writes (or rewrites) exactly three files:

- `data/ase/9_evaluation/metrics.json` — top-level metric keys (`judge_recall`, `reachable_recall`, `non_llm_metrics`, `threshold_calibration`, `mcnemar`, `paired_overlap`, `per_category_recall`, `costs`, `tokens`) plus a `conditions` list. Each metric maps to a per-condition sub-object keyed by `generic` / `tuned_100` / `tuned_200`; e.g. `metrics.judge_recall.tuned_100` holds the tuned-100 recall, `metrics.non_llm_metrics.tuned_100` holds `{avg_rouge_l, avg_chrf, n_comments}`.
- `data/ase/9_evaluation/comparison.md` — Markdown rendering of the same numbers.
- `data/ase/9_evaluation/metadata.json` — evaluate run timestamp + config provenance.

No other files in `data/` are modified. Every `raw.json` envelope, every `review.json`, every `verdicts.json`, and every input under `1_collected/` -- `5_optimization/` is read-only.

### Headline numbers per subject (from `metrics.json`)

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
