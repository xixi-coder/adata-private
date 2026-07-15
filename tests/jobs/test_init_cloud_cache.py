# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import pandas as pd

fake_market = types.SimpleNamespace(get_market_index=lambda *args, **kwargs: pd.DataFrame())
fake_stock = types.SimpleNamespace(market=fake_market)
sys.modules.setdefault("adata", types.SimpleNamespace(stock=fake_stock))
sys.modules.setdefault(
    "jobs.common.cloud_cache_sync",
    types.SimpleNamespace(
        sync_cache_from_drive=lambda *args, **kwargs: False,
        sync_cache_to_drive=lambda *args, **kwargs: False,
        write_json=lambda path, payload: None,
    ),
)

from jobs.three_dim_resonance import init_cloud_cache


class InitCloudCacheTest(unittest.TestCase):
    def test_update_benchmarks_writes_all_configured_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = init_cloud_cache.FiveYearCloudCacheBuilder()
            builder.cache_dir = tmpdir
            builder.benchmark_file = os.path.join(tmpdir, "benchmark_000300.csv")
            builder.benchmark_files = {
                code: os.path.join(tmpdir, config["file"])
                for code, config in init_cloud_cache.BENCHMARK_INDEXES.items()
            }

            def fake_get_market_index(index_code, start_date, k_type=1):
                return pd.DataFrame(
                    {
                        "index_code": [index_code, index_code],
                        "trade_date": ["2026-07-08", "2026-07-09"],
                        "close": [100.0, 101.0],
                    }
                )

            with patch.object(
                init_cloud_cache.adata.stock.market,
                "get_market_index",
                side_effect=fake_get_market_index,
            ) as market_index:
                result = builder._update_benchmarks("2026-07-08")

            self.assertEqual(
                result,
                {
                    "000300": True,
                    "399006": True,
                    "000688": True,
                },
            )
            self.assertEqual(market_index.call_count, 3)
            for config in init_cloud_cache.BENCHMARK_INDEXES.values():
                benchmark_path = os.path.join(tmpdir, config["file"])
                self.assertTrue(os.path.exists(benchmark_path))
                benchmark = pd.read_csv(benchmark_path)
                self.assertEqual(benchmark["trade_date"].astype(str).max(), "2026-07-08")


if __name__ == "__main__":
    unittest.main()
