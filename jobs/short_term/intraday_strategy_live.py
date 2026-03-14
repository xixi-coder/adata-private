# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import sys
from typing import Dict, List
from zoneinfo import ZoneInfo

import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

import adata
from jobs.common.cloud_cache_sync import SHARED_MARKET_CACHE_ARCHIVE, sync_cache_from_drive, sync_cache_to_drive
from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy

SIGNAL_COLUMNS_ZH = {
    "trade_date": "交易日期",
    "signal_time": "信号时间",
    "code": "股票代码",
    "short_name": "股票简称",
    "signal_price": "信号价格",
    "change_pct": "当时涨幅(%)",
    "vwap": "分时均价",
    "opening_high": "开盘区间高点",
    "cum_amount": "累计成交额",
    "first30_amount_ratio": "前30分钟成交额比",
    "prev_pct_change": "前日涨幅(%)",
    "daily_score": "日线候选分",
}

CANDIDATE_COLUMNS_ZH = {
    "code": "股票代码",
    "short_name": "股票简称",
    "prev_date": "候选依据日期",
    "prev_close": "前日收盘价",
    "prev_amount": "前日成交额",
    "prev_pct_change": "前日涨幅(%)",
    "daily_score": "日线候选分",
}

MINUTE_COLUMNS_ZH = {
    "stock_code": "股票代码",
    "trade_time": "交易时间",
    "price": "价格",
    "change": "涨跌额",
    "change_pct": "涨跌幅(%)",
    "volume": "成交量",
    "avg_price": "接口均价",
    "amount": "成交额",
    "trade_date": "交易日期",
    "time_only": "时刻",
    "cum_amount": "累计成交额",
    "cum_volume": "累计成交量",
    "vwap": "分时均价",
    "minute_return": "分钟收益率",
    "rolling_high_5": "近5分钟最高价",
    "rolling_low_5": "近5分钟最低价",
}


