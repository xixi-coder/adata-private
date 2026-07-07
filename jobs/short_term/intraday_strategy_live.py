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
    "candidate_type": "候选类型",
    "signal_price": "信号价格",
    "change_pct": "当时涨幅(%)",
    "vwap": "分时均价",
    "opening_high": "开盘区间高点",
    "cum_amount": "累计成交额",
    "first30_amount_ratio": "前30分钟成交额比",
    "prev_pct_change": "前日涨幅(%)",
    "daily_score": "日线候选分",
    "candidate_note": "候选说明",
}

CANDIDATE_COLUMNS_ZH = {
    "code": "股票代码",
    "short_name": "股票简称",
    "candidate_type": "候选类型",
    "prev_date": "候选依据日期",
    "prev_close": "前日收盘价",
    "prev_amount": "前日成交额",
    "prev_pct_change": "前日涨幅(%)",
    "daily_score": "日线候选分",
    "candidate_note": "候选说明",
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


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value == "":
        return default
    return value in {"1", "true", "yes", "y", "on"}


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
        confirmation_minutes=3,
        max_vwap_extension_pct=0.035,
        max_minute_return_pct=0.025,
        max_open_to_signal_pct=0.045,
        max_opening_gap_pct=0.055,
        min_intraday_market_change_pct=-0.8,
        intraday_market_index_codes=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # 日线候选池大小（只对前 N 只股票抓分钟线，控制扫描成本）
        self.candidate_size = int(candidate_size)
        # 分时信号判定时间窗
        self.signal_start_time = signal_start_time
        self.signal_end_time = signal_end_time
        # 开盘区间长度，用于计算“开盘区间高点”
        self.opening_range_minutes = int(opening_range_minutes)
        # 信号阈值：突破幅度、当时涨幅区间、量能约束
        self.min_breakout_pct = float(min_breakout_pct)
        self.min_price_change_pct = float(min_price_change_pct)
        self.max_price_change_pct = float(max_price_change_pct)
        self.min_first30_amount_ratio = float(min_first30_amount_ratio)
        self.min_intraday_amount = float(min_intraday_amount)
        # 过滤单分钟脉冲：突破要连续站稳，且不能离分时均价过远或瞬时拉升过急。
        self.confirmation_minutes = max(1, int(confirmation_minutes))
        self.max_vwap_extension_pct = float(max_vwap_extension_pct)
        self.max_minute_return_pct = float(max_minute_return_pct)
        self.max_open_to_signal_pct = float(max_open_to_signal_pct)
        self.max_opening_gap_pct = float(max_opening_gap_pct)
        self.min_intraday_market_change_pct = float(min_intraday_market_change_pct)
        self.intraday_market_index_codes = list(intraday_market_index_codes or ["000300", "399006"])
        self.minute_cache_dir = os.path.join(self.cache_dir, "minute_live")
        self.last_minute_trade_dates: List[str] = []
        self.last_intraday_market_snapshot: Dict[str, float] = {}

    @staticmethod
    def _now_shanghai() -> dt.datetime:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))

    @staticmethod
    def _time_from_text(text: str, default: dt.time) -> dt.time:
        try:
            return dt.datetime.strptime(text, "%H:%M:%S").time()
        except Exception:
            return default

    @staticmethod
    def _shift_time_text(text: str, minutes: int) -> str:
        try:
            base = dt.datetime.strptime(text, "%H:%M:%S")
        except Exception:
            return text
        return (base + dt.timedelta(minutes=minutes)).strftime("%H:%M:%S")

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
        dfx = dfx.drop_duplicates(subset=["trade_time"], keep="last")

        numeric_cols = ["price", "change", "change_pct", "volume", "avg_price", "amount"]
        for col in numeric_cols:
            dfx[col] = pd.to_numeric(dfx[col], errors="coerce")

        dfx = dfx.dropna(subset=["price", "change_pct", "volume", "amount"])
        dfx = dfx[dfx["trade_time"].dt.strftime("%H:%M:%S") >= "09:30:00"]
        if dfx.empty:
            return pd.DataFrame()

        dfx["trade_date"] = dfx["trade_time"].dt.strftime("%Y-%m-%d")
        dfx["time_only"] = dfx["trade_time"].dt.strftime("%H:%M:%S")
        # 下面这些是分时信号会直接用到的衍生字段
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

    def _should_refresh_minute_cache(self, trade_date: str) -> bool:
        """
        下午盘任务需要看到更长的分时区间, 因此在交易日后半段自动刷新同日分钟缓存。
        """
        now = self._now_shanghai()
        return trade_date == now.strftime("%Y-%m-%d") and now.time() >= dt.time(13, 0)

    def _previous_trade_date(self, target_date: str) -> str:
        bench_dates = [d for d in self.benchmark_df.index if d < target_date]
        return bench_dates[-1] if bench_dates else ""

    def resolve_scan_trade_date(self) -> tuple[str, bool, str]:
        if not self.stock_data:
            self.load_data()

        # 实盘扫描仅针对“今天”，非交易日直接跳过并写入备注
        target_date = self._now_shanghai().strftime("%Y-%m-%d")
        is_trade_day = self.is_trade_day(target_date)
        if is_trade_day:
            return target_date, True, ""
        return target_date, False, f"{target_date} 不是交易日，已跳过分时扫描。"

    def _runtime_window_status(self, trade_date: str) -> dict:
        now = self._now_shanghai()
        start_time = self._time_from_text(self.signal_start_time, dt.time(9, 30))
        end_time = self._time_from_text(
            os.getenv("INTRADAY_RUNTIME_END_TIME", self._shift_time_text(self.signal_end_time, 30)),
            dt.time(15, 0),
        )
        if now.time() < start_time:
            return {
                "ok": False,
                "note": (
                    f"{trade_date} 当前时间 {now.strftime('%H:%M:%S')} 早于运行窗口 "
                    f"{start_time.strftime('%H:%M:%S')}，跳过分时扫描。"
                ),
            }
        if now.time() > end_time:
            return {
                "ok": False,
                "note": (
                    f"{trade_date} 当前时间 {now.strftime('%H:%M:%S')} 晚于运行窗口 "
                    f"{end_time.strftime('%H:%M:%S')}，跳过分时扫描。"
                ),
            }
        return {
            "ok": True,
            "note": f"{trade_date} 处于运行窗口 {start_time.strftime('%H:%M:%S')}~{end_time.strftime('%H:%M:%S')}",
        }

    def _candidate_profile(self, code: str, row: pd.Series) -> dict:
        limit = self._board_limit(code) * 100.0
        is_main_board = limit <= 10.0
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        hot_pct = min(limit * 0.82, 8.8 if is_main_board else 12.5)
        weak_pct_low = max(3.5 if is_main_board else 4.5, limit * 0.30)
        weak_pct_high = min(7.5 if is_main_board else 10.5, limit * 0.9)

        candidate_type = "强势观察"
        candidate_note = "当前结构偏强，等待分时确认。"
        type_bonus = 0.0

        if (
            row["pct_change"] >= hot_pct
            and row["close_pos"] >= 0.82
            and row["upper_shadow_pct"] <= 1.6
            and amount_ratio >= 1.4
            and turn_ratio >= 1.15
        ):
            candidate_type = "龙头加速"
            candidate_note = "前日已接近加速段，次日只接受温和高开后的承接。"
            type_bonus = 0.55
        elif (
            row["upper_shadow_pct"] >= 1.0
            and row["upper_shadow_pct"] <= 2.8
            and row["close_pos"] >= 0.75
            and row["close"] >= row["close_hh30_prev"] * 0.99
            and row["ret5"] >= 4.0
        ):
            candidate_type = "分歧修复"
            candidate_note = "前日有分歧但收回高位，更适合等回踩确认。"
            type_bonus = 0.35
        elif (
            weak_pct_low <= row["pct_change"] <= weak_pct_high
            and row["close_pos"] >= 0.82
            and row["upper_shadow_pct"] <= 1.2
            and row["ret5"] >= 3.0
        ):
            candidate_type = "弱转强"
            candidate_note = "前日强势但未过热，适合盘中突破确认。"
            type_bonus = 0.75
        elif row["close_pos"] >= 0.78 and row["upper_shadow_pct"] <= 2.0:
            candidate_note = "整体偏强，但需要盘中进一步确认。"

        return {
            "candidate_type": candidate_type,
            "candidate_note": candidate_note,
            "type_bonus": type_bonus,
        }

    def _daily_candidate_score(self, code: str, row: pd.Series) -> float:
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        breakout_pct = (row["close"] / row["close_hh30_prev"] - 1.0) * 100.0 if row["close_hh30_prev"] > 0 else 0.0
        limit = self._board_limit(code) * 100.0
        is_main_board = limit <= 10.0
        ret20_hot_line = 55.0 if is_main_board else 95.0
        profile = self._candidate_profile(code, row)
        return (
            row["pct_change"] * 1.1
            + row["ret5"] * 0.3
            + min(turn_ratio, 2.8) * 2.0
            + min(amount_ratio, 3.0) * 1.6
            + breakout_pct * 0.9
            + row["close_pos"] * 1.8
            - row["upper_shadow_pct"] * 0.6
            - max(amount_ratio - 3.5, 0.0) * 1.2
            - max(turn_ratio - 3.2, 0.0) * 1.4
            - max(row["ret20"] - ret20_hot_line, 0.0) * 0.08
            + float(profile["type_bonus"])
        )

    def _is_daily_candidate(self, code: str, row: pd.Series) -> bool:
        limit = self._board_limit(code) * 100.0
        is_main_board = limit <= 10.0
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        lower_rise = max(3.5 if is_main_board else 4.5, limit * 0.30)
        upper_rise = min(9.85 if is_main_board else limit * 0.92, limit * 0.985)
        max_turnover = 35.0 if is_main_board else 45.0
        max_prev_rise = 8.8 if is_main_board else 14.0
        max_gap_open = 5.5 if is_main_board else 8.5
        max_ret20 = 70.0 if is_main_board else 125.0
        cond1 = row["close"] > row["ma5"] > row["ma10"] > row["ma20"]
        cond2 = lower_rise <= row["pct_change"] <= upper_rise
        cond3 = 3.0 <= row["turnover_ratio"] <= max_turnover
        cond4 = 1.10 <= turn_ratio <= 3.8
        cond5 = row["amount"] >= max(row["amt_ma5"] * 1.15, 150_000_000) and amount_ratio <= 4.5
        cond6 = row["ret5"] >= 3.0 and row["ret10"] >= 6.0 and row["ret20"] <= max_ret20
        cond7 = row["close_pos"] >= 0.68 and row["upper_shadow_pct"] <= 2.8
        cond8 = row["close"] >= (row["close_hh30_prev"] * 0.985) or row["high"] >= (row["high_hh10_prev"] * 0.995)
        cond9 = -4.0 <= row["prev_pct_change"] <= max_prev_rise
        cond10 = row["gap_open_pct"] <= max_gap_open
        return bool(cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8 and cond9 and cond10)

    def build_daily_candidates(self, trade_date: str) -> pd.DataFrame:
        # 候选依据日 = 扫描日的前一交易日，避免用到未收盘/未稳定的日线
        prev_date = self._previous_trade_date(trade_date)
        if not prev_date:
            return pd.DataFrame()

        rows: List[Dict] = []
        for code, df in self.stock_data.items():
            if prev_date not in df.index:
                continue
            row = df.loc[prev_date]
            if not self._is_daily_candidate(code, row):
                continue
            profile = self._candidate_profile(code, row)
            rows.append(
                {
                    "code": code,
                    "short_name": self.stock_names.get(code, code),
                    "candidate_type": profile["candidate_type"],
                    "prev_date": prev_date,
                    "prev_close": float(row["close"]),
                    "prev_amount": float(row["amount"]),
                    "prev_pct_change": float(row["pct_change"]),
                    "daily_score": self._daily_candidate_score(code, row),
                    "candidate_note": profile["candidate_note"],
                }
            )

        if not rows:
            return pd.DataFrame()

        candidate_df = pd.DataFrame(rows).sort_values("daily_score", ascending=False).head(self.candidate_size)
        return candidate_df.reset_index(drop=True)

    def fetch_minute_data(self, code: str, trade_date: str = "", prefer_cache: bool = True) -> pd.DataFrame:
        # 先复用本地分钟缓存；缺失时再请求接口，降低重复调用
        cached_df = pd.DataFrame()
        if prefer_cache and trade_date:
            cached_df = self._load_cached_minute_data(trade_date, code)
            if not cached_df.empty and not self._should_refresh_minute_cache(trade_date):
                return cached_df

        fresh_df = self._normalize_minute_df(code, adata.stock.market.get_market_min(stock_code=code))
        if cached_df.empty:
            return fresh_df
        if fresh_df.empty:
            return cached_df

        merged_df = pd.concat([cached_df, fresh_df], ignore_index=True)
        return self._normalize_minute_df(code, merged_df)

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

    def _intraday_market_ok(self) -> bool:
        snapshots: Dict[str, float] = {}
        for index_code in self.intraday_market_index_codes:
            try:
                index_df = adata.stock.market.get_market_index_min(index_code=index_code)
            except Exception:
                continue
            if index_df is None or index_df.empty or "change_pct" not in index_df.columns:
                continue
            change_pct = pd.to_numeric(index_df["change_pct"], errors="coerce").dropna()
            if change_pct.empty:
                continue
            snapshots[index_code] = float(change_pct.iloc[-1])
        self.last_intraday_market_snapshot = snapshots
        if not snapshots:
            return True
        return all(change_pct >= self.min_intraday_market_change_pct for change_pct in snapshots.values())

    def _has_recent_confirmation(self, signal_window: pd.DataFrame, current_index: int) -> bool:
        if self.confirmation_minutes <= 1:
            return bool(signal_window["base_signal_ok"].iloc[current_index])
        if current_index + 1 < self.confirmation_minutes:
            return False
        recent = signal_window.iloc[current_index + 1 - self.confirmation_minutes : current_index + 1]
        return bool(recent["base_signal_ok"].all())

    def generate_signal_for_stock(self, candidate_row: pd.Series, minute_df: pd.DataFrame) -> Dict:
        if minute_df.empty:
            return {}

        candidate_type = str(candidate_row.get("candidate_type", "")).strip() or "强势观察"
        candidate_note = str(candidate_row.get("candidate_note", "")).strip()
        if candidate_type == "龙头加速":
            open_to_signal_cap = min(self.max_open_to_signal_pct, 0.03)
            vwap_extension_cap = min(self.max_vwap_extension_pct, 0.025)
            minute_return_cap = min(self.max_minute_return_pct, 0.02)
        elif candidate_type == "分歧修复":
            open_to_signal_cap = min(self.max_open_to_signal_pct, 0.04)
            vwap_extension_cap = min(self.max_vwap_extension_pct, 0.03)
            minute_return_cap = min(self.max_minute_return_pct, 0.022)
        else:
            open_to_signal_cap = self.max_open_to_signal_pct
            vwap_extension_cap = self.max_vwap_extension_pct
            minute_return_cap = self.max_minute_return_pct

        # 基准线：开盘区间高点（默认 09:30-09:45）
        opening_high = self._opening_range_high(minute_df)
        if opening_high <= 0:
            return {}

        # 先做一次“量能预检”，避免在弱量股票上逐分钟扫描
        amount_ratio_30 = self._first_30_amount_ratio(minute_df, float(candidate_row["prev_amount"]))
        if amount_ratio_30 < self.min_first30_amount_ratio:
            return {}

        # 只在策略设定窗口内找第一个满足条件的时刻
        signal_window = minute_df[
            (minute_df["time_only"] >= self.signal_start_time) & (minute_df["time_only"] <= self.signal_end_time)
        ].copy()
        if signal_window.empty:
            return {}

        open_price = float(minute_df["price"].iloc[0])
        opening_gap_pct = float(minute_df["change_pct"].iloc[0]) / 100.0
        signal_window["breakout_ok"] = signal_window["price"] >= opening_high * (1.0 + self.min_breakout_pct)
        signal_window["vwap_ok"] = signal_window["price"] >= signal_window["vwap"] * 1.002
        signal_window["price_change_ok"] = signal_window["change_pct"].between(
            self.min_price_change_pct,
            self.max_price_change_pct,
        )
        signal_window["amount_ok"] = signal_window["cum_amount"] >= self.min_intraday_amount
        signal_window["reclaim_ok"] = signal_window["price"] >= signal_window["rolling_high_5"] * 0.999
        signal_window["vwap_extension_ok"] = (
            signal_window["price"] <= signal_window["vwap"] * (1.0 + vwap_extension_cap)
        )
        signal_window["minute_return_ok"] = signal_window["minute_return"] <= minute_return_cap
        signal_window["open_to_signal_ok"] = (
            signal_window["price"] <= open_price * (1.0 + open_to_signal_cap)
        )
        signal_window["opening_gap_ok"] = opening_gap_pct <= self.max_opening_gap_pct
        signal_window["base_signal_ok"] = (
            signal_window["breakout_ok"]
            & signal_window["vwap_ok"]
            & signal_window["price_change_ok"]
            & signal_window["amount_ok"]
            & signal_window["reclaim_ok"]
            & signal_window["vwap_extension_ok"]
            & signal_window["minute_return_ok"]
            & signal_window["open_to_signal_ok"]
            & signal_window["opening_gap_ok"]
        )

        for current_index, (_, row) in enumerate(signal_window.iterrows()):
            if self._has_recent_confirmation(signal_window, current_index):
                return {
                    "trade_date": row["trade_date"],
                    "signal_time": row["trade_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    "code": candidate_row["code"],
                    "short_name": candidate_row["short_name"],
                    "candidate_type": candidate_type,
                    "signal_price": round(float(row["price"]), 3),
                    "change_pct": round(float(row["change_pct"]), 2),
                    "vwap": round(float(row["vwap"]), 3),
                    "opening_high": round(float(opening_high), 3),
                    "cum_amount": round(float(row["cum_amount"]), 2),
                    "first30_amount_ratio": round(amount_ratio_30, 4),
                    "prev_pct_change": round(float(candidate_row["prev_pct_change"]), 2),
                    "daily_score": round(float(candidate_row["daily_score"]), 3),
                    "candidate_note": candidate_note,
                }
        return {}

    def run_live_scan(self, enforce_runtime_window: bool = True) -> tuple[pd.DataFrame, str, bool, str]:
        if not self.stock_data:
            self.load_data()

        resolved_trade_date, is_trade_day, note = self.resolve_scan_trade_date()
        if not is_trade_day:
            print(note)
            self.last_minute_trade_dates = []
            return pd.DataFrame(), resolved_trade_date, False, note

        if enforce_runtime_window:
            runtime_status = self._runtime_window_status(resolved_trade_date)
            if not runtime_status["ok"]:
                print(runtime_status["note"])
                self.last_minute_trade_dates = []
                return pd.DataFrame(), resolved_trade_date, is_trade_day, runtime_status["note"]
            note = runtime_status["note"]
        else:
            note = "手动执行，已跳过运行窗口判断。"

        candidates = self.build_daily_candidates(resolved_trade_date)
        if candidates.empty:
            self.last_minute_trade_dates = []
            return pd.DataFrame(), resolved_trade_date, is_trade_day, note

        if not self._intraday_market_ok():
            snapshot = ", ".join(
                f"{code}:{change_pct:.2f}%" for code, change_pct in self.last_intraday_market_snapshot.items()
            )
            note = f"盘中指数环境偏弱，跳过开仓扫描（{snapshot}）。"
            print(note)
            self.last_minute_trade_dates = []
            return pd.DataFrame(), resolved_trade_date, is_trade_day, note

        # 对每只候选股执行：分钟数据获取 -> 信号判定
        minute_map: Dict[str, pd.DataFrame] = {}
        signals: List[Dict] = []
        for _, row in candidates.iterrows():
            minute_df = self.fetch_minute_data(row["code"], trade_date=resolved_trade_date, prefer_cache=True)
            minute_map[row["code"]] = minute_df
            signal = self.generate_signal_for_stock(row, minute_df)
            if signal:
                signals.append(signal)

        # 无论是否出信号，都落分钟缓存，便于后续排查与回测复盘
        self.last_minute_trade_dates = self._extract_trade_dates(minute_map)
        self.cache_minute_data(resolved_trade_date, minute_map)
        if not signals:
            return pd.DataFrame(), resolved_trade_date, is_trade_day, note
        return self._sort_by_daily_score(pd.DataFrame(signals)), resolved_trade_date, is_trade_day, note


if __name__ == "__main__":
    signal_start_time = os.getenv("INTRADAY_SIGNAL_START_TIME", "09:45:00")
    signal_end_time = os.getenv("INTRADAY_SIGNAL_END_TIME", "10:30:00")
    confirmation_minutes = int(os.getenv("INTRADAY_CONFIRMATION_MINUTES", "3"))
    max_vwap_extension_pct = float(os.getenv("INTRADAY_MAX_VWAP_EXTENSION_PCT", "0.035"))
    max_minute_return_pct = float(os.getenv("INTRADAY_MAX_MINUTE_RETURN_PCT", "0.025"))
    max_open_to_signal_pct = float(os.getenv("INTRADAY_MAX_OPEN_TO_SIGNAL_PCT", "0.045"))
    max_opening_gap_pct = float(os.getenv("INTRADAY_MAX_OPENING_GAP_PCT", "0.055"))
    min_intraday_market_change_pct = float(os.getenv("INTRADAY_MIN_MARKET_CHANGE_PCT", "-0.8"))
    strategy = IntradaySignalStrategy(
        candidate_size=60,
        signal_start_time=signal_start_time,
        signal_end_time=signal_end_time,
        confirmation_minutes=confirmation_minutes,
        max_vwap_extension_pct=max_vwap_extension_pct,
        max_minute_return_pct=max_minute_return_pct,
        max_open_to_signal_pct=max_open_to_signal_pct,
        max_opening_gap_pct=max_opening_gap_pct,
        min_intraday_market_change_pct=min_intraday_market_change_pct,
        max_positions=6,
        universe_size=None,
        max_position_weight=0.2,
        stop_loss_pct=0.045,
        take_profit_pct=0.12,
        trailing_stop_pct=0.06,
        max_hold_days=4,
    )
    # 阶段1：同步云端缓存并准备日线数据底座
    sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    strategy.load_data(allow_online_update=True)
    strategy.sync_active_cache_to_shared()
    now = strategy._now_shanghai()
    resolved_trade_date, is_trade_day, note = strategy.resolve_scan_trade_date()
    runtime_status = strategy._runtime_window_status(resolved_trade_date) if is_trade_day else {"ok": False, "note": note}
    if not is_trade_day or not runtime_status["ok"]:
        candidate_df = pd.DataFrame()
        signal_df = pd.DataFrame()
        note = runtime_status["note"]
    else:
        candidate_df = strategy._sort_by_daily_score(strategy.build_daily_candidates(resolved_trade_date))
        signal_df, _, _, run_note = strategy.run_live_scan()
        signal_df = strategy._sort_by_daily_score(signal_df)
        note = run_note or note

    # 阶段2：生成并落盘本次候选、信号和汇总
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
    did_build_candidates = bool(is_trade_day)
    if did_build_candidates:
        candidate_reference_date = strategy._previous_trade_date(resolved_trade_date)
    elif not candidate_df.empty and "prev_date" in candidate_df.columns:
        candidate_reference_date = str(candidate_df["prev_date"].iloc[0])
    summary = {
        "run_time": now.strftime("%Y-%m-%d %H:%M:%S"),
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
    # 阶段3：回传缓存（含分钟缓存）到云端，供后续任务复用
    sync_cache_to_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    print(strategy.to_chinese_signals(signal_df).to_string(index=False))

    if not signal_df.empty:
        print("\n--- 信号跟踪 (实时) ---")
        try:
            tracking_rows = []
            for _, row in signal_df.iterrows():
                code = str(row['code']).zfill(6)
                m_df = adata.stock.market.get_market_min(stock_code=code)
                if not m_df.empty:
                    last_price = float(m_df.iloc[-1]['price'])
                    # 推算昨收 = 信号价 / (1 + 当时涨幅%)
                    prev_close = float(row['signal_price']) / (1 + float(row['change_pct']) / 100.0)
                    curr_pct = (last_price / prev_close - 1.0) * 100.0
                    tracking_rows.append({
                        "股票代码": code,
                        "股票简称": row['short_name'],
                        "信号时间": row['signal_time'].split()[-1],
                        "信号价格": row['signal_price'],
                        "信号涨幅(%)": row['change_pct'],
                        "当前价格": last_price,
                        "最新涨幅(%)": round(curr_pct, 2),
                        "信号后涨跌(%)": round(curr_pct - float(row['change_pct']), 2)
                    })
            if tracking_rows:
                print(pd.DataFrame(tracking_rows).to_string(index=False))
        except Exception as e:
            print(f"实时跟踪获取失败: {e}")

    print(
        f"\n输出文件:\n- {candidate_path}\n- {signal_path}\n- {summary_json_path}\n"
        f"- {latest_candidate_path}\n- {latest_signal_path}\n- {latest_summary_json_path}"
    )
