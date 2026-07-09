# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"
CACHE_SYNCED_ENV = "A_SHARE_CACHE_ALREADY_SYNCED"
CACHE_CONSUMER_TASKS = {"volatility", "boll", "a_share_review", "three_dim"}


@dataclass(frozen=True)
class RunnerTask:
    name: str
    script: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def command(self) -> list[str]:
        return [sys.executable, self.script, *self.args]


TASKS: dict[str, RunnerTask] = {
    "short_term_intraday": RunnerTask(
        name="short_term_intraday",
        script="jobs/short_term/intraday_strategy_live.py",
        env={"INTRADAY_CACHE_TTL_SECONDS": "120"},
        description="短线分时扫描：T-1 日线候选 + 最新分钟确认。",
    ),
    "short_term_minute_replay": RunnerTask(
        name="short_term_minute_replay",
        script="jobs/short_term/intraday_strategy_live.py",
        env={
            "INTRADAY_CACHE_TTL_SECONDS": "0",
            "INTRADAY_SKIP_RUNTIME_WINDOW": "true",
            "INTRADAY_FORCE_LATEST_MINUTE": "true",
        },
        description="短线候选股分钟缓存采集：盘后补采当日 1 分钟数据。",
    ),
    "volatility": RunnerTask(
        name="volatility",
        script="jobs/volatility/run_daily.py",
        env={
            "VOLATILITY_ENABLE_MINE_CLEARANCE": "true",
            "VOLATILITY_MAX_MINE_CHECKS": "120",
            "VOLATILITY_MAX_TAG_CHECKS": "80",
        },
        description="波动结构日线扫描。",
    ),
    "boll": RunnerTask(
        name="boll",
        script="jobs/boll/run_daily.py",
        env={"BOLL_ENABLE_MINE_CLEARANCE": "true", "BOLL_MAX_MINE_CHECKS": "120"},
        description="BOLL 战法日线扫描。",
    ),
    "a_share_review": RunnerTask(
        name="a_share_review",
        script="jobs/a_share_allocation/run_daily.py",
        env={
            "A_SHARE_REVIEW_ALLOW_ONLINE_UPDATE": "false",
            "A_SHARE_REVIEW_UNIVERSE_SIZE": "800",
            "A_SHARE_REVIEW_MAX_POSITIONS": "18",
            "A_SHARE_REVIEW_REBALANCE_PERIOD": "20",
            "A_SHARE_REVIEW_INCLUDE_DIVIDEND": "true",
        },
        description="每日投资复盘。",
    ),
    "three_dim": RunnerTask(
        name="three_dim",
        script="jobs/three_dim_resonance/run_daily.py",
        description="三维共振日线建议。",
    ),
    "shared_cache": RunnerTask(
        name="shared_cache",
        script="jobs/three_dim_resonance/init_cloud_cache.py",
        env={
            "CACHE_STOCK_MAX_WORKERS": "8",
            "AUTO_COMMIT_MINUTES": "0",
        },
        description="共享日线/财务/基准缓存维护。",
    ),
    "dividend_cache": RunnerTask(
        name="dividend_cache",
        script="jobs/dividend_sync/sync_dividend_cache_once.py",
        description="分红缓存维护。",
    ),
}


PROFILES: dict[str, tuple[str, ...]] = {
    "intraday": ("short_term_intraday",),
    # 兼容历史手动输入；下午版不再是独立任务，统一走 short_term_intraday。
    "intraday_pm": ("short_term_intraday",),
    "minute_cache": ("short_term_minute_replay",),
    "eod": ("volatility", "boll", "a_share_review", "three_dim"),
    "maintenance": ("shared_cache", "dividend_cache"),
    "all": ("short_term_intraday", "volatility", "boll", "a_share_review", "three_dim"),
}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_task_names(profile: str, tasks: str = "") -> list[str]:
    selected = _split_csv(tasks) if tasks else list(PROFILES.get(profile, ()))
    if not selected:
        raise ValueError(f"未知 profile: {profile}")
    unknown = [name for name in selected if name not in TASKS]
    if unknown:
        raise ValueError(f"未知 task: {','.join(unknown)}")
    return selected


