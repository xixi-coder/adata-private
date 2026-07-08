# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from jobs.common.send_a_share_runner_email import build_runner_email_body


class AShareRunnerEmailTest(unittest.TestCase):
    def test_build_email_body_includes_summary_and_task_output(self):
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
            (volatility_dir / "latest_email_body.txt").write_text("波动结构正文", encoding="utf-8")

            manifest_dir = base / "data/cache"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "three_dim_cache_manifest.json").write_text(
                json.dumps({"status": "ok"}, ensure_ascii=False),
                encoding="utf-8",
            )

            body = build_runner_email_body(summary, base)

        self.assertIn("Profile: eod", body)
        self.assertIn("- volatility: success, 12.3s, returncode=0", body)
        self.assertIn("波动结构正文", body)
        self.assertIn('"status": "ok"', body)


if __name__ == "__main__":
    unittest.main()
