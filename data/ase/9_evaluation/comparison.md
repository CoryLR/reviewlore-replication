# Evaluation Results: ase

## Judge Recall

- generic: 0.318
- tuned_100: 0.409
- tuned_200: 0.500

## Non-LLM Metrics

- generic: ROUGE-L=0.102, ChrF++=0.192
- tuned_100: ROUGE-L=0.117, ChrF++=0.215
- tuned_200: ROUGE-L=0.124, ChrF++=0.219

## Statistical Tests

- generic_vs_tuned_100: p=0.6875 (adjusted), OR=0.5
- generic_vs_tuned_200: p=0.4375 (adjusted), OR=0.2

## Costs

- collect: $0.00
- consolidation: $9.50
- evaluate: $0.00
- extraction: $7.05
- filter: $2.69
- judge: $6.39
- label: $1.38
- review: $28.44
- split: $0.00
- **Total: $55.46**
