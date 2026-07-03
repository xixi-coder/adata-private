# -*- coding: utf-8 -*-
"""
雪球关注用户监听（xueqiu-user-monitor）手动测试脚本

包含两部分：
  1) 离线冒烟测试：用假采集器 + 临时快照目录，验证「首轮建快照 → 次轮检测变化 → 通知」
     的完整链路，不依赖网络，随时可跑。
  2) 真实接口测试：连雪球真实接口采集自选股与动态，支持匿名或传入登录 Cookie。

配置：
  目标用户 ID 与登录 Cookie 建议写在项目根的 `.env.local` 里（已被 .gitignore 忽略）：
    XUEQIU_UID=1247347556
    XUEQIU_COOKIE=xq_a_token=xxx; u=123
  脚本启动时会自动加载 `.env.local`。命令行参数 --uid / --cookie 优先级高于配置文件。

用法：
  # 默认直接跑真实接口测试（uid 与 cookie 取自 .env.local）
  .venv/bin/python examples/xueqiu_monitor_test.py

  # 命令行覆盖配置文件里的值
  .venv/bin/python examples/xueqiu_monitor_test.py --uid 1247347556 --cookie "xq_a_token=xxx; u=123"

  # 额外附带离线冒烟测试
  .venv/bin/python examples/xueqiu_monitor_test.py --offline

说明：
  - 雪球用户 ID 即用户主页 URL `xueqiu.com/u/<ID>` 后面的数字。
  - 自选股走 HTTP 接口，带登录 Cookie 可稳定采集（前提是对方公开了自选股）。
  - 动态接口受阿里云 WAF（JS challenge + 滑块验证）保护，HTTP 拿不到 JSON，
    脚本改用 Playwright 有头浏览器加载用户主页后从 DOM 抽取动态；运行时会弹出
    浏览器窗口。无头模式会被 WAF 拦截，故默认有头。
  - 依赖：pip install playwright 且 python -m playwright install chromium
"""
import argparse
import os
import shutil
import tempfile

import pandas as pd

# 复用项目现成的 .env.local 加载工具（把配置读入 os.environ）
from jobs.common.local_env import load_local_env

import adata
from adata.xueqiu import MonitorConfig, XueqiuMonitor
from adata.xueqiu.collector import XueqiuCollector
from adata.xueqiu.notifier import NotificationChannel
from adata.xueqiu.snapshot import SnapshotStore


class _CapturingChannel(NotificationChannel):
    """捕获型通知渠道：记录收到的摘要与事件，便于断言与打印。"""

    def __init__(self):
        self.calls = []

    def send(self, summary, events):
        self.calls.append((summary, list(events)))


class _FakeCollector:
    """假采集器：按轮次返回预设数据，用于离线验证变化检测链路。"""

    def __init__(self):
        # 当前轮次：0 表示首轮，1 表示次轮
        self.round = 0

    def get_watchlist(self, uid):
        if self.round == 0:
            # 首轮：1 只自选股
            return pd.DataFrame([
                {"stock_code": "SH600297", "short_name": "广汇汽车"},
            ])
        # 次轮：新增 1 只自选股
        return pd.DataFrame([
            {"stock_code": "SH600297", "short_name": "广汇汽车"},
            {"stock_code": "SZ000001", "short_name": "平安银行"},
        ])

    def get_posts(self, uid):
        posts_columns = ["post_id", "publish_time", "content", "source_url"]
        if self.round == 0:
            # 首轮：无动态
            return pd.DataFrame([], columns=posts_columns)
        # 次轮：新增 1 条动态
        return pd.DataFrame([
            {
                "post_id": "p1",
                "publish_time": "2024-01-02 10:00:00",
                "content": "看好银行板块",
                "source_url": "https://xueqiu.com/1/p1",
            },
        ])


