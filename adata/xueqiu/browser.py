# -*- coding: utf-8 -*-
"""
@desc: 雪球动态浏览器采集后端（Playwright）
       雪球的动态接口（user_timeline.json）受阿里云 WAF 的 JS challenge 与
       滑块验证保护，普通 HTTP 请求（含带登录 Cookie）拿不到 JSON。经验证：
       用「有头（headless=False）真实浏览器」加载用户主页 xueqiu.com/u/<uid>，
       可正常通过 WAF 并渲染出动态列表，再从 DOM 抽取动态字段即可。

       本后端仅用于动态采集；自选股仍走 collector.py 的 HTTP 接口。
       Playwright 为可选依赖，仅在实际调用浏览器采集时才导入。
@author: xueqiu-user-monitor
"""
import logging

import pandas as pd

# 动态返回列（与 collector.get_posts 保持一致）
_POSTS_COLUMNS = ["post_id", "publish_time", "content", "source_url"]

# 雪球站点根地址，用于把相对链接拼成完整来源链接
XUEQIU_HOME_URL = "https://xueqiu.com"

# 常用桌面 UA，尽量贴近真实浏览器，降低被反爬识别的概率
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 从用户主页 DOM 抽取动态字段的 JS：
# 每条动态是 article.timeline__item，内部带 data-id 的锚点提供 post_id 与相对链接。
_EXTRACT_JS = r"""
() => {
  const items = Array.from(document.querySelectorAll('article.timeline__item'));
  return items.map(el => {
    let postId = null, href = null;
    const idAnchor = el.querySelector('a[data-id]');
    if (idAnchor) {
      postId = idAnchor.getAttribute('data-id');
      href = idAnchor.getAttribute('href');
    }
    // 时间：优先取带 datetime 的元素，否则取显示的相对时间文本
    let timeText = null;
    const timeEl = el.querySelector('.timeline__item__date a, .date-and-source, time');
    if (timeEl) {
      timeText = (timeEl.getAttribute('datetime') || timeEl.textContent || '').trim();
    }
    // 正文
    let content = null;
    const c = el.querySelector('.timeline__item__content, .content, .timeline__item__bd');
    if (c) content = c.innerText.trim();
    return {postId, href, timeText, content};
  });
}
"""

logger = logging.getLogger("adata")


def _clean_publish_time(time_text):
    """清洗动态时间文本。

    主页渲染的时间形如「47分钟前· 来自雪球」，此处去掉「· 来自雪球」等后缀，
    仅保留时间描述部分。若为空则返回空字符串。
    """
    if not time_text:
        return ""
    # 去掉「· 来自雪球」这类来源后缀
    text = str(time_text).split("·")[0].strip()
    return text


def _to_cookie_list(cookies_dict):
    """将 cookie dict 转为 Playwright 需要的 cookie 列表（绑定到 .xueqiu.com）。"""
    return [
        {"name": str(k), "value": str(v), "domain": ".xueqiu.com", "path": "/"}
        for k, v in (cookies_dict or {}).items()
    ]


def fetch_posts_via_browser(uid, cookies_dict=None, headless=False,
                            wait_ms=4000, timeout_ms=40000):
    """用 Playwright 加载用户主页并从 DOM 抽取动态，返回结构化 DataFrame。

    :param uid: 目标雪球用户 ID
    :param cookies_dict: 登录 Cookie（dict）；为空则匿名（匿名下动态基本不可见）
    :param headless: 是否无头。经验证雪球 WAF 会拦截无头浏览器，故默认有头（False）
    :param wait_ms: 页面加载后等待渲染的毫秒数
    :param timeout_ms: 单次导航超时（毫秒）
    :return: 列为 [post_id, publish_time, content, source_url] 的 DataFrame，
             按主页默认顺序（从新到旧）排列
    :raises RuntimeError: 未安装 playwright，或页面被 WAF 滑块验证拦截时
    """
    # Playwright 为可选依赖，仅在此处导入，避免影响模块其它功能
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 playwright，无法使用浏览器动态采集。请先执行："
            "\n  pip install playwright"
            "\n  python -m playwright install chromium"
        ) from exc

    profile_url = f"{XUEQIU_HOME_URL}/u/{uid}"
    rows = []
    with sync_playwright() as p:
        # 有头模式 + 关闭自动化特征标志，降低被 WAF 识别的概率
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(user_agent=_DEFAULT_UA)
            context.add_cookies(_to_cookie_list(cookies_dict))
            page = context.new_page()
            # 用 domcontentloaded（雪球页面有长轮询，networkidle 会超时）
            page.goto(profile_url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)

            # 检测是否被 WAF 滑块验证拦截
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            lowered = (body_text or "").lower()
            if "slide to" in lowered or "verification" in lowered or "请拖" in body_text:
                raise RuntimeError(
                    f"访问用户 {uid} 主页被 WAF 滑块验证拦截。"
                    f"建议：改用有头模式并人工完成一次验证，或复用已通过验证的浏览器会话。"
                )

            raw_items = page.evaluate(_EXTRACT_JS)
            for item in raw_items or []:
                post_id = item.get("postId")
                # 缺少 post_id 的动态跳过并记录警告（需求 3.4）
                if not post_id:
                    logger.warning(f"动态缺少标识：用户 {uid} 存在一条缺少唯一标识的动态，已跳过")
                    continue
                href = item.get("href") or ""
                # 相对链接拼接为完整来源链接
                if href.startswith("http"):
                    source_url = href
                elif href:
                    source_url = XUEQIU_HOME_URL + (href if href.startswith("/") else "/" + href)
                else:
                    source_url = XUEQIU_HOME_URL
                rows.append({
                    "post_id": str(post_id),
                    "publish_time": _clean_publish_time(item.get("timeText")),
                    "content": item.get("content") or "",
                    "source_url": source_url,
                })
        finally:
            browser.close()

    # 主页默认按时间从新到旧排列，保留 DOM 顺序即满足需求 3.2
    return pd.DataFrame(rows, columns=_POSTS_COLUMNS)
