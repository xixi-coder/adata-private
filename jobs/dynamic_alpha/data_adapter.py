from __future__ import annotations

import json
import math
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from jobs.common.a_share_metadata import is_supported_a_share_code, normalize_code
from jobs.common.cloud_cache_sync import SHARED_MARKET_CACHE_ARCHIVE, sync_cache_from_drive


FINANCE_COLUMNS = {
    "stock_code",
    "notice_date",
    "report_date",
    "roe_wtd",
    "gross_margin",
    "asset_liab_ratio",
    "basic_eps",
    "net_asset_ps",
    "oper_cf_ps",
}


@dataclass
class DriveResearchData:
    panel: pd.DataFrame
    fundamentals: pd.DataFrame
    benchmark: pd.DataFrame
    audit: dict[str, Any]
    effective_end_date: pd.Timestamp


def sync_drive_cache(project_root: str, enabled: bool = True) -> bool:
    """Restore the shared Drive bundle into data/cache without writing to Drive."""

    if not enabled:
        return False
    return sync_cache_from_drive(project_root, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])


def load_drive_research_data(
    project_root: str,
    *,
    requested_end_date: str | None = None,
    max_stocks: int | None = 800,
    complete_ratio: float = 0.95,
    coverage_lookback: int = 120,
    attach_dividends: bool = True,
) -> DriveResearchData:
    """Load and adapt the repository's Drive cache for Dynamic Alpha."""

    root = Path(project_root)
    cache_dir = root / "data" / "cache"
    cache_file = cache_dir / "full_data_v3_5year.pkl"
    if not cache_file.exists():
        raise FileNotFoundError(f"Drive market cache is missing: {cache_file}")
    with cache_file.open("rb") as handle:
        payload = pickle.load(handle)
    stock_map = payload.get("stock", payload) if isinstance(payload, dict) else {}
    if not isinstance(stock_map, dict) or not stock_map:
        raise ValueError("Drive market cache does not contain a stock-to-DataFrame mapping")

    normalized_map = {
        normalize_code(code): frame
        for code, frame in stock_map.items()
        if is_supported_a_share_code(normalize_code(code)) and isinstance(frame, pd.DataFrame) and not frame.empty
    }
    coverage = build_coverage_table(normalized_map)
    complete_date, coverage_details = choose_complete_date(
        coverage,
        requested_end_date=requested_end_date,
        complete_ratio=complete_ratio,
        coverage_lookback=coverage_lookback,
    )
    selected_codes, selection_details = select_liquid_codes(normalized_map, complete_date, max_stocks=max_stocks)
    metadata = load_current_metadata(root)
    industry_map, industry_source = load_industry_map(cache_dir)
    panel = build_market_panel(
        normalized_map,
        selected_codes,
        effective_end_date=complete_date,
        metadata=metadata,
        industry_map=industry_map,
    )
    if attach_dividends:
        panel, dividend_details = attach_realized_dividend_yield(panel, cache_dir / "dividend")
    else:
        dividend_details = {"enabled": False, "file_count": 0, "parsed_event_count": 0, "coverage": 0.0}
    fundamentals, finance_details = load_fundamentals(
        cache_dir / "finance",
        selected_codes,
        latest_market_date=complete_date,
    )
    benchmark, benchmark_details = load_benchmark(cache_dir / "benchmark_000300.csv", complete_date)

    stock_history = panel.groupby("stock_code")["trade_date"].agg(["min", "max", "count"])
    stock_history["history_years"] = (stock_history["max"] - stock_history["min"]).dt.days / 365.25
    history_years = float(stock_history["history_years"].median())
    warnings: list[str] = []
    if history_years < 3.0:
        warnings.append(f"有效行情跨度只有 {history_years:.2f} 年，不足以验证长期年化目标。")
    if max_stocks and max_stocks > 0:
        warnings.append("股票池按结束日流动性截取，仅适合近期研究；历史绩效存在股票池选择偏差。")
    if industry_source == "missing":
        warnings.append("未找到行业映射，行业中性和行业仓位上限将自动退化。")
    if finance_details["invalid_notice_rows"]:
        warnings.append(f"财务数据有 {finance_details['invalid_notice_rows']} 行无效公告日期，已排除。")
    warnings.append("缓存使用当前股票名称识别 ST/退市状态，不等同于历史逐日状态。")
    warnings.append("行情由现有缓存生成逻辑标记为前复权口径；回测交易价仍需进一步处理公司行动。")

    audit = {
        "source": {
            "archive": SHARED_MARKET_CACHE_ARCHIVE,
            "market_cache": str(cache_file),
            "finance_dir": str(cache_dir / "finance"),
            "dividend_dir": str(cache_dir / "dividend"),
            "benchmark_file": str(cache_dir / "benchmark_000300.csv"),
        },
        "coverage": coverage_details,
        "universe": selection_details,
        "market": {
            "row_count": int(len(panel)),
            "stock_count": int(panel["stock_code"].nunique()),
            "start_date": panel["trade_date"].min().strftime("%Y-%m-%d"),
            "end_date": panel["trade_date"].max().strftime("%Y-%m-%d"),
            "history_years": round(history_years, 3),
            "maximum_history_years": round(float(stock_history["history_years"].max()), 3),
            "median_rows_per_stock": float(stock_history["count"].median()),
            "price_adjustment": "forward_adjusted_by_cache_builder",
        },
        "finance": finance_details,
        "dividend": dividend_details,
        "benchmark": benchmark_details,
        "industry": {
            "source": industry_source,
            "mapped_stock_count": int(panel.loc[panel["industry"] != "UNKNOWN", "stock_code"].nunique()),
            "coverage": round(float((panel["industry"] != "UNKNOWN").mean()), 6),
        },
        "research_limitations": warnings,
        "formal_long_horizon_ready": bool(history_years >= 5.0 and industry_source != "missing"),
    }
    return DriveResearchData(panel, fundamentals, benchmark, audit, complete_date)


