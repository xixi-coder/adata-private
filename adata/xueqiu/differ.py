# -*- coding: utf-8 -*-
"""
@desc: 变化检测纯函数（differ）
       无副作用地完成股票代码标准化与变化比对：
       - normalize_stock_code：股票代码标准化
       - diff_watchlist：自选股新增比对
       - diff_posts：动态新增比对

       比对纯函数（diff_watchlist / diff_posts）的
       具体实现见任务 2.3、2.5。
@author: xueqiu-user-monitor
"""
import re
from dataclasses import dataclass

from adata.common.utils.code_utils import compile_exchange_by_stock_code

# change_type 取值常量：新增自选股 / 新发布动态
CHANGE_TYPE_NEW_WATCHLIST_STOCK = "new_watchlist_stock"
CHANGE_TYPE_NEW_POST = "new_post"

# 项目统一支持的交易所后缀集合（上交所 / 深交所 / 北交所）
_EXCHANGE_SUFFIXES = ("SH", "SZ", "BJ")

# 「代码.交易所」标准形式，如 600297.SH
_NORMALIZED_PATTERN = re.compile(r"^(\d+)\.(SH|SZ|BJ)$")
# 雪球常见前缀形式，如 SH600297 / SZ000001 / BJ430047
_PREFIX_PATTERN = re.compile(r"^(SH|SZ|BJ)(\d+)$")


def normalize_stock_code(raw_code: str) -> str:
    """标准化股票代码，使同一只股票跨采集批次具有一致的代码表示（需求 2.2）。

    统一目标形式为「数字代码.交易所后缀」，例如 ``600297.SH``。
    该函数保证：
      - 确定性：相同输入永远得到相同输出；
      - 幂等性：``normalize_stock_code(normalize_stock_code(x)) == normalize_stock_code(x)``。

    支持的输入形式（大小写不敏感、允许首尾空白）：
      - 已标准化形式：``600297.SH`` → 原样返回（大写化后）；
      - 雪球前缀形式：``SH600297`` / ``SZ000001`` → ``600297.SH`` / ``000001.SZ``；
      - 纯数字形式：``600297`` → 复用 ``compile_exchange_by_stock_code``
        依据代码前两位推断交易所后缀 → ``600297.SH``；
      - 无法识别的形式（如空值、港美股代码、异常前缀）：
        返回去空白并大写化后的字符串，同样满足确定性与幂等性。
    """
    # 处理空值：统一返回空字符串，保证确定性与幂等性
    if raw_code is None:
        return ""

    # 去除首尾空白并大写化，消除大小写与空白带来的表示差异
    code = str(raw_code).strip().upper()
    if not code:
        return ""

    # 情形一：已是「数字.交易所」标准形式，直接返回（幂等的关键分支）
    if _NORMALIZED_PATTERN.match(code):
        return code

    # 情形二：雪球前缀形式（SH600297 等），将前缀转为后缀
    prefix_match = _PREFIX_PATTERN.match(code)
    if prefix_match:
        exchange, digits = prefix_match.group(1), prefix_match.group(2)
        return f"{digits}.{exchange}"

    # 情形三：纯数字形式，复用项目既有的交易所判定逻辑补全后缀
    if code.isdigit():
        compiled = compile_exchange_by_stock_code(code)
        # compile_exchange_by_stock_code 对可识别前缀补全为「代码.交易所」；
        # 无法识别时原样返回纯数字，两种结果均满足确定性与幂等性
        return compiled

    # 情形四：无法识别的形式，返回规范化（去空白、大写）后的字符串
    return code


@dataclass
class ChangeEvent:
    """比对新采集数据与历史快照后识别出的一次变化事件。

    change_type 取值为 "new_watchlist_stock"（新增自选股）
    或 "new_post"（新发布动态）。不同类型使用不同的可选字段：
      - 新增自选股事件：stock_code、short_name（需求 5.4）
      - 新发布动态事件：post_id、publish_time、content、source_url（需求 6.3）
    """
    # 被监控用户 ID
    uid: str
    # 变化类型："new_watchlist_stock" | "new_post"
    change_type: str
    # 新增自选股类事件字段（需求 5.4）
    stock_code: str = None
    short_name: str = None
    # 新发布动态类事件字段（需求 6.3）
    post_id: str = None
    publish_time: str = None
    content: str = None
    source_url: str = None


