# -*- coding: utf-8 -*-
"""
@desc: 雪球采集器（XueqiuCollector）
       负责调用雪球公开接口并解析为结构化数据（自选股与动态），
       仅做「请求 + 解析」，不做比对与持久化。

       本文件实现任务 6.1：凭证与匿名会话处理，以及采集器骨架与异常定义。
       get_watchlist / get_posts 的具体解析逻辑分别由任务 6.2、6.3 实现。
@author: xueqiu-user-monitor
"""
import logging
from datetime import datetime

import pandas as pd

from adata.common import requests
from adata.common.headers.xueqiu_headers import web_headers, json_headers
# get_watchlist 需要对股票代码做标准化，复用 differ 中的纯函数
from adata.xueqiu.differ import normalize_stock_code

# 统一使用项目约定的 adata logger 记录警告与错误
logger = logging.getLogger("adata")

# 雪球站点根地址，用于拼接动态来源链接等
XUEQIU_HOME_URL = "https://xueqiu.com"

# 匿名会话取 Cookie 的页面：访问首页根仅能拿到 WAF 的 acw_tc，
# 访问 /hq 行情页才会下发完整会话令牌（xq_a_token、xqat、u 等），
# 这些令牌是后续访问数据接口的必要凭证（需求 2.4）。
XUEQIU_SESSION_URL = "https://xueqiu.com/hq"

# 自选股接口地址模板（需求 2.1）
WATCHLIST_URL = ("https://stock.xueqiu.com/v5/stock/portfolio/stock/list.json"
                 "?size=1000&category=1&pid=-1&uid={uid}")

# 自选股返回列（固定顺序）
_WATCHLIST_COLUMNS = ["stock_code", "short_name"]

# 用户动态接口地址模板（需求 3.1）
POSTS_URL = "https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1"

# 动态返回列（固定顺序）
_POSTS_COLUMNS = ["post_id", "publish_time", "content", "source_url"]

# 判定毫秒时间戳的阈值：13 位（>= 1e11）视为毫秒，需除以 1000 转为秒
_MILLISECOND_TS_THRESHOLD = 1e11

# 业务错误提示中表明「非公开 / 无权限 / 不可访问」的关键词（需求 2.3）
_NOT_ACCESSIBLE_KEYWORDS = (
    "非公开", "未公开", "私密", "隐私", "无权", "权限", "不可见", "不可访问",
    "无法访问", "not accessible", "private", "permission", "forbidden",
)


# ---------------------------------------------------------------------------
# 采集相关异常（对应 design.md「Error Handling」表格，供任务 6.2 / 6.3 / 9 使用）
# ---------------------------------------------------------------------------
class WatchlistNotAccessibleError(Exception):
    """自选股不可访问：某被监控用户的自选股为非公开状态或无法访问（需求 2.3）。

    承载被监控用户 ID（uid），便于编排层记录错误并跳过该用户的自选股比对。
    """

    def __init__(self, uid, message: str = None):
        # 保存 uid 以便上层定位是哪个用户的自选股不可访问
        self.uid = uid
        super().__init__(message or f"自选股不可访问：用户 {uid} 的自选股为非公开状态或无法访问")


class CollectRequestError(Exception):
    """接口请求失败：对雪球接口的请求返回非成功状态或超时（需求 9.1）。

    承载被监控用户 ID（uid）。匿名会话获取阶段的请求失败没有对应用户时，
    uid 允许为 None。
    """

    def __init__(self, uid, message: str = None):
        # 保存 uid 以便上层在错误信息中附带被监控用户 ID
        self.uid = uid
        super().__init__(message or f"接口请求失败：用户 {uid} 的雪球接口请求返回非成功状态或超时")


class ResponseParseError(Exception):
    """响应解析失败：雪球接口返回的响应无法解析为预期的数据结构（需求 9.2）。

    承载被监控用户 ID（uid）与本次原始响应内容（raw），用于排查。
    """

    def __init__(self, uid, raw=None, message: str = None):
        # 保存 uid 与原始响应，满足需求 9.2「保留本次原始响应内容用于排查」
        self.uid = uid
        self.raw = raw
        super().__init__(message or f"响应解析失败：用户 {uid} 的雪球接口响应无法解析为预期结构")


