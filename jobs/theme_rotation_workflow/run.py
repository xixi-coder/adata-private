# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
DEFAULT_THEME_RADAR = os.path.join(PROJECT_ROOT, "jobs", "theme_monitor", "outputs", "latest_theme_radar.csv")
DEFAULT_MARKET_CONTEXT = os.path.join(PROJECT_ROOT, "jobs", "theme_monitor", "outputs", "latest_market_context.json")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.theme_rotation_workflow import ThemeRotationWorkflow
from jobs.common.email_format import set_rich_email_content


def _now_shanghai() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _read_csv(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_csv(path)


def _read_json(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_csv(path: str, df: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _safe_fetch(label: str, fetcher, *args, **kwargs) -> pd.DataFrame:
    try:
        df = fetcher(*args, **kwargs)
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            return df
        return pd.DataFrame(df)
    except Exception as exc:
        print(f"[theme-rotation] {label} 获取失败: {exc}")
        return pd.DataFrame()


def _fetch_live_theme_radar() -> tuple[pd.DataFrame, dict[str, Any]]:
    import adata
    from jobs.theme_monitor.market_context import MarketContextCollector
    from strategies.theme_monitor import ThemeMonitorStrategy

    hot = adata.sentiment.hot
    hot_stocks = _safe_fetch("同花顺热股榜", hot.hot_rank_100_ths)
    hot_concepts = _safe_fetch("同花顺热门概念", hot.hot_concept_20_ths, plate_type=1)
    hot_industries = _safe_fetch("同花顺热门行业", hot.hot_concept_20_ths, plate_type=2)
    popularity_stocks = _safe_fetch("东方财富人气榜", hot.pop_rank_100_east)
    market_context, _ = MarketContextCollector(adata).collect()
    radar, _ = ThemeMonitorStrategy(top_limit=30, representative_limit=5).build_theme_radar(
        hot_stocks=hot_stocks,
        hot_concepts=hot_concepts,
        hot_industries=hot_industries,
        popularity_stocks=popularity_stocks,
        previous_snapshot={},
    )
    return radar, market_context


def _load_positions(path: str) -> dict[str, float]:
    if not path:
        return {}
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return {}
    return {str(k): float(v) for k, v in payload.items() if isinstance(v, int | float | str) and str(v).strip()}


def _build_markdown(summary: dict[str, Any], plan: pd.DataFrame, run_time: str) -> str:
    lines = [
        "# A 股主线轮动 Workflow",
        "",
        f"- 运行时间：{run_time}",
        f"- 市场风险偏好：{summary.get('risk_mode', '未知')}",
        f"- 主线：{summary.get('main_line') or '暂无'}",
        f"- 副主线：{summary.get('satellite_line') or '暂无'}",
        f"- 成长进攻仓目标：{summary.get('growth_weight', 0):.0%}",
        f"- 现金/防守缓冲：{summary.get('cash_or_defense_weight', 0):.0%}",
        "",
        "## 组合动作",
        "",
        "| 排名 | 篮子 | 动作 | 目标仓位 | 建议ETF | 综合分 | 趋势 | 资金 | 催化 | 兑现 | 拥挤 | 说明 |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    if plan.empty:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | 无可用数据 |")
    else:
        for _, row in plan.iterrows():
            lines.append(
                "| {rank} | {basket} | {action} | {weight_band} | {suggested_etfs} | {final_score:.2f} | "
                "{trend_score:.2f} | {fund_score:.2f} | {catalyst_score:.2f} | "
                "{evidence_score:.2f} | {crowding_score:.2f} | {note} |".format(**row.to_dict())
            )
    lines.extend(
        [
            "",
            "## 执行规则",
            "",
            "- 主线：可以作为本周进攻核心，但拥挤分高时只做回撤确认。",
            "- 副主线：作为成长内部对冲或补涨观察，不和主线同时拉满。",
            "- 观察：保留小仓位或只跟踪，不主动加仓。",
            "- 回避：等待趋势、资金或事件重新确认。",
            "",
            "提示：本报告只用于流程化复盘和仓位纪律，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def _send_email_if_configured(body: str) -> None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过主线轮动邮件通知。")
        return
    subject = os.getenv("THEME_ROTATION_EMAIL_SUBJECT", "主线轮动")
    title = os.getenv("THEME_ROTATION_EMAIL_TITLE", "A 股主线轮动 Workflow")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    set_rich_email_content(msg, body, title=title)
    recipients = [item.strip() for item in mail_to.split(",") if item.strip()]
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)
    print(f"已发送主线轮动邮件通知：{subject}，收件人：{mail_to}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股主线轮动 workflow")
    parser.add_argument("--theme-radar", default=DEFAULT_THEME_RADAR, help="Theme monitor CSV path.")
    parser.add_argument("--market-context", default=DEFAULT_MARKET_CONTEXT, help="Market context JSON path.")
    parser.add_argument("--positions", default="", help="Optional JSON: basket name -> current weight.")
    parser.add_argument("--fetch-live", action="store_true", help="Fetch live theme data instead of reading files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if args.fetch_live:
        try:
            theme_radar, market_context = _fetch_live_theme_radar()
        except Exception as exc:
            print(f"[theme-rotation] 在线抓取失败，输出空计划: {exc}")
            theme_radar, market_context = pd.DataFrame(), {}
    else:
        theme_radar = _read_csv(args.theme_radar)
        market_context = _read_json(args.market_context)
        if theme_radar.empty:
            print("[theme-rotation] 未找到主题雷达文件，改用在线抓取。")
            try:
                theme_radar, market_context = _fetch_live_theme_radar()
            except Exception as exc:
                print(f"[theme-rotation] 在线抓取失败，输出空计划: {exc}")
                theme_radar, market_context = pd.DataFrame(), market_context or {}

    positions = _load_positions(args.positions)
    workflow = ThemeRotationWorkflow()
    plan, summary = workflow.build_plan(theme_radar, market_context=market_context, current_positions=positions)
    run_time = _now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "run_time": run_time,
        "summary": summary,
        "plan": plan.to_dict("records"),
    }
    markdown = _build_markdown(summary, plan, run_time)

    _write_csv(os.path.join(OUTPUT_DIR, "latest_theme_rotation_plan.csv"), plan)
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), payload)
    with open(os.path.join(OUTPUT_DIR, "latest_report.md"), "w", encoding="utf-8") as f:
        f.write(markdown + "\n")
    print(markdown)
    if os.getenv("THEME_ROTATION_SEND_EMAIL_IN_SCRIPT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}:
        _send_email_if_configured(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
