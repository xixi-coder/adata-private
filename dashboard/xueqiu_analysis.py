"""Lightweight, deterministic analysis for Xueqiu posts."""

import html
import re
from collections import Counter
from datetime import datetime


POSITIVE_WORDS = {
    "看好", "增长", "增持", "突破", "机会", "景气", "超预期", "改善",
    "低估", "买入", "反弹", "新高", "强势", "受益", "向好", "价值",
}
NEGATIVE_WORDS = {
    "看空", "下跌", "减持", "风险", "高估", "卖出", "回撤", "低迷",
    "不及预期", "承压", "谨慎", "泡沫", "利空", "恶化", "下调", "亏损",
}
STOP_WORDS = {
    "今天", "市场", "公司", "这个", "还是", "目前", "已经", "可能", "比较",
    "一个", "我们", "需要", "就是", "如果", "没有", "继续", "关注", "认为",
}
STOCK_PATTERN = re.compile(r"\$([^$()（）]{1,20})\(([A-Z]{2}\d{5,6}|\d{6})\)\$")
TAG_PATTERN = re.compile(r"#([^#]{2,20})#")
HTML_PATTERN = re.compile(r"<[^>]+>")
WORD_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9.+-]{1,15}")


def clean_content(value):
    text = html.unescape(HTML_PATTERN.sub(" ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def normalize_influencers(payload):
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    if len(items) > 12:
        raise ValueError("最多支持 12 位用户")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("用户格式不正确")
        uid = str(item.get("uid", "")).strip()
        name = str(item.get("name", "")).strip()
        if not uid.isdigit():
            raise ValueError("雪球用户 ID 必须是数字")
        if uid in seen:
            continue
        result.append({"uid": uid, "name": name[:24] or f"用户 {uid[-4:]}"})
        seen.add(uid)
    return result


def _sentiment(text):
    positive = sum(text.count(word) for word in POSITIVE_WORDS)
    negative = sum(text.count(word) for word in NEGATIVE_WORDS)
    score = positive - negative
    if score > 0:
        return "positive", min(100, 55 + score * 12)
    if score < 0:
        return "negative", max(-100, -55 + score * 12)
    return "neutral", 0


def analyze_posts(posts, influencers, source="demo"):
    names = {item["uid"]: item["name"] for item in influencers}
    analyzed, stocks, topics = [], Counter(), Counter()
    for index, raw in enumerate(posts):
        text = clean_content(raw.get("content"))
        if not text:
            continue
        label, score = _sentiment(text)
        mentions = [{"name": name, "code": code} for name, code in STOCK_PATTERN.findall(text)]
        for stock in mentions:
            stocks[(stock["name"], stock["code"]) ] += 1
        explicit_topics = TAG_PATTERN.findall(text)
        candidates = explicit_topics or [
            word for word in WORD_PATTERN.findall(STOCK_PATTERN.sub(" ", text))
            if word not in STOP_WORDS and word not in POSITIVE_WORDS and word not in NEGATIVE_WORDS
        ]
        topics.update(candidates[:5])
        uid = str(raw.get("uid", ""))
        analyzed.append({
            "id": str(raw.get("id") or f"local-{index}"),
            "uid": uid,
            "author": names.get(uid, raw.get("author") or f"用户 {uid[-4:]}"),
            "publishedAt": str(raw.get("publish_time") or raw.get("publishedAt") or ""),
            "content": text,
            "url": str(raw.get("source_url") or raw.get("url") or ""),
            "sentiment": label,
            "score": score,
            "stocks": mentions,
        })
    analyzed.sort(key=lambda item: item["publishedAt"], reverse=True)
    counts = Counter(item["sentiment"] for item in analyzed)
    directional = counts["positive"] + counts["negative"]
    bullish = round(counts["positive"] / directional * 100) if directional else 50
    authors = []
    for user in influencers:
        own = [item for item in analyzed if item["uid"] == user["uid"]]
        avg = round(sum(item["score"] for item in own) / len(own)) if own else 0
        authors.append({**user, "postCount": len(own), "score": avg})
    return {
        "source": source,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": {
            "postCount": len(analyzed), "authorCount": len([a for a in authors if a["postCount"]]),
            "bullishPct": bullish, "stockCount": len(stocks),
        },
        "sentiment": {"positive": counts["positive"], "neutral": counts["neutral"], "negative": counts["negative"]},
        "topics": [{"name": name, "count": count} for name, count in topics.most_common(8)],
        "stocks": [{"name": key[0], "code": key[1], "count": count} for key, count in stocks.most_common(8)],
        "authors": authors,
        "posts": analyzed,
    }


def demo_posts(influencers):
    users = influencers or [{"uid": "10001", "name": "价值研究员"}]
    samples = [
        ("2026-08-07 10:26", "半导体设备订单仍在改善，国产替代是中期主线。$北方华创(SZ002371)$ 估值不低，等回撤后的机会。#半导体#"),
        ("2026-08-07 09:48", "银行板块的低估值与高股息依然提供防守价值，$招商银行(SH600036)$ 资产质量值得继续跟踪。#高股息#"),
        ("2026-08-06 21:14", "新能源车价格竞争仍然激烈，上游盈利承压。短期保持谨慎，不因一次反弹改变判断。#新能源#"),
        ("2026-08-06 15:32", "$中际旭创(SZ300308)$ 海外需求增长超预期，但强势上涨后需要观察成交量，追高风险在增加。#AI算力#"),
        ("2026-08-05 19:08", "消费复苏斜率比较温和，白酒渠道库存改善是积极信号，真正的买入机会仍取决于价格。#消费#"),
        ("2026-08-05 11:41", "市场缩量时更看重现金流。$中国神华(SH601088)$ 的分红确定性仍有价值，但周期品不能只看股息率。#煤炭#"),
        ("2026-08-04 14:20", "机器人产业趋势向好，订单兑现前容易出现主题泡沫。当前关注零部件而不是概念扩散。#机器人#"),
        ("2026-08-04 09:56", "医药的政策预期改善，创新药出海可能继续催化，但单一项目失败风险不能忽略。#创新药#"),
    ]
    return [{"id": f"demo-{i}", "uid": users[i % len(users)]["uid"], "publish_time": time_, "content": content, "source_url": ""}
            for i, (time_, content) in enumerate(samples)]
