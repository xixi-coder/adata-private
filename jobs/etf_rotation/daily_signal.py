from __future__ import annotations

import argparse
import json
import os
import smtplib
import urllib.request
from argparse import Namespace
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd

from jobs.common.daily_job import now_shanghai
from jobs.common.email_format import set_rich_email_content
from jobs.common.local_env import load_local_env
from jobs.etf_rotation.backtest import ETF_NAMES, download_ths_etf, load_etf_csv, run_backtest
from jobs.etf_rotation.run import ETF_CODES, build_config


def _load_calendar_frame(year: int) -> pd.DataFrame:
    project_root = Path(__file__).resolve().parents[2]
    cached = project_root / "adata" / "stock" / "cache" / "calendar" / f"{year}.csv"
    if cached.exists():
        return pd.read_csv(cached)

    rows: list[dict[str, Any]] = []
    for month in range(1, 13):
        url = f"https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month={year}-{month}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows.extend(payload.get("data") or [])
    if not rows:
        return pd.DataFrame(columns=["trade_date", "trade_status"])
    return pd.DataFrame(rows).rename(columns={"jyrq": "trade_date", "jybz": "trade_status"})


def _load_trade_dates(run_date: pd.Timestamp) -> list[pd.Timestamp]:
    years = {run_date.year - 1, run_date.year}
    if run_date.month == 12:
        years.add(run_date.year + 1)
    dates: set[pd.Timestamp] = set()
    for year in sorted(years):
        calendar = _load_calendar_frame(year)
        if calendar.empty:
            continue
        open_dates = calendar[pd.to_numeric(calendar["trade_status"], errors="coerce").eq(1)]
        dates.update(pd.to_datetime(open_dates["trade_date"], errors="coerce").dropna().dt.normalize())
    if not dates:
        raise RuntimeError("A股交易日历不可用，为避免节假日误发交易信号，本次任务已停止")
    return sorted(dates)


def _completed_weekly_signal_dates(
    frames: dict[str, pd.DataFrame],
    run_date: pd.Timestamp,
    data_date: pd.Timestamp,
) -> set[pd.Timestamp]:
    common_dates: set[pd.Timestamp] | None = None
    for frame in frames.values():
        dates = set(pd.to_datetime(frame["trade_date"], errors="coerce").dropna().dt.normalize())
        common_dates = dates if common_dates is None else common_dates.intersection(dates)
    eligible = sorted(date for date in (common_dates or set()) if date <= data_date)
    if not eligible:
        raise RuntimeError("两只ETF没有可用于生成信号的共同日线")
    date_frame = pd.DataFrame({"trade_date": eligible})
    date_frame["week"] = date_frame["trade_date"].dt.to_period("W-FRI")
    current_week = run_date.to_period("W-FRI")
    completed = date_frame[date_frame["week"] < current_week]
    return set(completed.groupby("week", sort=True)["trade_date"].max().tolist())