# ---------------------------------------------------------------------------
# Cookie 解析辅助
# ---------------------------------------------------------------------------
def _parse_credential(credential) -> dict:
    """将传入的 Credential（Cookie）统一解析为 dict 形式。

    支持两种输入：
      - dict：形如 ``{"xq_a_token": "...", "u": "..."}``，原样拷贝返回；
      - str：形如 ``"xq_a_token=xxx; u=123"`` 的 Cookie 字符串，按分号切分解析。

    :param credential: Cookie（dict 或 cookie 字符串），可为 None
    :return: 解析后的 cookie dict；无有效内容时返回空 dict
    """
    # 无凭证时返回空 dict，交由调用方走匿名会话流程
    if not credential:
        return {}

    # dict 形式：直接拷贝一份，避免外部引用被意外修改
    if isinstance(credential, dict):
        return {str(k): str(v) for k, v in credential.items()}

    # 字符串形式：按「; 」切分为若干 key=value 对
    if isinstance(credential, str):
        cookies = {}
        for pair in credential.split(";"):
            pair = pair.strip()
            # 跳过空片段与不含「=」的非法片段
            if not pair or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    # 其它类型视为无效凭证
    return {}


# ---------------------------------------------------------------------------
# 动态解析辅助（供 get_posts 使用）
# ---------------------------------------------------------------------------
def _format_publish_time(created_at) -> str:
    """将雪球动态的发布时间戳格式化为 ``YYYY-MM-DD HH:MM:SS`` 字符串。

    雪球动态的 ``created_at`` 通常为毫秒时间戳（13 位），需除以 1000 转为秒；
    兼容传入秒级时间戳的情况。无法解析时返回空字符串（不影响其余字段）。

    :param created_at: 原始时间戳（数字或数字字符串），可为 None
    :return: 格式化后的时间字符串；无法解析时返回空字符串
    """
    # 缺失时间戳时返回空字符串
    if created_at is None:
        return ""
    # 统一转为浮点数，非法值返回空字符串
    try:
        ts = float(created_at)
    except (TypeError, ValueError):
        return ""
    # 毫秒时间戳（13 位）需除以 1000 转为秒
    if ts >= _MILLISECOND_TS_THRESHOLD:
        ts = ts / 1000.0
    # 转为本地时间字符串；越界等异常时兜底为空字符串
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _build_source_url(target) -> str:
    """由动态的相对链接 ``target`` 拼接为完整的来源链接。

    - 已是完整链接（http/https 开头）时原样返回；
    - 相对路径拼接到雪球站点根地址；
    - target 缺失时兜底返回雪球首页地址。

    :param target: 动态相对链接（如 ``/1234/567890``），可为 None
    :return: 完整来源链接
    """
    # 缺失时兜底为雪球首页
    if not target:
        return XUEQIU_HOME_URL
    target = str(target).strip()
    if not target:
        return XUEQIU_HOME_URL
    # 已是完整链接则原样返回
    if target.startswith("http://") or target.startswith("https://"):
        return target
    # 相对路径统一以「/」开头后拼接站点根地址
    if not target.startswith("/"):
        target = "/" + target
    return XUEQIU_HOME_URL + target


