# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests


EASTMONEY_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


@dataclass
class CostAnchorConfig:
    """成本锚观察池配置。"""

    lookback_days: int = 180
    near_low: float = -0.08
    near_high: float = 0.10
    min_executive_amount: float = 100_000
    page_size: int = 500
    request_timeout: int = 15
    manual_anchor_path: str | None = None


class EastmoneyDataClient:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://data.eastmoney.com/",
            }
        )

    def fetch_pages(self, params: dict[str, Any], page_size: int = 500) -> pd.DataFrame:
        params = dict(params)
        params.setdefault("source", "WEB")
        params.setdefault("client", "WEB")
        params["pageSize"] = str(page_size)
        params["pageNumber"] = "1"

        first = self._get(params)
        result = first.get("result") or {}
        total_pages = int(result.get("pages") or 0)
        frames = [pd.DataFrame(result.get("data") or [])]

        for page in range(2, total_pages + 1):
            params["pageNumber"] = str(page)
            payload = self._get(params)
            data = (payload.get("result") or {}).get("data") or []
            frames.append(pd.DataFrame(data))

        frames = [frame for frame in frames if not frame.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(EASTMONEY_DATA_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("东方财富接口返回结构异常")
        return payload


class CostAnchorStrategy:
    """构建定增价、员工持股成本、高管增持均价三类成本锚观察池。"""

    def __init__(self, config: CostAnchorConfig | None = None, client: EastmoneyDataClient | None = None):
        self.config = config or CostAnchorConfig()
        self.client = client or EastmoneyDataClient(timeout=self.config.request_timeout)

    def build_watchlist(self) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
        start_date = self._start_date()
        private_placement = self.fetch_private_placement_anchors(start_date)
        executive = self.fetch_executive_increase_anchors(start_date)
        shareholder_events = self.fetch_shareholder_increase_events(start_date)
        shareholder = self.build_shareholder_increase_anchors(shareholder_events)
        manual = self.load_manual_anchors(self.config.manual_anchor_path)

        anchors = pd.concat([private_placement, executive, shareholder, manual], ignore_index=True)
        anchors = self._score_and_filter(anchors)

        side_reports = {
            "shareholder_increase_events": shareholder_events,
            "employee_plan_events": pd.DataFrame(),
        }
        if not side_reports["shareholder_increase_events"].empty:
            holder = side_reports["shareholder_increase_events"]["holder_name"].fillna("")
            side_reports["employee_plan_events"] = side_reports["shareholder_increase_events"][
                holder.str.contains("员工持股计划", na=False)
            ].copy()
        return anchors, side_reports

    def fetch_private_placement_anchors(self, start_date: str) -> pd.DataFrame:
        params = {
            "sortColumns": "ISSUE_DATE",
            "sortTypes": "-1",
            "reportName": "RPT_SEO_DETAIL",
            "columns": "ALL",
            "quoteColumns": "f2~01~SECURITY_CODE~NEW_PRICE",
            "quoteType": "0",
            "filter": f'(SEO_TYPE="1")(ISSUE_DATE>\'{start_date}\')',
        }
        raw = self.client.fetch_pages(params, page_size=self.config.page_size)
        if raw.empty:
            return pd.DataFrame()

        df = pd.DataFrame(
            {
                "stock_code": raw.get("SECURITY_CODE"),
                "stock_name": raw.get("SECURITY_NAME_ABBR"),
                "anchor_type": "定增价",
                "anchor_price": raw.get("ISSUE_PRICE"),
                "current_price": raw.get("NEW_PRICE"),
                "anchor_date": raw.get("ISSUE_DATE"),
                "event_date": raw.get("ISSUE_LISTING_DATE"),
                "lockup": raw.get("LOCKIN_PERIOD"),
                "holder_name": raw.get("ISSUE_OBJECT"),
                "amount": raw.get("TOTAL_RAISE_FUNDS"),
                "source": "东方财富 RPT_SEO_DETAIL",
                "note": raw.get("ISSUE_WAY"),
            }
        )
        return self._normalize_anchor_frame(df)

    def fetch_executive_increase_anchors(self, start_date: str) -> pd.DataFrame:
        params = {
            "sortColumns": "CHANGE_DATE,SECURITY_CODE,PERSON_NAME",
            "sortTypes": "-1,1,1",
            "reportName": "RPT_EXECUTIVE_HOLD_DETAILS",
            "columns": "ALL",
            "quoteColumns": "f2~01~SECURITY_CODE~NEW_PRICE",
            "filter": f"(CHANGE_DATE>'{start_date}')(CHANGE_SHARES>0)(AVERAGE_PRICE>0)",
        }
        raw = self.client.fetch_pages(params, page_size=self.config.page_size)
        if raw.empty:
            return pd.DataFrame()

        raw = raw.copy()
        raw["CHANGE_SHARES"] = pd.to_numeric(raw.get("CHANGE_SHARES"), errors="coerce")
        raw["AVERAGE_PRICE"] = pd.to_numeric(raw.get("AVERAGE_PRICE"), errors="coerce")
        raw["CHANGE_AMOUNT"] = pd.to_numeric(raw.get("CHANGE_AMOUNT"), errors="coerce")
        raw["weighted_amount"] = raw["CHANGE_AMOUNT"].fillna(raw["CHANGE_SHARES"] * raw["AVERAGE_PRICE"])
        raw = raw[(raw["CHANGE_SHARES"] > 0) & (raw["AVERAGE_PRICE"] > 0)]
        if raw.empty:
            return pd.DataFrame()

        grouped_rows = []
        for stock_code, group in raw.groupby("SECURITY_CODE", dropna=True):
            total_shares = group["CHANGE_SHARES"].sum()
            total_amount = group["weighted_amount"].sum()
            if total_shares <= 0 or total_amount < self.config.min_executive_amount:
                continue
            latest = group.sort_values("CHANGE_DATE").iloc[-1]
            names = "、".join(group["PERSON_NAME"].dropna().astype(str).drop_duplicates().head(5).tolist())
            positions = "、".join(group["POSITION_NAME"].dropna().astype(str).drop_duplicates().head(5).tolist())
            grouped_rows.append(
                {
                    "stock_code": stock_code,
                    "stock_name": latest.get("SECURITY_NAME"),
                    "anchor_type": "高管增持均价",
                    "anchor_price": total_amount / total_shares,
                    "current_price": latest.get("NEW_PRICE"),
                    "anchor_date": latest.get("CHANGE_DATE"),
                    "event_date": latest.get("CHANGE_DATE"),
                    "lockup": "",
                    "holder_name": names,
                    "amount": total_amount,
                    "source": "东方财富 RPT_EXECUTIVE_HOLD_DETAILS",
                    "note": positions,
                }
            )
        return self._normalize_anchor_frame(pd.DataFrame(grouped_rows))

    def fetch_shareholder_increase_events(self, start_date: str) -> pd.DataFrame:
        params = {
            "sortColumns": "END_DATE,SECURITY_CODE,EITIME",
            "sortTypes": "-1,-1,-1",
            "reportName": "RPT_SHARE_HOLDER_INCREASE",
            "quoteColumns": "f2~01~SECURITY_CODE~NEWEST_PRICE,f3~01~SECURITY_CODE~CHANGE_RATE_QUOTES",
            "quoteType": "0",
            "columns": "ALL",
            "filter": f'(DIRECTION="增持")(END_DATE>\'{start_date}\')',
        }
        raw = self.client.fetch_pages(params, page_size=self.config.page_size)
        if raw.empty:
            return pd.DataFrame()

        df = pd.DataFrame(
            {
                "stock_code": raw.get("SECURITY_CODE"),
                "stock_name": raw.get("SECURITY_NAME_ABBR"),
                "current_price": raw.get("NEWEST_PRICE"),
                "holder_name": raw.get("HOLDER_NAME"),
                "trade_average_price": raw.get("TRADE_AVERAGE_PRICE"),
                "change_shares_10k": raw.get("CHANGE_NUM"),
                "change_ratio_total_pct": raw.get("CHANGE_RATIO"),
                "start_date": raw.get("START_DATE"),
                "end_date": raw.get("END_DATE"),
                "announce_date": raw.get("NOTICE_DATE"),
                "market": raw.get("MARKET"),
                "source": "东方财富 RPT_SHARE_HOLDER_INCREASE",
                "note": "trade_average_price 为空时，请从公告补入 manual_anchors.csv。",
            }
        )
        return self._normalize_event_frame(df)

    def build_shareholder_increase_anchors(self, events: pd.DataFrame) -> pd.DataFrame:
        if events.empty or "trade_average_price" not in events.columns:
            return pd.DataFrame()
        df = events.copy()
        df["anchor_price"] = pd.to_numeric(df["trade_average_price"], errors="coerce")
        df = df[df["anchor_price"] > 0]
        if df.empty:
            return pd.DataFrame()

        holder = df["holder_name"].fillna("")
        df["anchor_type"] = "重要股东增持均价"
        df.loc[holder.str.contains("员工持股计划", na=False), "anchor_type"] = "员工持股成本"
        df["anchor_date"] = df["end_date"]
        df["event_date"] = df["announce_date"]
        df["lockup"] = ""
        df["amount"] = df["anchor_price"] * pd.to_numeric(df["change_shares_10k"], errors="coerce") * 10000
        df["source"] = "东方财富 RPT_SHARE_HOLDER_INCREASE"
        df["note"] = df["market"].fillna("").astype(str)
        return self._normalize_anchor_frame(
            df[
                [
                    "stock_code",
                    "stock_name",
                    "anchor_type",
                    "anchor_price",
                    "current_price",
                    "anchor_date",
                    "event_date",
                    "lockup",
                    "holder_name",
                    "amount",
                    "source",
                    "note",
                ]
            ]
        )

    def load_manual_anchors(self, path: str | None) -> pd.DataFrame:
        if not path or not os.path.exists(path):
            return pd.DataFrame()
        df = pd.read_csv(path, dtype={"stock_code": str})
        if df.empty:
            return pd.DataFrame()
        required = {"stock_code", "anchor_type", "anchor_price"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"手工成本锚缺少字段: {', '.join(sorted(missing))}")
        for col in [
            "stock_name",
            "current_price",
            "anchor_date",
            "event_date",
            "lockup",
            "holder_name",
            "amount",
            "source",
            "note",
        ]:
            if col not in df.columns:
                df[col] = ""
        missing_price = pd.to_numeric(df["current_price"], errors="coerce").isna()
        if missing_price.any():
            prices = self.fetch_current_prices(df.loc[missing_price, "stock_code"].dropna().astype(str).tolist())
            df.loc[missing_price, "current_price"] = df.loc[missing_price, "stock_code"].map(prices)
        return self._normalize_anchor_frame(df)

    def fetch_current_prices(self, stock_codes: list[str]) -> dict[str, float]:
        codes = [str(code).replace(".0", "").zfill(6) for code in stock_codes if str(code).strip()]
        if not codes:
            return {}
        secids = ",".join(f"{1 if code.startswith(('6', '9')) else 0}.{code}" for code in sorted(set(codes)))
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "fltt": "2",
            "secids": secids,
            "fields": "f12,f14,f2",
        }
        response = self.client.session.get(url, params=params, timeout=self.config.request_timeout)
        response.raise_for_status()
        rows = ((response.json().get("data") or {}).get("diff") or [])
        prices = {}
        for row in rows:
            code = str(row.get("f12") or "").zfill(6)
            price = pd.to_numeric(row.get("f2"), errors="coerce")
            if code and pd.notna(price) and float(price) > 0:
                prices[code] = float(price)
        return prices

    def write_manual_template(self, path: str) -> None:
        if os.path.exists(path):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df = pd.DataFrame(
            columns=[
                "stock_code",
                "stock_name",
                "anchor_type",
                "anchor_price",
                "current_price",
                "anchor_date",
                "event_date",
                "lockup",
                "holder_name",
                "amount",
                "source",
                "note",
            ]
        )
        df.to_csv(path, index=False, encoding="utf-8-sig")

    def _score_and_filter(self, anchors: pd.DataFrame) -> pd.DataFrame:
        if anchors.empty:
            return anchors
        anchors = anchors.copy()
        anchors = anchors[(anchors["anchor_price"] > 0) & (anchors["current_price"] > 0)]
        anchors["distance_pct"] = anchors["current_price"] / anchors["anchor_price"] - 1
        anchors = anchors[anchors["distance_pct"].between(self.config.near_low, self.config.near_high)]
        if anchors.empty:
            return anchors

        anchors["abs_distance_pct"] = anchors["distance_pct"].abs()
        anchors["anchor_date"] = pd.to_datetime(anchors["anchor_date"], errors="coerce")
        anchors["days_since_anchor"] = (pd.Timestamp.today().normalize() - anchors["anchor_date"]).dt.days
        anchors["anchor_strength"] = anchors["anchor_type"].map(
            {
                "定增价": 80,
                "员工持股成本": 85,
                "实控人增持均价": 90,
                "重要股东增持均价": 82,
                "高管增持均价": 75,
            }
        ).fillna(70)
        proximity_score = (1 - (anchors["abs_distance_pct"] / max(abs(self.config.near_low), self.config.near_high))).clip(
            lower=0
        )
        recency_score = (1 - anchors["days_since_anchor"].fillna(self.config.lookback_days) / max(self.config.lookback_days, 1)).clip(
            lower=0
        )
        anchors["score"] = anchors["anchor_strength"] + proximity_score * 15 + recency_score * 5
        anchors["signal"] = anchors["distance_pct"].apply(self._signal_text)
        anchors = anchors.sort_values(["score", "abs_distance_pct"], ascending=[False, True])
        return anchors.reset_index(drop=True)

    def _normalize_anchor_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["stock_code"] = df["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        df["anchor_price"] = pd.to_numeric(df["anchor_price"], errors="coerce")
        df["current_price"] = pd.to_numeric(df["current_price"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        for col in ["anchor_date", "event_date"]:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    @staticmethod
    def _normalize_event_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["stock_code"] = df["stock_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        for col in ["current_price", "trade_average_price", "change_shares_10k", "change_ratio_total_pct"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ["start_date", "end_date", "announce_date"]:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    def _start_date(self) -> str:
        start = dt.date.today() - dt.timedelta(days=self.config.lookback_days)
        return start.strftime("%Y-%m-%d")

    @staticmethod
    def _signal_text(distance_pct: float) -> str:
        if distance_pct < -0.03:
            return "破锚观察：等重新站回锚位"
        if distance_pct < 0:
            return "锚位下方：观察承接"
        if distance_pct <= 0.03:
            return "贴近锚位：重点观察"
        return "锚位上方：等回踩"