def build_coverage_table(stock_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    counts: dict[pd.Timestamp, int] = {}
    for frame in stock_map.values():
        date_column = "trade_date" if "trade_date" in frame else "trade_time" if "trade_time" in frame else ""
        if not date_column:
            continue
        dates = pd.to_datetime(frame[date_column], errors="coerce").dropna().dt.normalize().drop_duplicates()
        for date in dates:
            counts[date] = counts.get(date, 0) + 1
    if not counts:
        raise ValueError("No valid trade dates found in Drive market cache")
    return pd.DataFrame(
        {"trade_date": list(counts.keys()), "stock_count": list(counts.values())}
    ).sort_values("trade_date").reset_index(drop=True)


def choose_complete_date(
    coverage: pd.DataFrame,
    *,
    requested_end_date: str | None,
    complete_ratio: float,
    coverage_lookback: int,
) -> tuple[pd.Timestamp, dict[str, Any]]:
    if not 0 < complete_ratio <= 1:
        raise ValueError("complete_ratio must be in (0, 1]")
    table = coverage.copy()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    requested = pd.Timestamp(requested_end_date).normalize() if requested_end_date else None
    if requested is not None:
        table = table[table["trade_date"] <= requested]
    if table.empty:
        raise ValueError("No market coverage on or before requested end date")
    recent = table.tail(max(int(coverage_lookback), 1))
    peak_count = int(recent["stock_count"].max())
    minimum_count = int(math.ceil(peak_count * complete_ratio))
    passing = recent[recent["stock_count"] >= minimum_count]
    if passing.empty:
        raise RuntimeError("No date passes the cross-section completeness threshold")
    effective = pd.Timestamp(passing.iloc[-1]["trade_date"])
    effective_count = int(passing.iloc[-1]["stock_count"])
    latest_observed = pd.Timestamp(table.iloc[-1]["trade_date"])
    return effective, {
        "requested_end_date": requested.strftime("%Y-%m-%d") if requested is not None else None,
        "latest_observed_date": latest_observed.strftime("%Y-%m-%d"),
        "effective_end_date": effective.strftime("%Y-%m-%d"),
        "coverage_lookback_sessions": int(min(len(table), coverage_lookback)),
        "peak_stock_count": peak_count,
        "minimum_complete_count": minimum_count,
        "effective_stock_count": effective_count,
        "effective_ratio_to_peak": round(effective_count / peak_count, 6) if peak_count else 0.0,
        "complete_ratio_parameter": complete_ratio,
    }


def select_liquid_codes(
    stock_map: dict[str, pd.DataFrame],
    effective_end_date: pd.Timestamp,
    *,
    max_stocks: int | None,
) -> tuple[list[str], dict[str, Any]]:
    rows = []
    for code, raw in stock_map.items():
        date_column = "trade_date" if "trade_date" in raw else "trade_time" if "trade_time" in raw else ""
        if not date_column or "amount" not in raw:
            continue
        dates = pd.to_datetime(raw[date_column], errors="coerce").dt.normalize()
        eligible = raw.loc[dates <= effective_end_date].copy()
        if eligible.empty or not dates.eq(effective_end_date).any():
            continue
        amount = pd.to_numeric(eligible["amount"], errors="coerce").tail(20).mean()
        rows.append((code, float(amount) if pd.notna(amount) else -1.0, len(eligible)))
    rows.sort(key=lambda item: (-item[1], item[0]))
    available_count = len(rows)
    selected = rows[:max_stocks] if max_stocks and max_stocks > 0 else rows
    codes = [code for code, _, _ in selected]
    return codes, {
        "selection_mode": "effective_end_date_liquidity" if max_stocks and max_stocks > 0 else "all_cached_stocks",
        "available_stock_count": available_count,
        "selected_stock_count": len(codes),
        "max_stocks_parameter": max_stocks,
        "minimum_selected_rows": int(min((item[2] for item in selected), default=0)),
    }


def build_market_panel(
    stock_map: dict[str, pd.DataFrame],
    codes: Iterable[str],
    *,
    effective_end_date: pd.Timestamp,
    metadata: dict[str, dict[str, Any]],
    industry_map: dict[str, str],
) -> pd.DataFrame:
    pieces = []
    required = ["open", "high", "low", "close", "volume", "amount"]
    for code in codes:
        raw = stock_map.get(code)
        if raw is None or raw.empty:
            continue
        frame = raw.copy()
        if "trade_date" not in frame:
            if "trade_time" not in frame:
                continue
            frame["trade_date"] = frame["trade_time"]
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
        frame = frame[frame["trade_date"] <= effective_end_date]
        if frame.empty or any(column not in frame for column in required):
            continue
        frame["stock_code"] = code
        keep = ["stock_code", "trade_date", *required]
        keep.extend(column for column in ["pre_close", "turnover_ratio"] if column in frame)
        frame = frame[keep].copy()
        for column in required + ["pre_close", "turnover_ratio"]:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        current = metadata.get(code, {})
        frame["stock_name"] = str(current.get("short_name") or "")
        frame["industry"] = industry_map.get(code, "UNKNOWN") or "UNKNOWN"
        pieces.append(frame)
    if not pieces:
        raise ValueError("No valid market rows remain after Drive adaptation")
    return (
        pd.concat(pieces, ignore_index=True)
        .dropna(subset=["trade_date", "open", "high", "low", "close", "amount"])
        .sort_values(["stock_code", "trade_date"])
        .drop_duplicates(["stock_code", "trade_date"], keep="last")
        .reset_index(drop=True)
    )


def load_current_metadata(project_root: Path) -> dict[str, dict[str, Any]]:
    path = project_root / "tests" / "utils" / "all_code.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype={"stock_code": str})
    if "stock_code" not in frame:
        return {}
    frame["stock_code"] = frame["stock_code"].map(normalize_code)
    if "short_name" not in frame:
        frame["short_name"] = ""
    return frame.drop_duplicates("stock_code").set_index("stock_code")[["short_name"]].to_dict("index")


def load_industry_map(cache_dir: Path) -> tuple[dict[str, str], str]:
    candidates = [cache_dir / "stock_industry.csv", cache_dir / "industry.csv"]
    for path in candidates:
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype={"stock_code": str})
        code_column = next((column for column in ["stock_code", "code", "股票代码"] if column in frame), "")
        industry_column = next(
            (column for column in ["industry", "industry_name", "sw_industry", "申万行业", "行业"] if column in frame),
            "",
        )
        if not code_column or not industry_column:
            continue
        frame["_code"] = frame[code_column].map(normalize_code)
        frame["_industry"] = frame[industry_column].fillna("").astype(str).str.strip()
        mapping = frame[frame["_industry"] != ""].drop_duplicates("_code").set_index("_code")["_industry"].to_dict()
        return mapping, str(path)
    return {}, "missing"


