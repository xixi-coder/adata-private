# -*- coding: utf-8 -*-
"""
雪球关注用户监听 —— 定时任务入口（GitHub Actions / 本地均可运行）

职责：
  1. 从环境变量（或本地 .env.local）读取被监控用户名单、登录 Cookie 与邮件配置；
  2. 执行一轮监听：采集各被监控用户的自选股 → 与上次快照比对 → 得到「新增自选股」变化；
  3. 有变化时通过 163 邮箱发送通知；
  4. 快照持久化在仓库内的 snapshots 目录，由 workflow 提交回仓库以实现跨运行比对。

本部署形态为「只做自选股」：不采集动态（雪球动态接口受 WAF 保护，在无显示 /
数据中心 IP 的 GitHub 托管 runner 上不可行）。因此这里用一个只采自选股的采集器包装，
动态恒为空、不会产生动态变化事件。

环境变量：
  XUEQIU_UIDS   必填，被监控雪球用户 ID，逗号分隔，例如 "1247347556,4267080012"
                （兼容单个 XUEQIU_UID）
  XUEQIU_COOKIE 选填，登录 Cookie 串；不配则匿名（匿名下多数自选股不可见）
  MAIL_163_USER / SMTP_USER   发件 163 邮箱账号
  MAIL_163_PASS / SMTP_PASS   163 邮箱 SMTP 授权码
  MAIL_TO                      收件人，逗号分隔
  SMTP_HOST（默认 smtp.163.com）/ SMTP_PORT（默认 465）
  XUEQIU_SNAPSHOT_DIR          快照目录，默认 jobs/xueqiu_monitor/snapshots

用法：
  python jobs/xueqiu_monitor/run.py
"""
import logging
import os
import sys

import pandas as pd

# 允许直接以脚本方式运行时能 import 到项目包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from jobs.common.local_env import load_local_env
from adata.xueqiu import MonitorConfig, XueqiuMonitor
from adata.xueqiu.collector import XueqiuCollector
from adata.xueqiu.notifier import ConsoleChannel, EmailChannel, Notifier
from adata.xueqiu.snapshot import SnapshotStore

logger = logging.getLogger("adata")

# 快照默认目录：放在任务目录下，便于 workflow 提交回仓库
_DEFAULT_SNAPSHOT_DIR = os.path.join(_PROJECT_ROOT, "jobs", "xueqiu_monitor", "snapshots")

# 动态列（用于构造空动态 DataFrame）
_POSTS_COLUMNS = ["post_id", "publish_time", "content", "source_url"]


class WatchlistOnlyCollector:
    """只采集自选股的采集器包装：get_posts 恒返回空，从而不产生动态变化事件。

    自选股走底层 XueqiuCollector 的 HTTP 接口；这样既保持核心模块不变，
    又实现了「只做自选股」的部署形态。
    """

    def __init__(self, credential=None):
        self._inner = XueqiuCollector(credential=credential)

    def get_watchlist(self, uid):
        return self._inner.get_watchlist(uid)

    def get_posts(self, uid):
        # 部署形态为只做自选股，动态恒为空
        return pd.DataFrame([], columns=_POSTS_COLUMNS)


def _read_user_ids():
    """从环境变量读取被监控用户 ID 列表（兼容 XUEQIU_UIDS / XUEQIU_UID）。"""
    raw = os.environ.get("XUEQIU_UIDS") or os.environ.get("XUEQIU_UID") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _build_notifier():
    """按环境变量构造通知器：配置了邮件则用邮件渠道，否则退回控制台。"""
    smtp_user = (os.environ.get("SMTP_USER") or os.environ.get("MAIL_163_USER") or "").strip()
    smtp_pass = (os.environ.get("SMTP_PASS") or os.environ.get("MAIL_163_PASS") or "").strip()
    mail_to = (os.environ.get("MAIL_TO") or "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "smtp.163.com").strip()
    smtp_port = os.environ.get("SMTP_PORT", "465").strip()

    channels = []
    if smtp_user and smtp_pass and mail_to:
        channels.append(EmailChannel(
            smtp_user=smtp_user, smtp_pass=smtp_pass, recipients=mail_to,
            smtp_host=smtp_host, smtp_port=smtp_port,
        ))
        logger.info("已启用邮件通知渠道，收件人：%s", mail_to)
    else:
        logger.warning("未配置完整邮件 Secrets，改用控制台通知渠道")
        channels.append(ConsoleChannel())
    return Notifier(channels=channels)


def main():
    # 本地运行时加载 .env.local；GitHub Actions 走注入的环境变量
    load_local_env()

    user_ids = _read_user_ids()
    if not user_ids:
        logger.error("未配置被监控用户：请设置环境变量 XUEQIU_UIDS（逗号分隔）")
        raise SystemExit(1)

    cookie = os.environ.get("XUEQIU_COOKIE") or None
    snapshot_dir = os.environ.get("XUEQIU_SNAPSHOT_DIR", _DEFAULT_SNAPSHOT_DIR)

    logger.info("被监控用户：%s", user_ids)
    logger.info("快照目录：%s", snapshot_dir)

    config = MonitorConfig(user_ids=user_ids, credential=cookie)
    monitor = XueqiuMonitor(
        config,
        collector=WatchlistOnlyCollector(credential=cookie),
        store=SnapshotStore(base_dir=snapshot_dir),
        notifier=_build_notifier(),
    )

    # 执行一轮：采集→比对→（有变化则）邮件通知
    events = monitor.run_once()
    logger.info("本轮检测到 %d 条新增自选股变化", len(events))
    for e in events:
        logger.info("  新增自选股 用户%s %s %s", e.uid, e.stock_code, e.short_name)
    return 0


if __name__ == "__main__":
    # 基础日志配置（GitHub Actions 控制台可见）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    raise SystemExit(main())
