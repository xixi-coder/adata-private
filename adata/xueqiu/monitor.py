# -*- coding: utf-8 -*-
"""
@desc: 监听编排与调度（XueqiuMonitor）
       编排入口：聚合采集器、快照存储、比对纯函数与通知器，
       提供单轮执行（run_once）与周期调度（start）。

       本文件实现任务 8.1：__init__ 与 run_once 单轮编排。
       周期调度 start 由任务 8.2 实现。
@author: xueqiu-user-monitor
"""
import logging
import time
from datetime import datetime

from adata.xueqiu.collector import (
    XueqiuCollector,
    WatchlistNotAccessibleError,
    CollectRequestError,
    ResponseParseError,
)
from adata.xueqiu.differ import diff_watchlist, diff_posts
from adata.xueqiu.notifier import Notifier
from adata.xueqiu.snapshot import Snapshot, SnapshotStore

# 统一使用项目约定的 adata logger 记录警告与错误
logger = logging.getLogger("adata")


class EmptyMonitorListError(Exception):
    """监控名单为空：使用者提供的被监控用户名单为空（需求 1.2）。

    在启动一轮监听前立即抛出并终止本轮，不进入逐用户处理流程。
    """

    def __init__(self, message: str = None):
        super().__init__(message or "监控名单为空")


class InvalidIntervalError(Exception):
    """执行间隔非法：使用者配置的执行间隔不是正整数（需求 8.3）。

    当 interval_seconds 小于等于 0 或非整数时，在进入周期循环前立即抛出
    并终止启动，不执行任何一轮监听。
    """

    def __init__(self, message: str = None):
        super().__init__(message or "执行间隔非法")


def _to_records(df):
    """将采集得到的 DataFrame 转为 list[dict]（按行记录）。

    比对纯函数（diff_watchlist / diff_posts）与快照存储均以 list[dict]
    作为数据结构，此处统一将 DataFrame 转换为记录列表。

    :param df: 采集器返回的 DataFrame，可能为 None
    :return: list[dict]，每个元素对应 DataFrame 的一行
    """
    # 容错：采集器约定返回 DataFrame，此处兼容 None
    if df is None:
        return []
    # to_dict(orient="records") 将每一行转为一个 dict
    return df.to_dict(orient="records")


