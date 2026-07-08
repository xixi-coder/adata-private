# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class MarketDataContext:
    run_date: str
    is_trade_day: bool
    session: str
    daily_target_date: str
    intraday_date: str
    allow_today_daily: bool
    should_fetch_intraday: bool
    note: str


def now_shanghai() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _session_for_time(value: dt.time, is_trade_day: bool) -> str:
    if not is_trade_day:
        return "non_trading"
    if value < dt.time(9, 15):
        return "pre_market"
    if value < dt.time(11, 30):
        return "morning"
    if value < dt.time(13, 0):
        return "lunch"
    if value < dt.time(15, 30):
        return "afternoon"
    return "after_close"


def _load_trade_dates(year: int, loader: Callable[[int], pd.DataFrame] | None = None) -> list[str]:
    if loader is None:
        import adata

        loader = adata.stock.info.trade_calendar
    calendar = loader(year)
    if calendar is None or calendar.empty or "trade_date" not in calendar.columns:
        return []
    df = calendar.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "trade_status" in df.columns:
        status = pd.to_numeric(df["trade_status"], errors="coerce").fillna(0).astype(int)
        df = df[status.eq(1)]
    return sorted(df["trade_date"].dropna().astype(str).tolist())


def _surrounding_trade_dates(run_date: str, loader: Callable[[int], pd.DataFrame] | None) -> list[str]:
    parsed = pd.to_datetime(run_date)
    year = parsed.year
    dates = set(_load_trade_dates(year, loader))
    if parsed.month == 1:
        dates.update(_load_trade_dates(year - 1, loader))
    if parsed.month == 12:
        dates.update(_load_trade_dates(year + 1, loader))
    return sorted(dates)


def resolve_market_data_context(
    now: dt.datetime | None = None,
    requested_date: str = "",
    trade_calendar_loader: Callable[[int], pd.DataFrame] | None = None,
) -> MarketDataContext:
    current = now or now_shanghai()
    run_date = requested_date or current.strftime("%Y-%m-%d")
    trade_dates = _surrounding_trade_dates(run_date, trade_calendar_loader)
    is_trade_day = run_date in trade_dates
    session = _session_for_time(current.time(), is_trade_day)

    previous_dates = [d for d in trade_dates if d < run_date]
    latest_dates = [d for d in trade_dates if d <= run_date]
    previous_trade_date = previous_dates[-1] if previous_dates else ""
    latest_trade_date = latest_dates[-1] if latest_dates else run_date

    allow_today_daily = is_trade_day and session == "after_close"
    if allow_today_daily:
        daily_target_date = run_date
    elif is_trade_day and previous_trade_date:
        daily_target_date = previous_trade_date
    else:
        daily_target_date = latest_trade_date

    should_fetch_intraday = is_trade_day and session in {"morning", "lunch", "afternoon"}
    intraday_date = run_date if should_fetch_intraday else ""
    note = (
        f"session={session}, daily_target_date={daily_target_date}, "
        f"intraday_date={intraday_date or 'none'}"
    )
    return MarketDataContext(
        run_date=run_date,
        is_trade_day=is_trade_day,
        session=session,
        daily_target_date=daily_target_date,
        intraday_date=intraday_date,
        allow_today_daily=allow_today_daily,
        should_fetch_intraday=should_fetch_intraday,
        note=note,
    )