def run_offline_smoke_test():
    """离线冒烟测试：验证首轮初始化无变化、次轮检测到新增自选股与新动态。"""
    print("=" * 60)
    print("离线冒烟测试（mock 采集器，不连网络）")
    print("=" * 60)

    tmp_dir = tempfile.mkdtemp(prefix="xueqiu_snapshot_")
    try:
        capture = _CapturingChannel()
        config = MonitorConfig(user_ids=["1234567890"], channels=[capture])
        fake = _FakeCollector()
        monitor = XueqiuMonitor(
            config,
            collector=fake,
            store=SnapshotStore(base_dir=tmp_dir),
        )

        # 首轮：无历史快照，存初始快照，视为无变化
        fake.round = 0
        first_events = monitor.run_once()
        print(f"第一轮事件数：{len(first_events)}（期望 0）")
        assert len(first_events) == 0, "首轮应无变化"
        assert len(capture.calls) == 0, "首轮无事件不应发送通知"

        # 次轮：新增 1 只自选股 + 1 条动态
        fake.round = 1
        second_events = monitor.run_once()
        print(f"第二轮事件数：{len(second_events)}（期望 2）")
        for event in second_events:
            label = event.stock_code or event.post_id
            detail = event.short_name or event.content
            print(f"  -> {event.change_type}: {label} {detail}")
        assert len(second_events) == 2, "次轮应检测到 2 个变化"
        assert len(capture.calls) == 1, "次轮有事件应发送 1 次通知"

        print("离线冒烟测试通过 ✅\n")
    finally:
        # 清理临时快照目录
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_real_test(uid, cookie=None, headless=False):
    """真实接口测试：连雪球采集指定用户的自选股与动态。

    自选股走 HTTP 接口；动态走 Playwright 浏览器后端（雪球动态接口受阿里云
    WAF 保护，HTTP 拿不到 JSON，需用真实浏览器加载主页后抽取）。

    :param uid: 目标雪球用户 ID
    :param cookie: 可选，浏览器登录后的 Cookie（字符串或 dict）；为空则匿名访问
    :param headless: 浏览器后端是否无头。雪球 WAF 会拦截无头浏览器，默认有头
    """
    print("=" * 60)
    print(f"真实接口测试（uid={uid}，{'带 Cookie' if cookie else '匿名'}）")
    print("=" * 60)

    # 开启浏览器后端用于动态采集；自选股仍走 HTTP
    collector = XueqiuCollector(
        credential=cookie,
        use_browser_for_posts=True,
        browser_headless=headless,
    )

    # 采集自选股（HTTP 接口）
    try:
        watchlist = collector.get_watchlist(uid)
        print(f"[自选股] 行数：{len(watchlist)}")
        if len(watchlist):
            print(watchlist.head(20).to_string(index=False))
        else:
            print("  （空：该用户未公开自选股，或匿名态不可见）")
    except Exception as exc:
        print(f"[自选股] 采集异常：{type(exc).__name__}: {exc}")

    # 采集动态（Playwright 浏览器后端，会弹出浏览器窗口穿过 WAF）
    try:
        print("\n[动态] 正在用浏览器加载用户主页采集动态（会弹出浏览器窗口）……")
        posts = collector.get_posts(uid)
        print(f"[动态] 行数：{len(posts)}")
        if len(posts):
            print(posts.head(10).to_string(index=False))
        else:
            print("  （空：无可访问动态）")
    except Exception as exc:
        print(f"[动态] 采集异常：{type(exc).__name__}: {exc}")

    print()


def main():
    parser = argparse.ArgumentParser(description="雪球关注用户监听手动测试脚本")
    parser.add_argument("--offline", action="store_true",
                        help="额外跑离线冒烟测试（默认不跑）")
    parser.add_argument("--uid", default=None,
                        help="目标雪球用户 ID；缺省时取 .env.local 的 XUEQIU_UID")
    parser.add_argument("--cookie", default=None,
                        help="雪球登录 Cookie 串；缺省时取 .env.local 的 XUEQIU_COOKIE")
    parser.add_argument("--headless", action="store_true",
                        help="动态采集使用无头浏览器（注意：雪球 WAF 会拦截无头，默认有头）")
    args = parser.parse_args()

    # 启动时加载 .env.local（若存在），把 XUEQIU_UID / XUEQIU_COOKIE 读入环境变量
    load_local_env()

    # 仅在显式指定 --offline 时才跑离线冒烟测试
    if args.offline:
        run_offline_smoke_test()

    # 默认执行真实接口测试；uid / cookie 命令行优先，其次回退 .env.local
    uid = args.uid or os.environ.get("XUEQIU_UID")
    cookie = args.cookie or os.environ.get("XUEQIU_COOKIE")
    if not uid:
        print("未指定目标用户 ID：请用 --uid 传入，或在 .env.local 配置 XUEQIU_UID")
        return
    run_real_test(uid, cookie=cookie, headless=args.headless)


if __name__ == "__main__":
    main()
