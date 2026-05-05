# Evaluation Results: storybook

## Judge Recall

- generic: 0.236
- tuned_100: 0.273
- tuned_200: 0.261

## Non-LLM Metrics

- generic: ROUGE-L=0.117, ChrF++=0.190
- tuned_100: ROUGE-L=0.117, ChrF++=0.188
- tuned_200: ROUGE-L=0.119, ChrF++=0.191

## Statistical Tests

- generic_vs_tuned_100: p=0.6899 (adjusted), OR=0.6470588235294118
- generic_vs_tuned_200: p=0.6899 (adjusted), OR=0.7142857142857143

## Costs

- collect: $0.00
- consolidation: $14.11
- evaluate: $0.00
- extraction: $18.48
- filter: $0.00
- judge: $50.98
- label: $6.94
- review: $166.80
- split: $0.00
- **Total: $257.32**
