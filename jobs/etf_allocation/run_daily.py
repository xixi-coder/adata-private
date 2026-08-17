from __future__ import annotations

import argparse
import json
import os
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd

from jobs.common.daily_job import now_shanghai
from jobs.common.email_format import set_rich_email_content
from jobs.common.local_env import load_local_env
from jobs.etf_allocation.strategy import ETF_UNIVERSE, ETFAllocationConfig, build_target_weights, prepare_snapshot
from jobs.etf_rotation.backtest import download_ths_etf, load_etf_csv
from jobs.etf_rotation.daily_signal import _load_trade_dates


def _completed_signal_date(trade_dates: list[pd.Timestamp], run_date: pd.Timestamp) -> pd.Timestamp:
    previous = [date for date in trade_dates if date < run_date]
    if not previous:
        raise RuntimeError(f"交易日历中找不到 {run_date:%Y-%m-%d} 之前的交易日")
    completed = [date for date in previous if date.to_period("M") < run_date.to_period("M")]
    if not completed:
        raise RuntimeError("没有已完成月份可用于生成ETF信号")
    return completed[-1]


def _position_text(weights: dict[str, float]) -> str:
    parts = [
        f"{code} {ETF_UNIVERSE[code]['name']} {weight:.0%}"
        for code, weight in weights.items()
        if weight >= 0.005
    ]
    cash = max(0.0, 1.0 - sum(weights.values()))
    if cash >= 0.005:
        parts.append(f"现金 {cash:.0%}")
    return "，".join(parts)


def _render_text(summary: dict[str, Any]) -> str:
    lines = [
        f"宽基与行业ETF轮动 | {summary['run_date']}",
        f"状态：{summary['action_summary']}",
        f"信号日期：{summary['signal_date']}",
        f"目标仓位：{summary['target_position']}",
        f"信号依据：{summary['reason']}",
        "",
        "入选ETF：",
    ]
    selected = [item for item in summary["ranking"] if item["target_weight"] >= 0.005]
    if selected:
        for item in selected:
            lines.append(
                f"- {item['code']} {item['name']}（{item['group']}）：目标 {item['target_weight']:.0%}，"
                f"20/60/120日 {item['ret20']:+.1%}/{item['ret60']:+.1%}/{item['ret120']:+.1%}，"
                f"年化波动 {item['volatility20']:.1%}"
            )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "观察排名：",
            "排名 | 类型 | ETF | 通过过滤 | 综合分",
        ]
    )
    for index, item in enumerate(summary["ranking"][:10], start=1):
        lines.append(
            f"{index} | {item['group']} | {item['code']} {item['name']} | "
            f"{'是' if item['eligible'] else '否'} | {item['score']:.3f}"
        )
    lines.extend(
        [
            "",
            "说明：月频模型使用上月最后一个交易日的收盘数据；目标仓位是研究建议，不读取真实账户，也不会自动下单。",
        ]
    )
    return "\n".join(lines) + "\n"


