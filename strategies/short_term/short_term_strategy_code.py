# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import pickle
import shutil
from typing import Optional
from zoneinfo import ZoneInfo

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


matplotlib.use("Agg")
plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


class ShortTermDisagreementStrategy:
    """
    非 ST 全市场个股“妖股短线”日线代理策略

    说明:
    - 覆盖全市场普通个股, 自动排除 ST/*ST/PT/退市和非个股代码
    - 用日线近似“强势龙头分歧转一致”
    - 持仓周期以 1~4 个交易日为主
    """

    def __init__(
        self,
        initial_capital=1_000_000,
        max_positions=8,
        universe_size=None,
        max_position_weight=0.2,
        stop_loss_pct=0.045,
        take_profit_pct=0.12,
        trailing_stop_pct=0.06,
        max_hold_days=4,
        buy_fee_rate=0.0003,
        sell_fee_rate=0.0013,
        min_rise_pct=7.0,
        max_rise_pct=9.8,
        min_turnover_pct=5.0,
        max_turnover_pct=32.0,
        turnover_accel=1.4,
        amount_accel=1.5,
        min_amount=400_000_000,
        min_ret5_pct=6.0,
        min_ret10_pct=12.0,
        max_ret10_pct=75.0,
        limit_lower_ratio=0.7,
        limit_upper_ratio=0.995,
        min_listing_days=250,
    ):
        self.initial_capital = float(initial_capital)
        self.max_positions = int(max_positions)
        self.universe_size = int(universe_size) if universe_size else None
        self.max_position_weight = float(max_position_weight)
        self.stop_loss_pct = float(stop_loss_pct)
        self.take_profit_pct = float(take_profit_pct)
        self.trailing_stop_pct = float(trailing_stop_pct)
        self.max_hold_days = int(max_hold_days)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.min_rise_pct = float(min_rise_pct)
        self.max_rise_pct = float(max_rise_pct)
        self.min_turnover_pct = float(min_turnover_pct)
        self.max_turnover_pct = float(max_turnover_pct)
        self.turnover_accel = float(turnover_accel)
        self.amount_accel = float(amount_accel)
        self.min_amount = float(min_amount)
        self.min_ret5_pct = float(min_ret5_pct)
        self.min_ret10_pct = float(min_ret10_pct)
        self.max_ret10_pct = float(max_ret10_pct)
        self.limit_lower_ratio = float(limit_lower_ratio)
        self.limit_upper_ratio = float(limit_upper_ratio)
        self.min_listing_days = int(min_listing_days)

        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(self.base_dir, "data", "cache")
        self.shared_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.full_cache_file = self.shared_cache_file
        self.benchmark_file = os.path.join(self.cache_dir, "benchmark_000300.csv")
        self.metadata_file = os.path.join(self.base_dir, "tests", "utils", "all_code.csv")

        self.stock_data = {}
        self.stock_names = {}
        self.stock_meta = {}
        self.benchmark_df = None
        self.positions = {}
        self.completed_trades = []
        self.equity_curve = []
        self.cash = float(initial_capital)
        self.bench_base = None

    @staticmethod
    def _normalize_code(value) -> str:
        if pd.isna(value):
            return ""
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text.zfill(6) if text.isdigit() else text

    @staticmethod
    def _is_supported_equity_code(code: str) -> bool:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            return False
        # 排除 B 股和其他明显非普通 A 股代码
        return not code.startswith(("200", "900"))

    @staticmethod
    def _board_limit(code: str) -> float:
        if code.startswith(("300", "301", "688", "689")):
            return 0.20
        if code.startswith(("430", "8", "92")):
            return 0.30
        return 0.10

    @staticmethod
    def _now_shanghai() -> dt.datetime:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))

    @staticmethod
    def _is_excluded_short_name(short_name: str) -> bool:
        if not isinstance(short_name, str):
            return False
        name = short_name.strip().upper().replace(" ", "")
        if not name:
            return False
        excluded_prefixes = ("ST", "*ST", "SST", "S*ST", "PT", "*PT")
        if name.startswith(excluded_prefixes):
            return True
        excluded_keywords = ("退", "摘牌", "指数", "ETF", "LOF", "基金", "转债")
        return any(keyword in short_name for keyword in excluded_keywords)

    def _load_stock_metadata(self):
        if not os.path.exists(self.metadata_file):
            print(f"未找到股票简称映射文件: {self.metadata_file}")
            self.stock_meta = {}
            return

        meta = pd.read_csv(self.metadata_file, dtype={"stock_code": str})
        if "stock_code" not in meta.columns:
            print("股票简称映射文件缺少 stock_code 列, 跳过 ST 过滤")
            self.stock_meta = {}
            return

        meta["stock_code"] = meta["stock_code"].map(self._normalize_code)
        if "short_name" not in meta.columns:
            meta["short_name"] = ""
        meta["short_name"] = meta["short_name"].fillna("").astype(str).str.strip()
        meta["list_date"] = pd.to_datetime(meta["list_date"], errors="coerce") if "list_date" in meta.columns else pd.NaT
        meta = meta.drop_duplicates("stock_code").set_index("stock_code")
        self.stock_meta = meta[["short_name", "list_date"]].to_dict("index")
        print(f"加载股票元数据完成: {len(self.stock_meta)} 条")

    def _resolve_cache_file(self) -> str:
        return self.shared_cache_file

    @staticmethod
    def _read_cache_file(path: str) -> dict:
        with open(path, "rb") as f:
            cache = pickle.load(f)
        if not isinstance(cache, dict):
            raise ValueError(f"缓存文件格式错误: {path}")
        return cache

    def sync_active_cache_to_shared(self) -> bool:
        if self.full_cache_file == self.shared_cache_file:
            return False
        if not os.path.exists(self.full_cache_file):
            return False
        shutil.copyfile(self.full_cache_file, self.shared_cache_file)
        self.full_cache_file = self.shared_cache_file
        print(f"已将当前可用缓存同步到共享缓存: {self.shared_cache_file}")
        return True

    @staticmethod
    def _trade_dates_for_years(adata_module, years: list[int]) -> list[str]:
        trade_dates = set()
        for year in years:
            calendar_df = adata_module.stock.info.trade_calendar(year=year)
            if calendar_df is None or calendar_df.empty:
                continue
            df = calendar_df.copy()
            df["trade_date"] = df["trade_date"].astype(str)
            df["trade_status"] = pd.to_numeric(df["trade_status"], errors="coerce").fillna(0).astype(int)
            trade_dates.update(df.loc[df["trade_status"] == 1, "trade_date"].tolist())
        return sorted(trade_dates)

    def _clock_based_target_date(self, today_str: str) -> str:
        today = pd.to_datetime(today_str, errors="coerce")
        if pd.isna(today):
            return today_str
        if today.weekday() >= 5:
            return (today - pd.tseries.offsets.BDay(1)).strftime("%Y-%m-%d")
        now = self._now_shanghai()
        if now.time() < dt.time(15, 30):
            return (today - pd.tseries.offsets.BDay(1)).strftime("%Y-%m-%d")
        return today.strftime("%Y-%m-%d")

    def _fallback_target_date_from_benchmark(self, today_str: str) -> str:
        candidates = [self._clock_based_target_date(today_str)]
        if os.path.exists(self.benchmark_file):
            try:
                bench = pd.read_csv(self.benchmark_file)
                if not bench.empty and "trade_date" in bench.columns:
                    bench["trade_date"] = bench["trade_date"].astype(str)
                    valid_dates = sorted(d for d in bench["trade_date"].tolist() if d <= today_str)
                    if valid_dates:
                        candidates.append(valid_dates[-1])
            except Exception:
                pass
        return max(candidates) if candidates else today_str

    def _incremental_target_date(self, adata_module) -> str:
        now = self._now_shanghai()
        today_str = now.strftime("%Y-%m-%d")
        try:
            trade_dates = self._trade_dates_for_years(adata_module, [now.year - 1, now.year, now.year + 1])
        except Exception as exc:
            fallback_date = self._fallback_target_date_from_benchmark(today_str)
            print(f"获取交易日历失败，回退到本地基准推断更新目标日: {fallback_date} ({exc})")
            return fallback_date
        if not trade_dates:
            return self._fallback_target_date_from_benchmark(today_str)
        completed_dates = [d for d in trade_dates if d <= today_str]
        if not completed_dates:
            return self._fallback_target_date_from_benchmark(today_str)
        latest_trade_date = completed_dates[-1]
        # 日K在收盘并稳定后再把当天视为“可完成更新”的目标日期。
        if latest_trade_date == today_str and now.time() < dt.time(15, 30):
            return completed_dates[-2] if len(completed_dates) >= 2 else today_str
        return latest_trade_date

    def load_data(self, allow_online_update: Optional[bool] = None, max_update_codes: Optional[int] = None):
        import adata
        import concurrent.futures

        self._load_stock_metadata()
        os.makedirs(self.cache_dir, exist_ok=True)
        self.full_cache_file = self._resolve_cache_file()
        if allow_online_update is None:
            allow_online_update = not os.path.exists(self.full_cache_file)

        if os.path.exists(self.full_cache_file):
            print("加载全量股票缓存...")
            cache = self._read_cache_file(self.full_cache_file)
        else:
            if not allow_online_update:
                raise FileNotFoundError(f"未找到缓存 {self.full_cache_file}，且已禁用在线更新")
            print(f"未找到缓存 {self.full_cache_file}，将自动拉取数据...")
            cache = {"stock": {}, "update_meta": {}}

        raw_stock = cache.setdefault("stock", {})
        update_meta = cache.setdefault("update_meta", {})
        stock_last_checked = update_meta.setdefault("stock_last_checked", {})

        target_date = self._incremental_target_date(adata)
        default_start_date = (self._now_shanghai() - dt.timedelta(days=365 * 5 + 30)).strftime("%Y-%m-%d")
        excluded_name_count_before_fetch = 0
        valid_codes = []
        for code, meta in self.stock_meta.items():
            if not self._is_supported_equity_code(code):
                continue
            if self._is_excluded_short_name(meta.get("short_name", "")):
                excluded_name_count_before_fetch += 1
                continue
            valid_codes.append(code)
        if not valid_codes:
            try:
                df_codes = adata.stock.info.all_code()
                if df_codes is not None and not df_codes.empty:
                    df_codes = df_codes.copy()
                    df_codes["stock_code"] = df_codes["stock_code"].map(self._normalize_code)
                    df_codes = df_codes[df_codes["stock_code"].map(self._is_supported_equity_code)]
                    if "short_name" in df_codes.columns:
                        before = len(df_codes)
                        df_codes = df_codes[~df_codes["short_name"].fillna("").map(self._is_excluded_short_name)]
                        excluded_name_count_before_fetch = max(before - len(df_codes), 0)
                    valid_codes = df_codes["stock_code"].tolist()
            except Exception as e:
                print(f"获取股票列表失败: {e}")
        print(
            f"预抓取过滤完成: valid_codes={len(valid_codes)}, "
            f"excluded_st_or_delisted={excluded_name_count_before_fetch}"
        )

        pending_codes = [c for c in valid_codes if stock_last_checked.get(c) != target_date]
        if max_update_codes is not None and max_update_codes > 0:
            pending_codes = pending_codes[:max_update_codes]

        def fetch_incremental(code, df):
            try:
                if df is not None and not df.empty:
                    last_date = pd.to_datetime(df['trade_time'].max()).strftime('%Y-%m-%d')
                    if last_date < target_date:
                        new_data = adata.stock.market.get_market(stock_code=code, start_date=last_date)
                        if new_data is not None and not new_data.empty:
                            merged_df = pd.concat([df, new_data]).drop_duplicates('trade_time').sort_values('trade_time')
                            return code, merged_df, pd.to_datetime(merged_df['trade_time'].max()).strftime('%Y-%m-%d') >= target_date
                        return code, df, False
                    return code, df, False
                else:
                    new_data = adata.stock.market.get_market(stock_code=code, start_date=default_start_date)
                    if new_data is not None and not new_data.empty:
                        return code, new_data, pd.to_datetime(new_data['trade_time'].max()).strftime('%Y-%m-%d') >= target_date
            except Exception as exc:
                print(f"[数据更新失败] {code}: {exc}")
            return code, df, False

        updated_count = 0
        checked_count = 0

        if allow_online_update:
            print(f"检查并更新股票最新数据 (目标交易日: {target_date})...")
            if pending_codes:
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(fetch_incremental, c, raw_stock.get(c)): c for c in pending_codes}
                    for idx, f in enumerate(concurrent.futures.as_completed(futures), start=1):
                        code, new_df, checked = f.result()
                        if checked:
                            stock_last_checked[code] = target_date
                            checked_count += 1
                        if new_df is not raw_stock.get(code):
                            raw_stock[code] = new_df
                            updated_count += 1
                        if idx % 100 == 0 or idx == len(futures):
                            print(
                                f"[fetch-progress] completed={idx}/{len(futures)}, "
                                f"updated={updated_count}, checked={checked_count}"
                            )
        else:
            print("使用共享缓存，跳过在线日K增量更新。")

        if os.path.exists(self.benchmark_file):
            bench = pd.read_csv(self.benchmark_file)
        else:
            bench = pd.DataFrame()

        if allow_online_update:
            try:
                if not bench.empty:
                    bench_last_date = str(bench['trade_date'].max())
                    if bench_last_date < target_date:
                        new_bench = adata.stock.market.get_market_index(index_code='000300', start_date=bench_last_date)
                        if new_bench is not None and not new_bench.empty:
                            bench = pd.concat([bench, new_bench]).drop_duplicates('trade_date').sort_values('trade_date')
                            bench.to_csv(self.benchmark_file, index=False)
                else:
                    bench = adata.stock.market.get_market_index(index_code='000300', start_date=default_start_date)
                    if bench is not None and not bench.empty:
                        bench.to_csv(self.benchmark_file, index=False)
            except Exception as e:
                print(f"更新指数数据失败: {e}")
        elif bench.empty:
            raise FileNotFoundError(f"未找到指数缓存 {self.benchmark_file}，且已禁用在线更新")

        if updated_count > 0 or checked_count > 0:
            if updated_count > 0:
                print(f"更新了 {updated_count} 只股票的新增日K数据并回写缓存。")
            with open(self.full_cache_file, "wb") as f:
                pickle.dump(cache, f)

        print("预处理股票数据并构建流动性股票池...")
        processed = {}
        excluded_name_count = 0
        excluded_listing_count = 0
        excluded_non_equity_count = 0

        for raw_code, df in raw_stock.items():
            code = self._normalize_code(raw_code)
            if not self._is_supported_equity_code(code):
                continue

            meta = self.stock_meta.get(code, {})
            if not meta:
                excluded_non_equity_count += 1
                continue
            short_name = meta.get("short_name", "")
            if self._is_excluded_short_name(short_name):
                excluded_name_count += 1
                continue

            if df is None or df.empty or len(df) < 160:
                continue

            use_cols = ["open", "close", "high", "low", "amount", "pre_close", "turnover_ratio", "trade_time"]
            missing = [c for c in use_cols if c not in df.columns]
            if missing:
                continue

            dfx = df[use_cols].copy()
            for col in ["open", "close", "high", "low", "amount", "pre_close", "turnover_ratio"]:
                dfx[col] = pd.to_numeric(dfx[col], errors="coerce")

            dfx["trade_date"] = pd.to_datetime(dfx["trade_time"]).dt.strftime("%Y-%m-%d")
            dfx = dfx.sort_values("trade_date").drop_duplicates("trade_date")
            dfx = dfx.set_index("trade_date")

            last_trade_date = pd.to_datetime(dfx.index.max(), errors="coerce")
            list_date = meta.get("list_date")
            if pd.notna(list_date) and pd.notna(last_trade_date):
                if (last_trade_date - list_date).days < self.min_listing_days:
                    excluded_listing_count += 1
                    continue

            dfx["pct_change"] = (dfx["close"] / dfx["pre_close"] - 1.0) * 100.0
            dfx["ma5"] = dfx["close"].rolling(5).mean()
            dfx["ma10"] = dfx["close"].rolling(10).mean()
            dfx["ma20"] = dfx["close"].rolling(20).mean()
            dfx["ma60"] = dfx["close"].rolling(60).mean()
            dfx["amt_ma5"] = dfx["amount"].rolling(5).mean()
            dfx["turn_ma5"] = dfx["turnover_ratio"].rolling(5).mean()
            dfx["close_hh20"] = dfx["close"].rolling(20).max()
            dfx["close_hh30_prev"] = dfx["close"].rolling(30).max().shift(1)
            dfx["high_hh10_prev"] = dfx["high"].rolling(10).max().shift(1)
            dfx["liq_60"] = dfx["amount"].rolling(60).mean()
            dfx["prev_pct_change"] = dfx["pct_change"].shift(1)
            dfx["ret5"] = (dfx["close"] / dfx["close"].shift(5) - 1.0) * 100.0
            dfx["ret10"] = (dfx["close"] / dfx["close"].shift(10) - 1.0) * 100.0
            dfx["ret20"] = (dfx["close"] / dfx["close"].shift(20) - 1.0) * 100.0
            dfx["body_pct"] = ((dfx["close"] - dfx["open"]).abs() / dfx["pre_close"]) * 100.0
            dfx["upper_shadow_pct"] = ((dfx["high"] - dfx[["open", "close"]].max(axis=1)) / dfx["pre_close"]).clip(lower=0) * 100.0
            dfx["range_pct"] = ((dfx["high"] - dfx["low"]) / dfx["pre_close"]) * 100.0
            dfx["gap_open_pct"] = (dfx["open"] / dfx["pre_close"] - 1.0) * 100.0

            spread = (dfx["high"] - dfx["low"]).replace(0, np.nan)
            dfx["close_pos"] = ((dfx["close"] - dfx["low"]) / spread).clip(lower=0, upper=1).fillna(0)

            dfx = dfx.dropna(
                subset=[
                    "close",
                    "pre_close",
                    "ma10",
                    "ma20",
                    "ma60",
                    "amt_ma5",
                    "turn_ma5",
                    "turnover_ratio",
                    "prev_pct_change",
                    "ret5",
                    "ret10",
                    "ret20",
                    "close_hh30_prev",
                    "high_hh10_prev",
                ]
            )
            if dfx.empty:
                continue

            liq_score = float(dfx["liq_60"].iloc[-1]) if not pd.isna(dfx["liq_60"].iloc[-1]) else 0.0
            processed[code] = dfx
            self.stock_names[code] = short_name or code
            if self.universe_size and liq_score <= 0:
                continue

        if self.universe_size:
            liquidity_rank = sorted(
                ((code, float(df["liq_60"].iloc[-1])) for code, df in processed.items()),
                key=lambda item: item[1],
                reverse=True,
            )
            selected = {code for code, _ in liquidity_rank[: self.universe_size]}
            self.stock_data = {code: df for code, df in processed.items() if code in selected}
        else:
            self.stock_data = processed
        self.stock_names = {code: self.stock_names.get(code, code) for code in self.stock_data}
        print(
            f"股票池构建完成: {len(self.stock_data)} 只 | "
            f"剔除ST/退市/指数={excluded_name_count} | "
            f"剔除非个股={excluded_non_equity_count} | 剔除次新={excluded_listing_count}"
        )

        bench = pd.read_csv(self.benchmark_file)
        bench["trade_date"] = pd.to_datetime(bench["trade_date"]).dt.strftime("%Y-%m-%d")
        bench = bench.sort_values("trade_date").drop_duplicates("trade_date")
        bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
        bench["change_pct"] = pd.to_numeric(bench["change_pct"], errors="coerce")
        bench["ma20"] = bench["close"].rolling(20).mean()
        bench = bench.dropna(subset=["close", "ma20"])
        self.benchmark_df = bench.set_index("trade_date")
        print(f"基准数据加载完成: {self.benchmark_df.index.min()} ~ {self.benchmark_df.index.max()}")

    def _market_ok(self, date_str: str) -> bool:
        if date_str not in self.benchmark_df.index:
            return False
        row = self.benchmark_df.loc[date_str]
        return bool((row["close"] > row["ma20"]) and (row["change_pct"] > -0.8))

    def _signal_score(self, code: str, row: pd.Series) -> float:
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        breakout_pct = (row["close"] / row["close_hh30_prev"] - 1.0) * 100.0 if row["close_hh30_prev"] > 0 else 0.0
        limit = self._board_limit(code) * 100.0
        limit_proximity = max(limit - row["pct_change"], 0.0)
        return (
            row["pct_change"] * 1.1
            + turn_ratio * 2.2
            + amount_ratio * 1.6
            + breakout_pct * 2.8
            + row["ret10"] * 0.18
            + row["close_pos"] * 2.5
            - limit_proximity * 1.1
            - row["upper_shadow_pct"] * 1.3
        )

    def _is_entry_signal(self, code: str, row: pd.Series) -> bool:
        limit = self._board_limit(code) * 100.0
        lower_rise = max(self.min_rise_pct, limit * self.limit_lower_ratio)
        upper_rise = min(self.max_rise_pct if limit <= 10.0 else limit * self.limit_upper_ratio, limit * self.limit_upper_ratio)
        cond1 = row["close"] > row["ma5"] > row["ma10"] > row["ma20"] > row["ma60"]
        cond2 = lower_rise <= row["pct_change"] <= upper_rise
        cond3 = self.min_turnover_pct <= row["turnover_ratio"] <= self.max_turnover_pct
        cond4 = row["turn_ma5"] > 0 and row["turnover_ratio"] >= (row["turn_ma5"] * self.turnover_accel)
        cond5 = row["amount"] >= (row["amt_ma5"] * self.amount_accel) and row["amount"] >= self.min_amount
        cond6 = self.min_ret5_pct <= row["ret5"] and self.min_ret10_pct <= row["ret10"] <= self.max_ret10_pct
        cond7 = row["close_pos"] >= 0.78 and row["close"] >= (row["high"] * 0.992) and row["upper_shadow_pct"] <= 1.6
        cond8 = row["body_pct"] >= 4.0 and row["range_pct"] >= 5.0 and row["low"] <= (row["pre_close"] * 1.03)
        cond9 = row["close"] >= (row["close_hh30_prev"] * 1.01) and row["high"] >= row["high_hh10_prev"]
        cond10 = -3.0 <= row["prev_pct_change"] <= 7.0
        cond11 = row["gap_open_pct"] <= 6.0
        return bool(cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8 and cond9 and cond10 and cond11)

    def _current_total_equity(self, date_str: str) -> float:
        market_value = 0.0
        for code, pos in self.positions.items():
            df = self.stock_data.get(code)
            if df is None or df.empty:
                continue
            if date_str in df.index:
                pos["last_price"] = float(df.loc[date_str, "close"])
            market_value += pos["shares"] * pos["last_price"]
        return self.cash + market_value

    def _close_position(self, code: str, date_str: str, price: float, reason: str):
        pos = self.positions[code]
        gross = pos["shares"] * price
        sell_fee = gross * self.sell_fee_rate
        net = gross - sell_fee
        self.cash += net
        pnl = net - pos["cost"]
        pnl_pct = pnl / pos["cost"] if pos["cost"] > 0 else 0.0

        self.completed_trades.append(
            {
                "code": code,
                "short_name": pos["short_name"],
                "buy_date": pos["buy_date"],
                "sell_date": date_str,
                "buy_price": round(pos["buy_price"], 3),
                "sell_price": round(price, 3),
                "shares": int(pos["shares"]),
                "holding_days": int(pos["holding_days"]),
                "entry_score": round(pos["entry_score"], 3),
                "profit": round(pnl, 2),
                "profit_pct": round(pnl_pct * 100.0, 2),
                "reason": reason,
            }
        )
        del self.positions[code]

    def run_backtest(self, start_date: str, end_date: str):
        if self.benchmark_df is None or not self.stock_data:
            raise RuntimeError("请先调用 load_data()")

        self.cash = float(self.initial_capital)
        self.positions = {}
        self.completed_trades = []
        self.equity_curve = []
        self.bench_base = None

        all_dates = [d for d in self.benchmark_df.index if start_date <= d <= end_date]
        if not all_dates:
            raise ValueError(f"回测区间无有效交易日: {start_date} ~ {end_date}")

        print(f"开始回测: {all_dates[0]} ~ {all_dates[-1]} | 股票池={len(self.stock_data)}")

        for date_str in all_dates:
            to_exit = []
            for code, pos in list(self.positions.items()):
                df = self.stock_data.get(code)
                if df is None or date_str not in df.index:
                    continue

                row = df.loc[date_str]
                pos["holding_days"] += 1
                pos["last_price"] = float(row["close"])
                pos["max_close"] = max(pos["max_close"], pos["last_price"])

                reason = None
                trailing_stop_price = pos["max_close"] * (1.0 - self.trailing_stop_pct)
                if row["close"] <= pos["buy_price"] * (1.0 - self.stop_loss_pct):
                    reason = "stop_loss"
                elif row["close"] <= trailing_stop_price and pos["holding_days"] >= 2:
                    reason = "trail_stop"
                elif row["close"] >= pos["buy_price"] * (1.0 + self.take_profit_pct):
                    reason = "take_profit"
                elif pos["holding_days"] == 1 and (row["pct_change"] < -2.5 or row["close"] < row["ma5"]):
                    reason = "next_day_fail"
                elif pos["holding_days"] >= 2 and row["close"] < row["ma5"] and row["pct_change"] < 0:
                    reason = "ma5_break"
                elif pos["holding_days"] >= self.max_hold_days:
                    reason = "timeout"

                if reason:
                    to_exit.append((code, float(row["close"]), reason))

            for code, sell_price, reason in to_exit:
                self._close_position(code, date_str, sell_price, reason)

            if self._market_ok(date_str) and len(self.positions) < self.max_positions:
                candidates = []
                for code, df in self.stock_data.items():
                    if code in self.positions or date_str not in df.index:
                        continue
                    row = df.loc[date_str]
                    if self._is_entry_signal(code, row):
                        score = self._signal_score(code, row)
                        candidates.append((code, float(row["close"]), score))

                candidates.sort(key=lambda x: x[2], reverse=True)
                for code, buy_price, score in candidates:
                    if len(self.positions) >= self.max_positions:
                        break

                    slots_left = self.max_positions - len(self.positions)
                    total_equity = self._current_total_equity(date_str)
                    unit_budget = min(self.cash / max(slots_left, 1), total_equity * self.max_position_weight)
                    shares = int(unit_budget / buy_price / 100) * 100
                    if shares < 100:
                        continue

                    gross = shares * buy_price
                    buy_fee = gross * self.buy_fee_rate
                    total_cost = gross + buy_fee
                    if total_cost > self.cash:
                        continue

                    self.cash -= total_cost
                    self.positions[code] = {
                        "buy_date": date_str,
                        "buy_price": buy_price,
                        "shares": int(shares),
                        "cost": float(total_cost),
                        "holding_days": 0,
                        "max_close": buy_price,
                        "last_price": buy_price,
                        "entry_score": float(score),
                        "short_name": self.stock_names.get(code, code),
                    }

            total = self._current_total_equity(date_str)
            bench_close = float(self.benchmark_df.loc[date_str, "close"])
            if self.bench_base is None:
                self.bench_base = bench_close
            self.equity_curve.append(
                {
                    "date": date_str,
                    "total": total,
                    "benchmark": (bench_close / self.bench_base) * self.initial_capital,
                    "cash": self.cash,
                    "positions": len(self.positions),
                }
            )

        last_date = all_dates[-1]
        for code in list(self.positions.keys()):
            df = self.stock_data[code]
            last_price = float(df.loc[last_date, "close"]) if last_date in df.index else self.positions[code]["last_price"]
            self._close_position(code, last_date, last_price, "end_of_test")

        print(f"回测结束. 完成交易: {len(self.completed_trades)}")

    def _compute_metrics(self) -> dict:
        eq = pd.DataFrame(self.equity_curve)
        eq["date"] = pd.to_datetime(eq["date"])
        eq = eq.sort_values("date")
        eq["ret"] = eq["total"].pct_change().fillna(0.0)

        total_return = eq["total"].iloc[-1] / self.initial_capital - 1.0
        bench_return = eq["benchmark"].iloc[-1] / self.initial_capital - 1.0
        n_days = len(eq)
        annual_ret = (1.0 + total_return) ** (252.0 / max(n_days, 1)) - 1.0
        annual_vol = eq["ret"].std(ddof=0) * np.sqrt(252.0)
        sharpe = (eq["ret"].mean() / eq["ret"].std(ddof=0) * np.sqrt(252.0)) if eq["ret"].std(ddof=0) > 0 else 0.0

        nav = eq["total"] / self.initial_capital
        rolling_max = nav.cummax()
        drawdown = nav / rolling_max - 1.0
        max_drawdown = float(drawdown.min())

        trades = pd.DataFrame(self.completed_trades)
        if trades.empty:
            win_rate = 0.0
            avg_profit_pct = 0.0
            profit_factor = 0.0
            avg_hold_days = 0.0
        else:
            win_rate = float((trades["profit"] > 0).mean())
            avg_profit_pct = float(trades["profit_pct"].mean())
            gain = float(trades.loc[trades["profit"] > 0, "profit"].sum())
            loss = float(-trades.loc[trades["profit"] < 0, "profit"].sum())
            profit_factor = (gain / loss) if loss > 0 else 0.0
            avg_hold_days = float(trades["holding_days"].mean())

        return {
            "initial_capital": round(self.initial_capital, 2),
            "final_capital": round(float(eq["total"].iloc[-1]), 2),
            "strategy_total_return_pct": round(total_return * 100.0, 2),
            "benchmark_total_return_pct": round(bench_return * 100.0, 2),
            "excess_return_pct": round((total_return - bench_return) * 100.0, 2),
            "annualized_return_pct": round(annual_ret * 100.0, 2),
            "annualized_volatility_pct": round(annual_vol * 100.0, 2),
            "sharpe_ratio": round(float(sharpe), 3),
            "max_drawdown_pct": round(max_drawdown * 100.0, 2),
            "trade_count": int(len(trades)),
            "win_rate_pct": round(win_rate * 100.0, 2),
            "avg_trade_return_pct": round(avg_profit_pct, 2),
            "profit_factor": round(profit_factor, 3),
            "avg_holding_days": round(avg_hold_days, 2),
        }

    def save_outputs(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)

        eq_df = pd.DataFrame(self.equity_curve).sort_values("date")
        eq_path = os.path.join(out_dir, "short_term_equity_curve.csv")
        eq_df.to_csv(eq_path, index=False, encoding="utf-8-sig")

        trade_df = pd.DataFrame(self.completed_trades).sort_values(["buy_date", "code"])
        trade_path = os.path.join(out_dir, "short_term_trade_log.csv")
        trade_df.to_csv(trade_path, index=False, encoding="utf-8-sig")

        metrics = self._compute_metrics()
        metrics_path = os.path.join(out_dir, "short_term_metrics.json")
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)

        self._plot_report(eq_df, out_dir)

        print("\n关键指标:")
        for k, v in metrics.items():
            print(f"- {k}: {v}")
        print(f"\n输出文件:\n- {eq_path}\n- {trade_path}\n- {metrics_path}")

    def _plot_report(self, eq_df: pd.DataFrame, out_dir: str):
        if eq_df.empty:
            return

        dfx = eq_df.copy()
        dfx["date"] = pd.to_datetime(dfx["date"])
        dfx = dfx.sort_values("date")
        dfx["nav"] = dfx["total"] / self.initial_capital
        dfx["bench_nav"] = dfx["benchmark"] / self.initial_capital
        dfx["drawdown"] = dfx["nav"] / dfx["nav"].cummax() - 1.0

        fig, axes = plt.subplots(2, 1, figsize=(15, 10), gridspec_kw={"height_ratios": [3, 1]})
        axes[0].plot(dfx["date"], dfx["nav"], label="非ST妖股短线净值", color="#c0392b", linewidth=2.0)
        axes[0].plot(dfx["date"], dfx["bench_nav"], label="沪深300净值", color="#7f8c8d", linestyle="--")
        axes[0].set_title("非ST主板妖股短线策略回测")
        axes[0].legend(loc="upper left")
        axes[0].grid(alpha=0.3)

        axes[1].fill_between(dfx["date"], dfx["drawdown"], 0, color="#3498db", alpha=0.35)
        axes[1].set_title("策略回撤")
        axes[1].grid(alpha=0.3)

        plt.tight_layout()
        fig_path = os.path.join(out_dir, "short_term_strategy_report.png")
        plt.savefig(fig_path, dpi=120)
        plt.close(fig)


if __name__ == "__main__":
    strategy = ShortTermDisagreementStrategy(
        initial_capital=1_000_000,
        max_positions=6,
        universe_size=None,
        max_position_weight=0.18,
        stop_loss_pct=0.045,
        take_profit_pct=0.12,
        trailing_stop_pct=0.06,
        max_hold_days=4,
        min_rise_pct=7.0,
        min_turnover_pct=5.0,
        max_turnover_pct=30.0,
        amount_accel=1.5,
        limit_lower_ratio=0.7,
    )

    strategy.load_data()

    start = "2021-03-01"
    end = "2026-03-03"
    strategy.run_backtest(start, end)

    output_dir = os.path.dirname(os.path.abspath(__file__))
    strategy.save_outputs(output_dir)
