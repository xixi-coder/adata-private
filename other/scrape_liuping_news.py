#!/usr/bin/env python3
"""Find recent Education-section articles related to Liuping Middle School."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE = "http://www.ahssnews.com/news/jy/"
ARTICLE_RE = re.compile(r"^/news/jy/20\d{4}/t20\d{6}_\d+\.html$")
DATE_RE = re.compile(r"时间：\s*(20\d{2}-\d{2}-\d{2})")
SCHOOL_TERMS = ("柳坪中学", "柳坪初中")
AUTHOR_TERM = "吴林凤"


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.in_body = False
        self.body_parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if tag == "a" and href:
            self.links.append(href)
        if tag == "title":
            self.in_title = True
        if attrs_dict.get("id") == "fontZoom":
            self.in_body = True
            self.depth = 1
        elif self.in_body and tag not in {"br", "img", "meta", "link", "input", "hr"}:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.in_body and tag not in {"br", "img", "meta", "link", "input", "hr"}:
            self.depth -= 1
            if self.depth <= 0:
                self.in_body = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_body:
            self.body_parts.append(data)


def fetch(url: str, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 liuping-news-research"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def clean(text: str) -> str:
    return re.sub(r"\s+", "", unescape(text))


def article_links(html: str, page_url: str) -> set[str]:
    parser = PageParser()
    parser.feed(html)
    links = set()
    for href in parser.links:
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc == "www.ahssnews.com" and ARTICLE_RE.match(parsed.path):
            links.add(absolute)
    return links


def article_record(url: str, html: str) -> dict[str, str] | None:
    parser = PageParser()
    parser.feed(html)
    title = clean("".join(parser.title_parts)).removesuffix("宿松新闻网")
    body = clean("".join(parser.body_parts))
    match = DATE_RE.search(html)
    if not match:
        return None
    published = match.group(1)
    school_match = next((term for term in SCHOOL_TERMS if term in body), "")
    # The site places the author in the article metadata and often repeats it
    # as the final correspondent credit inside the正文区域.
    author_in_body = AUTHOR_TERM in body
    author_in_page = AUTHOR_TERM in html
    if not school_match or not (author_in_body or author_in_page):
        return None
    return {
        "date": published,
        "title": title,
        "school_match": school_match,
        "author_in_body": "yes" if author_in_body else "no",
        "url": url,
    }


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def url_date(url: str) -> date | None:
    match = re.search(r"/t(20\d{4})\d{2}_", url)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m").date().replace(day=1)


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=(today - timedelta(days=3 * 365 + 1)).isoformat())
    parser.add_argument("--end", default=today.isoformat())
    parser.add_argument("--max-pages", type=int, default=150)
    parser.add_argument("--output", type=Path, help="Optional CSV output path")
    args = parser.parse_args()
    start, end = parse_date(args.start), parse_date(args.end)

    links: set[str] = set()
    for page in range(1, args.max_pages + 1):
        page_url = BASE if page == 1 else urljoin(BASE, f"index_{page}.html")
        try:
            page_links = article_links(fetch(page_url), page_url)
        except Exception as exc:
            print(f"warning: could not read {page_url}: {exc}", file=sys.stderr)
            continue
        page_dates = [value for value in (url_date(link) for link in page_links) if value]
        if page_dates and max(page_dates) < start.replace(day=1):
            print(f"page {page}: older than --start, stopping pagination", file=sys.stderr)
            break
        before = len(links)
        links.update(page_links)
        print(f"page {page}: {len(page_links)} article links, {len(links)} total", file=sys.stderr)
        if page > 1 and not page_links and len(links) == before:
            break
        time.sleep(0.05)

    rows: list[dict[str, str]] = []
    for index, url in enumerate(sorted(links), 1):
        try:
            record = article_record(url, fetch(url))
        except Exception as exc:
            print(f"warning: could not read {url}: {exc}", file=sys.stderr)
            continue
        if record and start <= parse_date(record["date"]) <= end:
            rows.append(record)
        if index % 25 == 0:
            print(f"checked {index}/{len(links)} articles", file=sys.stderr)

    rows.sort(key=lambda row: (row["date"], row["title"]), reverse=True)
    fields = ["date", "title", "school_match", "author_in_body", "url"]
    if args.output:
        with args.output.open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"matched {len(rows)} articles", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
