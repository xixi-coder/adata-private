from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ETF_NAMES = {
    "513100": "国泰纳指ETF",
    "159915": "易方达创业板ETF",
}


@dataclass(frozen=True)
class RotationConfig:
    short_window: int = 20
    medium_window: int = 60
    long_window: int = 120
    momentum_weights: tuple[float, float, float] = (0.20, 0.30, 0.50)
    switch_buffer: float = 0.03
    single_asset_weight: float = 0.70
    leader_weight: float = 0.70
    follower_weight: float = 0.30
    medium_volatility: float = 0.30
    high_volatility: float = 0.40
    medium_vol_scale: float = 0.80
    high_vol_scale: float = 0.60
    volatility_window: int | None = None
    require_medium_return_positive: bool = True
    rebalance_threshold: float = 0.02
    trading_cost_rate: float = 0.001
    initial_capital: float = 100_000.0
    profile_name: str = "custom"

    def __post_init__(self) -> None:
        if not math.isclose(sum(self.momentum_weights), 1.0):
            raise ValueError("momentum_weights must sum to 1")
        if self.short_window >= self.medium_window or self.medium_window >= self.long_window:
            raise ValueError("momentum windows must be strictly increasing")
        if self.leader_weight + self.follower_weight > 1.0:
            raise ValueError("leader and follower weights cannot exceed 100%")
        if self.trading_cost_rate < 0:
            raise ValueError("trading_cost_rate cannot be negative")
        if self.volatility_window is not None and self.volatility_window < 2:
            raise ValueError("volatility_window must be at least 2")
        if not 0 <= self.rebalance_threshold < 1:
            raise ValueError("rebalance_threshold must be between 0 and 1")


@dataclass
class BacktestResult:
    nav: pd.DataFrame
    signals: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, Any]

    def write(
        self,
        output_dir: str | Path,
        comparisons: dict[str, "BacktestResult"] | None = None,
    ) -> None:
        from jobs.etf_rotation.report import write_html_report

        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.nav.to_csv(path / "nav.csv", index=False, encoding="utf-8-sig")
        self.signals.to_csv(path / "signals.csv", index=False, encoding="utf-8-sig")
        self.trades.to_csv(path / "trades.csv", index=False, encoding="utf-8-sig")
        with (path / "summary.json").open("w", encoding="utf-8") as file:
            json.dump(self.summary, file, ensure_ascii=False, indent=2)
        for name, comparison in (comparisons or {}).items():
            comparison.nav.to_csv(path / f"{name}_nav.csv", index=False, encoding="utf-8-sig")
            comparison.signals.to_csv(path / f"{name}_signals.csv", index=False, encoding="utf-8-sig")
            comparison.trades.to_csv(path / f"{name}_trades.csv", index=False, encoding="utf-8-sig")
            with (path / f"{name}_summary.json").open("w", encoding="utf-8") as file:
                json.dump(comparison.summary, file, ensure_ascii=False, indent=2)
        write_html_report(self, path / "report.html", comparisons=comparisons)


def download_ths_etf(code: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
    """Download front-adjusted exchange ETF bars from the public THS line endpoint."""
    url = f"http://d.10jqka.com.cn/v6/line/hs_{code}/01/last36000.js"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    payload = json.loads(text[text.index("{") : text.rfind("}") + 1])
    if not payload.get("data"):
        raise ValueError(f"No market data returned for ETF {code}")
    rows = [item.split(",")[:7] for item in payload["data"].split(";")]
    frame = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "volume", "amount"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "open", "close"])
    if start_date:
        frame = frame[frame["trade_date"] >= pd.Timestamp(start_date)]
    if end_date:
        frame = frame[frame["trade_date"] <= pd.Timestamp(end_date)]
    return frame.reset_index(drop=True)