class XueqiuCollector:
    """雪球采集器：调用雪球公开接口并解析为结构化数据。

    仅负责「请求 + 解析」，不做比对与持久化。所有网络请求统一经
    ``adata.common.requests``（内置重试、超时与限流等待），请求头集中
    在 ``adata.common.headers.xueqiu_headers``。

    凭证处理（需求 2.4）：
      - 提供了有效 Credential（Cookie）时，优先使用该 Cookie 访问数据接口；
      - 未提供有效 Credential 时，先匿名访问雪球首页获取会话 Cookie
        （如 xq_a_token、u）并缓存，再携带该 Cookie 请求数据接口。
    """

    def __init__(self, credential=None, use_browser_for_posts=False,
                 browser_headless=False):
        """初始化采集器。

        :param credential: 访问雪球接口所需的 Cookie，可为 dict 或 cookie 字符串；
                           不提供或为空时采用匿名方式访问（需求 2.4）
        :param use_browser_for_posts: 动态采集是否使用 Playwright 浏览器后端。
                           雪球动态接口受 WAF 保护，HTTP 拿不到 JSON；开启后
                           get_posts 改为用浏览器加载用户主页并抽取动态。
        :param browser_headless: 浏览器后端是否无头。经验证雪球 WAF 会拦截无头
                           浏览器，故默认有头（False）。
        """
        # 记录原始凭证，便于排查
        self.credential = credential
        # 解析后的凭证 Cookie（dict）；为空表示未提供有效凭证
        self._credential_cookies = _parse_credential(credential)
        # 匿名会话 Cookie 缓存；首次匿名访问首页后填充，避免重复获取
        self._session_cookies = None
        # 动态采集是否走浏览器后端，以及是否无头
        self.use_browser_for_posts = use_browser_for_posts
        self.browser_headless = browser_headless

    def _ensure_session(self) -> dict:
        """确保存在可用的会话 Cookie，并返回请求数据接口时应携带的 cookies。

        优先级（需求 2.4）：
          1. 若已提供有效 Credential（Cookie），直接使用；
          2. 否则匿名访问雪球首页获取会话 Cookie，并缓存复用。

        :return: 后续数据接口请求应携带的 cookies（dict）
        """
        # 情形一：提供了有效凭证，优先使用
        if self._credential_cookies:
            return self._credential_cookies

        # 情形二：无凭证时，匿名访问首页获取会话 Cookie（缓存后复用）
        if not self._session_cookies:
            self._session_cookies = self._fetch_anonymous_cookies()
        return self._session_cookies

    # 别名：便于按 _get_cookies 语义调用，二者返回同一份 cookies
    def _get_cookies(self) -> dict:
        """返回请求数据接口应携带的 cookies，语义等同于 :meth:`_ensure_session`。"""
        return self._ensure_session()

    def _fetch_anonymous_cookies(self) -> dict:
        """匿名访问雪球行情页，获取会话 Cookie（如 xq_a_token、xqat、u）。

        使用 ``web_headers`` 经统一请求工具访问 ``/hq`` 行情页，从响应中提取
        Set-Cookie。注意：访问首页根仅能拿到 WAF 的 ``acw_tc``，只有 ``/hq``
        等页面才会下发完整会话令牌，这些令牌是访问数据接口的必要凭证（需求 2.4）。
        请求失败或未取得任何 Cookie 时抛出 :class:`CollectRequestError`。

        :return: 匿名会话 cookies（dict）
        """
        try:
            # 经统一请求工具访问行情页（内置重试与超时），使用网页请求头
            res = requests.request(method='get', url=XUEQIU_SESSION_URL, headers=web_headers)
        except Exception as exc:
            # 网络异常（超时、连接失败等）统一封装为接口请求失败
            logger.error(f"匿名获取雪球会话 Cookie 失败：{exc}")
            raise CollectRequestError(uid=None, message=f"匿名获取雪球会话 Cookie 失败：{exc}") from exc

        # 响应为空或状态非 200 均视为请求失败
        if res is None or res.status_code != 200:
            status = getattr(res, "status_code", "NA")
            logger.error(f"匿名获取雪球会话 Cookie 返回非成功状态：status={status}")
            raise CollectRequestError(uid=None, message=f"匿名获取雪球会话 Cookie 返回非成功状态：status={status}")

        # 从响应中提取会话 Cookie
        cookies = res.cookies.get_dict()
        if not cookies:
            logger.warning("匿名访问雪球首页未获取到任何会话 Cookie")
        return cookies

    def _request_json(self, uid, url):
        """携带会话 Cookie 请求雪球 JSON 数据接口，返回解析后的 JSON 对象。

        供任务 6.2 / 6.3 的 get_watchlist / get_posts 复用：统一完成
        「取 cookies → 发请求 → 校验状态 → 解析 JSON」的通用流程，并在
        边界处将异常封装为规范错误（需求 9.1、9.2）。

        :param uid: 被监控用户 ID（用于错误信息）
        :param url: 目标 JSON 接口地址
        :return: 解析后的 JSON（通常为 dict）
        """
        # 确保携带可用会话 Cookie（凭证优先，否则匿名会话）
        cookies = self._ensure_session()
        try:
            # 使用 JSON 接口请求头，经统一请求工具访问
            res = requests.request(method='get', url=url, headers=json_headers, cookies=cookies)
        except Exception as exc:
            # 网络异常统一封装为接口请求失败，附带 uid（需求 9.1）
            logger.error(f"请求雪球接口失败：uid={uid}, url={url}, error={exc}")
            raise CollectRequestError(uid=uid) from exc

        # 非成功状态视为接口请求失败（需求 9.1）
        if res is None or res.status_code != 200:
            status = getattr(res, "status_code", "NA")
            logger.error(f"请求雪球接口返回非成功状态：uid={uid}, url={url}, status={status}")
            raise CollectRequestError(uid=uid)

        # 解析 JSON，失败则封装为响应解析失败并保留原始响应（需求 9.2）
        try:
            return res.json()
        except Exception as exc:
            raw = getattr(res, "text", None)
            logger.error(f"解析雪球接口响应失败：uid={uid}, url={url}, error={exc}")
            raise ResponseParseError(uid=uid, raw=raw) from exc

    def get_watchlist(self, uid: str) -> pd.DataFrame:
        """采集某被监控用户的自选股列表（需求 2.1、2.2、2.3、9.1、9.2）。

        请求自选股接口并解析为列 ``[stock_code, short_name]`` 的 DataFrame，
        其中股票代码经 :func:`normalize_stock_code` 标准化，使同一只股票在
        不同采集批次中具有一致的代码表示（需求 2.2）。

        错误处理：
          - 自选股为非公开状态或无权限访问 -> :class:`WatchlistNotAccessibleError`（需求 2.3）；
          - 接口非成功状态或超时 -> :class:`CollectRequestError`（需求 9.1，由 ``_request_json`` 封装）；
          - 响应无法解析为预期结构 -> :class:`ResponseParseError`（需求 9.2）。

        :param uid: 被监控用户 ID
        :return: 列为 ``[stock_code, short_name]`` 的 DataFrame；
                 自选股为空时返回保留列的空 DataFrame
        """
        # 经统一流程请求并解析 JSON（接口失败/超时、JSON 解析失败在此被封装）
        url = WATCHLIST_URL.format(uid=uid)
        resp = self._request_json(uid, url)

        # 预期顶层为 dict，否则视为无法解析为预期结构（需求 9.2）
        if not isinstance(resp, dict):
            raise ResponseParseError(uid=uid, raw=resp)

        # 先判定业务错误：雪球以 error_code（非 0）配合 error_description 表达错误
        error_code = resp.get("error_code")
        error_desc = resp.get("error_description") or resp.get("error_msg") or ""
        if error_code:
            # 错误提示表明「非公开 / 无权限 / 不可访问」时抛不可访问错误（需求 2.3）
            if self._is_not_accessible(error_desc):
                logger.warning(f"用户 {uid} 的自选股不可访问：{error_desc}")
                raise WatchlistNotAccessibleError(uid=uid, message=error_desc or None)
            # 其它业务错误无法得到预期的自选股结构，视为响应解析失败并保留原始响应
            logger.error(f"用户 {uid} 自选股接口返回业务错误：code={error_code}, desc={error_desc}")
            raise ResponseParseError(uid=uid, raw=resp)

        # 解析自选股列表：预期位于 data.stocks
        data = resp.get("data")
        if not isinstance(data, dict):
            raise ResponseParseError(uid=uid, raw=resp)
        stocks = data.get("stocks")
        # stocks 缺失或类型不符视为无法解析为预期结构（需求 9.2）
        if stocks is None or not isinstance(stocks, list):
            raise ResponseParseError(uid=uid, raw=resp)

        # 逐项解析为 (标准化代码, 名称)；容忍个别条目字段异常
        rows = []
        for stock in stocks:
            if not isinstance(stock, dict):
                continue
            # symbol 为雪球带交易所前缀的代码（如 SH600297），做标准化处理
            raw_symbol = stock.get("symbol") or stock.get("stock_code") or stock.get("code")
            short_name = stock.get("name") or stock.get("short_name")
            stock_code = normalize_stock_code(raw_symbol)
            rows.append({"stock_code": stock_code, "short_name": short_name})

        # 返回固定列的 DataFrame；空自选股时保留列返回空表（需求 2.1）
        return pd.DataFrame(rows, columns=_WATCHLIST_COLUMNS)

    @staticmethod
    def _is_not_accessible(error_desc: str) -> bool:
        """根据接口的业务错误提示判断是否属于「自选股不可访问」（需求 2.3）。

        :param error_desc: 接口返回的错误描述文本
        :return: 命中「非公开 / 无权限 / 不可访问」等关键词时返回 True
        """
        if not error_desc:
            return False
        text = str(error_desc).lower()
        # 关键词大小写不敏感匹配（中文关键词不受 lower 影响）
        return any(keyword.lower() in text for keyword in _NOT_ACCESSIBLE_KEYWORDS)

    def get_posts(self, uid: str) -> pd.DataFrame:
        """采集某被监控用户的发布动态列表（需求 3.1、3.2、3.3、3.4）。

        请求用户动态接口并解析为列 ``[post_id, publish_time, content, source_url]``
        的 DataFrame，并按 ``publish_time`` 从新到旧排序（需求 3.1、3.2）。

        处理规则：
          - 无任何可访问动态时返回保留列的空 DataFrame（需求 3.3）；
          - 缺少唯一标识（post_id）的动态被跳过，并记录一条「动态缺少标识」警告（需求 3.4）；
          - 接口非成功状态或超时 -> :class:`CollectRequestError`（需求 9.1，由 ``_request_json`` 封装）；
          - 响应无法解析为预期结构 -> :class:`ResponseParseError`（需求 9.2）。

        :param uid: 被监控用户 ID
        :return: 列为 ``[post_id, publish_time, content, source_url]`` 的 DataFrame，
                 按发布时间从新到旧排序；无可访问动态时返回保留列的空 DataFrame
        """
        # 浏览器后端：雪球动态接口受 WAF 保护，HTTP 拿不到 JSON，
        # 开启开关时改用 Playwright 加载用户主页并从 DOM 抽取动态。
        if self.use_browser_for_posts:
            from adata.xueqiu.browser import fetch_posts_via_browser
            # 优先使用传入的登录 Cookie；否则退回匿名会话 Cookie
            cookies = self._credential_cookies or self._ensure_session()
            return fetch_posts_via_browser(
                uid, cookies_dict=cookies, headless=self.browser_headless,
            )

        # 经统一流程请求并解析 JSON（接口失败/超时、JSON 解析失败在此被封装）
        url = POSTS_URL.format(uid=uid)
        resp = self._request_json(uid, url)

        # 预期顶层为 dict，否则视为无法解析为预期结构（需求 9.2）
        if not isinstance(resp, dict):
            raise ResponseParseError(uid=uid, raw=resp)

        # 业务错误（error_code 非 0）无法得到预期动态结构，视为响应解析失败并保留原始响应
        error_code = resp.get("error_code")
        error_desc = resp.get("error_description") or resp.get("error_msg") or ""
        if error_code:
            logger.error(f"用户 {uid} 动态接口返回业务错误：code={error_code}, desc={error_desc}")
            raise ResponseParseError(uid=uid, raw=resp)

        # 动态列表预期位于 statuses 字段
        statuses = resp.get("statuses")
        # statuses 缺失视为无可访问动态，返回保留列的空 DataFrame（需求 3.3）
        if statuses is None:
            return pd.DataFrame([], columns=_POSTS_COLUMNS)
        # 类型不符视为无法解析为预期结构（需求 9.2）
        if not isinstance(statuses, list):
            raise ResponseParseError(uid=uid, raw=resp)

        # 逐条解析动态；以 (排序键, 行数据) 形式暂存，便于后续按时间排序
        parsed = []
        for status in statuses:
            # 容忍个别条目类型异常，跳过非 dict 项
            if not isinstance(status, dict):
                continue
            # post_id 取 id 字段；缺失则跳过并记录「动态缺少标识」警告（需求 3.4）
            raw_id = status.get("id")
            if raw_id is None:
                logger.warning(f"动态缺少标识：用户 {uid} 存在一条缺少唯一标识的动态，已跳过")
                continue
            post_id = str(raw_id)
            # created_at 为毫秒时间戳，转为可读时间字符串
            created_at = status.get("created_at")
            publish_time = _format_publish_time(created_at)
            # 正文优先取 text，其次 description（可能含 HTML，原样保留）
            content = status.get("text") or status.get("description") or ""
            # 由相对链接 target 拼接完整来源链接
            source_url = _build_source_url(status.get("target"))
            # 排序键优先用原始时间戳（数值），无法解析时兜底为 -inf 使其排在最后
            try:
                sort_key = float(created_at)
            except (TypeError, ValueError):
                sort_key = float("-inf")
            parsed.append((sort_key, {
                "post_id": post_id,
                "publish_time": publish_time,
                "content": content,
                "source_url": source_url,
            }))

        # 按发布时间从新到旧排序（时间戳降序，需求 3.2）
        parsed.sort(key=lambda item: item[0], reverse=True)
        rows = [row for _, row in parsed]

        # 返回固定列的 DataFrame；无有效动态时保留列返回空表（需求 3.3）
        return pd.DataFrame(rows, columns=_POSTS_COLUMNS)