def load_fundamentals(
    finance_dir: Path,
    codes: Iterable[str],
    *,
    latest_market_date: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    pieces = []
    file_count = 0
    raw_rows = 0
    invalid_notice_rows = 0
    for code in codes:
        path = finance_dir / f"{code}.csv"
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path, usecols=lambda column: column in FINANCE_COLUMNS)
        except Exception:
            continue
        if frame.empty or "notice_date" not in frame or "report_date" not in frame:
            continue
        file_count += 1
        raw_rows += len(frame)
        notice = pd.to_datetime(frame["notice_date"], errors="coerce").dt.normalize()
        report = pd.to_datetime(frame["report_date"], errors="coerce").dt.normalize()
        valid = (
            notice.notna()
            & report.notna()
            & (notice >= pd.Timestamp("1990-01-01"))
            & (notice <= latest_market_date)
            & (report <= notice)
        )
        invalid_notice_rows += int((~valid).sum())
        frame = frame.loc[valid].copy()
        if frame.empty:
            continue
        frame["stock_code"] = code
        frame["announce_date"] = notice.loc[valid]
        frame["report_date"] = report.loc[valid]
        for column in FINANCE_COLUMNS.difference({"stock_code", "notice_date", "report_date"}):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        annualizer = frame["report_date"].dt.month.map({3: 4.0, 6: 2.0, 9: 4.0 / 3.0, 12: 1.0}).fillna(1.0)
        eps = pd.to_numeric(frame.get("basic_eps"), errors="coerce")
        operating_cf = pd.to_numeric(frame.get("oper_cf_ps"), errors="coerce")
        adapted = pd.DataFrame(
            {
                "stock_code": code,
                "announce_date": frame["announce_date"],
                "report_date": frame["report_date"],
                "roe": pd.to_numeric(frame.get("roe_wtd"), errors="coerce") / 100.0,
                "gross_margin": pd.to_numeric(frame.get("gross_margin"), errors="coerce") / 100.0,
                "debt_ratio": pd.to_numeric(frame.get("asset_liab_ratio"), errors="coerce") / 100.0,
                "eps_ttm": eps * annualizer,
                "operating_cashflow_ps_ttm": operating_cf * annualizer,
                "net_asset_ps": pd.to_numeric(frame.get("net_asset_ps"), errors="coerce"),
            }
        )
        adapted["cashflow_to_profit"] = adapted["operating_cashflow_ps_ttm"] / adapted[
            "eps_ttm"
        ].abs().replace(0, np.nan)
        adapted = adapted.sort_values(["announce_date", "report_date"]).drop_duplicates(
            "announce_date", keep="last"
        )
        pieces.append(adapted)
    columns = [
        "stock_code",
        "announce_date",
        "report_date",
        "roe",
        "gross_margin",
        "debt_ratio",
        "eps_ttm",
        "operating_cashflow_ps_ttm",
        "net_asset_ps",
        "cashflow_to_profit",
    ]
    fundamentals = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=columns)
    if not fundamentals.empty:
        fundamentals = fundamentals.sort_values(["stock_code", "announce_date", "report_date"]).reset_index(drop=True)
    return fundamentals, {
        "file_count": file_count,
        "raw_row_count": raw_rows,
        "adapted_row_count": int(len(fundamentals)),
        "stock_count": int(fundamentals["stock_code"].nunique()) if not fundamentals.empty else 0,
        "invalid_notice_rows": invalid_notice_rows,
        "announcement_alignment": "next_market_session",
        "interim_value_method": "annualized_cumulative_per_share_proxy",
    }