def _send_email(summary: dict[str, Any], body: str) -> list[str]:
    load_local_env()
    smtp_user = (os.getenv("SMTP_USER") or os.getenv("MAIL_163_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or os.getenv("MAIL_163_PASS") or "").strip()
    recipients = [
        item.strip()
        for item in os.getenv("MAIL_TO", "").replace(";", ",").split(",")
        if item.strip()
    ]
    if not smtp_user or not smtp_pass or not recipients:
        raise RuntimeError("ETF邮件配置不完整：需要SMTP_USER/MAIL_163_USER、SMTP_PASS/MAIL_163_PASS和MAIL_TO")
    message = EmailMessage()
    message["Subject"] = f"[宽基与行业ETF轮动] {summary['run_date']} {summary['action_summary']}"
    message["From"] = smtp_user
    message["To"] = ", ".join(recipients)
    set_rich_email_content(
        message,
        body,
        title=f"宽基与行业ETF轮动 | {summary['run_date']}",
        preheader=summary["action_summary"],
    )
    host = os.getenv("SMTP_HOST", "smtp.163.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(message, to_addrs=recipients)
    return recipients


def generate_signal(args: argparse.Namespace) -> dict[str, Any]:
    run_date = pd.Timestamp(args.date or now_shanghai().strftime("%Y-%m-%d")).normalize()
    trade_dates = _load_trade_dates(run_date)
    is_trade_day = run_date in trade_dates
    signal_date = _completed_signal_date(trade_dates, run_date)
    planned_dates = [date for date in trade_dates if date > signal_date]
    planned_execution_date = planned_dates[0] if planned_dates else None
    should_trade_today = bool(is_trade_day and planned_execution_date == run_date)

    config = ETFAllocationConfig()
    warmup_start = (signal_date - timedelta(days=400)).strftime("%Y-%m-%d")
    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
        frames = {code: load_etf_csv(csv_dir / f"{code}.csv") for code in ETF_UNIVERSE}
    else:
        frames = {
            code: download_ths_etf(code, warmup_start, signal_date.strftime("%Y-%m-%d"))
            for code in ETF_UNIVERSE
        }
    snapshot = prepare_snapshot(frames, signal_date, config)
    if len(snapshot) != len(ETF_UNIVERSE):
        missing = sorted(set(ETF_UNIVERSE).difference(snapshot["code"].astype(str)))
        raise RuntimeError(f"ETF行情覆盖不完整，缺失：{','.join(missing)}")
    stale = snapshot[pd.to_datetime(snapshot["trade_date"]).dt.normalize() != signal_date]
    if not stale.empty:
        raise RuntimeError("ETF行情日期未对齐信号日：" + ",".join(stale["code"].astype(str)))

    weights, reason, defensive = build_target_weights(snapshot, config)
    action_summary = "今日按目标仓位调仓" if should_trade_today else "今日无需调仓"
    ranking = []
    for _, row in snapshot.iterrows():
        ranking.append(
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "group": str(row["group"]),
                "eligible": bool(row["eligible"]),
                "score": round(float(row["score"]), 6),
                "ret20": round(float(row["ret20"]), 6),
                "ret60": round(float(row["ret60"]), 6),
                "ret120": round(float(row["ret120"]), 6),
                "volatility20": round(float(row["volatility20"]), 6),
                "target_weight": round(float(weights[str(row["code"])]), 6),
            }
        )
    summary = {
        "generated_at": now_shanghai().isoformat(timespec="seconds"),
        "run_date": run_date.strftime("%Y-%m-%d"),
        "is_trade_day": is_trade_day,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "planned_execution_date": planned_execution_date.strftime("%Y-%m-%d") if planned_execution_date else None,
        "should_trade_today": should_trade_today,
        "action_summary": action_summary,
        "market_regime": "防守" if defensive else "正常",
        "reason": reason,
        "target_weights": {code: round(weight, 6) for code, weight in weights.items()},
        "target_position": _position_text(weights),
        "ranking": ranking,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    body = _render_text(summary)
    (output_dir / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "latest_email_body.txt").write_text(body, encoding="utf-8")
    (output_dir / "latest_ranking.csv").write_text(snapshot.to_csv(index=False), encoding="utf-8-sig")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="宽基与行业ETF月频轮动")
    parser.add_argument("--date", default="", help="运行日期，默认上海时区当天")
    parser.add_argument("--csv-dir", help="可选的本地ETF日线CSV目录")
    parser.add_argument("--output-dir", default="jobs/etf_allocation/outputs")
    parser.add_argument("--send-email", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = generate_signal(args)
    body = _render_text(summary)
    print(body, end="")
    if args.send_email and summary["should_trade_today"]:
        recipients = _send_email(summary, body)
        print(f"邮件已发送：{len(recipients)} 个收件人")
    elif args.send_email:
        print("不是本周计划执行日，跳过重复邮件")


if __name__ == "__main__":
    main()
