# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

import pandas as pd


A_SHARE_INDEXES = [
    ("000001", "上证指数"),
    ("399001", "深成指"),
    ("399006", "创业板指"),
    ("000300", "沪深300"),
]

GLOBAL_INDEXES = [
    ("^IXIC", "纳指", "us_growth"),
    ("^GSPC", "标普500", "us_broad"),
    ("^SOX", "费半", "semi"),
    ("^N225", "日经225", "japan"),
    ("^KS11", "KOSPI", "korea"),
    ("^KQ11", "KOSDAQ", "korea_growth"),
    ("^HSI", "恒生指数", "hk"),
    ("^HSTECH", "恒生科技", "hk_tech"),
]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(str(value).replace("%", "").strip())
    except Exception:
        return default


def _safe_last_row(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {}
    return df.iloc[-1].to_dict()


def fetch_yahoo_quote(symbol: str, timeout: int = 8) -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=1d&interval=1m"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("chart", {}).get("result") or []
    if not result:
        return {}
    meta = result[0].get("meta", {})
    price = _to_float(meta.get("regularMarketPrice"))
    prev_close = _to_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
    change_pct = (price / prev_close - 1.0) * 100.0 if price and prev_close else 0.0
    return {
        "symbol": symbol,
        "price": price,
        "change_pct": round(change_pct, 2),
        "currency": meta.get("currency", ""),
        "exchange": meta.get("exchangeName", ""),
        "time": meta.get("regularMarketTime", ""),
    }


class MarketContextCollector:
    def __init__(
        self,
        adata_module,
        yahoo_fetcher: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.adata = adata_module
        self.yahoo_fetcher = yahoo_fetcher or fetch_yahoo_quote

    def collect(self) -> tuple[dict[str, Any], pd.DataFrame]:
        rows = []
        rows.extend(self._collect_a_share_indexes())
        rows.extend(self._collect_northbound())
        rows.extend(self._collect_global_indexes())
        context_df = pd.DataFrame(
            rows,
            columns=[
                "scope",
                "code",
                "name",
                "category",
                "price",
                "change_pct",
                "net_inflow",
                "trade_time",
                "note",
            ],
        )
        summary = self._build_summary(context_df)
        return summary, context_df

    def _collect_a_share_indexes(self) -> list[dict[str, Any]]:
        rows = []
        for code, name in A_SHARE_INDEXES:
            try:
                df = self.adata.stock.market.get_market_index_current(index_code=code)
                row = _safe_last_row(df)
            except Exception as exc:
                rows.append(self._error_row("A股", code, name, "a_share_index", str(exc)))
                continue
            if not row:
                rows.append(self._error_row("A股", code, name, "a_share_index", "empty"))
                continue
            rows.append(
                {
                    "scope": "A股",
                    "code": code,
                    "name": name,
                    "category": "a_share_index",
                    "price": _to_float(row.get("price")),
                    "change_pct": _to_float(row.get("change_pct")),
                    "net_inflow": 0.0,
                    "trade_time": str(row.get("trade_time") or ""),
                    "note": "",
                }
            )
        return rows

    def _collect_northbound(self) -> list[dict[str, Any]]:
        try:
            df = self.adata.sentiment.north.north_flow_current()
            row = _safe_last_row(df)
        except Exception as exc:
            return [self._error_row("资金", "northbound", "北向资金", "northbound", str(exc))]
        if not row:
            return [self._error_row("资金", "northbound", "北向资金", "northbound", "empty")]
        return [
            {
                "scope": "资金",
                "code": "northbound",
                "name": "北向资金",
                "category": "northbound",
                "price": 0.0,
                "change_pct": 0.0,
                "net_inflow": _to_float(row.get("net_tgt")),
                "trade_time": str(row.get("trade_time") or ""),
                "note": "",
            }
        ]

    def _collect_global_indexes(self) -> list[dict[str, Any]]:
        rows = []
        for symbol, name, category in GLOBAL_INDEXES:
            try:
                quote = self.yahoo_fetcher(symbol)
            except Exception as exc:
                rows.append(self._error_row("海外", symbol, name, category, str(exc)))
                continue
            if not quote:
                rows.append(self._error_row("海外", symbol, name, category, "empty"))
                continue
            rows.append(
                {
                    "scope": "海外",
                    "code": symbol,
                    "name": name,
                    "category": category,
                    "price": _to_float(quote.get("price")),
                    "change_pct": _to_float(quote.get("change_pct")),
                    "net_inflow": 0.0,
                    "trade_time": str(quote.get("time") or ""),
                    "note": "",
                }
            )
        return rows

    @staticmethod
    def _error_row(scope: str, code: str, name: str, category: str, error: str) -> dict[str, Any]:
        return {
            "scope": scope,
            "code": code,
            "name": name,
            "category": category,
            "price": 0.0,
            "change_pct": 0.0,
            "net_inflow": 0.0,
            "trade_time": "",
            "note": f"获取失败: {error}",
        }

    def _build_summary(self, context_df: pd.DataFrame) -> dict[str, Any]:
        a_share = context_df[context_df["category"].eq("a_share_index")].copy()
        global_df = context_df[context_df["scope"].eq("海外")].copy()
        north = context_df[context_df["category"].eq("northbound")].copy()

        a_change = pd.to_numeric(a_share["change_pct"], errors="coerce") if not a_share.empty else pd.Series(dtype=float)
        global_change = (
            pd.to_numeric(global_df["change_pct"], errors="coerce") if not global_df.empty else pd.Series(dtype=float)
        )
        north_inflow = 0.0
        if not north.empty:
            north_inflow = _to_float(north.iloc[0].get("net_inflow")) / 100_000_000

        risk_appetite = self._risk_appetite(a_change.mean() if not a_change.empty else 0.0, north_inflow)
        semi_tailwind = self._tailwind(global_df, {"semi", "korea", "korea_growth", "japan"})
        ai_tailwind = self._tailwind(global_df, {"us_growth", "semi", "hk_tech"})
        hk_china_tailwind = self._tailwind(global_df, {"hk", "hk_tech"})

        return {
            "risk_appetite": risk_appetite,
            "a_share_avg_change_pct": round(float(a_change.mean()), 2) if not a_change.empty else 0.0,
            "global_avg_change_pct": round(float(global_change.mean()), 2) if not global_change.empty else 0.0,
            "northbound_net_inflow_yi": round(north_inflow, 2),
            "external_ai_tailwind": ai_tailwind,
            "external_semi_tailwind": semi_tailwind,
            "hk_china_tailwind": hk_china_tailwind,
            "a_share_indexes": self._compact_rows(a_share),
            "global_indexes": self._compact_rows(global_df),
        }

    @staticmethod
    def _risk_appetite(a_share_avg_change_pct: float, north_inflow_yi: float) -> str:
        if a_share_avg_change_pct >= 0.6 and north_inflow_yi >= 10:
            return "强"
        if a_share_avg_change_pct <= -0.6 or north_inflow_yi <= -20:
            return "弱"
        return "中性"

    @staticmethod
    def _tailwind(global_df: pd.DataFrame, categories: set[str]) -> str:
        if global_df.empty:
            return "未知"
        sub = global_df[global_df["category"].isin(categories)]
        if sub.empty:
            return "未知"
        avg = pd.to_numeric(sub["change_pct"], errors="coerce").mean()
        if avg >= 0.8:
            return "强"
        if avg <= -0.8:
            return "弱"
        return "中性"

    @staticmethod
    def _compact_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
        rows = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "code": row.get("code", ""),
                    "name": row.get("name", ""),
                    "change_pct": round(_to_float(row.get("change_pct")), 2),
                    "note": row.get("note", ""),
                }
            )
        return rows