def load_benchmark(path: Path, effective_end_date: pd.Timestamp) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.exists():
        return pd.DataFrame(columns=["trade_date", "close"]), {"available": False}
    frame = pd.read_csv(path)
    if not {"trade_date", "close"} <= set(frame.columns):
        return pd.DataFrame(columns=["trade_date", "close"]), {"available": False}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = (
        frame[frame["trade_date"] <= effective_end_date][["trade_date", "close"]]
        .dropna()
        .sort_values("trade_date")
        .drop_duplicates("trade_date")
    )
    return frame, {
        "available": not frame.empty,
        "row_count": int(len(frame)),
        "start_date": frame["trade_date"].min().strftime("%Y-%m-%d") if not frame.empty else None,
        "end_date": frame["trade_date"].max().strftime("%Y-%m-%d") if not frame.empty else None,
    }


def attach_realized_dividend_yield(
    panel: pd.DataFrame,
    dividend_dir: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not dividend_dir.exists():
        out = panel.copy()
        out["dividend_yield_ttm"] = np.nan
        return out, {"enabled": True, "file_count": 0, "parsed_event_count": 0, "coverage": 0.0}
    pieces = []
    file_count = 0
    parsed_events = 0
    stocks_with_events = 0
    for code, prices in panel.groupby("stock_code", sort=False):
        prices = prices.copy()
        path = dividend_dir / f"{code}.csv"
        if not path.exists():
            prices["dividend_yield_ttm"] = np.nan
            pieces.append(prices)
            continue
        file_count += 1
        try:
            events = pd.read_csv(path)
        except Exception:
            prices["dividend_yield_ttm"] = np.nan
            pieces.append(prices)
            continue
        if events.empty or "ex_dividend_date" not in events or "dividend_plan" not in events:
            prices["dividend_yield_ttm"] = np.nan
            pieces.append(prices)
            continue
        events["event_date"] = pd.to_datetime(events["ex_dividend_date"], errors="coerce").dt.normalize()
        events["cash_per_share"] = events["dividend_plan"].map(parse_cash_dividend_per_share)
        events = events.dropna(subset=["event_date"])
        events = events[events["cash_per_share"] > 0].sort_values("event_date")
        if events.empty:
            prices["dividend_yield_ttm"] = np.nan
            pieces.append(prices)
            continue
        parsed_events += len(events)
        stocks_with_events += 1
        event_series = events.groupby("event_date")["cash_per_share"].sum()
        daily_cash = prices["trade_date"].map(event_series).fillna(0.0)
        indexed_cash = pd.Series(daily_cash.to_numpy(), index=pd.DatetimeIndex(prices["trade_date"]))
        trailing_cash = indexed_cash.rolling("365D", min_periods=1).sum().to_numpy()
        prices["dividend_yield_ttm"] = trailing_cash / prices["close"].replace(0, np.nan)
        pieces.append(prices)
    out = pd.concat(pieces, ignore_index=True).sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    coverage = float(out["dividend_yield_ttm"].notna().mean()) if len(out) else 0.0
    return out, {
        "enabled": True,
        "file_count": file_count,
        "parsed_event_count": parsed_events,
        "stocks_with_events": stocks_with_events,
        "coverage": round(coverage, 6),
        "method": "realized_cash_dividends_over_trailing_365_days",
    }


def parse_cash_dividend_per_share(plan: Any) -> float:
    if not isinstance(plan, str) or not plan.strip():
        return 0.0
    text = plan.replace(" ", "")
    for pattern in (
        r"每?10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
        r"10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
        r"10派([0-9]+(?:\.[0-9]+)?)元",
    ):
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)) / 10.0
    match = re.search(r"每股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元", text)
    return float(match.group(1)) if match else 0.0


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
