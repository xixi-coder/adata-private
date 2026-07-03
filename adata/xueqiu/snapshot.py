# -*- coding: utf-8 -*-
"""
@desc: 快照持久化（Snapshot / SnapshotStore）
       按 uid 将 Watchlist 与 Posts 持久化为本地 JSON 文件，并支持读取，
       用于下一次采集时的变化比对。

@author: xueqiu-user-monitor
"""
import json
import os
from dataclasses import asdict, dataclass, field, fields


@dataclass
class Snapshot:
    """某个被监控用户一次采集所得的自选股与动态快照。

    用于下一次采集时的变化比对；序列化为 {base_dir}/{uid}.json，
    序列化与反序列化互为逆操作，满足往返一致（需求 4.4）。
    """
    # 雪球用户 ID（数字字符串），唯一标识被监控用户
    uid: str
    # 自选股列表，元素为 {stock_code, short_name} 字典
    watchlist: list = field(default_factory=list)
    # 动态列表，元素为 {post_id, publish_time, content, source_url} 字典
    posts: list = field(default_factory=list)
    # 采集时间戳（字符串）
    collected_at: str = ""


# 默认快照存储目录：用户主目录下的 .adata 缓存路径
DEFAULT_SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".adata", "xueqiu", "snapshots")


class SnapshotStore:
    """快照持久化与读取。

    按 uid 将 Snapshot 序列化为本地 JSON 文件（{base_dir}/{uid}.json），
    并支持按 uid 读取最近一次快照。序列化使用 UTF-8 编码且 ensure_ascii=False，
    正确保留中文内容；save 后 load 得到的内容与原 Snapshot 等价（需求 4.4）。
    """

    def __init__(self, base_dir: str = None):
        # base_dir 为 None 时选用默认目录
        self.base_dir = base_dir if base_dir else DEFAULT_SNAPSHOT_DIR
        # 确保存储目录存在
        os.makedirs(self.base_dir, exist_ok=True)

    def _file_path(self, uid: str) -> str:
        """根据 uid 计算快照文件路径。"""
        return os.path.join(self.base_dir, "{}.json".format(uid))

    def save(self, uid: str, snapshot: Snapshot) -> None:
        """将 Snapshot 序列化为 JSON 写入 {base_dir}/{uid}.json。

        需求 4.1：一次采集成功后将该用户的快照持久化存储。
        """
        # 将 dataclass 转为普通字典后序列化
        data = asdict(snapshot)
        file_path = self._file_path(uid)
        with open(file_path, "w", encoding="utf-8") as f:
            # ensure_ascii=False 以正确保留中文内容
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, uid: str) -> "Snapshot | None":
        """读取该用户最近一次持久化的快照；不存在则返回 None。

        需求 4.2：比对前读取该用户最近一次的快照。
        """
        file_path = self._file_path(uid)
        # 无历史快照文件时返回 None
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 仅取 Snapshot 已定义的字段，避免多余键导致构造失败
        valid_keys = {f.name for f in fields(Snapshot)}
        kwargs = {k: v for k, v in data.items() if k in valid_keys}
        return Snapshot(**kwargs)
