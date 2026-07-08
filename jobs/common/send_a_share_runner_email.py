# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Union


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs.common.email_format import set_rich_email_content


TASK_OUTPUTS: dict[str, tuple[str, ...]] = {
    "short_term_intraday": ("jobs/short_term/outputs/latest_summary.txt",),
    "short_term_intraday_pm": ("jobs/short_term/outputs/latest_summary.txt",),
    "volatility": ("jobs/volatility/outputs/latest_email_body.txt",),
    "boll": ("jobs/boll/outputs/latest_email_body.txt",),
    "a_share_review": ("jobs/a_share_allocation/outputs/latest_email_body.txt",),
    "three_dim": ("jobs/three_dim_resonance/outputs/latest_email_body.txt",),
    "shared_cache": ("data/cache/three_dim_cache_manifest.json",),
    "dividend_cache": ("data/cache/dividend/dividend_sync_manifest.json",),
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _render_task_result(task: dict[str, Any]) -> str:
    status = task.get("status", "unknown")
    elapsed = task.get("elapsed_seconds", 0)
    returncode = task.get("returncode")
    returncode_text = "" if returncode is None else f", returncode={returncode}"
    return f"- {task.get('name', 'unknown')}: {status}, {elapsed}s{returncode_text}"


def _load_task_output(base_dir: Path, task_name: str) -> str:
    for relative_path in TASK_OUTPUTS.get(task_name, ()):
        path = base_dir / relative_path
        if not path.exists():
            continue
        if path.suffix == ".json":
            data = _read_json(path)
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _read_text(path)
    return ""


def build_runner_email_body(summary: dict[str, Any], base_dir: Union[str, Path] = PROJECT_ROOT) -> str:
    base_path = Path(base_dir)
    profile = summary.get("profile", "unknown")
    status = summary.get("status", "unknown")
    trade_date = summary.get("trade_date") or "自动"
    tasks = list(summary.get("tasks") or [])

    lines = [
        "A股统一任务调度",
        "",
        f"Profile: {profile}",
        f"状态: {status}",
        f"交易日: {trade_date}",
        f"完成: {summary.get('completed_count', 0)}/{summary.get('task_count', len(tasks))}",
        f"失败: {summary.get('failed_count', 0)}",
        "",
        "任务结果:",
    ]
    lines.extend(_render_task_result(task) for task in tasks)

    for task in tasks:
        task_name = str(task.get("name", "")).strip()
        output = _load_task_output(base_path, task_name)
        if not output:
            continue
        lines.extend(["", f"========== {task_name} ==========", "", output])

    if not tasks:
        lines.extend(["", "未找到 runner 摘要中的任务明细，请检查 Run profile 步骤日志。"])

    return "\n".join(lines).strip() + "\n"


def send_email(subject: str, body: str) -> bool:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过 A股统一任务调度邮件通知。")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    set_rich_email_content(msg, body, title="A股统一任务调度")

    recipients = [item.strip() for item in mail_to.split(",") if item.strip()]
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)
    print(f"已发送 A股统一任务调度邮件通知，收件人：{mail_to}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="发送 A股统一任务调度汇总邮件")
    parser.add_argument(
        "--summary",
        default=str(PROJECT_ROOT / "jobs/outputs/latest_runner_summary.json"),
        help="runner summary json 路径",
    )
    parser.add_argument("--base-dir", default=str(PROJECT_ROOT), help="产物路径基准目录")
    args = parser.parse_args()

    summary_path = Path(args.summary)
    if summary_path.exists():
        summary = _read_json(summary_path)
    else:
        print(f"未找到 runner 摘要文件：{summary_path}")
        summary = {
            "profile": os.getenv("A_SHARE_RUNNER_PROFILE", "unknown"),
            "status": "unknown",
            "task_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "tasks": [],
        }

    body = build_runner_email_body(summary, args.base_dir)
    subject = f"A股统一任务调度 - {summary.get('profile', 'unknown')} - {summary.get('status', 'unknown')}"
    send_email(subject, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