def diff_posts(uid, new_posts, old_posts) -> list:
    """动态新增比对：识别「新发布动态」类型的变化事件（需求 6.1、6.2、6.3）。

    按动态唯一标识 ``post_id`` 计算差集：将 ``post_id`` 出现在
    ``new_posts`` 中但不在 ``old_posts`` 中的每条动态识别为一个
    ``CHANGE_TYPE_NEW_POST`` 类型的 :class:`ChangeEvent`。

    该函数保证：
      - 结果事件集合恰好等于新旧 ``post_id`` 集合的差集（需求 6.1）；
      - 当 ``new_posts`` 中所有 ``post_id`` 均已存在于 ``old_posts`` 时，
        不产生任何事件（需求 6.2）；
      - 每个事件包含 uid、post_id、publish_time、content、source_url（需求 6.3）。

    :param uid: 被监控用户 ID
    :param new_posts: 本次新采集的动态列表，元素为 dict，
                      含 post_id、publish_time、content、source_url 键
    :param old_posts: 历史快照中的动态列表，元素为 dict（结构同上）
    :return: 「新发布动态」类型的 ChangeEvent 列表
    """
    # 兼容 None 输入，统一按空列表处理，保证确定性
    new_posts = new_posts or []
    old_posts = old_posts or []

    # 收集历史快照中已存在的 post_id 集合，用于差集判定
    old_post_ids = set()
    for post in old_posts:
        post_id = post.get("post_id")
        if post_id is not None:
            old_post_ids.add(post_id)

    events = []
    # 记录本轮新列表中已处理过的 post_id，避免新列表内部重复导致重复事件，
    # 从而使事件集合恰好等于「差集」（每个新 post_id 至多一个事件）
    seen_post_ids = set()
    for post in new_posts:
        post_id = post.get("post_id")
        # 缺少 post_id 的动态无法参与按标识的差集比对，跳过
        if post_id is None:
            continue
        # 已存在于历史或本轮已处理过的 post_id 不再产生事件
        if post_id in old_post_ids or post_id in seen_post_ids:
            continue
        seen_post_ids.add(post_id)
        # 生成「新发布动态」事件，携带需求 6.3 要求的完整字段
        events.append(
            ChangeEvent(
                uid=uid,
                change_type=CHANGE_TYPE_NEW_POST,
                post_id=post_id,
                publish_time=post.get("publish_time"),
                content=post.get("content"),
                source_url=post.get("source_url"),
            )
        )

    return events


def _extract_watchlist_item(item) -> tuple[str, str]:
    """从单个自选股条目中解析出（标准化股票代码, 股票名称）。

    自选股条目以 dict 形式给出（含 ``stock_code``、``short_name`` 键），
    代码经 ``normalize_stock_code`` 标准化后再用于比对，
    以保证同一只股票跨采集批次具有一致的代码表示（需求 2.2）。
    """
    # 兼容缺失键的情况，缺失时以空值处理
    raw_code = item.get("stock_code")
    short_name = item.get("short_name")
    # 标准化代码，作为比对与事件中使用的统一代码表示
    return normalize_stock_code(raw_code), short_name


def diff_watchlist(uid, new_wl, old_wl) -> list:
    """比对新旧自选股列表，返回「新增自选股」变化事件列表（需求 5.1–5.4）。

    参数：
      - uid：被监控用户 ID；
      - new_wl：本次采集的自选股列表，元素为 dict（含 stock_code、short_name）；
      - old_wl：历史快照中的自选股列表，元素为 dict（含 stock_code、short_name）。

    比对规则（对应 Property 6）：
      - 按「标准化股票代码」判定差集：产生的事件集合恰好等于
        「出现在 new_wl 中但不在 old_wl 中」的股票集合；
      - 新旧列表一致时不产生任何事件；
      - 仅存在于历史（old_wl）而不在 new_wl 中的股票不算新增，不产生事件；
      - 每个事件包含 uid、stock_code（标准化后）、short_name。
    """
    # 空值兜底，统一按空列表处理，保证纯函数的确定性
    new_wl = new_wl or []
    old_wl = old_wl or []

    # 历史列表中所有标准化代码的集合，用于快速判定是否为新增
    old_codes = {normalize_stock_code(item.get("stock_code")) for item in old_wl}

    events = []
    # 已产生事件的代码集合，避免同一新增代码在新列表中重复出现时生成多个事件
    seen_codes = set()
    for item in new_wl:
        code, short_name = _extract_watchlist_item(item)
        # 仅当该标准化代码不在历史中、且本轮尚未产生过对应事件时，才视为新增
        if code not in old_codes and code not in seen_codes:
            seen_codes.add(code)
            events.append(
                ChangeEvent(
                    uid=uid,
                    change_type=CHANGE_TYPE_NEW_WATCHLIST_STOCK,
                    stock_code=code,
                    short_name=short_name,
                )
            )
    return events
