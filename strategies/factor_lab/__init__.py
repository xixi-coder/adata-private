"""A-share daily factor research helpers."""

from .factor_engine import (
    CORE_15_FACTORS,
    add_forward_returns,
    align_financials_to_daily,
    apply_universe_filters,
    compute_a_share_factors,
    evaluate_factor_ic,
    preprocess_factors,
    quantile_group_test,
    run_core_15_pipeline,
)

__all__ = [
    "CORE_15_FACTORS",
    "add_forward_returns",
    "align_financials_to_daily",
    "apply_universe_filters",
    "compute_a_share_factors",
    "evaluate_factor_ic",
    "preprocess_factors",
    "quantile_group_test",
    "run_core_15_pipeline",
]

