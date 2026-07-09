# -*- coding: utf-8 -*-
import tempfile
import unittest
from unittest.mock import patch

from jobs import a_share_runner


class AShareRunnerTest(unittest.TestCase):
    def test_resolve_intraday_profile(self):
        self.assertEqual(a_share_runner.resolve_task_names("intraday"), ["short_term_intraday"])

    def test_resolve_intraday_pm_profile_to_intraday_task(self):
        self.assertEqual(a_share_runner.resolve_task_names("intraday_pm"), ["short_term_intraday"])

    def test_tasks_override_profile(self):
        self.assertEqual(
            a_share_runner.resolve_task_names("eod", "volatility,boll"),
            ["volatility", "boll"],
        )

    def test_unknown_task_raises(self):
        with self.assertRaises(ValueError):
            a_share_runner.resolve_task_names("eod", "missing_task")

    def test_dry_run_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(a_share_runner, "OUTPUT_DIR", tmpdir):
            summary = a_share_runner.run_profile(
                profile="eod",
                task_names=["volatility", "boll"],
                trade_date="2026-04-17",
                dry_run=True,
            )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["completed_count"], 2)
        self.assertEqual([item["status"] for item in summary["tasks"]], ["dry_run", "dry_run"])

    def test_task_env_does_not_override_existing_env(self):
        task = a_share_runner.TASKS["short_term_intraday"]
        with patch.dict("os.environ", {"INTRADAY_CACHE_TTL_SECONDS": "30"}, clear=True):
            env = a_share_runner._build_env(task, trade_date="2026-04-17")

        self.assertEqual(env["INTRADAY_CACHE_TTL_SECONDS"], "30")
        self.assertEqual(env["TRADE_DATE"], "2026-04-17")

    def test_manual_intraday_forces_latest_minute_data(self):
        task = a_share_runner.TASKS["short_term_intraday"]
        with patch.dict("os.environ", {"A_SHARE_MANUAL_TRIGGER": "true"}, clear=True):
            env = a_share_runner._build_env(task, trade_date="")

        self.assertEqual(env["INTRADAY_SKIP_RUNTIME_WINDOW"], "true")
        self.assertEqual(env["INTRADAY_FORCE_LATEST_MINUTE"], "true")

    def test_maintenance_tasks_are_tuned_for_runtime(self):
        shared_cache = a_share_runner.TASKS["shared_cache"]
        dividend_cache = a_share_runner.TASKS["dividend_cache"]

        self.assertEqual(shared_cache.env["CACHE_STOCK_MAX_WORKERS"], "8")
        self.assertEqual(shared_cache.env["AUTO_COMMIT_MINUTES"], "0")
        self.assertNotIn("--sync-shared-cache", dividend_cache.args)


if __name__ == "__main__":
    unittest.main()
