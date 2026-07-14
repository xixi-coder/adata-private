# -*- coding: utf-8 -*-
from __future__ import annotations

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

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import adata
from jobs.common.email_format import set_rich_email_content
from jobs.common.local_env import load_local_env
from jobs.theme_monitor.market_context import MarketContextCollector
from adata.stock.market.concept_capital_flow import ConceptCapitalFlow
from strategies.theme_monitor import ThemeMonitorStrategy


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
STATE_DIR = os.path.join(CURRENT_DIR, "state")
SNAPSHOT_FILE = os.path.join(STATE_DIR, "latest_snapshot.json")


def _now_shanghai() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return default if value == "" else int(value)


def _safe_fetch(label: str, fetcher, *args, **kwargs) -> pd.DataFrame:
    try:
        df = fetcher(*args, **kwargs)
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            return df
        return pd.DataFrame(df)
    except Exception as exc:
        print(f"[theme-monitor] {label} 获取失败: {exc}")
        return pd.DataFrame()


def _read_snapshot() -> dict[str, Any]:
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _write_csv(path: str, df: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _format_market_context(market_context: dict[str, Any]) -> list[str]:
    if not market_context:
        return ["市场环境暂无可用数据。"]
    a_share_items = []
    for item in market_context.get("a_share_indexes", [])[:4]:
        if item.get("note"):
            continue
        a_share_items.append(f"{item.get('name', '')}{item.get('change_pct', 0):+.2f}%")
    global_items = []
    for item in market_context.get("global_indexes", [])[:8]:
        if item.get("note"):
            continue
        global_items.append(f"{item.get('name', '')}{item.get('change_pct', 0):+.2f}%")
    lines = [
        (
            f"A股风险偏好: {market_context.get('risk_appetite', '未知')} | "
            f"北向净流入: {market_context.get('northbound_net_inflow_yi', 0)}亿 | "
            f"A股均涨跌: {market_context.get('a_share_avg_change_pct', 0):+.2f}%"
        ),
        (
            f"海外风向(非A股涨跌): AI={market_context.get('external_ai_tailwind', '未知')} / "
            f"半导体={market_context.get('external_semi_tailwind', '未知')} / "
            f"港股中国资产={market_context.get('hk_china_tailwind', '未知')}"
        ),
    ]
    if a_share_items:
        lines.append("A股指数: " + "，".join(a_share_items))
    if global_items:
        lines.append("海外指数: " + "，".join(global_items))
    return lines


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(str(value).replace("%", "").strip())
    except Exception:
        return default


def _format_yi(value: Any) -> str:
    return f"{_to_float(value) / 100_000_000:+.2f}亿"


def _format_capital_flow_section(concept_capital_flow: pd.DataFrame) -> list[str]:
    if concept_capital_flow is None or concept_capital_flow.empty:
        return ["无可用板块资金流数据。"]
    required = {"index_name", "main_net_inflow"}
    if not required.issubset(set(concept_capital_flow.columns)):
        return ["板块资金流字段不完整。"]
    flow = concept_capital_flow.copy()
    flow["main_net_inflow_num"] = pd.to_numeric(flow["main_net_inflow"], errors="coerce").fillna(0.0)
    lines = ["净流入 Top10:"]
    for idx, (_, row) in enumerate(flow.sort_values("main_net_inflow_num", ascending=False).head(10).iterrows(), 1):
        lines.append(
            f"{idx}. {row.get('index_name', '')} | 主力净流入{_format_yi(row.get('main_net_inflow'))} | "
            f"涨跌{_to_float(row.get('change_pct')):+.2f}% | 净占比{_to_float(row.get('main_net_inflow_rate')):+.2f}%"
        )
    lines.append("净流出 Top5:")
    for idx, (_, row) in enumerate(flow.sort_values("main_net_inflow_num", ascending=True).head(5).iterrows(), 1):
        lines.append(
            f"{idx}. {row.get('index_name', '')} | 主力净流入{_format_yi(row.get('main_net_inflow'))} | "
            f"涨跌{_to_float(row.get('change_pct')):+.2f}% | 净占比{_to_float(row.get('main_net_inflow_rate')):+.2f}%"
        )
    return lines


def _build_email_body(
    summary: dict[str, Any],
    radar: pd.DataFrame,
    hot_stocks: pd.DataFrame,
    market_context: dict[str, Any],
    concept_capital_flow: pd.DataFrame,
) -> str:
    lines = [
        "盘面舆论板块雷达",
        f"{summary['run_time']} | 主题 {summary['theme_count']} 个 | 热股 {summary['hot_stock_count']} 只",
        "",
        "一、市场环境",
    ]
    lines.extend(_format_market_context(market_context))
    lines.extend(["", "二、板块资金净流入"])
    lines.extend(_format_capital_flow_section(concept_capital_flow))

    lines.extend(["", "三、升温/发酵主题"])
    if radar.empty:
        lines.append("无可用主题数据。")
    else:
        focus = radar[radar["status"].isin(["新晋升温", "快速升温", "持续发酵"])].head(8)
        if focus.empty:
            focus = radar.head(5)
        for _, row in focus.iterrows():
            lines.append(
                f"{int(row['rank'])}. {row['theme']} | 分{row['score']} | {row['status']} | "
                f"热股{row['hot_stock_count']} | 热股均涨跌{row.get('avg_hot_stock_change_pct', 0):+.2f}% | "
                f"主力净流入{row.get('main_net_inflow_yi', 0):+.2f}亿 | "
                f"{row['representatives']}"
            )

    lines.extend(["", "四、降温/分歧"])
    cooling = (
        radar[radar["status"].isin(["降温", "震荡观察", "高热分歧"])].head(5)
        if not radar.empty
        else pd.DataFrame()
    )
    if cooling.empty:
        lines.append("暂无明显降温主题。")
    else:
        for _, row in cooling.iterrows():
            lines.append(f"{row['theme']} | 分{row['score']} | {row['status']} | {row['note']}")

    lines.extend(["", "五、热股榜 Top10"])
    if hot_stocks.empty:
        lines.append("无可用热股数据。")
    else:
        for _, row in hot_stocks.head(10).iterrows():
            lines.append(
                f"{row.get('rank', '')}. {row.get('stock_code', '')} {row.get('short_name', '')} "
                f"{row.get('change_pct', '')}% | {row.get('concept_tag', '')}"
            )
    lines.extend(["", "提示: 本雷达只做盘面热度和主题方向监控，不构成交易建议。"])
    return "\n".join(lines)


def _send_email_if_configured(body: str) -> None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过邮件通知。")
        return
    msg = EmailMessage()
    msg["Subject"] = "盘面舆论板块雷达"
    msg["From"] = smtp_user
    msg["To"] = mail_to
    set_rich_email_content(msg, body, title="盘面舆论板块雷达")
    recipients = [item.strip() for item in mail_to.split(",") if item.strip()]
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)


