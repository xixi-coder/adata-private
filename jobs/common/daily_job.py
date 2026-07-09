# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


def now_shanghai() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def read_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value == "":
        return default
    return value in {"1", "true", "yes", "y", "on"}


def read_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return default if value == "" else int(value)


def read_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return default if value == "" else float(value)


def write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_trade_calendar(year: int) -> pd.DataFrame:
    import adata

    calendar = adata.stock.info.trade_calendar(year=year)
    if calendar is None or calendar.empty:
        return pd.DataFrame()
    calendar = calendar.copy()
    calendar["trade_date"] = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    calendar["trade_status"] = pd.to_numeric(calendar["trade_status"], errors="coerce").fillna(0).astype(int)
    return calendar.dropna(subset=["trade_date"])


def resolve_trade_date(requested: str | None = None, action: str = "执行任务", skip_action: str = "跳过") -> tuple[str, bool, str]:
    today = requested or now_shanghai().strftime("%Y-%m-%d")
    try:
        year = pd.to_datetime(today).year
    except Exception:
        return today, False, f"日期格式无效: {today}"

    try:
        calendar = load_trade_calendar(year)
    except Exception as exc:
        calendar = pd.DataFrame()
        print(f"交易日历获取失败，退化为工作日判断: {exc}")
    if calendar.empty:
        weekday = pd.to_datetime(today).weekday()
        is_weekday = weekday < 5
        note = "交易日历不可用，已退化为工作日判断。" if is_weekday else "非工作日，跳过。"
        return today, is_weekday, note

    row = calendar[calendar["trade_date"] == today]
    if row.empty:
        return today, False, "交易日历未包含该日期，跳过。"
    is_trade_day = int(row.iloc[0]["trade_status"]) == 1
    return today, is_trade_day, f"交易日，{action}。" if is_trade_day else f"非A股交易日，{skip_action}。"
