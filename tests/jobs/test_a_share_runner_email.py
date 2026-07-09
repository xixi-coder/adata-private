# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from jobs.common.send_a_share_runner_email import build_task_email_body, iter_task_emails


class AShareRunnerEmailTest(unittest.TestCase):
    def test_iter_task_emails_splits_outputs_by_task(self):
        summary = {
            "profile": "eod",
            "trade_date": "2026-07-08",
            "task_count": 2,
            "completed_count": 2,
            "failed_count": 0,
            "status": "success",
            "tasks": [
                {"name": "volatility", "status": "success", "returncode": 0, "elapsed_seconds": 12.3},
                {"name": "shared_cache", "status": "success", "returncode": 0, "elapsed_seconds": 4.5},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            volatility_dir = base / "jobs/volatility/outputs"
            volatility_dir.mkdir(parents=True)
            (volatility_dir / "latest_email_body.txt").write_text(
                "A股波动结构扫描\n\n一、资金聚焦\n1. 半导体 | 2只 | 均分88\n",
                encoding="utf-8",
            )

            manifest_dir = base / "data/cache"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "three_dim_cache_manifest.json").write_text(
                json.dumps(
                    {
                        "updated_at": "2026-07-09 09:05:51",
                        "stock_count": 5295,
                        "selected_code_count": 5035,
                        "pending_stock_update_count": 2469,
                        "updated_stock_count": 1785,
                        "checked_stock_count": 2424,
                        "refreshed_finance_count": 0,
                        "benchmark_updated": False,
                        "benchmark_updates": {
                            "000300": False,
                            "399006": True,
                            "000688": False,
                        },
                        "cache_changed": True,
                        "auto_commit_minutes": 30,
                        "auto_commit_count": 4,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            emails = iter_task_emails(summary, base)

        self.assertEqual([email[0] for email in emails], ["[成功] A股波动结构扫描", "[成功] A股共享缓存维护"])
        self.assertIn("序号 | 方向 | 数量 | 均分/代表", emails[0][2])
        self.assertNotIn("Profile: eod", emails[0][2])
        self.assertIn("日线补齐: 待检查 2469只", emails[1][2])
        self.assertIn("创业板指(399006): 已更新", emails[1][2])
        self.assertIn("运行中自动回传: 每 30 分钟检查一次，共回传 4 次", emails[1][2])

    def test_boll_email_removes_operational_lines_and_adds_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output_dir = base / "jobs/boll/outputs"
            output_dir.mkdir(parents=True)
            (output_dir / "latest_email_body.txt").write_text(
                "\n".join(
                    [
                        "A股BOLL战法扫描",
                        "",
                        "- 运行时间: 2026-07-08 18:31:45",
                        "- 请求日期: 2026-07-08",
                        "- 信号日期: 2026-07-07",
                        "",
                        "下轨止跌观察",
                        "1. 688663 | 新风光 | 84.48 | 低 | 82.84 | 80.671 | 83.668 | 79.058 | 入选依据",
                    ]
                ),
                encoding="utf-8",
            )

            body = build_task_email_body("boll", base)

        self.assertNotIn("运行时间", body)
        self.assertNotIn("请求日期", body)
        self.assertIn("序号 | 股票代码 | 股票名称 | 评分", body)

    def test_dividend_cache_email_formats_manifest_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            output_dir = base / "data/cache/dividend"
            output_dir.mkdir(parents=True)
            (output_dir / "dividend_sync_manifest.json").write_text(
                json.dumps(
                    {
                        "synced_at": "2026-07-09 11:35:16",
                        "archive_name": "dividend_cache_bundle.tar.gz",
                        "total_codes": 5035,
                        "max_workers": 10,
                        "refresh_days": 180,
                        "retry": 2,
                        "summary": {
                            "skip_fresh": 4395,
                            "keep_existing": 0,
                            "created_non_empty": 541,
                            "updated_non_empty": 0,
                            "created_empty": 93,
                            "updated_empty": 0,
                            "failed": 6,
                        },
                        "failed_codes": [
                            {"code": "600066", "error": "Invalid \\\\escape: line 1 column 13231"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            body = build_task_email_body("dividend_cache", base)

        self.assertIn("新鲜跳过: 4395只", body)
        self.assertIn("新建缓存: 非空 541只，空结果 93只", body)
        self.assertIn("600066 | Invalid", body)


if __name__ == "__main__":
    unittest.main()