def main() -> int:
    load_local_env()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    hot = adata.sentiment.hot
    hot_stocks = _safe_fetch("同花顺热股榜", hot.hot_rank_100_ths)
    hot_concepts = _safe_fetch("同花顺热门概念", hot.hot_concept_20_ths, plate_type=1)
    hot_industries = _safe_fetch("同花顺热门行业", hot.hot_concept_20_ths, plate_type=2)
    popularity_stocks = _safe_fetch("东方财富人气榜", hot.pop_rank_100_east)
    concept_capital_flow = _safe_fetch(
        "东方财富概念板块资金流",
        ConceptCapitalFlow().all_capital_flow_east,
        days_type=1,
    )
    market_context, market_context_df = MarketContextCollector(adata).collect()

    previous_snapshot = _read_snapshot()
    strategy = ThemeMonitorStrategy(
        top_limit=_read_int_env("THEME_MONITOR_TOP_LIMIT", 20),
        representative_limit=_read_int_env("THEME_MONITOR_REPRESENTATIVE_LIMIT", 5),
    )
    radar, snapshot = strategy.build_theme_radar(
        hot_stocks=hot_stocks,
        hot_concepts=hot_concepts,
        hot_industries=hot_industries,
        popularity_stocks=popularity_stocks,
        concept_capital_flow=concept_capital_flow,
        previous_snapshot=previous_snapshot,
    )

    now = _now_shanghai()
    summary = {
        "run_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "theme_count": int(len(radar)),
        "hot_stock_count": int(len(hot_stocks)),
        "hot_concept_count": int(len(hot_concepts)),
        "hot_industry_count": int(len(hot_industries)),
        "popularity_stock_count": int(len(popularity_stocks)),
        "concept_capital_flow_count": int(len(concept_capital_flow)),
        "market_context": market_context,
        "top_themes": radar.head(10).to_dict("records"),
    }
    snapshot.update({"updated_at": summary["run_time"]})

    _write_csv(os.path.join(OUTPUT_DIR, "latest_hot_stocks.csv"), hot_stocks)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_hot_concepts.csv"), hot_concepts)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_hot_industries.csv"), hot_industries)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_popularity_stocks.csv"), popularity_stocks)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_concept_capital_flow.csv"), concept_capital_flow)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_theme_radar.csv"), radar)
    _write_csv(os.path.join(OUTPUT_DIR, "latest_market_context.csv"), market_context_df)
    _write_json(os.path.join(OUTPUT_DIR, "latest_market_context.json"), market_context)
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)
    _write_json(SNAPSHOT_FILE, snapshot)

    body = _build_email_body(summary, radar, hot_stocks, market_context, concept_capital_flow)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)
    if os.getenv("THEME_MONITOR_SEND_EMAIL_IN_SCRIPT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}:
        _send_email_if_configured(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
