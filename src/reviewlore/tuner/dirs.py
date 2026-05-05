"""Centralized pipeline directory names.

Single source of truth for numbered output directory names.
All modules import from here instead of using hardcoded strings.
"""

DIRS = {
    "collected": "1_collected",
    "filtered": "2_filtered",
    "labeled": "3_labeled",
    "split": "4_split",
    "optimization": "5_optimization",
    "reviews": "6_reviews",
    "judgments": "7_judgments",
    "validation": "8_validation",
    "case_studies": "8_case_studies",
    "evaluation": "9_evaluation",
}
