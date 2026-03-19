# -*- coding: utf-8 -*-
import json
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from jobs.three_dim_resonance.live.strategy import ThreeDimResonanceLiveStrategy


if __name__ == "__main__":
    strategy = ThreeDimResonanceLiveStrategy(
        initial_capital=1_000_000,
        max_positions=8,
        universe_size=1800,
        max_position_weight=0.20,
    )
    summary = strategy.run_daily(os.getenv("TRADE_DATE", "").strip())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