def load_etf_csv(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    date_column = "trade_date" if "trade_date" in frame.columns else "date"
    required = {date_column, "open", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
    frame = frame.rename(columns={date_column: "trade_date"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["trade_date", "open", "close"]).sort_values("trade_date").reset_index(drop=True)


def _prepare_data(frames: dict[str, pd.DataFrame], config: RotationConfig) -> pd.DataFrame:
    prepared = []
    for code, raw in frames.items():
        columns = ["trade_date", "open", "close"]
        columns.extend(column for column in ("high", "low") if column in raw.columns)
        frame = raw[columns].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.drop_duplicates("trade_date", keep="last").sort_values("trade_date").set_index("trade_date")
        for column in ("open", "high", "low", "close"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "high" not in frame:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        if "low" not in frame:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        frame["high"] = frame[["open", "high", "close"]].max(axis=1)
        frame["low"] = frame[["open", "low", "close"]].min(axis=1)
        close = frame["close"]
        frame["ret_short"] = close.pct_change(config.short_window)
        frame["ret_medium"] = close.pct_change(config.medium_window)
        frame["ret_long"] = close.pct_change(config.long_window)
        frame["trend_ma"] = close.rolling(config.long_window).mean()
        volatility_window = config.volatility_window or config.short_window
        frame["vol_short"] = close.pct_change().rolling(volatility_window).std(ddof=1) * math.sqrt(252)
        w20, w60, w120 = config.momentum_weights
        frame["score"] = (
            w20 * frame["ret_short"] + w60 * frame["ret_medium"] + w120 * frame["ret_long"]
        )
        frame = frame.add_prefix(f"{code}_")
        prepared.append(frame)
    if not 1 <= len(prepared) <= 2:
        raise ValueError("This strategy requires one or two ETFs")
    return pd.concat(prepared, axis=1, join="inner").dropna(
        subset=[f"{code}_open" for code in frames] + [f"{code}_close" for code in frames]
    )


def _target_weights(
    row: pd.Series,
    codes: list[str],
    config: RotationConfig,
    current_leader: str | None,
) -> tuple[dict[str, float], str | None, str]:
    eligible = {}
    for code in codes:
        above_trend = bool(row[f"{code}_close"] > row[f"{code}_trend_ma"])
        positive_return = (
            bool(row[f"{code}_ret_medium"] > 0) if config.require_medium_return_positive else True
        )
        eligible[code] = above_trend and positive_return
    scores = {code: float(row[f"{code}_score"]) for code in codes}
    passing = [code for code in codes if eligible[code]]
    weights = {code: 0.0 for code in codes}

    if not passing:
        reason = "该ETF未通过趋势过滤" if len(codes) == 1 else "两只均未通过趋势过滤"
        return weights, None, reason

    if len(passing) == 1:
        leader = passing[0]
        weights[leader] = config.single_asset_weight
        reason = f"仅{ETF_NAMES.get(leader, leader)}通过趋势过滤"
    else:
        leader = current_leader if current_leader in passing else None
        if leader is None:
            gap = abs(scores[codes[0]] - scores[codes[1]])
            if gap < config.switch_buffer:
                weights = {code: 0.50 for code in codes}
                leader = None
                reason = "两只均通过且动量接近，等权配置"
            else:
                leader = max(passing, key=scores.get)
                follower = next(code for code in passing if code != leader)
                weights[leader] = config.leader_weight
                weights[follower] = config.follower_weight
                reason = f"{ETF_NAMES.get(leader, leader)}动量领先"
        else:
            challenger = next(code for code in passing if code != leader)
            if scores[challenger] >= scores[leader] + config.switch_buffer:
                leader = challenger
            follower = next(code for code in passing if code != leader)
            weights[leader] = config.leader_weight
            weights[follower] = config.follower_weight
            reason = f"{ETF_NAMES.get(leader, leader)}保持或取得领先"

    allocated = [code for code, weight in weights.items() if weight > 0]
    max_volatility = max(float(row[f"{code}_vol_short"]) for code in allocated)
    if max_volatility > config.high_volatility:
        scale = config.high_vol_scale
    elif max_volatility > config.medium_volatility:
        scale = config.medium_vol_scale
    else:
        scale = 1.0
    if scale < 1.0:
        weights = {code: weight * scale for code, weight in weights.items()}
        reason += f"；波动率降仓至{scale:.0%}"
    return weights, leader, reason


def _rebalance_at_open(
    date: pd.Timestamp,
    open_prices: dict[str, float],
    shares: dict[str, float],
    cash: float,
    target_weights: dict[str, float],
    cost_rate: float,
    rebalance_threshold: float,
) -> tuple[dict[str, float], float, list[dict[str, Any]]]:
    current_values = {code: shares[code] * open_prices[code] for code in shares}
    equity_before = cash + sum(current_values.values())
    current_weights = {
        code: current_values[code] / equity_before if equity_before else 0.0 for code in shares
    }
    max_weight_change = max(
        abs(target_weights.get(code, 0.0) - current_weights[code]) for code in shares
    )
    if max_weight_change < rebalance_threshold:
        return shares.copy(), cash, []

    equity_after = equity_before
    for _ in range(30):
        target_values = {code: target_weights.get(code, 0.0) * equity_after for code in shares}
        traded_notional = sum(abs(target_values[code] - current_values[code]) for code in shares)
        updated_equity = equity_before - traded_notional * cost_rate
        if abs(updated_equity - equity_after) < 1e-8:
            equity_after = updated_equity
            break
        equity_after = updated_equity

    target_values = {code: target_weights.get(code, 0.0) * equity_after for code in shares}
    trade_values = {code: target_values[code] - current_values[code] for code in shares}
    total_cost = sum(abs(value) for value in trade_values.values()) * cost_rate
    new_shares = {code: target_values[code] / open_prices[code] for code in shares}
    new_cash = equity_before - sum(target_values.values()) - total_cost
    trades = []
    for code, trade_value in trade_values.items():
        if abs(trade_value) < 1e-8:
            continue
        trades.append(
            {
                "trade_date": date,
                "fund_code": code,
                "fund_name": ETF_NAMES.get(code, code),
                "side": "BUY" if trade_value > 0 else "SELL",
                "price": open_prices[code],
                "quantity": abs(new_shares[code] - shares[code]),
                "notional": abs(trade_value),
                "cost": abs(trade_value) * cost_rate,
            }
        )
    return new_shares, new_cash, trades


def _performance_summary(nav: pd.DataFrame, trades: pd.DataFrame, config: RotationConfig) -> dict[str, Any]:
    values = nav.set_index("trade_date")["nav"]
    daily_returns = values.pct_change().dropna()
    elapsed_days = max((values.index[-1] - values.index[0]).days, 1)
    total_return = float(values.iloc[-1] / values.iloc[0] - 1)
    annual_return = float((values.iloc[-1] / values.iloc[0]) ** (365.25 / elapsed_days) - 1)
    annual_volatility = float(daily_returns.std(ddof=1) * math.sqrt(252)) if len(daily_returns) > 1 else 0.0
    sharpe = (
        float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252))
        if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0
        else 0.0
    )
    drawdown = values / values.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
    return {
        "start_date": values.index[0].strftime("%Y-%m-%d"),
        "end_date": values.index[-1].strftime("%Y-%m-%d"),
        "initial_capital": config.initial_capital,
        "ending_equity": round(float(values.iloc[-1]), 2),
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "annual_volatility": round(annual_volatility, 6),
        "sharpe_zero_rate": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "max_drawdown": round(max_drawdown, 6),
        "trade_count": int(len(trades)),
        "rebalance_count": int(trades["trade_date"].nunique()) if not trades.empty else 0,
        "transaction_cost": round(float(trades["cost"].sum()), 2) if not trades.empty else 0.0,
        "assumptions": {
            "signal": "每周最后一个共同交易日收盘后",
            "execution": "下一共同交易日开盘",
            "cash_return": "0%",
            "trading_cost_rate": config.trading_cost_rate,
            "premium_filter": "未回测；实盘下单前另行检查",
        },
        "parameters": {
            "profile": config.profile_name,
            "momentum_windows": [config.short_window, config.medium_window, config.long_window],
            "momentum_weights": list(config.momentum_weights),
            "volatility_window": config.volatility_window or config.short_window,
            "switch_buffer": config.switch_buffer,
            "rebalance_threshold": config.rebalance_threshold,
        },
    }


def run_backtest(
    frames: dict[str, pd.DataFrame],
    config: RotationConfig | None = None,
    start_date: str = "",
    end_date: str = "",
    signal_dates: set[pd.Timestamp] | None = None,
) -> BacktestResult:
    config = config or RotationConfig()
    codes = list(frames)
    market = _prepare_data(frames, config)
    if start_date:
        evaluation = market[market.index >= pd.Timestamp(start_date)]
    else:
        evaluation = market
    if end_date:
        evaluation = evaluation[evaluation.index <= pd.Timestamp(end_date)]
    if evaluation.empty:
        raise ValueError("No overlapping ETF data in the requested period")

    if signal_dates is None:
        weekly_signal_dates = set(
            evaluation.groupby(evaluation.index.to_period("W-FRI"), sort=True)
            .apply(lambda item: item.index[-1])
            .tolist()
        )
    else:
        weekly_signal_dates = {pd.Timestamp(date).normalize() for date in signal_dates}
    shares = {code: 0.0 for code in codes}
    cash = config.initial_capital
    leader: str | None = None
    pending_target: dict[str, float] | None = None
    pending_signal_date: pd.Timestamp | None = None
    nav_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for date, row in evaluation.iterrows():
        open_prices = {code: float(row[f"{code}_open"]) for code in codes}
        if pending_target is not None:
            shares, cash, executed = _rebalance_at_open(
                date,
                open_prices,
                shares,
                cash,
                pending_target,
                config.trading_cost_rate,
                config.rebalance_threshold,
            )
            for trade in executed:
                trade["signal_date"] = pending_signal_date
            trade_rows.extend(executed)
            pending_target = None
            pending_signal_date = None

        close_values = {code: shares[code] * float(row[f"{code}_close"]) for code in codes}
        equity = cash + sum(close_values.values())
        nav_row = {"trade_date": date, "nav": equity}
        for code in codes:
            nav_row[f"weight_{code}"] = close_values[code] / equity if equity else 0.0
            for field in ("open", "high", "low", "close"):
                nav_row[f"{field}_{code}"] = float(row[f"{code}_{field}"])
        nav_row["cash_weight"] = cash / equity if equity else 0.0
        nav_rows.append(nav_row)

        if date in weekly_signal_dates and all(pd.notna(row[f"{code}_score"]) for code in codes):
            target, leader, reason = _target_weights(row, codes, config, leader)
            pending_target = target
            pending_signal_date = date
            signal_row: dict[str, Any] = {
                "signal_date": date,
                "execute_date": None,
                "reason": reason,
                "leader": leader or "",
            }
            for code in codes:
                signal_row[f"close_{code}"] = float(row[f"{code}_close"])
                signal_row[f"score_{code}"] = float(row[f"{code}_score"])
                signal_row[f"ret_short_{code}"] = float(row[f"{code}_ret_short"])
                signal_row[f"ret_medium_{code}"] = float(row[f"{code}_ret_medium"])
                signal_row[f"ret_long_{code}"] = float(row[f"{code}_ret_long"])
                signal_row[f"trend_ma_{code}"] = float(row[f"{code}_trend_ma"])
                signal_row[f"vol_short_{code}"] = float(row[f"{code}_vol_short"])
                signal_row[f"target_weight_{code}"] = target[code]
            signal_rows.append(signal_row)

    nav = pd.DataFrame(nav_rows)
    for code in codes:
        benchmark = evaluation[f"{code}_close"].astype(float)
        nav[f"benchmark_{code}"] = (benchmark / benchmark.iloc[0] * config.initial_capital).to_numpy()
    signals = pd.DataFrame(signal_rows)
    trades = pd.DataFrame(trade_rows)
    if not signals.empty and not trades.empty:
        execution_dates = trades.groupby("signal_date")["trade_date"].min()
        signals["execute_date"] = signals["signal_date"].map(execution_dates)
    summary = _performance_summary(nav, trades, config)
    summary["benchmarks"] = {}
    elapsed_days = max((evaluation.index[-1] - evaluation.index[0]).days, 1)
    for code in codes:
        close = evaluation[f"{code}_close"].astype(float)
        daily_returns = close.pct_change().dropna()
        total_return = float(close.iloc[-1] / close.iloc[0] - 1)
        annual_return = float((close.iloc[-1] / close.iloc[0]) ** (365.25 / elapsed_days) - 1)
        annual_volatility = (
            float(daily_returns.std(ddof=1) * math.sqrt(252)) if len(daily_returns) > 1 else 0.0
        )
        sharpe = (
            float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252))
            if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0
            else 0.0
        )
        drawdown = close / close.cummax() - 1
        summary["benchmarks"][code] = {
            "name": ETF_NAMES.get(code, code),
            "total_return": round(total_return, 6),
            "annual_return": round(annual_return, 6),
            "annual_volatility": round(annual_volatility, 6),
            "sharpe_zero_rate": round(sharpe, 4),
            "max_drawdown": round(float(drawdown.min()), 6),
        }
    return BacktestResult(nav=nav, signals=signals, trades=trades, summary=summary)