def _trade_actions(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    for code in target_weights:
        current = float(current_weights.get(code, 0.0))
        target = float(target_weights[code])
        change = target - current
        if abs(change) < threshold:
            continue
        item = {
            "code": code,
            "name": ETF_NAMES.get(code, code),
            "current_weight": round(current, 6),
            "target_weight": round(target, 6),
            "weight_change": round(change, 6),
        }
        (buys if change > 0 else sells).append(item)
    return buys, sells


def _position_text(weights: dict[str, float]) -> str:
    allocated = [
        f"{code} {ETF_NAMES.get(code, code)} {weight:.0%}"
        for code, weight in weights.items()
        if weight >= 0.005
    ]
    cash_weight = max(0.0, 1.0 - sum(weights.values()))
    if cash_weight >= 0.005:
        allocated.append(f"现金 {cash_weight:.0%}")
    return "，".join(allocated) if allocated else "空仓"


def _render_text(summary: dict[str, Any]) -> str:
    lines = [
        f"ETF轮动盘前信号 | {summary['run_date']}",
        f"状态：{summary['action_summary']}",
        f"是否交易日：{'是' if summary['is_trade_day'] else '否'}",
        f"信号日期：{summary.get('signal_date') or '-'}",
        f"计划执行日：{summary.get('planned_execution_date') or '-'}",
        f"当前模型仓位：{summary['current_position']}",
        f"信号目标仓位：{summary['target_position']}",
        f"信号依据：{summary.get('reason') or '-'}",
        "",
        "今日卖出：",
    ]
    if summary["sell_actions"]:
        for item in summary["sell_actions"]:
            lines.append(
                f"- {item['code']} {item['name']}：{item['current_weight']:.0%} -> "
                f"{item['target_weight']:.0%}"
            )
    else:
        lines.append("- 无")
    lines.append("今日买入：")
    if summary["buy_actions"]:
        for item in summary["buy_actions"]:
            lines.append(
                f"- {item['code']} {item['name']}：{item['current_weight']:.0%} -> "
                f"{item['target_weight']:.0%}"
            )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "说明：这是模型仓位建议，不读取真实账户，也不会自动下单；实盘下单前需检查513100折溢价。",
        ]
    )
    return "\n".join(lines) + "\n"