class XueqiuMonitor:
    """监听编排入口：串联采集→读取历史快照→比对→写新快照→通知。

    依赖（采集器、快照存储、通知器）均可注入，便于测试；未注入时按
    配置构造默认实现。
    """

    def __init__(self, config, collector=None, store=None, notifier=None):
        """初始化监听器。

        :param config: MonitorConfig，含被监控用户名单、间隔、渠道与凭证
        :param collector: 采集器，未注入时用 XueqiuCollector(credential=config.credential)
        :param store: 快照存储，未注入时用 SnapshotStore()
        :param notifier: 通知器，未注入时用 Notifier(config.channels)
        """
        # 集中管理的监听配置
        self.config = config
        # 采集器：默认按配置凭证构造
        self.collector = collector if collector is not None \
            else XueqiuCollector(credential=config.credential)
        # 快照存储：默认使用默认目录
        self.store = store if store is not None else SnapshotStore()
        # 通知器：默认按配置渠道构造，并把 uid->名称映射作为 name_map 传入，
        # 使通知中以「名称(uid)」展示（渠道为空时 Notifier 内部回退到控制台渠道）
        self.notifier = notifier if notifier is not None \
            else Notifier(config.channels, name_map=getattr(config, "user_names", None))

    def run_once(self) -> list:
        """执行一轮完整监听：逐用户采集→比对→写快照，汇总事件后通知。

        流程（对应设计文档「一轮监听时序」）：
          1. 名单为空时抛出「监控名单为空」错误并终止本轮（需求 1.2）；
          2. 逐个被监控用户采集自选股与动态，转为 list[dict] 并构造新快照；
          3. 读取历史快照：无历史快照时存初始快照且视为无变化（需求 4.3）；
          4. 有历史快照时用纯函数比对得到事件，随后写入新快照；
          5. 单用户采集异常时记录错误、保留旧快照（不 save）、跳过该用户，
             继续处理下一个用户（需求 8.4、9.3）；
          6. 汇总所有用户的变化事件并调用通知器（无事件时不发送）。

        :return: 本轮汇总的全部 ChangeEvent 列表
        """
        # 需求 1.2：名单为空立即终止本轮
        if not self.config.user_ids:
            logger.error("监控名单为空，终止本轮监听")
            raise EmptyMonitorListError()

        # 汇总本轮所有用户的变化事件
        all_events = []

        # 逐个被监控用户处理（需求 8.2）
        for uid in self.config.user_ids:
            try:
                # 采集自选股与动态（接口异常/解析异常在此被捕获）
                watchlist_df = self.collector.get_watchlist(uid)
                posts_df = self.collector.get_posts(uid)
            except (CollectRequestError, ResponseParseError,
                    WatchlistNotAccessibleError) as exc:
                # 已知采集异常：记录错误并保留旧快照（不 save），跳过该用户（需求 8.4、9.3）
                logger.error(f"采集用户 {uid} 失败，保留旧快照并跳过：{exc}")
                continue
            except Exception as exc:
                # 兜底：其它未预期异常同样隔离，避免影响其余用户（需求 8.4）
                logger.error(f"采集用户 {uid} 发生未预期错误，保留旧快照并跳过：{exc}")
                continue

            # 将采集结果转为 list[dict]，作为比对与快照的统一结构
            new_watchlist = _to_records(watchlist_df)
            new_posts = _to_records(posts_df)

            # 构造本次采集的新快照，collected_at 使用当前时间戳字符串
            new_snapshot = Snapshot(
                uid=uid,
                watchlist=new_watchlist,
                posts=new_posts,
                collected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            # 读取历史快照
            old_snapshot = self.store.load(uid)

            if old_snapshot is None:
                # 需求 4.3：无历史快照时存初始快照并视为无变化（不产生事件）
                self.store.save(uid, new_snapshot)
                logger.info(f"用户 {uid} 无历史快照，已存初始快照并视为无变化")
                continue

            # 有历史快照：用纯函数比对得到变化事件（需求 5、6）
            watchlist_events = diff_watchlist(uid, new_watchlist, old_snapshot.watchlist)
            posts_events = diff_posts(uid, new_posts, old_snapshot.posts)
            all_events.extend(watchlist_events)
            all_events.extend(posts_events)

            # 采集成功后写入新快照（需求 4.1）
            self.store.save(uid, new_snapshot)

        # 汇总全部事件后通知（无事件时 Notifier 内部不发送，需求 7.1、7.2）
        self.notifier.notify(all_events)

        return all_events

    def start(self, interval_seconds: int = None) -> None:
        """周期调度入口：按固定间隔周期性执行 run_once（需求 8.1、8.3）。

        流程：
          1. 确定生效间隔：优先使用传入的 interval_seconds；为 None 时回退到
             config.interval_seconds；
          2. 校验生效间隔为正整数（需求 8.3）：非 int 或 <=0 时抛出
             「执行间隔非法」错误并在进入循环前立即终止启动；
          3. 否则进入循环：每轮调用 run_once，随后 sleep(interval_seconds)；
          4. 支持通过 KeyboardInterrupt 优雅中断：捕获后记录日志并退出循环，
             不向上抛出，便于运行时/测试停止。

        :param interval_seconds: 执行间隔（秒）；为 None 时回退到 config.interval_seconds
        :raises InvalidIntervalError: 生效间隔非正整数时抛出
        """
        # 传入间隔优先；为 None 时回退到配置中的间隔（需求 8.1）
        effective_interval = interval_seconds \
            if interval_seconds is not None else self.config.interval_seconds

        # 需求 8.3：校验间隔必须为正整数，否则终止启动
        # 注意排除 bool（bool 是 int 的子类），避免 True/False 被误判为合法间隔
        if not isinstance(effective_interval, int) or isinstance(effective_interval, bool) \
                or effective_interval <= 0:
            logger.error(f"执行间隔非法：{effective_interval}，终止启动")
            raise InvalidIntervalError()

        logger.info(f"周期监听启动，执行间隔 {effective_interval} 秒")

        # 进入周期循环：每经间隔时长执行一轮完整监听（需求 8.1）
        try:
            while True:
                # 单轮内部已隔离单用户异常（需求 8.4、9.3），此处不再吞掉编排级异常
                self.run_once()
                # 轮次之间按间隔休眠
                time.sleep(effective_interval)
        except KeyboardInterrupt:
            # 支持优雅中断：记录日志后退出循环
            logger.info("接收到中断信号，周期监听已停止")
