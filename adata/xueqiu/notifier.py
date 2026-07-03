# -*- coding: utf-8 -*-
"""
@desc: 通知器（Notifier / NotificationChannel）
       将 ChangeEvent 通过一个或多个通知渠道推送给使用者，
       支持多渠道分发、单渠道失败隔离与默认控制台渠道。
@author: xueqiu-user-monitor
"""
import logging
from abc import ABC, abstractmethod

from adata.xueqiu.differ import (
    ChangeEvent,
    CHANGE_TYPE_NEW_WATCHLIST_STOCK,
    CHANGE_TYPE_NEW_POST,
)

# 统一使用项目约定的 adata logger 记录错误
logger = logging.getLogger("adata")


def _format_event(event: ChangeEvent) -> str:
    """将单个变化事件格式化为一行可读文本。

    - 新增自选股：展示被监控用户 ID、股票代码与股票名称。
    - 新发布动态：展示被监控用户 ID、发布时间、正文内容与来源链接。
    - 其他未知类型：兜底展示事件本身。
    """
    if event.change_type == CHANGE_TYPE_NEW_WATCHLIST_STOCK:
        return (f"[新增自选股] 用户 {event.uid} 新增股票 "
                f"{event.stock_code} {event.short_name}")
    if event.change_type == CHANGE_TYPE_NEW_POST:
        return (f"[新发布动态] 用户 {event.uid} 于 {event.publish_time} "
                f"发布：{event.content} （来源：{event.source_url}）")
    # 兜底：未知的变化类型
    return f"[变化] 用户 {event.uid}：{event}"


def build_summary(events: list) -> str:
    """根据全部变化事件构造通知摘要文本（需求 7.1）。"""
    lines = [f"检测到 {len(events)} 条变化："]
    # 逐条格式化，编号从 1 开始便于阅读
    for index, event in enumerate(events, start=1):
        lines.append(f"{index}. {_format_event(event)}")
    return "\n".join(lines)


class NotificationChannel(ABC):
    """通知渠道抽象基类。

    每种具体渠道（控制台、文件、Webhook、IM 机器人等）都需实现 send 方法，
    接收摘要文本与全部变化事件并完成实际推送。
    """

    @abstractmethod
    def send(self, summary: str, events: list) -> None:
        """发送一次通知；具体渠道负责实现真正的推送逻辑。"""
        raise NotImplementedError


class ConsoleChannel(NotificationChannel):
    """控制台通知渠道：将摘要直接打印到标准输出。"""

    def send(self, summary: str, events: list) -> None:
        # 直接输出到控制台
        print(summary)


class FileChannel(NotificationChannel):
    """文件通知渠道：将摘要追加写入指定文件。"""

    def __init__(self, file_path: str, encoding: str = "utf-8"):
        # 目标文件路径与编码（默认 utf-8 以支持中文）
        self.file_path = file_path
        self.encoding = encoding

    def send(self, summary: str, events: list) -> None:
        # 以追加方式写入，保留历史通知记录
        with open(self.file_path, "a", encoding=self.encoding) as f:
            f.write(summary)
            f.write("\n")


class EmailChannel(NotificationChannel):
    """邮件通知渠道：通过 SMTP（默认 163 邮箱 SSL）发送变化摘要。

    与项目现有邮件发送方式一致（smtplib.SMTP_SSL，163 邮箱 465 端口）。
    适合在 GitHub Actions 等无人值守环境中把「新增自选股」等变化推送到邮箱。
    """

    def __init__(self, smtp_user, smtp_pass, recipients,
                 smtp_host="smtp.163.com", smtp_port=465,
                 subject_prefix="雪球监听"):
        """初始化邮件渠道。

        :param smtp_user: 发件邮箱账号（如 163 邮箱地址）
        :param smtp_pass: 邮箱 SMTP 授权码（非登录密码）
        :param recipients: 收件人，可为逗号分隔字符串或列表
        :param smtp_host: SMTP 服务器地址，默认 163
        :param smtp_port: SMTP SSL 端口，默认 465
        :param subject_prefix: 邮件主题前缀
        """
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        # 收件人统一规整为列表
        if isinstance(recipients, str):
            self.recipients = [x.strip() for x in recipients.split(",") if x.strip()]
        else:
            self.recipients = [x for x in (recipients or []) if x]
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.subject_prefix = subject_prefix

    def send(self, summary: str, events: list) -> None:
        """发送一封包含变化摘要的邮件。"""
        import smtplib
        from email.message import EmailMessage

        # 缺少必要配置时直接跳过（由 Notifier 的失败隔离兜底记录）
        if not self.smtp_user or not self.smtp_pass or not self.recipients:
            raise ValueError("邮件渠道缺少 smtp_user / smtp_pass / recipients 配置")

        # 主题里带上事件数量，便于在邮件列表中一眼看到
        subject = f"{self.subject_prefix}：检测到 {len(events)} 条新变化"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp_user
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(summary)

        # 使用 SSL 连接发送（与项目现有 163 邮件方式一致）
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
            server.login(self.smtp_user, self.smtp_pass)
            server.send_message(msg, to_addrs=self.recipients)


class Notifier:
    """通知器：将变化事件分发到一个或多个通知渠道。

    - 未配置任何渠道时，默认使用 ConsoleChannel（需求 7.5）。
    - 无事件时不发送任何通知（需求 7.2）。
    - 有事件时向每个渠道发送包含全部事件的摘要（需求 7.1、7.3）。
    - 单个渠道发送失败时记录「通知发送失败」错误并继续其余渠道（需求 7.4）。
    """

    def __init__(self, channels: list = None):
        # 未配置渠道时回退到默认控制台渠道（需求 7.5）
        if channels:
            self.channels = channels
        else:
            self.channels = [ConsoleChannel()]

    def notify(self, events: list) -> None:
        """向所有已配置渠道分发变化事件。"""
        # 无事件时不发送（需求 7.2）
        if not events:
            return

        # 构造包含全部事件的摘要（需求 7.1）
        summary = build_summary(events)

        # 逐个渠道发送，单渠道失败不影响其余渠道（需求 7.3、7.4）
        for channel in self.channels:
            try:
                channel.send(summary, events)
            except Exception as e:
                # 记录「通知发送失败」错误后继续其余渠道（需求 7.4）
                logger.error(f"通知发送失败：渠道 {channel} 发送异常：{e}")
