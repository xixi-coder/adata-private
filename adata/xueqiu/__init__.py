# -*- coding: utf-8 -*-
"""
@desc: 雪球关注用户监听模块（xueqiu-user-monitor）
       周期性采集被监控雪球用户的自选股与动态，比对历史快照识别变化并通知。

       本文件为包入口，聚合导出核心类并对外暴露门面实例 ``xueqiu``，
       与 sentiment、fund 等模块保持一致的导入方式（顶层 `from adata.xueqiu import xueqiu`）。

       用法示例：
           import adata
           # 通过门面工厂方法快速构造一个配置好的监听器
           m = adata.xueqiu.monitor(user_ids=["1234567890"], interval_seconds=60)
           m.run_once()            # 执行一轮监听
           # m.start()             # 按配置间隔周期执行

           # 高级用法：直接使用底层类自行组装
           cfg = adata.xueqiu.MonitorConfig(user_ids=["1234567890"], interval_seconds=60)
           monitor = adata.xueqiu.XueqiuMonitor(cfg)
@author: xueqiu-user-monitor
"""
from adata.xueqiu.config import MonitorConfig
from adata.xueqiu.monitor import XueqiuMonitor

# 关键类模块级导出，便于高级用法直接引用
__all__ = ["Xueqiu", "XueqiuMonitor", "MonitorConfig", "xueqiu"]


class Xueqiu:
    """雪球监听门面类：零参可实例化，提供便捷入口。

    由于 ``XueqiuMonitor`` 需要一个 ``MonitorConfig`` 才能构造，无法像
    ``Sentiment`` 那样零参实例化。因此本门面类作为对外统一入口：
      - 自身零参即可实例化为模块级单例 ``xueqiu``；
      - 通过 ``monitor(...)`` 工厂方法内部构造 ``MonitorConfig`` 并返回
        配置好的 ``XueqiuMonitor``；
      - 同时把关键类（``XueqiuMonitor``、``MonitorConfig``）作为属性暴露，
        便于高级用法自行组装采集器、快照存储与通知器。
    """

    # 关键类作为属性暴露，便于 `adata.xueqiu.XueqiuMonitor` / 高级组装
    XueqiuMonitor = XueqiuMonitor
    MonitorConfig = MonitorConfig

    def monitor(self, user_ids, interval_seconds=None,
                channels=None, credential=None) -> XueqiuMonitor:
        """工厂方法：构造并返回一个配置好的 ``XueqiuMonitor``。

        内部先根据参数构造 ``MonitorConfig``（自动完成用户 ID 校验、去重），
        再用其构造 ``XueqiuMonitor``，采集器、快照存储与通知器均采用默认实现。

        :param user_ids: 被监控的雪球用户 ID 名单（可迭代），非法/重复项会被归一化处理
        :param interval_seconds: 周期执行间隔（秒），供 ``start`` 使用；为 None 时需在 start 时显式传入
        :param channels: 通知渠道列表；为 None 时默认使用控制台渠道
        :param credential: 访问雪球接口的凭证（如 Cookie）；为 None 时匿名访问
        :return: 配置好的 XueqiuMonitor 实例
        """
        # 集中构造监听配置（内部完成名单归一化与集中参数管理）
        config = MonitorConfig(
            user_ids=user_ids,
            interval_seconds=interval_seconds,
            channels=channels,
            credential=credential,
        )
        # 用配置构造监听器，其余依赖使用默认实现
        return XueqiuMonitor(config)


# 门面实例，供顶层 `from adata.xueqiu import xueqiu` 导入使用
xueqiu = Xueqiu()
