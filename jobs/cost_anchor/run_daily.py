# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.cost_anchor import CostAnchorConfig, CostAnchorStrategy


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
DEFAULT_MANUAL_PATH = os.path.join(CURRENT_DIR, "manual_anchors.csv")


def _format_pct(value: float) -> str:
    if pd.isna(value):
        return ""
    return f"{value * 100:.1f}%"


def _write_summary(path: str, watchlist: pd.DataFrame, side_reports: dict[str, pd.DataFrame]) -> None:
    lines = [
        "# 三锚成本观察池",
        "",
        "筛选逻辑：当前价距离成本锚在配置区间内，成本锚包括定增价、员工持股成本、实控人/高管增持均价。",
        "",
    ]
    if watchlist.empty:
        lines.append("本次没有筛出贴近成本锚的标的。")
    else:
        display = watchlist.head(50).copy()
        display["distance_pct"] = display["distance_pct"].map(_format_pct)
        display["score"] = display["score"].round(1)
        display["anchor_date"] = pd.to_datetime(display["anchor_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        cols = [
            "stock_code",
            "stock_name",
            "anchor_type",
            "anchor_price",
            "current_price",
            "distance_pct",
            "anchor_date",
            "lockup",
            "holder_name",
            "signal",
            "score",
        ]
        lines.append(display[cols].to_markdown(index=False))

    shareholder_events = side_reports.get("shareholder_increase_events", pd.DataFrame())
    employee_events = side_reports.get("employee_plan_events", pd.DataFrame())
    lines.extend(
        [
            "",
            "## 待补成本事件",
            "",
            f"- 股东增持事件：{len(shareholder_events)} 条，已导出 `shareholder_increase_events.csv`。",
            f"- 员工持股计划事件：{len(employee_events)} 条，已导出 `employee_plan_events.csv`。",
            "- `trade_average_price` 为空的事件需要从公告补入 `manual_anchors.csv`，补完后会进入观察池。",
            "",
        ]
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 A 股三锚成本观察池")
    parser.add_argument("--lookback-days", type=int, default=180, help="事件回看天数")
    parser.add_argument("--near-low", type=float, default=-0.08, help="相对锚位下沿，例如 -0.08")
    parser.add_argument("--near-high", type=float, default=0.10, help="相对锚位上沿，例如 0.10")
    parser.add_argument("--min-executive-amount", type=float, default=100_000, help="高管增持聚合最小金额")
    parser.add_argument("--manual-anchor-path", default=DEFAULT_MANUAL_PATH, help="手工成本锚 CSV 路径")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="输出目录")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    config = CostAnchorConfig(
        lookback_days=args.lookback_days,
        near_low=args.near_low,
        near_high=args.near_high,
        min_executive_amount=args.min_executive_amount,
        manual_anchor_path=args.manual_anchor_path,
    )
    strategy = CostAnchorStrategy(config=config)
    strategy.write_manual_template(args.manual_anchor_path)

    watchlist, side_reports = strategy.build_watchlist()
    watchlist_path = os.path.join(args.output_dir, "cost_anchor_watchlist.csv")
    summary_path = os.path.join(args.output_dir, "latest_summary.md")
    shareholder_path = os.path.join(args.output_dir, "shareholder_increase_events.csv")
    employee_path = os.path.join(args.output_dir, "employee_plan_events.csv")

    watchlist.to_csv(watchlist_path, index=False, encoding="utf-8-sig")
    side_reports.get("shareholder_increase_events", pd.DataFrame()).to_csv(
        shareholder_path, index=False, encoding="utf-8-sig"
    )
    side_reports.get("employee_plan_events", pd.DataFrame()).to_csv(employee_path, index=False, encoding="utf-8-sig")
    _write_summary(summary_path, watchlist, side_reports)

    print(f"观察池: {watchlist_path} ({len(watchlist)} rows)")
    print(f"摘要: {summary_path}")
    print(f"手工成本锚模板: {args.manual_anchor_path}")


if __name__ == "__main__":
    main()
