from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import requests
from openai import OpenAI


DEFAULT_MODEL = "z-ai/glm5"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "spm",
    "from",
    "source",
}
HIGH_CREDIBILITY_SOURCE_HINTS = (
    "证券时报",
    "上海证券报",
    "中国证券报",
    "财联社",
    "第一财经",
    "新华社",
    "经济日报",
    "财新",
    "路透",
    "彭博",
    "东方财富",
    "新浪财经",
    "同花顺",
)
SOURCE_PRESETS = {
    # A 股常见财经信息源（可按需调整）
    "a_share_core": [
        "证券时报",
        "上海证券报",
        "中国证券报",
        "财联社",
        "第一财经",
        "新华社",
        "经济日报",
        "财新",
        "东方财富",
        "新浪财经",
        "同花顺",
    ]
}


def _build_client(api_key: str | None = None, base_url: str = DEFAULT_BASE_URL) -> OpenAI:
    final_api_key = api_key or os.getenv("NVIDIA_API_KEY") or os.getenv("GROQ_API_KEY")
    if not final_api_key:
        raise RuntimeError("未找到 API key，请先 `export NVIDIA_API_KEY=...`（或兼容使用 GROQ_API_KEY）")
    return OpenAI(api_key=final_api_key, base_url=base_url)


def _google_news_rss_search(query: str, limit: int = 50, hl: str = "zh-CN", gl: str = "CN") -> list[dict]:
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    out: list[dict] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        source_name = (source.text or "").strip() if source is not None else ""
        if not title or not link:
            continue
        out.append({"title": title, "link": link, "source": source_name, "published_at": pub_date})
        if len(out) >= limit:
            break
    return out


def _normalize_title(title: str) -> str:
    title = title.lower().strip()
    # 移除标题尾部可能重复携带的站点名，避免同文多源时被误当新信息。
    title = re.sub(r"\s*[-|｜]\s*[^-|｜]{1,18}$", "", title)
    title = re.sub(r"[^\u4e00-\u9fff0-9a-z]+", "", title)
    return title


def _canonical_link(link: str) -> str:
    parsed = urllib.parse.urlsplit(link.strip())
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    clean_qs = [(k, v) for k, v in query_pairs if k.lower() not in TRACKING_QUERY_KEYS]
    clean_query = urllib.parse.urlencode(clean_qs, doseq=True)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, clean_query, ""))


