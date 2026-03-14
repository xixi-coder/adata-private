# -*- coding: utf-8 -*-
import os
import sys


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy


if __name__ == "__main__":
    strategy = ShortTermDisagreementStrategy()
    strategy.load_data()
    print(f"缓存初始化完成: {strategy.full_cache_file}")