class IntradaySignalStrategy(ShortTermDisagreementStrategy):
    """
    分时版短线信号扫描器

    设计思路:
    - 用前一交易日的日线数据做候选池预筛
    - 用当天 1 分钟分时确认“弱转强 / 开盘区间突破”
    - 输出实时信号列表并缓存分钟数据, 为后续分时回测积累样本
    """

    def __init__(
        self,
        candidate_size=80,
        signal_start_time="09:45:00",
        signal_end_time="10:30:00",
        opening_range_minutes=15,
        min_breakout_pct=0.003,
        min_price_change_pct=2.0,
        max_price_change_pct=9.5,
        min_first30_amount_ratio=0.08,
        min_intraday_amount=80_000_000,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.candidate_size = int(candidate_size)
        self.signal_start_time = signal_start_time
        self.signal_end_time = signal_end_time
        self.opening_range_minutes = int(opening_range_minutes)
        self.min_breakout_pct = float(min_breakout_pct)
        self.min_price_change_pct = float(min_price_change_pct)
        self.max_price_change_pct = float(max_price_change_pct)
        self.min_first30_amount_ratio = float(min_first30_amount_ratio)
        self.min_intraday_amount = float(min_intraday_amount)
        self.minute_cache_dir = os.path.join(self.cache_dir, "minute_live")
        self.last_minute_trade_dates: List[str] = []

    @staticmethod
    def _now_shanghai() -> dt.datetime:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))

    @staticmethod
    def _extract_trade_dates(minute_map: Dict[str, pd.DataFrame]) -> List[str]:
        trade_dates = set()
        for df in minute_map.values():
            if df is None or df.empty or "trade_date" not in df.columns:
                continue
            trade_dates.update(df["trade_date"].dropna().astype(str).tolist())
        return sorted(trade_dates)

    def is_trade_day(self, trade_date: str) -> bool:
        year = int(trade_date[:4])
        calendar_df = adata.stock.info.trade_calendar(year=year)
        if calendar_df.empty:
            return False
        row = calendar_df[calendar_df["trade_date"] == trade_date]
        if row.empty:
            return False
        return bool(int(row.iloc[0]["trade_status"]) == 1)

    def _normalize_minute_df(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()

        dfx = df.copy()
        dfx["stock_code"] = code
        dfx["trade_time"] = pd.to_datetime(dfx["trade_time"], errors="coerce")
        dfx = dfx.dropna(subset=["trade_time"]).sort_values("trade_time")

        numeric_cols = ["price", "change", "change_pct", "volume", "avg_price", "amount"]
        for col in numeric_cols:
            dfx[col] = pd.to_numeric(dfx[col], errors="coerce")

        dfx = dfx.dropna(subset=["price", "change_pct", "volume", "amount"])
        dfx = dfx[dfx["trade_time"].dt.strftime("%H:%M:%S") >= "09:30:00"]
        if dfx.empty:
            return pd.DataFrame()

        dfx["trade_date"] = dfx["trade_time"].dt.strftime("%Y-%m-%d")
        dfx["time_only"] = dfx["trade_time"].dt.strftime("%H:%M:%S")
        dfx["cum_amount"] = dfx["amount"].cumsum()
        dfx["cum_volume"] = dfx["volume"].cumsum()
        dfx["vwap"] = (dfx["cum_amount"] / dfx["cum_volume"]).where(dfx["cum_volume"] > 0, dfx["price"])
        dfx["minute_return"] = dfx["price"].pct_change().fillna(0.0)
        dfx["rolling_high_5"] = dfx["price"].rolling(5, min_periods=1).max()
        dfx["rolling_low_5"] = dfx["price"].rolling(5, min_periods=1).min()
        return dfx.reset_index(drop=True)

    def _minute_cache_path(self, trade_date: str, code: str) -> str:
        return os.path.join(self.minute_cache_dir, trade_date, f"{code}.csv")

    def _load_cached_minute_data(self, trade_date: str, code: str) -> pd.DataFrame:
        path = self._minute_cache_path(trade_date, code)
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(path)
        except Exception:
            return pd.DataFrame()
        inverse_map = {value: key for key, value in MINUTE_COLUMNS_ZH.items()}
        df = df.rename(columns={col: inverse_map.get(col, col) for col in df.columns})
        if "stock_code" not in df.columns:
            df["stock_code"] = code
        return self._normalize_minute_df(code, df)

    def _previous_trade_date(self, target_date: str) -> str:
        bench_dates = [d for d in self.benchmark_df.index if d < target_date]
        return bench_dates[-1] if bench_dates else ""

    def _latest_trade_date_on_or_before(self, target_date: str) -> str:
        bench_dates = [d for d in self.benchmark_df.index if d <= target_date]
        return bench_dates[-1] if bench_dates else ""

    def resolve_scan_trade_date(self, requested_date: str = "", allow_fallback: bool = False) -> tuple[str, bool, str]:
        if not self.stock_data:
            self.load_data()

        target_date = requested_date or self._now_shanghai().strftime("%Y-%m-%d")
        is_trade_day = self.is_trade_day(target_date)
        if is_trade_day:
            return target_date, True, ""
        if not allow_fallback:
            return target_date, False, f"{target_date} 不是交易日，已跳过分时扫描。"

        fallback_date = self._latest_trade_date_on_or_before(target_date)
        if not fallback_date:
            return target_date, False, f"{target_date} 不是交易日，且未找到更早的交易日。"
        if fallback_date == target_date:
            return fallback_date, True, ""
        return (
            fallback_date,
            False,
            f"{target_date} 不是交易日，已回退到最近交易日 {fallback_date} 进行扫描。",
        )

    def _daily_candidate_score(self, row: pd.Series) -> float:
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        return (
            row["pct_change"] * 1.1
            + row["ret5"] * 0.3
            + turn_ratio * 2.0
            + amount_ratio * 1.6
            + row["close_pos"] * 1.8
            - row["upper_shadow_pct"] * 0.6
        )

    def _is_daily_candidate(self, row: pd.Series) -> bool:
        cond1 = row["close"] > row["ma5"] > row["ma10"] > row["ma20"]
        cond2 = 3.0 <= row["pct_change"] <= 19.5
        cond3 = 2.5 <= row["turnover_ratio"] <= 45.0
        cond4 = row["turn_ma5"] > 0 and row["turnover_ratio"] >= (row["turn_ma5"] * 1.05)
        cond5 = row["amount"] >= max(row["amt_ma5"] * 1.1, 120_000_000)
        cond6 = row["ret5"] >= 2.0 and row["ret10"] >= 4.0
        cond7 = row["close_pos"] >= 0.6 and row["upper_shadow_pct"] <= 3.5
        return bool(cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7)

    def build_daily_candidates(self, trade_date: str) -> pd.DataFrame:
        prev_date = self._previous_trade_date(trade_date)
        if not prev_date:
            return pd.DataFrame()

        rows: List[Dict] = []
        for code, df in self.stock_data.items():
            if prev_date not in df.index:
                continue
            row = df.loc[prev_date]
            if not self._is_daily_candidate(row):
                continue
            rows.append(
                {
                    "code": code,
                    "short_name": self.stock_names.get(code, code),
                    "prev_date": prev_date,
                    "prev_close": float(row["close"]),
                    "prev_amount": float(row["amount"]),
                    "prev_pct_change": float(row["pct_change"]),
                    "daily_score": self._daily_candidate_score(row),
                }
            )

        if not rows:
            return pd.DataFrame()

        candidate_df = pd.DataFrame(rows).sort_values("daily_score", ascending=False).head(self.candidate_size)
        return candidate_df.reset_index(drop=True)

    def fetch_minute_data(self, code: str, trade_date: str = "", prefer_cache: bool = True) -> pd.DataFrame:
        if prefer_cache and trade_date:
            cached_df = self._load_cached_minute_data(trade_date, code)
            if not cached_df.empty:
                return cached_df
        df = adata.stock.market.get_market_min(stock_code=code)
        return self._normalize_minute_df(code, df)

    def cache_minute_data(self, trade_date: str, minute_map: Dict[str, pd.DataFrame]):
        out_dir = os.path.join(self.minute_cache_dir, trade_date)
        os.makedirs(out_dir, exist_ok=True)
        for code, df in minute_map.items():
            if df.empty:
                continue
            df.to_csv(os.path.join(out_dir, f"{code}.csv"), index=False, encoding="utf-8-sig")

    @staticmethod
    def _rename_columns(df: pd.DataFrame, rename_map: Dict[str, str]) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=[rename_map[key] for key in rename_map.keys()])
        keep_cols = [col for col in rename_map.keys() if col in df.columns]
        out = df[keep_cols].copy()
        for code_col in ["code", "stock_code"]:
            if code_col in out.columns:
                out[code_col] = out[code_col].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        return out.rename(columns={col: rename_map[col] for col in keep_cols})

    def to_chinese_candidates(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_columns(df, CANDIDATE_COLUMNS_ZH)

    def to_chinese_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_columns(df, SIGNAL_COLUMNS_ZH)

    def to_chinese_minute(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_columns(df, MINUTE_COLUMNS_ZH)

    @staticmethod
    def _sort_by_daily_score(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or "daily_score" not in df.columns:
            return df
        sort_cols = ["daily_score"]
        ascending = [False]
        if "signal_time" in df.columns:
            sort_cols.append("signal_time")
            ascending.append(True)
        return df.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)

    def _opening_range_high(self, minute_df: pd.DataFrame) -> float:
        window_end = (
            dt.datetime.combine(dt.date.today(), dt.datetime.strptime("09:30:00", "%H:%M:%S").time())
            + dt.timedelta(minutes=self.opening_range_minutes)
        ).time()
        or_df = minute_df[minute_df["time_only"] <= window_end.strftime("%H:%M:%S")]
        return float(or_df["price"].max()) if not or_df.empty else 0.0

    def _first_30_amount_ratio(self, minute_df: pd.DataFrame, prev_amount: float) -> float:
        first30 = minute_df[minute_df["time_only"] <= "10:00:00"]
        amount = float(first30["amount"].sum()) if not first30.empty else 0.0
        return amount / prev_amount if prev_amount > 0 else 0.0

    def generate_signal_for_stock(self, candidate_row: pd.Series, minute_df: pd.DataFrame) -> Dict:
        if minute_df.empty:
            return {}

        opening_high = self._opening_range_high(minute_df)
        if opening_high <= 0:
            return {}

        amount_ratio_30 = self._first_30_amount_ratio(minute_df, float(candidate_row["prev_amount"]))
        if amount_ratio_30 < self.min_first30_amount_ratio:
            return {}

        signal_window = minute_df[
            (minute_df["time_only"] >= self.signal_start_time) & (minute_df["time_only"] <= self.signal_end_time)
        ].copy()
        if signal_window.empty:
            return {}

        for _, row in signal_window.iterrows():
            breakout_ok = row["price"] >= opening_high * (1.0 + self.min_breakout_pct)
            vwap_ok = row["price"] >= row["vwap"] * 1.002
            price_change_ok = self.min_price_change_pct <= row["change_pct"] <= self.max_price_change_pct
            amount_ok = row["cum_amount"] >= self.min_intraday_amount
            reclaim_ok = row["price"] >= row["rolling_high_5"] * 0.999
            if breakout_ok and vwap_ok and price_change_ok and amount_ok and reclaim_ok:
                return {
                    "trade_date": row["trade_date"],
                    "signal_time": row["trade_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    "code": candidate_row["code"],
                    "short_name": candidate_row["short_name"],
                    "signal_price": round(float(row["price"]), 3),
                    "change_pct": round(float(row["change_pct"]), 2),
                    "vwap": round(float(row["vwap"]), 3),
                    "opening_high": round(float(opening_high), 3),
                    "cum_amount": round(float(row["cum_amount"]), 2),
                    "first30_amount_ratio": round(amount_ratio_30, 4),
                    "prev_pct_change": round(float(candidate_row["prev_pct_change"]), 2),
                    "daily_score": round(float(candidate_row["daily_score"]), 3),
                }
        return {}

    def run_live_scan(self, trade_date: str = "", allow_fallback: bool = False) -> tuple[pd.DataFrame, str, bool, str]:
        if not self.stock_data:
            self.load_data()

        resolved_trade_date, is_trade_day, note = self.resolve_scan_trade_date(
            requested_date=trade_date,
            allow_fallback=allow_fallback,
        )
        if not is_trade_day and not allow_fallback:
            print(note)
            self.last_minute_trade_dates = []
            return pd.DataFrame(), resolved_trade_date, False, note

        candidates = self.build_daily_candidates(resolved_trade_date)
        if candidates.empty:
            self.last_minute_trade_dates = []
            return pd.DataFrame(), resolved_trade_date, is_trade_day, note

        minute_map: Dict[str, pd.DataFrame] = {}
        signals: List[Dict] = []
        for _, row in candidates.iterrows():
            minute_df = self.fetch_minute_data(row["code"], trade_date=resolved_trade_date, prefer_cache=True)
            minute_map[row["code"]] = minute_df
            signal = self.generate_signal_for_stock(row, minute_df)
            if signal:
                signals.append(signal)

        self.last_minute_trade_dates = self._extract_trade_dates(minute_map)
        self.cache_minute_data(resolved_trade_date, minute_map)
        if not signals:
            return pd.DataFrame(), resolved_trade_date, is_trade_day, note
        return self._sort_by_daily_score(pd.DataFrame(signals)), resolved_trade_date, is_trade_day, note


if __name__ == "__main__":
    strategy = IntradaySignalStrategy(
        candidate_size=60,
        max_positions=6,
        universe_size=None,
        max_position_weight=0.2,
        stop_loss_pct=0.045,
        take_profit_pct=0.12,
        trailing_stop_pct=0.06,
        max_hold_days=4,
    )
    sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    strategy.load_data(allow_online_update=False)
    now = strategy._now_shanghai()
    requested_trade_date = os.getenv("TRADE_DATE", "").strip() or now.strftime("%Y-%m-%d")
    event_name = os.getenv("GITHUB_EVENT_NAME", "").strip().lower()
    allow_fallback = event_name != "schedule"
    resolved_trade_date, is_trade_day, note = strategy.resolve_scan_trade_date(
        requested_date=requested_trade_date,
        allow_fallback=allow_fallback,
    )
    if not allow_fallback and not is_trade_day:
        candidate_df = pd.DataFrame()
        signal_df = pd.DataFrame()
    else:
        candidate_df = strategy._sort_by_daily_score(strategy.build_daily_candidates(resolved_trade_date))
        signal_df, _, _, run_note = strategy.run_live_scan(
            trade_date=requested_trade_date,
            allow_fallback=allow_fallback,
        )
        signal_df = strategy._sort_by_daily_score(signal_df)
        note = run_note or note

    output_dir = os.path.join(CURRENT_DIR, "outputs")
    os.makedirs(output_dir, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    candidate_path = os.path.join(output_dir, f"候选池_{ts}.csv")
    signal_path = os.path.join(output_dir, f"分时信号_{ts}.csv")
    latest_candidate_path = os.path.join(output_dir, "latest_candidates.csv")
    latest_signal_path = os.path.join(output_dir, "latest_signals.csv")
    summary_json_path = os.path.join(output_dir, f"summary_{ts}.json")
    latest_summary_json_path = os.path.join(output_dir, "latest_summary.json")
    summary_txt_path = os.path.join(output_dir, f"summary_{ts}.txt")
    latest_summary_txt_path = os.path.join(output_dir, "latest_summary.txt")
    candidate_out = strategy.to_chinese_candidates(candidate_df)
    signal_out = strategy.to_chinese_signals(signal_df)
    candidate_out.to_csv(candidate_path, index=False, encoding="utf-8-sig")
    signal_out.to_csv(signal_path, index=False, encoding="utf-8-sig")
    candidate_out.to_csv(latest_candidate_path, index=False, encoding="utf-8-sig")
    signal_out.to_csv(latest_signal_path, index=False, encoding="utf-8-sig")
    candidate_reference_date = ""
    if not candidate_df.empty and "prev_date" in candidate_df.columns:
        candidate_reference_date = str(candidate_df["prev_date"].iloc[0])
    summary = {
        "run_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "requested_trade_date": requested_trade_date,
        "trade_date": resolved_trade_date,
        "is_trade_day": is_trade_day,
        "candidate_reference_date": candidate_reference_date,
        "candidate_count": int(len(candidate_df)),
        "signal_count": int(len(signal_df)),
        "minute_trade_dates": strategy.last_minute_trade_dates,
        "note": note,
    }
    for path in [summary_json_path, latest_summary_json_path]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    summary_lines = [
        f"运行时间: {summary['run_time']}",
        f"请求日期: {summary['requested_trade_date']}",
        f"扫描日期: {summary['trade_date']}",
        f"是否交易日: {'是' if summary['is_trade_day'] else '否'}",
        f"候选依据日: {summary['candidate_reference_date']}",
        f"候选数量: {summary['candidate_count']}",
        f"信号数量: {summary['signal_count']}",
        f"分时数据日期: {','.join(summary['minute_trade_dates'])}",
        f"备注: {summary['note']}",
    ]
    for path in [summary_txt_path, latest_summary_txt_path]:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")
    sync_cache_to_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    print(strategy.to_chinese_signals(signal_df).to_string(index=False))
    print(
        f"\n输出文件:\n- {candidate_path}\n- {signal_path}\n- {summary_json_path}\n"
        f"- {latest_candidate_path}\n- {latest_signal_path}\n- {latest_summary_json_path}"
    )