def _parse_publish_time(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _tokenize_query(query: str) -> list[str]:
    parts = [p.strip().lower() for p in re.split(r"[\s,，;；/|]+", query) if p.strip()]
    # 避免单字噪音，至少保留 2 个字符。
    return [p for p in parts if len(p) >= 2]


def _source_bonus(source: str) -> float:
    for hint in HIGH_CREDIBILITY_SOURCE_HINTS:
        if hint in source:
            return 1.5
    return 0.0


def _parse_csv_tokens(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip().lower() for x in re.split(r"[,，;；|/]+", raw) if x.strip()]


def _source_allowed(source: str, whitelist: list[str], blacklist: list[str]) -> bool:
    s = (source or "").strip().lower()
    if blacklist and any(token in s for token in blacklist):
        return False
    if whitelist and not any(token in s for token in whitelist):
        return False
    return True


def _score_item(item: dict, query_tokens: list[str], now_utc: dt.datetime) -> float:
    title = item["title"].lower()
    source = item.get("source", "")
    score = 0.0

    if source:
        score += 0.6
    score += _source_bonus(source)

    publish_time = _parse_publish_time(item.get("published_at", ""))
    if publish_time:
        age_hours = max((now_utc - publish_time).total_seconds() / 3600.0, 0.0)
        if age_hours <= 6:
            score += 2.0
        elif age_hours <= 24:
            score += 1.2
        elif age_hours <= 72:
            score += 0.6

    if query_tokens:
        matched = sum(1 for token in query_tokens if token in title)
        score += min(matched, 3) * 1.0
        if matched == 0:
            score -= 0.8
    return score


def _near_duplicate(norm_title: str, selected_norm_titles: list[str], threshold: float = 0.9) -> bool:
    for t in selected_norm_titles:
        if SequenceMatcher(a=norm_title, b=t).ratio() >= threshold:
            return True
    return False


def _rank_and_filter(
    raw_items: list[dict],
    query: str,
    limit: int,
    max_per_source: int = 2,
    max_age_hours: int = 120,
    whitelist_sources: list[str] | None = None,
    blacklist_sources: list[str] | None = None,
) -> tuple[list[dict], dict]:
    stats = {
        "raw": len(raw_items),
        "after_source_filter": 0,
        "after_exact_dedup": 0,
        "after_filter": 0,
        "selected": 0,
    }
    if not raw_items:
        return [], stats

    now_utc = dt.datetime.now(dt.timezone.utc)
    query_tokens = _tokenize_query(query)
    whitelist_sources = whitelist_sources or []
    blacklist_sources = blacklist_sources or []

    exact_seen_links: set[str] = set()
    exact_seen_titles: set[str] = set()
    prepared: list[dict] = []
    for item in raw_items:
        if not _source_allowed(
            source=item.get("source", ""),
            whitelist=whitelist_sources,
            blacklist=blacklist_sources,
        ):
            continue
        norm_title = _normalize_title(item["title"])
        canonical_link = _canonical_link(item["link"])
        if not norm_title:
            continue
        if canonical_link in exact_seen_links or norm_title in exact_seen_titles:
            continue
        exact_seen_links.add(canonical_link)
        exact_seen_titles.add(norm_title)

        publish_time = _parse_publish_time(item.get("published_at", ""))
        if publish_time is not None:
            age_hours = max((now_utc - publish_time).total_seconds() / 3600.0, 0.0)
            if age_hours > max_age_hours:
                continue

        enriched = dict(item)
        enriched["norm_title"] = norm_title
        enriched["canonical_link"] = canonical_link
        enriched["quality_score"] = _score_item(item, query_tokens=query_tokens, now_utc=now_utc)
        prepared.append(enriched)

    stats["after_source_filter"] = len(prepared)
    stats["after_exact_dedup"] = len(prepared)
    if not prepared:
        return [], stats

    prepared.sort(
        key=lambda x: (
            x["quality_score"],
            _parse_publish_time(x.get("published_at", "")) or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc),
        ),
        reverse=True,
    )

    selected: list[dict] = []
    selected_norm_titles: list[str] = []
    source_counter: Counter[str] = Counter()

    for item in prepared:
        source_key = (item.get("source") or "unknown").strip().lower()
        if source_counter[source_key] >= max_per_source:
            continue
        if _near_duplicate(item["norm_title"], selected_norm_titles):
            continue
        selected.append(item)
        selected_norm_titles.append(item["norm_title"])
        source_counter[source_key] += 1
        if len(selected) >= limit:
            break

    # 兜底：若过滤过严导致数量不足，放宽来源上限但仍保留近似去重。
    if len(selected) < limit:
        existing_links = {x["canonical_link"] for x in selected}
        for item in prepared:
            if item["canonical_link"] in existing_links:
                continue
            if _near_duplicate(item["norm_title"], selected_norm_titles, threshold=0.93):
                continue
            selected.append(item)
            selected_norm_titles.append(item["norm_title"])
            existing_links.add(item["canonical_link"])
            if len(selected) >= limit:
                break

    stats["after_filter"] = len(prepared)
    stats["selected"] = len(selected)
    return selected, stats


def _build_summary_prompt(query: str, items: list[dict], stats: dict) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bullets = []
    for i, item in enumerate(items, start=1):
        bullets.append(
            f"{i}. 标题: {item['title']}\n"
            f"   来源: {item['source'] or '未知'}\n"
            f"   时间: {item['published_at'] or '未知'}\n"
            f"   质量分: {item['quality_score']:.2f}\n"
            f"   链接: {item['link']}"
        )
    raw_news = "\n".join(bullets)

    return textwrap.dedent(
        f"""
        你是一个信息分析助手。请基于下面“去重后的搜索结果”生成中文总结。

        要求：
        1) 先给“核心结论”（3-5条）。
        2) 再给“分主题总结”。
        3) 明确“已确认事实”和“推测/可能性”。
        4) 给“潜在风险与不确定性”（至少3条）。
        5) 最后给“可执行跟进建议”（3条以内）。
        6) 若多条新闻明显为同一事件，请合并叙述，不要重复。
        7) 不要编造来源，所有信息仅可来自提供的搜索结果。

        查询词: {query}
        抓取时间: {now}
        检索统计: 原始{stats['raw']}条 -> 来源过滤后{stats['after_source_filter']}条 -> 初步去重与时效过滤{stats['after_filter']}条 -> 入选{stats['selected']}条

        搜索结果:
        {raw_news}
        """
    ).strip()


