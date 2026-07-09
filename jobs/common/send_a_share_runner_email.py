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


TASK_EMAILS: dict[str, dict[str, Any]] = {
    "short_term_intraday": {
        "title": "短线分时扫描",
        "paths": ("jobs/short_term/outputs/latest_summary.txt",),
    },
    "volatility": {
        "title": "A股波动结构扫描",
        "paths": ("jobs/volatility/outputs/latest_email_body.txt",),
        "headers": {
            "一、资金聚焦": "序号 | 方向 | 数量 | 均分/代表",
        },
    },
    "boll": {
        "title": "BOLL战法扫描",
        "paths": ("jobs/boll/outputs/latest_email_body.txt",),
        "drop_prefixes": ("- 运行时间:", "- 请求日期:"),
        "headers": {
            "下轨止跌观察": "序号 | 股票代码 | 股票名称 | 评分 | 风险等级 | 趋势环境 | 收盘价 | BOLL下轨 | 观察价 | 失效价 | 入选依据",
            "上轨放量滞涨": "序号 | 股票代码 | 股票名称 | 评分 | 风险等级 | 趋势环境 | 收盘价 | BOLL上轨 | 观察价 | 失效价 | 入选依据",
        },
    },
    "a_share_review": {
        "title": "持仓复盘",
        "paths": ("jobs/a_share_allocation/outputs/latest_email_body.txt",),
        "headers": {
            "二、需要处理": "序号 | 股票代码 | 股票名称 | 主题 | 仓位 | 评分 | 建议动作 | 理由",
            "三、继续观察": "序号 | 股票代码 | 股票名称 | 主题 | 仓位 | 评分 | 趋势 | 建议动作 | 策略联动",
            "四、可加仓观察": "序号 | 股票代码 | 股票名称 | 主题 | 仓位 | 评分 | 建议动作 | 理由",
            "五、补位候选 Top5": "序号 | 股票代码 | 股票名称 | 收盘价 | 日涨跌% | 总分 | 策略联动 | 入选依据 | 操作提示",
        },
    },
    "three_dim": {
        "title": "三维共振策略建议",
        "paths": ("jobs/three_dim_resonance/outputs/latest_email_body.txt",),
    },
    "shared_cache": {
        "title": "共享缓存维护",
        "paths": ("data/cache/three_dim_cache_manifest.json",),
    },
    "dividend_cache": {
        "title": "分红缓存维护",
        "paths": ("data/cache/dividend/dividend_sync_manifest.json",),
    },
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _load_task_output(base_dir: Path, task_name: str) -> str:
    config = TASK_EMAILS.get(task_name, {})
    for relative_path in config.get("paths", ()):
        path = base_dir / relative_path
        if not path.exists():
            continue
        if path.suffix == ".json":
            data = _read_json(path)
            if task_name == "shared_cache":
                return _format_shared_cache_manifest(data)
            if task_name == "dividend_cache":
                return _format_dividend_cache_manifest(data)
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _read_text(path)
    return ""


def _format_seconds(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value >= 3600:
        return f"{value / 3600:.1f}小时"
    if value >= 60:
        return f"{value / 60:.1f}分钟"
    return f"{value:.1f}秒"


def _format_shared_cache_manifest(data: dict[str, Any]) -> str:
    pending = int(data.get("pending_stock_update_count") or 0)
    updated = int(data.get("updated_stock_count") or 0)
    checked = int(data.get("checked_stock_count") or 0)
    selected = int(data.get("selected_code_count") or 0)
    auto_commit_count = int(data.get("auto_commit_count") or 0)
    auto_commit_minutes = int(data.get("auto_commit_minutes") or 0)
    cache_changed = "是" if data.get("cache_changed") else "否"
    benchmark_updated = "是" if data.get("benchmark_updated") else "否"
    workers = data.get("stock_max_workers", "-")

    lines = [
        "日线/财务共享缓存维护",
        f"- 更新时间: {data.get('updated_at', '-')}",
        f"- 股票池: {selected}只，本地缓存记录 {data.get('stock_count', '-')}只",
        f"- 日线补齐: 待检查 {pending}只，完成检查 {checked}只，写入更新 {updated}只",
        f"- 财务缓存: 本次刷新 {data.get('refreshed_finance_count', 0)}只",
        f"- 沪深300基准: {'已更新' if data.get('benchmark_updated') else '无需更新'}",
        f"- 缓存变化: {cache_changed}",
    ]
    if workers != "-":
        lines.append(f"- 日线抓取并发: {workers}")
    if auto_commit_minutes > 0:
        lines.append(f"- 运行中自动回传: 每 {auto_commit_minutes} 分钟检查一次，共回传 {auto_commit_count} 次")
    else:
        lines.append("- 运行中自动回传: 已关闭，仅任务末尾统一回传")
    if data.get("cache_file"):
        lines.append(f"- 缓存文件: {data.get('cache_file')}")
    if data.get("finance_dir"):
        lines.append(f"- 财务目录: {data.get('finance_dir')}")
    lines.append("")
    lines.append("说明: 待检查不等于写入更新；接口返回无新增数据、停牌或缓存已足够新时，不会写入新数据。")
    if not benchmark_updated:
        lines.append("说明: 基准无需更新通常表示云端缓存已经覆盖目标交易日。")
    return "\n".join(lines).strip() + "\n"


def _format_dividend_cache_manifest(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    failed_codes = data.get("failed_codes") or []
    total_codes = int(data.get("total_codes") or 0)
    created = int(summary.get("created_non_empty") or 0)
    created_empty = int(summary.get("created_empty") or 0)
    updated = int(summary.get("updated_non_empty") or 0)
    updated_empty = int(summary.get("updated_empty") or 0)
    skip_fresh = int(summary.get("skip_fresh") or 0)
    keep_existing = int(summary.get("keep_existing") or 0)
    failed = int(summary.get("failed") or 0)

    lines = [
        "分红缓存维护",
        f"- 同步时间: {data.get('synced_at', '-')}",
        f"- 股票池: {total_codes}只，并发 {data.get('max_workers', '-')}，失败重试 {data.get('retry', '-') } 次",
        f"- 新鲜跳过: {skip_fresh}只",
        f"- 新建缓存: 非空 {created}只，空结果 {created_empty}只",
        f"- 更新缓存: 非空 {updated}只，空结果 {updated_empty}只",
        f"- 保留旧缓存: {keep_existing}只",
        f"- 失败: {failed}只",
        f"- 云端包: {data.get('archive_name', '-')}",
    ]
    if failed_codes:
        lines.append("")
        lines.append("失败样本")
        lines.append("股票代码 | 错误摘要")
        for item in failed_codes[:20]:
            error = str(item.get("error", "")).replace("\n", " ").strip()
            if len(error) > 90:
                error = error[:87] + "..."
            lines.append(f"{item.get('code', '-')} | {error}")
    lines.append("")
    lines.append("说明: 空结果通常表示接口没有返回该股票的分红记录；失败项会在下次维护继续重试。")
    return "\n".join(lines).strip() + "\n"


def _strip_operational_lines(body: str, task_name: str) -> str:
    drop_prefixes = TASK_EMAILS.get(task_name, {}).get("drop_prefixes", ())
    if not drop_prefixes:
        return body
    lines = [line for line in body.splitlines() if not line.strip().startswith(drop_prefixes)]
    return "\n".join(lines).strip() + "\n"


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and bool(stripped.split("|", 1)[0].strip())


def _with_table_headers(body: str, task_name: str) -> str:
    headers = TASK_EMAILS.get(task_name, {}).get("headers", {})
    if not headers:
        return body
    lines = body.splitlines()
    output: list[str] = []
    pending_header = ""
    for line in lines:
        stripped = line.strip()
        if stripped in headers:
            pending_header = headers[stripped]
            output.append(line)
            continue
        if pending_header and stripped:
            if _looks_like_table_row(stripped) and stripped != pending_header:
                output.append(pending_header)
            pending_header = ""
        output.append(line)
    return "\n".join(output).strip() + "\n"


def _prepare_body_for_email(body: str, task_name: str) -> str:
    body = _strip_operational_lines(body, task_name)
    body = _with_table_headers(body, task_name)
    return body


def build_task_email_body(task_name: str, base_dir: Union[str, Path] = PROJECT_ROOT) -> str:
    base_path = Path(base_dir)
    body = _load_task_output(base_path, task_name)
    if not body:
        return ""
    return _prepare_body_for_email(body, task_name)


def iter_task_emails(summary: dict[str, Any], base_dir: Union[str, Path] = PROJECT_ROOT) -> list[tuple[str, str, str]]:
    emails = []
    for task in summary.get("tasks") or []:
        task_name = str(task.get("name", "")).strip()
        config = TASK_EMAILS.get(task_name)
        if not config:
            continue
        body = build_task_email_body(task_name, base_dir)
        if not body:
            if task.get("status") == "failed":
                body = f"{config['title']}\n\n任务执行失败，未生成邮件正文。请查看 GitHub Actions 的 Run profile 日志。\n"
            else:
                continue
        status_prefix = "失败" if task.get("status") == "failed" else "成功"
        subject = f"[{status_prefix}] {config['title']}"
        emails.append((subject, config["title"], body))
    return emails


def send_email(subject: str, title: str, body: str) -> bool:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过统一任务调度邮件通知。")
        return False

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
    print(f"已发送邮件通知：{subject}，收件人：{mail_to}")
    return True


def _has_email_config() -> bool:
    return bool(
        os.getenv("SMTP_USER", "").strip()
        and os.getenv("SMTP_PASS", "").strip()
        and os.getenv("MAIL_TO", "").strip()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="发送统一任务调度汇总邮件")
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

    emails = iter_task_emails(summary, args.base_dir)
    if not emails:
        print("未找到可发送的任务邮件正文，跳过统一任务调度邮件通知。")
        return 0
    if not _has_email_config():
        print("未配置邮件参数，跳过统一任务调度邮件通知。")
        return 0
    for subject, title, body in emails:
        send_email(subject, title, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