def _is_manual_trigger() -> bool:
    manual_env = os.getenv("A_SHARE_MANUAL_TRIGGER", "").strip().lower()
    return manual_env in {"1", "true", "yes", "y", "on"} or os.getenv("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


def _build_env(task: RunnerTask, trade_date: str) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in task.env.items():
        env.setdefault(key, value)
    if task.name == "short_term_intraday" and _is_manual_trigger():
        env.setdefault("INTRADAY_SKIP_RUNTIME_WINDOW", "true")
        env.setdefault("INTRADAY_FORCE_LATEST_MINUTE", "true")
    if trade_date:
        env["TRADE_DATE"] = trade_date
    return env


def _run_task(task: RunnerTask, trade_date: str, dry_run: bool) -> dict:
    command = task.command()
    started = time.perf_counter()
    payload = {
        "name": task.name,
        "script": task.script,
        "command": command,
        "description": task.description,
        "status": "dry_run" if dry_run else "running",
        "returncode": None,
        "elapsed_seconds": 0.0,
    }
    print(f"[runner] start {task.name}: {' '.join(command)}", flush=True)
    if dry_run:
        payload["elapsed_seconds"] = 0.0
        return payload
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=_build_env(task, trade_date), check=False)
    payload["returncode"] = result.returncode
    payload["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    payload["status"] = "success" if result.returncode == 0 else "failed"
    print(f"[runner] done {task.name}: {payload['status']} ({payload['elapsed_seconds']}s)", flush=True)
    return payload


def _sync_shared_cache_once(task_names: Iterable[str], dry_run: bool) -> bool:
    if dry_run or not CACHE_CONSUMER_TASKS.intersection(task_names):
        return False
    started = time.perf_counter()
    print("[runner] sync shared cache from Google Drive: start", flush=True)
    try:
        from jobs.common.cloud_cache_sync import sync_cache_from_drive

        synced = sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    except Exception as exc:
        print(f"[runner] sync shared cache from Google Drive: failed ({exc})", flush=True)
        return False
    elapsed = round(time.perf_counter() - started, 2)
    if synced:
        os.environ[CACHE_SYNCED_ENV] = "true"
        print(f"[runner] sync shared cache from Google Drive: done ({elapsed}s)", flush=True)
    else:
        print(f"[runner] sync shared cache from Google Drive: skipped ({elapsed}s)", flush=True)
    return synced


def run_profile(
    profile: str,
    task_names: Iterable[str],
    trade_date: str = "",
    dry_run: bool = False,
    continue_on_error: bool = False,
) -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    task_names = list(task_names)
    cache_synced = _sync_shared_cache_once(task_names, dry_run=dry_run)
    tasks = [TASKS[name] for name in task_names]
    results = []
    for task in tasks:
        result = _run_task(task, trade_date=trade_date, dry_run=dry_run)
        results.append(result)
        if result["status"] == "failed" and not continue_on_error:
            break

    failed = [item for item in results if item["status"] == "failed"]
    summary = {
        "profile": profile,
        "trade_date": trade_date,
        "dry_run": dry_run,
        "task_count": len(tasks),
        "completed_count": len(results),
        "failed_count": len(failed),
        "status": "failed" if failed else "success",
        "shared_cache_synced": cache_synced,
        "tasks": results,
    }
    summary_path = os.path.join(OUTPUT_DIR, "latest_runner_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="统一任务调度入口")
    parser.add_argument(
        "--profile",
        default=os.getenv("A_SHARE_RUNNER_PROFILE", "intraday"),
        choices=sorted(PROFILES.keys()),
        help="任务组合：intraday / eod / maintenance / all；intraday_pm 会兼容映射到 intraday",
    )
    parser.add_argument(
        "--tasks",
        default=os.getenv("A_SHARE_RUNNER_TASKS", ""),
        help="逗号分隔的任务名；设置后覆盖 profile 默认任务。",
    )
    parser.add_argument("--trade-date", default=os.getenv("TRADE_DATE", ""), help="传给子任务的 TRADE_DATE")
    parser.add_argument("--dry-run", action="store_true", help="只展开任务，不执行子脚本")
    parser.add_argument("--continue-on-error", action="store_true", help="子任务失败后继续执行后续任务")
    args = parser.parse_args()

    task_names = resolve_task_names(args.profile, args.tasks)
    summary = run_profile(
        profile=args.profile,
        task_names=task_names,
        trade_date=args.trade_date,
        dry_run=args.dry_run,
        continue_on_error=args.continue_on_error,
    )
    return 1 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
