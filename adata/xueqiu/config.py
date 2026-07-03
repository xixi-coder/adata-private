# -*- coding: utf-8 -*-
"""
@desc: 监听配置（MonitorConfig）
       负责被监控用户名单的登记、去重与校验，以及执行间隔、通知渠道、
       凭证的集中管理。
@author: xueqiu-user-monitor
"""
import logging

# 统一使用项目约定的 adata logger 记录警告与错误
logger = logging.getLogger("adata")


class MonitorConfig:
    """监听配置：集中管理被监控用户名单、执行间隔、通知渠道与访问凭证。

    在初始化时对传入的用户 ID 名单进行归一化（校验 + 去重），
    校验规则见 ``normalize_user_ids``。
    """

    def __init__(self, user_ids, interval_seconds=None,
                 channels=None, credential=None, user_names=None):
        # 对用户 ID 名单进行归一化：剔除非法 ID、去重并保留首次出现顺序
        valid_ids, warnings = self.normalize_user_ids(user_ids)
        # 登记为 Monitored_User 的合法用户 ID 列表（需求 1.1）
        self.user_ids = valid_ids
        # 归一化过程中产生的「用户 ID 格式非法」警告列表（需求 1.4）
        self.warnings = warnings
        # 执行间隔（秒），周期调度使用；由 XueqiuMonitor 校验是否为正整数
        self.interval_seconds = interval_seconds
        # 通知渠道列表；为 None 时由 Notifier 使用默认控制台渠道
        self.channels = channels
        # 访问雪球接口所需的凭证（如 Cookie）；为 None 时匿名访问
        self.credential = credential
        # 可选的 uid -> 展示名称（备注名/昵称）映射，用于通知展示；为 None 时用空映射
        self.user_names = user_names or {}

    @staticmethod
    def normalize_user_ids(user_ids):
        """校验并去重用户 ID 名单。

        返回 (valid_unique_ids, warnings)：
          - 非数字字符串的 ID 被跳过，并生成一条「用户 ID 格式非法」警告（需求 1.4）
          - 重复 ID 去重后仅保留一个，保留首次出现顺序（需求 1.3）

        :param user_ids: 原始用户 ID 名单（可迭代）
        :return: (合法且去重后的用户 ID 列表, 警告信息列表)
        """
        valid_unique_ids = []
        warnings = []
        # 已登记的合法 ID 集合，用于 O(1) 去重判定
        seen = set()

        # user_ids 可能为 None，统一容错为空名单
        for user_id in (user_ids or []):
            # 统一转为字符串再判定，兼容传入整数等情况
            candidate = str(user_id) if user_id is not None else ""
            # 合法用户 ID 必须为非空的纯数字字符串
            if not candidate.isdigit():
                message = "用户 ID 格式非法：%s" % user_id
                warnings.append(message)
                logger.warning(message)
                continue
            # 已出现过的合法 ID 直接跳过，保留首次出现顺序
            if candidate in seen:
                continue
            seen.add(candidate)
            valid_unique_ids.append(candidate)

        return valid_unique_ids, warnings