def _send_email(summary: dict[str, Any], body: str) -> list[str]:
    load_local_env()
    smtp_user = (os.getenv("SMTP_USER") or os.getenv("MAIL_163_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or os.getenv("MAIL_163_PASS") or "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        raise RuntimeError(
            "邮件配置不完整，请在.env.local配置MAIL_163_USER（或SMTP_USER）、"
            "MAIL_163_PASS（或SMTP_PASS）和MAIL_TO"
        )

    recipients = [item.strip() for item in mail_to.replace(";", ",").split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO没有有效的收件邮箱")

    subject_prefix = os.getenv("ETF_ROTATION_EMAIL_SUBJECT", "ETF轮动盘前信号").strip()
    subject = f"[{subject_prefix}] {summary['run_date']} {summary['action_summary']}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    set_rich_email_content(
        msg,
        body,
        title=f"ETF轮动盘前信号 | {summary['run_date']}",
        preheader=summary["action_summary"],
    )

    host = os.getenv("SMTP_HOST", "smtp.163.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)
    return recipients


def generate_signal(args: argparse.Namespace) -> dict[str, Any]:
    run_date = pd.Timestamp(args.date or now_shanghai().strftime("%Y-%m-%d")).normalize()
    trade_dates = _load_trade_dates(run_date)
    is_trade_day = run_date in trade_dates
    previous_dates = [date for date in trade_dates if date < run_date]
    if not previous_dates:
        raise RuntimeError(f"交易日历中找不到 {run_date:%Y-%m-%d} 之前的交易日")
    data_date = previous_dates[-1]

    warmup_start = (pd.Timestamp(args.start) - timedelta(days=240)).strftime("%Y-%m-%d")
    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
        frames = {code: load_etf_csv(csv_dir / f"{code}.csv") for code in ETF_CODES}
    else:
        frames = {
            code: download_ths_etf(code, warmup_start, data_date.strftime("%Y-%m-%d"))
            for code in ETF_CODES
        }
    frames = {
        code: frame[pd.to_datetime(frame["trade_date"]) <= data_date].reset_index(drop=True)
        for code, frame in frames.items()
    }

    config = build_config(
        Namespace(
            profile=args.profile,
            capital=args.capital,
            cost=args.cost,
            rebalance_threshold=args.rebalance_threshold,
        )
    )
    signal_dates = _completed_weekly_signal_dates(frames, run_date, data_date)
    result = run_backtest(
        frames,
        config=config,
        start_date=args.start,
        end_date=data_date.strftime("%Y-%m-%d"),
        signal_dates=signal_dates,
    )
    if result.signals.empty:
        raise RuntimeError("尚无足够历史数据生成周频信号")

    latest_signal = result.signals.sort_values("signal_date").iloc[-1]
    signal_date = pd.Timestamp(latest_signal["signal_date"]).normalize()
    future_dates = [date for date in trade_dates if date > signal_date]
    planned_execution_date = future_dates[0] if future_dates else None
    should_trade_today = bool(
        is_trade_day and planned_execution_date is not None and run_date == planned_execution_date
    )

    latest_nav = result.nav.sort_values("trade_date").iloc[-1]
    current_weights = {code: float(latest_nav[f"weight_{code}"]) for code in ETF_CODES}
    target_weights = {code: float(latest_signal[f"target_weight_{code}"]) for code in ETF_CODES}
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    if should_trade_today:
        buys, sells = _trade_actions(current_weights, target_weights, config.rebalance_threshold)

    target_is_cash = sum(target_weights.values()) < 0.005
    if not is_trade_day:
        action_summary = "非A股交易日，今日不操作"
    elif not should_trade_today:
        action_summary = "不是本周计划执行日，今日无需调仓"
    elif not buys and not sells:
        action_summary = "目标仓位未发生有效变化，今日无需调仓"
    elif target_is_cash:
        action_summary = "今日卖出策略持仓，转为空仓"
    else:
        parts = []
        if sells:
            parts.append("卖出/减仓 " + "、".join(item["code"] for item in sells))
        if buys:
            parts.append("买入/加仓 " + "、".join(item["code"] for item in buys))
        action_summary = "；".join(parts)

    summary = {
        "generated_at": now_shanghai().isoformat(timespec="seconds"),
        "run_date": run_date.strftime("%Y-%m-%d"),
        "is_trade_day": is_trade_day,
        "data_date": data_date.strftime("%Y-%m-%d"),
        "profile": args.profile,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "planned_execution_date": (
            planned_execution_date.strftime("%Y-%m-%d") if planned_execution_date is not None else None
        ),
        "should_trade_today": should_trade_today,
        "should_buy_today": bool(buys),
        "should_sell_today": bool(sells),
        "action_summary": action_summary,
        "reason": str(latest_signal["reason"]),
        "current_weights": {code: round(weight, 6) for code, weight in current_weights.items()},
        "target_weights": {code: round(weight, 6) for code, weight in target_weights.items()},
        "current_position": _position_text(current_weights),
        "target_position": _position_text(target_weights),
        "buy_actions": buys,
        "sell_actions": sells,
        "assumptions": {
            "signal": "上一完整交易周的最后共同交易日收盘后",
            "execution": "下一A股交易日开盘",
            "account_positions": "模型推算，不读取真实账户",
            "premium_filter": "下单前人工检查513100折溢价",
        },
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_key = run_date.strftime("%Y%m%d")
    text = _render_text(summary)
    for path in (output_dir / f"signal_{date_key}.json", output_dir / "latest_signal.json"):
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / f"signal_{date_key}.txt").write_text(text, encoding="utf-8")
    (output_dir / "latest_signal.txt").write_text(text, encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ETF轮动策略盘前买卖信号")
    parser.add_argument("--date", default="", help="运行日期，默认Asia/Shanghai当天")
    parser.add_argument("--profile", choices=("optimized", "balanced", "screenshot"), default="optimized")
    parser.add_argument("--start", default="2014-01-01")
    parser.add_argument("--csv-dir")
    parser.add_argument("--output-dir", default="jobs/etf_rotation/live_outputs")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--cost", type=float, default=0.001)
    parser.add_argument("--rebalance-threshold", type=float, default=0.02)
    parser.add_argument("--send-email", action="store_true", help="将信号发送到MAIL_TO，失败时返回非零状态")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = generate_signal(args)
    text = _render_text(summary)
    print(text, end="")
    if args.send_email:
        recipients = _send_email(summary, text)
        print(f"邮件已发送：{len(recipients)} 个收件人")


if __name__ == "__main__":
    main()
