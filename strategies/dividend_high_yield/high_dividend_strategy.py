# -*- coding: utf-8 -*-
import argparse
import concurrent.futures
import datetime
import os
import pickle
import re
import time
from typing import Dict, List, Optional, Tuple

import adata
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# 禁用代理，避免本地环境代理影响数据抓取
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["all_proxy"] = ""
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["ALL_PROXY"] = ""


class HighDividendStrategy:
    """高股息策略：月度调仓 + 股息率排序 + 趋势/波动/流动性过滤"""

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        max_positions: int = 12,
        rebalance_period: int = 20,
        universe_size: int = 300,
        min_history_days: int = 120,
        min_dividend_yield: float = 0.02,
        min_trade_amount: float = 80_000_000,
        max_volatility: float = 0.45,
        trend_ma_window: int = 60,
        dividend_lookback_days: int = 365,
        transaction_cost: float = 0.0013,
        local_only: bool = False,
    ):
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.rebalance_period = rebalance_period
        self.universe_size = universe_size
        self.min_history_days = min_history_days
        self.min_dividend_yield = min_dividend_yield
        self.min_trade_amount = min_trade_amount
        self.max_volatility = max_volatility
        self.trend_ma_window = trend_ma_window
        self.dividend_lookback_days = dividend_lookback_days
        self.transaction_cost = transaction_cost
        self.local_only = local_only

        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(self.base_dir, "data", "cache")
        self.dividend_cache_dir = os.path.join(self.cache_dir, "dividend")
        os.makedirs(self.dividend_cache_dir, exist_ok=True)

        self.market_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.benchmark_cache_file = os.path.join(self.cache_dir, "benchmark_000300.csv")

        self.raw_stock_data: Dict[str, pd.DataFrame] = {}
        self.stock_data: Dict[str, pd.DataFrame] = {}
        self.dividend_events: Dict[str, pd.DataFrame] = {}
        self.universe_codes: List[str] = []
        self.market_index_df = pd.DataFrame()

        self.cash = initial_capital
        self.positions: Dict[str, Dict[str, float]] = {}
        self.trade_logs: List[Dict[str, object]] = []
        self.dividend_cash_logs: List[Dict[str, object]] = []
        self.equity_curve: List[Dict[str, object]] = []

    @staticmethod
    def _standardize_date_col(df: pd.DataFrame) -> pd.DataFrame:
        if "trade_date" not in df.columns:
            if "trade_time" not in df.columns:
                return pd.DataFrame()
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_time"]).dt.strftime("%Y-%m-%d")
        else:
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df

    @staticmethod
    def _parse_cash_dividend_per_share(plan: str) -> Optional[float]:
        """
        解析分红方案中的“每股现金分红”。
        常见格式：
        - 10股派5.20元
        - 每10股派2.00元(含税)
        - 10派3元
        - 每股派0.2元
        """
        if not isinstance(plan, str) or not plan:
            return None
        text = plan.replace(" ", "")

        patterns_per_10 = [
            r"每?10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
            r"10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
            r"10派([0-9]+(?:\.[0-9]+)?)元",
        ]
        for pattern in patterns_per_10:
            m = re.search(pattern, text)
            if m:
                return float(m.group(1)) / 10.0

        m_per_share = re.search(r"每股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元", text)
        if m_per_share:
            return float(m_per_share.group(1))
        return None

    def _prepare_single_stock_df(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()
        if "close" not in raw_df.columns or "amount" not in raw_df.columns:
            return pd.DataFrame()

        df = self._standardize_date_col(raw_df)
        if df.empty:
            return df
        df = df[["trade_date", "close", "amount"]].copy()
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df = df.sort_values("trade_date").drop_duplicates("trade_date")
        if len(df) < self.min_history_days:
            return pd.DataFrame()

        df["ret_1d"] = df["close"].pct_change()
        df["ma_trend"] = df["close"].rolling(window=self.trend_ma_window).mean()
        df["vol_60"] = df["ret_1d"].rolling(window=60).std() * np.sqrt(252)
        df["amount_ma20"] = df["amount"].rolling(window=20).mean()
        return df.set_index("trade_date")

    def load_market_data(self):
        if not os.path.exists(self.market_cache_file):
            raise FileNotFoundError(f"找不到市场缓存文件: {self.market_cache_file}")
        with open(self.market_cache_file, "rb") as f:
            data = pickle.load(f)
        if not isinstance(data, dict) or "stock" not in data:
            raise ValueError("市场缓存文件结构异常，未找到 stock 数据")
        self.raw_stock_data = data["stock"]
        print(f"已加载全市场缓存，共 {len(self.raw_stock_data)} 只股票")

    def load_benchmark(self, start_date: str):
        df = None
        if os.path.exists(self.benchmark_cache_file):
            df = pd.read_csv(self.benchmark_cache_file)
        if (df is None or df.empty) and (not self.local_only):
            print("本地无基准缓存，正在抓取沪深300...")
            df = adata.stock.market.get_market_index(index_code="000300", start_date=start_date)
            if df is not None and not df.empty:
                df.to_csv(self.benchmark_cache_file, index=False)
        if df is None or df.empty:
            raise RuntimeError("无法获取沪深300基准数据")

        df = self._standardize_date_col(df)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("trade_date")
        self.market_index_df = df.set_index("trade_date")
        print(f"基准数据范围: {df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}")

    def build_universe(self, start_date: str):
        candidates = []
        for code, raw_df in self.raw_stock_data.items():
            if code.startswith(("2", "4", "8", "9")):
                continue
            if raw_df is None or raw_df.empty:
                continue
            if "close" not in raw_df.columns or "amount" not in raw_df.columns:
                continue
            df = self._standardize_date_col(raw_df)
            if df.empty:
                continue
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
            df = df.sort_values("trade_date").drop_duplicates("trade_date")
            hist = df[df["trade_date"] <= start_date]
            # 股票池构建仅要求基础流动性窗口，避免因起始日过近导致样本过少
            if len(hist) < 20:
                continue
            avg_amount = hist["amount"].tail(60).mean()
            latest_close = hist["close"].iloc[-1]
            if pd.isna(avg_amount) or avg_amount <= 0 or pd.isna(latest_close) or latest_close <= 0:
                continue
            candidates.append((code, float(avg_amount)))

        candidates.sort(key=lambda x: x[1], reverse=True)
        self.universe_codes = [code for code, _ in candidates[: self.universe_size]]
        print(f"流动性筛选后股票池: {len(self.universe_codes)} 只")

        self.stock_data = {}
        for code in self.universe_codes:
            prepared = self._prepare_single_stock_df(self.raw_stock_data.get(code))
            if not prepared.empty:
                self.stock_data[code] = prepared
        self.universe_codes = sorted(self.stock_data.keys())
        print(f"指标处理完成，可回测股票池: {len(self.universe_codes)} 只")

    def _load_or_fetch_dividend_for_code(self, code: str) -> Tuple[str, pd.DataFrame]:
        cache_file = os.path.join(self.dividend_cache_dir, f"{code}.csv")
        dividend_df = None
        if os.path.exists(cache_file):
            try:
                dividend_df = pd.read_csv(cache_file)
            except Exception:
                dividend_df = None

        if (dividend_df is None or dividend_df.empty) and (not self.local_only):
            for _ in range(3):
                try:
                    dividend_df = adata.stock.market.get_dividend(stock_code=code)
                    break
                except Exception:
                    time.sleep(0.2)
            if dividend_df is not None and not dividend_df.empty:
                dividend_df.to_csv(cache_file, index=False, encoding="utf-8-sig")

        if dividend_df is None or dividend_df.empty:
            return code, pd.DataFrame(columns=["ex_dividend_date", "cash_per_share"])

        if "ex_dividend_date" not in dividend_df.columns:
            return code, pd.DataFrame(columns=["ex_dividend_date", "cash_per_share"])

        df = dividend_df.copy()
        df["ex_dividend_date"] = pd.to_datetime(df["ex_dividend_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "dividend_plan" in df.columns:
            df["cash_per_share"] = df["dividend_plan"].apply(self._parse_cash_dividend_per_share)
        else:
            df["cash_per_share"] = np.nan
        df = df.dropna(subset=["ex_dividend_date", "cash_per_share"])
        if df.empty:
            return code, pd.DataFrame(columns=["ex_dividend_date", "cash_per_share"])
        grouped = (
            df.groupby("ex_dividend_date", as_index=False)["cash_per_share"]
            .sum()
            .sort_values("ex_dividend_date")
        )
        return code, grouped

    def load_dividend_data(self):
        print(f"正在加载分红数据（股票池 {len(self.universe_codes)} 只）...")
        self.dividend_events = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(self._load_or_fetch_dividend_for_code, code): code
                for code in self.universe_codes
            }
            for future in concurrent.futures.as_completed(futures):
                code, events = future.result()
                self.dividend_events[code] = events
        valid = sum(1 for _, v in self.dividend_events.items() if not v.empty)
        print(f"分红数据加载完成，有效分红股票: {valid} / {len(self.universe_codes)}")

    def _calc_trailing_dividend_yield(self, code: str, today: str, price: float) -> float:
        if price <= 0:
            return 0.0
        events = self.dividend_events.get(code)
        if events is None or events.empty:
            return 0.0
        today_dt = pd.to_datetime(today)
        start_dt = today_dt - pd.Timedelta(days=self.dividend_lookback_days)
        eligible = events[
            (events["ex_dividend_date"] <= today)
            & (events["ex_dividend_date"] > start_dt.strftime("%Y-%m-%d"))
        ]
        if eligible.empty:
            return 0.0
        trailing_cash = float(eligible["cash_per_share"].sum())
        return trailing_cash / price

    def _handle_dividend_cash(self, today: str):
        for code, pos in self.positions.items():
            events = self.dividend_events.get(code)
            if events is None or events.empty:
                continue
            hit = events[events["ex_dividend_date"] == today]
            if hit.empty:
                continue
            cash_per_share = float(hit["cash_per_share"].sum())
            cash_income = pos["shares"] * cash_per_share
            self.cash += cash_income
            self.dividend_cash_logs.append(
                {
                    "date": today,
                    "code": code,
                    "shares": pos["shares"],
                    "cash_per_share": round(cash_per_share, 4),
                    "cash_income": round(cash_income, 2),
                }
            )

    def run_backtest(self, start_date: str, end_date: str):
        if self.market_index_df.empty:
            raise RuntimeError("请先加载基准数据")
        if not self.stock_data:
            raise RuntimeError("请先加载股票池数据")

        all_dates = sorted(self.market_index_df.index)
        trade_dates = [d for d in all_dates if start_date <= d <= end_date]
        if not trade_dates:
            raise RuntimeError("回测区间内无交易日")

        self.cash = self.initial_capital
        self.positions = {}
        self.trade_logs = []
        self.dividend_cash_logs = []
        self.equity_curve = []

        bench_base = None
        print(f"开始回测: {trade_dates[0]} ~ {trade_dates[-1]}")
        for idx, today in enumerate(trade_dates):
            self._handle_dividend_cash(today)

            if idx % self.rebalance_period == 0:
                candidates = []
                for code in self.universe_codes:
                    df = self.stock_data.get(code)
                    if df is None or today not in df.index:
                        continue
                    row = df.loc[today]
                    if pd.isna(row["close"]) or row["close"] <= 0:
                        continue
                    if pd.isna(row["ma_trend"]) or row["close"] <= row["ma_trend"]:
                        continue
                    if pd.isna(row["amount_ma20"]) or row["amount_ma20"] < self.min_trade_amount:
                        continue
                    if pd.isna(row["vol_60"]) or row["vol_60"] > self.max_volatility:
                        continue
                    dy = self._calc_trailing_dividend_yield(code, today, float(row["close"]))
                    if dy < self.min_dividend_yield:
                        continue
                    candidates.append(
                        {
                            "code": code,
                            "price": float(row["close"]),
                            "dividend_yield": dy,
                            "vol_60": float(row["vol_60"]),
                        }
                    )

                candidates.sort(key=lambda x: (x["dividend_yield"], -x["vol_60"]), reverse=True)
                target_codes = [x["code"] for x in candidates[: self.max_positions]]
                target_set = set(target_codes)

                # 先卖后买
                for code in list(self.positions.keys()):
                    if code in target_set:
                        continue
                    df = self.stock_data.get(code)
                    if df is None or today not in df.index:
                        continue
                    sell_price = float(df.loc[today, "close"])
                    pos = self.positions[code]
                    gross = pos["shares"] * sell_price
                    net = gross * (1 - self.transaction_cost)
                    self.cash += net
                    profit = net - pos["cost"]
                    self.trade_logs.append(
                        {
                            "code": code,
                            "side": "SELL",
                            "date": today,
                            "price": round(sell_price, 4),
                            "shares": int(pos["shares"]),
                            "amount": round(net, 2),
                            "profit": round(profit, 2),
                            "profit_pct": round(profit / pos["cost"], 6),
                            "reason": "调仓卖出",
                        }
                    )
                    del self.positions[code]

                for can in candidates:
                    code = can["code"]
                    if code in self.positions:
                        continue
                    if len(self.positions) >= self.max_positions:
                        break
                    slots_left = self.max_positions - len(self.positions)
                    budget = self.cash / max(slots_left, 1)
                    shares = int(budget / can["price"] / 100) * 100
                    if shares < 100:
                        continue
                    gross = shares * can["price"]
                    total_cost = gross * (1 + self.transaction_cost)
                    if total_cost > self.cash:
                        continue
                    self.cash -= total_cost
                    self.positions[code] = {
                        "buy_date": today,
                        "buy_price": can["price"],
                        "shares": shares,
                        "cost": total_cost,
                        "entry_dividend_yield": can["dividend_yield"],
                    }
                    self.trade_logs.append(
                        {
                            "code": code,
                            "side": "BUY",
                            "date": today,
                            "price": round(can["price"], 4),
                            "shares": int(shares),
                            "amount": round(total_cost, 2),
                            "profit": "",
                            "profit_pct": "",
                            "reason": f"买入(股息率={can['dividend_yield']:.2%})",
                        }
                    )

            # 统计权益
            market_value = 0.0
            for code, pos in self.positions.items():
                df = self.stock_data.get(code)
                if df is None or today not in df.index:
                    continue
                market_value += pos["shares"] * float(df.loc[today, "close"])

            total = self.cash + market_value
            bench = float(self.market_index_df.loc[today, "close"])
            if bench_base is None:
                bench_base = bench
            self.equity_curve.append(
                {
                    "date": today,
                    "total": total,
                    "benchmark": (bench / bench_base) * self.initial_capital,
                    "cash": self.cash,
                    "position_count": len(self.positions),
                }
            )

        if not self.equity_curve:
            raise RuntimeError("回测失败：未生成净值曲线")
        final_value = self.equity_curve[-1]["total"]
        print(f"回测结束，最终资产: {final_value:,.2f}")
        return final_value

    def save_results(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)

        trade_file = os.path.join(out_dir, "high_dividend_trade_log.csv")
        dividend_cash_file = os.path.join(out_dir, "high_dividend_dividend_cash_log.csv")
        equity_file = os.path.join(out_dir, "high_dividend_equity_curve.csv")
        metric_file = os.path.join(out_dir, "high_dividend_metrics.csv")
        plot_file = os.path.join(out_dir, "high_dividend_report.png")

        df_trade = pd.DataFrame(self.trade_logs)
        if not df_trade.empty:
            df_trade.to_csv(trade_file, index=False, encoding="utf-8-sig")

        df_div = pd.DataFrame(self.dividend_cash_logs)
        if not df_div.empty:
            df_div.to_csv(dividend_cash_file, index=False, encoding="utf-8-sig")

        df_eq = pd.DataFrame(self.equity_curve)
        df_eq.to_csv(equity_file, index=False, encoding="utf-8-sig")

        df_eq["date"] = pd.to_datetime(df_eq["date"])
        df_eq = df_eq.set_index("date")
        total_days = max((df_eq.index[-1] - df_eq.index[0]).days, 1)
        total_return = df_eq["total"].iloc[-1] / self.initial_capital - 1
        annual_return = (1 + total_return) ** (365 / total_days) - 1
        roll_max = df_eq["total"].cummax()
        drawdown = (df_eq["total"] - roll_max) / roll_max
        max_drawdown = drawdown.min()
        daily_ret = df_eq["total"].pct_change().dropna()
        if daily_ret.empty or daily_ret.std() == 0:
            sharpe = 0.0
        else:
            sharpe = (daily_ret.mean() * 252 - 0.02) / (daily_ret.std() * np.sqrt(252))

        sells = df_trade[df_trade["side"] == "SELL"].copy() if not df_trade.empty else pd.DataFrame()
        win_rate = 0.0
        if not sells.empty:
            sells["profit"] = pd.to_numeric(sells["profit"], errors="coerce")
            win_rate = (sells["profit"] > 0).mean()

        total_dividend_cash = float(df_div["cash_income"].sum()) if not df_div.empty else 0.0

        metrics = pd.DataFrame(
            {
                "metric": [
                    "total_return",
                    "annual_return",
                    "max_drawdown",
                    "sharpe",
                    "win_rate",
                    "sell_trades",
                    "dividend_cash",
                    "final_asset",
                ],
                "value": [
                    round(total_return, 6),
                    round(annual_return, 6),
                    round(max_drawdown, 6),
                    round(sharpe, 4),
                    round(float(win_rate), 6),
                    int(len(sells)),
                    round(total_dividend_cash, 2),
                    round(float(df_eq["total"].iloc[-1]), 2),
                ],
            }
        )
        metrics.to_csv(metric_file, index=False, encoding="utf-8-sig")

        self._plot_report(df_eq, plot_file)
        print(f"结果已保存至: {out_dir}")
        return {
            "trade_file": trade_file,
            "dividend_cash_file": dividend_cash_file,
            "equity_file": equity_file,
            "metric_file": metric_file,
            "plot_file": plot_file,
        }

    def _plot_report(self, df_eq: pd.DataFrame, save_path: str):
        plt.figure(figsize=(14, 8))
        plt.rcParams["font.sans-serif"] = [
            "Heiti TC",
            "STHeiti",
            "PingFang SC",
            "Arial Unicode MS",
            "SimHei",
            "Microsoft YaHei",
            "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False

        plt.plot(df_eq.index, df_eq["total"], label="高股息策略", lw=2.2, color="#E67E22")
        plt.plot(df_eq.index, df_eq["benchmark"], label="沪深300", lw=1.5, ls="--", color="#7F8C8D")
        plt.title("高股息策略回测报告")
        plt.legend()
        plt.grid(alpha=0.25)
        plt.tight_layout()
        plt.savefig(save_path, dpi=150)
        plt.close()


def main():
    parser = argparse.ArgumentParser(description="高股息策略回测")
    parser.add_argument("--start", type=str, default=None, help="开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="结束日期，格式 YYYY-MM-DD")
    parser.add_argument("--universe-size", type=int, default=300, help="股票池大小")
    parser.add_argument("--max-positions", type=int, default=12, help="最大持仓数")
    parser.add_argument("--min-dividend-yield", type=float, default=0.02, help="最低股息率")
    parser.add_argument("--rebalance-period", type=int, default=20, help="调仓周期(交易日)")
    parser.add_argument("--max-volatility", type=float, default=0.45, help="最大60日年化波动率")
    parser.add_argument("--local-only", action="store_true", help="只使用本地缓存，不联网抓取分红")
    parser.add_argument(
        "--out-dir",
        type=str,
        default=os.path.join("tests", "dividend_strategy_backtest_v1"),
        help="结果输出目录",
    )
    args = parser.parse_args()

    strategy = HighDividendStrategy(
        universe_size=args.universe_size,
        max_positions=args.max_positions,
        min_dividend_yield=args.min_dividend_yield,
        rebalance_period=args.rebalance_period,
        max_volatility=args.max_volatility,
        local_only=args.local_only,
    )
    strategy.load_market_data()

    # 计算默认回测区间
    latest_market_date = max(
        pd.to_datetime(df["trade_date"]).max()
        for df in strategy.raw_stock_data.values()
        if df is not None and not df.empty and "trade_date" in df.columns
    )
    if args.end:
        end_date = args.end
    else:
        end_date = latest_market_date.strftime("%Y-%m-%d")
    if args.start:
        start_date = args.start
    else:
        # 默认回测最近 240 个自然日，适配当前缓存覆盖并留出指标预热
        start_date = (pd.to_datetime(end_date) - pd.Timedelta(days=240)).strftime("%Y-%m-%d")

    strategy.load_benchmark(start_date=start_date)
    strategy.build_universe(start_date=start_date)
    strategy.load_dividend_data()
    strategy.run_backtest(start_date=start_date, end_date=end_date)
    strategy.save_results(args.out_dir)


if __name__ == "__main__":
    main()
