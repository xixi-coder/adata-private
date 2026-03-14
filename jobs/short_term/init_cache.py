# -*- coding: utf-8 -*-
import os
import sys


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from jobs.common.cloud_cache_sync import SHARED_MARKET_CACHE_ARCHIVE, sync_cache_from_drive, sync_cache_to_drive
from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy


if __name__ == "__main__":
    sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    strategy = ShortTermDisagreementStrategy()
    strategy.load_data(allow_online_update=True)
    sync_cache_to_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    print(f"缓存初始化完成: {strategy.full_cache_file}")