def summarize_news(
    query: str,
    limit: int = 12,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    max_per_source: int = 2,
    max_age_hours: int = 120,
    source_whitelist: str | None = None,
    source_blacklist: str | None = None,
    source_preset: str | None = None,
) -> str:
    raw_limit = max(limit * 5, 50)
    raw_items = _google_news_rss_search(query=query, limit=raw_limit)
    whitelist_tokens = _parse_csv_tokens(source_whitelist)
    blacklist_tokens = _parse_csv_tokens(source_blacklist)
    if source_preset:
        for item in SOURCE_PRESETS.get(source_preset, []):
            token = item.strip().lower()
            if token and token not in whitelist_tokens:
                whitelist_tokens.append(token)

    items, stats = _rank_and_filter(
        raw_items=raw_items,
        query=query,
        limit=limit,
        max_per_source=max_per_source,
        max_age_hours=max_age_hours,
        whitelist_sources=whitelist_tokens,
        blacklist_sources=blacklist_tokens,
    )
    if not items:
        return "没有搜索到高质量结果，请尝试缩短关键词或放宽时间范围。"

    client = _build_client(api_key=api_key, base_url=base_url)
    prompt = _build_summary_prompt(query=query, items=items, stats=stats)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    answer = resp.choices[0].message.content or ""

    refs = "\n".join([f"- {it['title']} | {it['source']} | {it['link']}" for it in items])
    stats_text = (
        f"检索统计: 原始{stats['raw']}条 -> 来源过滤后{stats['after_source_filter']}条 -> "
        f"初步去重与时效过滤{stats['after_filter']}条 -> 入选{stats['selected']}条"
    )
    return f"{stats_text}\n\n{answer}\n\n参考链接:\n{refs}"


def main() -> None:
    parser = argparse.ArgumentParser(description="联网搜索并用 GLM 生成摘要（含去重与质量过滤）")
    parser.add_argument("query", help="搜索关键词，例如：A股 机器人板块")
    parser.add_argument("--limit", type=int, default=12, help="最终用于总结的新闻条数，默认 12")
    parser.add_argument("--max-per-source", type=int, default=2, help="单一来源最多保留条数，默认 2")
    parser.add_argument("--max-age-hours", type=int, default=120, help="仅保留最近 N 小时新闻，默认 120")
    parser.add_argument(
        "--source-whitelist",
        default=None,
        help="来源白名单关键词，逗号分隔；若设置则只保留命中来源，如: 财联社,证券时报,新华社",
    )
    parser.add_argument(
        "--source-blacklist",
        default=None,
        help="来源黑名单关键词，逗号分隔；命中即剔除，如: 自媒体,营销",
    )
    parser.add_argument(
        "--source-preset",
        choices=sorted(SOURCE_PRESETS.keys()),
        default=None,
        help="来源白名单预设，例如: a_share_core",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="可选，直接传入 API key；不传则优先读 NVIDIA_API_KEY，再回退 GROQ_API_KEY",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI 兼容接口地址，默认 {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名，默认 {DEFAULT_MODEL}")
    args = parser.parse_args()

    result = summarize_news(
        query=args.query,
        limit=args.limit,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        max_per_source=args.max_per_source,
        max_age_hours=args.max_age_hours,
        source_whitelist=args.source_whitelist,
        source_blacklist=args.source_blacklist,
        source_preset=args.source_preset,
    )
    print(result)


if __name__ == "__main__":
    main()
