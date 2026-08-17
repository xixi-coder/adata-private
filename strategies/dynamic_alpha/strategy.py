from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


FACTOR_NAMES = ("momentum", "trend", "quality", "value", "risk")
PRICE_FACTOR_NAMES = ("momentum", "trend", "risk")
REQUIRED_PANEL_COLUMNS = {
    "stock_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
}
FUNDAMENTAL_VALUE_COLUMNS = {
    "roe",
    "operating_cash_flow",
    "net_profit",
    "cashflow_to_profit",
    "gross_margin",
    "debt_ratio",
    "earnings_yield",
    "fcf_yield",
    "book_to_price",
    "pe_ttm",
    "pb",
    "net_profit_ttm",
    "free_cash_flow_ttm",
    "eps_ttm",
    "operating_cashflow_ps_ttm",
    "net_asset_ps",
    "operating_cashflow_yield",
}


@dataclass(frozen=True)
class DynamicAlphaConfig:
    """Research defaults. They are deliberately constraints, not return targets."""

    initial_capital: float = 1_000_000.0
    min_history_days: int = 250
    min_amount_ma20: float = 100_000_000.0
    max_suspension_days_20: int = 2
    universe_limit: int = 800
    max_positions: int = 15
    entry_fraction: float = 0.03
    exit_fraction: float = 0.12
    max_stock_weight: float = 0.08
    max_industry_weight: float = 0.25
    min_industry_members: int = 5

    minimum_market_exposure: float = 0.10
    maximum_market_exposure: float = 1.00
    target_annual_volatility: float = 0.18
    volatility_lookback: int = 20
    drawdown_tier_1: float = 0.06
    drawdown_tier_2: float = 0.10
    drawdown_tier_3: float = 0.15
    drawdown_stop: float = 0.18

    forward_return_days: int = 20
    adaptive_lookback: int = 252
    ic_halflife: int = 60
    min_ic_observations: int = 12
    min_ic_stocks: int = 20
    adaptive_share: float = 0.40
    max_factor_weight: float = 0.35
    max_monthly_factor_change: float = 0.10
    prior_factor_weights: dict[str, float] = field(
        default_factory=lambda: {
            "momentum": 0.30,
            "trend": 0.25,
            "quality": 0.20,
            "value": 0.15,
            "risk": 0.10,
        }
    )

    atr_stop_multiple: float = 2.5
    trend_exit_confirm_days: int = 2
    stale_position_days: int = 20

    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    sell_stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0010
    rebalance_threshold: float = 0.005
    board_lot: int = 100
    price_limit_tolerance: float = 0.001

    def __post_init__(self) -> None:
        if not 0 < self.entry_fraction <= self.exit_fraction <= 1:
            raise ValueError("entry_fraction and exit_fraction must satisfy 0 < entry <= exit <= 1")
        if self.max_positions < 1 or self.board_lot < 1:
            raise ValueError("max_positions and board_lot must be positive")
        if not 0 <= self.minimum_market_exposure <= self.maximum_market_exposure <= 1:
            raise ValueError("market exposure bounds must be between zero and one")
        if self.forward_return_days < 1:
            raise ValueError("forward_return_days must be positive")
        if not math.isclose(sum(self.prior_factor_weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError("prior_factor_weights must sum to one")
        unknown = set(self.prior_factor_weights).difference(FACTOR_NAMES)
        if unknown:
            raise ValueError(f"unknown factors in prior_factor_weights: {sorted(unknown)}")


@dataclass
class BacktestResult:
    nav: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    factor_weights: pd.DataFrame
    metrics: dict[str, float | int]

    def write(self, output_dir: str | Path) -> dict[str, str]:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        files = {
            "nav": path / "nav.csv",
            "trades": path / "trades.csv",
            "signals": path / "signals.csv",
            "factor_weights": path / "factor_weights.csv",
            "metrics": path / "metrics.json",
        }
        self.nav.to_csv(files["nav"], index=False, encoding="utf-8-sig")
        self.trades.to_csv(files["trades"], index=False, encoding="utf-8-sig")
        self.signals.to_csv(files["signals"], index=False, encoding="utf-8-sig")
        self.factor_weights.to_csv(files["factor_weights"], index=False, encoding="utf-8-sig")
        with files["metrics"].open("w", encoding="utf-8") as handle:
            json.dump(self.metrics, handle, ensure_ascii=False, indent=2)
        return {name: str(file) for name, file in files.items()}


class DynamicAlphaStrategy:
    """
    Point-in-time adaptive A-share strategy.

    The implementation is independent from the repository's existing strategies. The only
    optional shared component is the command-line cache reader at the bottom of this file.
    Signals are formed after a session closes and are executable no earlier than the next
    session's open.
    """

    def __init__(self, config: DynamicAlphaConfig | None = None):
        self.config = config or DynamicAlphaConfig()
        self.panel = pd.DataFrame()
        self.features = pd.DataFrame()
        self.market_context = pd.DataFrame()
        self.ic_history = pd.DataFrame()
        self.calendar: list[pd.Timestamp] = []
        self._date_to_index: dict[pd.Timestamp, int] = {}

    def prepare(
        self,
        panel: pd.DataFrame,
        fundamentals: pd.DataFrame | None = None,
        benchmark: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Validate inputs and calculate all point-in-time features."""

        clean = self._standardize_panel(panel)
        self.calendar = sorted(clean["trade_date"].drop_duplicates().tolist())
        self._date_to_index = {date: index for index, date in enumerate(self.calendar)}
        clean = self._attach_point_in_time_fundamentals(clean, fundamentals)
        self.panel = clean
        self.features = self._compute_features(clean)
        self.market_context = self._compute_market_context(self.features, benchmark)
        self.ic_history = self._compute_ic_history(self.features)
        return self.features

    def factor_weights_as_of(
        self,
        signal_date: str | pd.Timestamp,
        available_factors: Iterable[str] | None = None,
        previous_weights: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Return weights using only IC observations whose outcomes were known by signal_date."""

        as_of = pd.Timestamp(signal_date).normalize()
        available = list(available_factors or FACTOR_NAMES)
        available = [factor for factor in FACTOR_NAMES if factor in available]
        if not available:
            raise ValueError("at least one factor must be available")

        priors = np.array([self.config.prior_factor_weights[factor] for factor in available], dtype=float)
        priors = priors / priors.sum()
        quality = np.zeros(len(available), dtype=float)
        if not self.ic_history.empty:
            known = self.ic_history[self.ic_history["known_at"] <= as_of]
            for index, factor in enumerate(available):
                values = (
                    known.loc[known["factor"] == factor]
                    .sort_values("signal_date")
                    .tail(self.config.adaptive_lookback)["ic"]
                    .dropna()
                )
                if len(values) < self.config.min_ic_observations:
                    continue
                ewm = values.ewm(halflife=self.config.ic_halflife, adjust=False)
                mean = float(ewm.mean().iloc[-1])
                std = float(ewm.std(bias=False).iloc[-1])
                if np.isfinite(mean) and np.isfinite(std) and std > 1e-9:
                    quality[index] = max(0.0, mean / std)

        adaptive = quality / quality.sum() if quality.sum() > 0 else priors
        share = self.config.adaptive_share
        raw = (1.0 - share) * priors + share * adaptive
        effective_cap = max(self.config.max_factor_weight, 1.0 / len(available))
        lower = np.zeros(len(available), dtype=float)
        upper = np.full(len(available), effective_cap, dtype=float)

        if previous_weights:
            previous = np.array([max(0.0, previous_weights.get(factor, 0.0)) for factor in available])
            if previous.sum() > 0:
                previous = previous / previous.sum()
                change = self.config.max_monthly_factor_change
                candidate_lower = np.maximum(0.0, previous - change)
                candidate_upper = np.minimum(effective_cap, previous + change)
                if candidate_lower.sum() <= 1.0 + 1e-12 and candidate_upper.sum() >= 1.0 - 1e-12:
                    lower, upper = candidate_lower, candidate_upper

        projected = self._bounded_normalize(raw, lower, upper)
        return {factor: float(projected[index]) for index, factor in enumerate(available)}

    def score_on_date(
        self,
        signal_date: str | pd.Timestamp,
        factor_weights: dict[str, float],
    ) -> pd.DataFrame:
        """Score the eligible cross-section for one close."""

        date = pd.Timestamp(signal_date).normalize()
        day = self.features[(self.features["trade_date"] == date) & self.features["eligible"]].copy()
        if day.empty:
            return day
        available = [
            factor
            for factor in FACTOR_NAMES
            if factor_weights.get(factor, 0.0) > 0 and day[f"factor_{factor}"].notna().any()
        ]
        if not available:
            return pd.DataFrame(columns=list(day.columns) + ["alpha_score", "alpha_rank"])
        numerator = pd.Series(0.0, index=day.index)
        denominator = pd.Series(0.0, index=day.index)
        for factor in available:
            values = day[f"factor_{factor}"]
            weight = float(factor_weights[factor])
            numerator = numerator + values.fillna(0.0) * weight
            denominator = denominator + values.notna().astype(float) * weight
        day["alpha_score"] = numerator / denominator.replace(0, np.nan)
        day = day.dropna(subset=["alpha_score"]).sort_values("alpha_score", ascending=False)
        day["alpha_rank"] = np.arange(1, len(day) + 1)
        day["alpha_percentile"] = day["alpha_rank"] / len(day)
        return day

    def run_backtest(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> BacktestResult:
        if self.features.empty:
            raise RuntimeError("call prepare() before run_backtest()")
        cfg = self.config
        dates = [
            date
            for date in self.calendar
            if (start_date is None or date >= pd.Timestamp(start_date))
            and (end_date is None or date <= pd.Timestamp(end_date))
        ]
        if len(dates) < 3:
            raise ValueError("backtest range must contain at least three sessions")

        date_rows = {
            date: sub.set_index("stock_code", drop=False)
            for date, sub in self.features[self.features["trade_date"].isin(dates)].groupby("trade_date")
        }
        signal_dates = set(
            pd.Series(dates, index=dates).groupby(pd.DatetimeIndex(dates).to_period("W-FRI")).last().tolist()
        )
        next_date = {dates[index]: dates[index + 1] for index in range(len(dates) - 1)}

        cash = float(cfg.initial_capital)
        positions: dict[str, dict[str, Any]] = {}
        pending_targets: dict[pd.Timestamp, dict[str, Any]] = {}
        pending_exits: dict[pd.Timestamp, dict[str, str]] = {}
        nav_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        signal_rows: list[dict[str, Any]] = []
        weight_rows: list[dict[str, Any]] = []
        factor_weights: dict[str, float] | None = None
        weight_month: tuple[int, int] | None = None
        peak_nav = float(cfg.initial_capital)

        for date in dates:
            rows = date_rows.get(date, pd.DataFrame())
            exits = pending_exits.pop(date, {})
            target_payload = pending_targets.pop(date, None)
            if exits:
                for code, reason in exits.items():
                    if code not in positions:
                        continue
                    sold = self._sell_position(
                        code,
                        date,
                        rows,
                        positions,
                        trade_rows,
                        reason,
                        cash,
                    )
                    cash = sold[0]
                    if not sold[1] and date in next_date:
                        pending_exits.setdefault(next_date[date], {})[code] = reason

            if target_payload is not None:
                target_weights = dict(target_payload["weights"])
                for code in exits:
                    target_weights[code] = 0.0
                cash, unresolved_sells = self._execute_rebalance(
                    date=date,
                    rows=rows,
                    target_weights=target_weights,
                    signal_date=target_payload["signal_date"],
                    positions=positions,
                    cash=cash,
                    trade_rows=trade_rows,
                )
                if unresolved_sells and date in next_date:
                    retry = pending_exits.setdefault(next_date[date], {})
                    for code in unresolved_sells:
                        retry[code] = "REBALANCE_SELL_RETRY"

            total, gross_exposure = self._mark_to_market(rows, positions, cash)
            peak_nav = max(peak_nav, total)
            drawdown = total / peak_nav - 1.0 if peak_nav > 0 else 0.0
            nav_rows.append(
                {
                    "trade_date": date,
                    "nav": total,
                    "cash": cash,
                    "gross_exposure": gross_exposure,
                    "position_count": len(positions),
                    "drawdown": drawdown,
                }
            )

            forced = self._update_position_risk(date, rows, positions)
            if date in next_date and forced:
                pending_exits.setdefault(next_date[date], {}).update(forced)

            if date not in signal_dates or date not in next_date:
                continue

            available = self._available_factors_for_date(date)
            if not available:
                # During the warm-up period there is no valid cross-section. Stay in
                # cash (or unwind existing positions) instead of manufacturing scores.
                pending_targets[next_date[date]] = {"signal_date": date, "weights": {}}
                continue
            current_month = (date.year, date.month)
            if factor_weights is None or current_month != weight_month:
                factor_weights = self.factor_weights_as_of(date, available, factor_weights)
                weight_month = current_month
                record = {"signal_date": date}
                record.update({factor: factor_weights.get(factor, 0.0) for factor in FACTOR_NAMES})
                weight_rows.append(record)

            scored = self.score_on_date(date, factor_weights)
            held_codes = set(positions).difference(forced)
            selected = self._select_candidates(scored, held_codes)
            regime = self._regime_exposure(date)
            recent_nav = pd.DataFrame(nav_rows)
            vol_scale = self._portfolio_volatility_scale(recent_nav)
            drawdown_scale = self._drawdown_scale(drawdown)
            target_exposure = float(np.clip(regime * vol_scale * drawdown_scale, 0.0, 1.0))
            targets = self._build_target_weights(selected, target_exposure)
            pending_targets[next_date[date]] = {
                "signal_date": date,
                "weights": targets,
            }

            context = self.market_context[self.market_context["trade_date"] == date]
            risk_score = float(context.iloc[-1]["risk_score"]) if not context.empty else np.nan
            for row in selected.itertuples():
                signal_rows.append(
                    {
                        "signal_date": date,
                        "execution_date": next_date[date],
                        "stock_code": row.stock_code,
                        "stock_name": row.stock_name,
                        "industry": row.industry,
                        "alpha_score": float(row.alpha_score),
                        "alpha_rank": int(row.alpha_rank),
                        "factor_momentum": float(row.factor_momentum) if pd.notna(row.factor_momentum) else np.nan,
                        "factor_trend": float(row.factor_trend) if pd.notna(row.factor_trend) else np.nan,
                        "factor_quality": float(row.factor_quality) if pd.notna(row.factor_quality) else np.nan,
                        "factor_value": float(row.factor_value) if pd.notna(row.factor_value) else np.nan,
                        "factor_risk": float(row.factor_risk) if pd.notna(row.factor_risk) else np.nan,
                        "target_weight": float(targets.get(row.stock_code, 0.0)),
                        "regime_exposure": regime,
                        "volatility_scale": vol_scale,
                        "drawdown_scale": drawdown_scale,
                        "target_exposure": target_exposure,
                        "market_risk_score": risk_score,
                    }
                )

        nav = pd.DataFrame(nav_rows)
        trades = pd.DataFrame(trade_rows)
        signals = pd.DataFrame(signal_rows)
        weights = pd.DataFrame(weight_rows)
        return BacktestResult(nav, trades, signals, weights, self._performance_metrics(nav, trades))

    def _standardize_panel(self, panel: pd.DataFrame) -> pd.DataFrame:
        missing = REQUIRED_PANEL_COLUMNS.difference(panel.columns)
        if missing:
            raise ValueError(f"panel missing required columns: {sorted(missing)}")
        df = panel.copy()
        df["stock_code"] = df["stock_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
        for column in ["open", "high", "low", "close", "volume", "amount", "pre_close", "market_cap"]:
            if column in df:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df = df.dropna(subset=["stock_code", "trade_date", "open", "high", "low", "close", "amount"])
        df = df[(df["close"] > 0) & (df["open"] > 0) & (df["amount"] >= 0)]
        df = df.sort_values(["stock_code", "trade_date"]).drop_duplicates(
            ["stock_code", "trade_date"], keep="last"
        )
        if "pre_close" not in df:
            df["pre_close"] = df.groupby("stock_code", sort=False)["close"].shift(1)
        if "industry" not in df:
            df["industry"] = "UNKNOWN"
        df["industry"] = df["industry"].fillna("UNKNOWN").astype(str)
        if "stock_name" not in df:
            df["stock_name"] = ""
        names = df["stock_name"].fillna("").astype(str).str.upper().str.replace(" ", "", regex=False)
        inferred_st = names.str.startswith(("ST", "*ST", "SST", "S*ST", "PT", "*PT"))
        inferred_st |= df["stock_name"].fillna("").astype(str).str.contains("退|摘牌", regex=True)
        if "is_st" in df:
            explicit = df["is_st"].fillna(False).astype(bool)
            df["is_st"] = explicit | inferred_st
        else:
            df["is_st"] = inferred_st
        # Fundamental values without an announcement timestamp are not safe for a
        # point-in-time backtest. The separate fundamentals input is authoritative.
        df = df.drop(columns=list(FUNDAMENTAL_VALUE_COLUMNS.intersection(df.columns)), errors="ignore")
        return df.reset_index(drop=True)

    def _attach_point_in_time_fundamentals(
        self,
        panel: pd.DataFrame,
        fundamentals: pd.DataFrame | None,
    ) -> pd.DataFrame:
        if fundamentals is None or fundamentals.empty:
            return panel
        required = {"stock_code", "announce_date"}
        missing = required.difference(fundamentals.columns)
        if missing:
            raise ValueError(f"fundamentals missing point-in-time columns: {sorted(missing)}")
        finance = fundamentals.copy()
        finance["stock_code"] = finance["stock_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        finance["announce_date"] = pd.to_datetime(finance["announce_date"], errors="coerce").dt.normalize()
        finance = finance.dropna(subset=["stock_code", "announce_date"])

        calendar_array = np.array(self.calendar, dtype="datetime64[ns]")
        announcement_array = finance["announce_date"].to_numpy(dtype="datetime64[ns]")
        available_indices = np.searchsorted(calendar_array, announcement_array, side="right")
        valid = available_indices < len(calendar_array)
        finance = finance.loc[valid].copy()
        finance["available_date"] = pd.to_datetime(calendar_array[available_indices[valid]])
        if finance.empty:
            return panel

        for column in FUNDAMENTAL_VALUE_COLUMNS:
            if column in finance:
                finance[column] = pd.to_numeric(finance[column], errors="coerce")
        if "cashflow_to_profit" not in finance and {"operating_cash_flow", "net_profit"} <= set(finance):
            denominator = finance["net_profit"].abs().replace(0, np.nan)
            finance["cashflow_to_profit"] = finance["operating_cash_flow"] / denominator
        if "earnings_yield" not in finance and "pe_ttm" in finance:
            finance["earnings_yield"] = np.where(finance["pe_ttm"] > 0, 1.0 / finance["pe_ttm"], np.nan)
        if "book_to_price" not in finance and "pb" in finance:
            finance["book_to_price"] = np.where(finance["pb"] > 0, 1.0 / finance["pb"], np.nan)

        ignored = {"announce_date", "available_date"}
        value_columns = [
            column
            for column in sorted(FUNDAMENTAL_VALUE_COLUMNS)
            if column in finance.columns and column not in ignored
        ]
        if not value_columns:
            return panel
        pieces = []
        for code, prices in panel.groupby("stock_code", sort=False):
            reports = finance[finance["stock_code"] == code].sort_values("available_date")
            if reports.empty:
                pieces.append(prices)
                continue
            merged = pd.merge_asof(
                prices.sort_values("trade_date"),
                reports[["available_date", *value_columns]].sort_values("available_date"),
                left_on="trade_date",
                right_on="available_date",
                direction="backward",
                allow_exact_matches=True,
                suffixes=("", "_fundamental"),
            )
            pieces.append(merged.drop(columns=["available_date"], errors="ignore"))
        out = pd.concat(pieces, ignore_index=True).sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
        # Per-share accounting values are point-in-time inputs. Combine them with each
        # day's price only after the announcement-date merge so valuation remains dynamic.
        if "eps_ttm" in out:
            derived = np.where(out["eps_ttm"] > 0, out["eps_ttm"] / out["close"], np.nan)
            if "earnings_yield" in out:
                out["earnings_yield"] = out["earnings_yield"].where(out["earnings_yield"].notna(), derived)
            else:
                out["earnings_yield"] = derived
        if "operating_cashflow_ps_ttm" in out:
            derived = out["operating_cashflow_ps_ttm"] / out["close"]
            if "operating_cashflow_yield" in out:
                out["operating_cashflow_yield"] = out["operating_cashflow_yield"].where(
                    out["operating_cashflow_yield"].notna(), derived
                )
            else:
                out["operating_cashflow_yield"] = derived
            if "eps_ttm" in out:
                ratio = out["operating_cashflow_ps_ttm"] / out["eps_ttm"].abs().replace(0, np.nan)
                if "cashflow_to_profit" in out:
                    out["cashflow_to_profit"] = out["cashflow_to_profit"].where(
                        out["cashflow_to_profit"].notna(), ratio
                    )
                else:
                    out["cashflow_to_profit"] = ratio
        if "net_asset_ps" in out:
            derived = np.where(out["net_asset_ps"] > 0, out["net_asset_ps"] / out["close"], np.nan)
            if "book_to_price" in out:
                out["book_to_price"] = out["book_to_price"].where(out["book_to_price"].notna(), derived)
            else:
                out["book_to_price"] = derived
        return out

    def _compute_features(self, panel: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        df = panel.copy().sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
        grouped = df.groupby("stock_code", sort=False, group_keys=False)
        df["listed_sessions"] = grouped.cumcount() + 1
        df["ret_1"] = grouped["close"].pct_change(fill_method=None)
        for horizon in (5, 20, 60, 120):
            df[f"ret_{horizon}"] = grouped["close"].pct_change(horizon, fill_method=None)
        df["momentum_20_skip5"] = grouped["close"].shift(5) / grouped["close"].shift(20) - 1.0
        df["momentum_60_skip5"] = grouped["close"].shift(5) / grouped["close"].shift(60) - 1.0
        df["momentum_120_skip5"] = grouped["close"].shift(5) / grouped["close"].shift(120) - 1.0

        for window, minimum in ((20, 10), (60, 30), (120, 60)):
            df[f"ma{window}"] = (
                grouped["close"].rolling(window, min_periods=minimum).mean().reset_index(level=0, drop=True)
            )
        df["ma60_slope20"] = df["ma60"] / grouped["ma60"].shift(20) - 1.0
        df["amount_ma20"] = (
            grouped["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
        )
        high_120 = grouped["high"].rolling(120, min_periods=60).max().reset_index(level=0, drop=True)
        low_120 = grouped["low"].rolling(120, min_periods=60).min().reset_index(level=0, drop=True)
        high_60 = grouped["close"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
        df["close_to_high120"] = df["close"] / high_120 - 1.0
        df["trend_distance60"] = df["close"] / df["ma60"] - 1.0
        df["drawdown_60"] = df["close"] / high_60 - 1.0
        df["new_high_120"] = df["close"] >= high_120 * 0.995
        df["new_low_120"] = df["close"] <= low_120 * 1.005

        previous_close = df["pre_close"].where(df["pre_close"].notna(), grouped["close"].shift(1))
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - previous_close).abs(),
                (df["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["true_range"] = true_range
        df["atr20_pct"] = (
            df.groupby("stock_code", sort=False)["true_range"]
            .rolling(20, min_periods=10)
            .mean()
            .reset_index(level=0, drop=True)
            / df["close"]
        )
        market_return = df.groupby("trade_date")["ret_1"].transform("median")
        df["idio_return"] = df["ret_1"] - market_return
        df["idio_vol20"] = (
            df.groupby("stock_code", sort=False)["idio_return"]
            .rolling(20, min_periods=10)
            .std(ddof=1)
            .reset_index(level=0, drop=True)
            * math.sqrt(252)
        )
        downside = df["ret_1"].clip(upper=0.0)
        df["downside_squared"] = downside.pow(2)
        df["downside_vol20"] = (
            df.groupby("stock_code", sort=False)["downside_squared"]
            .rolling(20, min_periods=10)
            .mean()
            .reset_index(level=0, drop=True)
            .pow(0.5)
            * math.sqrt(252)
        )

        session_number = df["trade_date"].map(self._date_to_index).astype(float)
        session_gap = session_number.groupby(df["stock_code"]).diff().fillna(1.0) - 1.0
        df["missing_sessions"] = session_gap.clip(lower=0.0) + ((df["volume"] <= 0) | (df["amount"] <= 0)).astype(float)
        df["suspension_days_20"] = (
            df.groupby("stock_code", sort=False)["missing_sessions"]
            .rolling(20, min_periods=1)
            .sum()
            .reset_index(level=0, drop=True)
        )

        basic_eligible = (
            (df["listed_sessions"] >= cfg.min_history_days)
            & (df["amount_ma20"] >= cfg.min_amount_ma20)
            & (df["suspension_days_20"] <= cfg.max_suspension_days_20)
            & ~df["is_st"]
            & (df["volume"] > 0)
        )
        if cfg.universe_limit:
            liquidity_rank = df["amount_ma20"].groupby(df["trade_date"]).rank(method="first", ascending=False)
            basic_eligible &= liquidity_rank <= cfg.universe_limit
        df["eligible"] = basic_eligible

        factor_inputs = {
            "momentum": [
                ("momentum_20_skip5", 0.25),
                ("momentum_60_skip5", 0.45),
                ("momentum_120_skip5", 0.30),
            ],
            "trend": [
                ("trend_distance60", 0.30),
                ("ma60_slope20", 0.30),
                ("close_to_high120", 0.25),
                ("ret_20", 0.15),
            ],
            "quality": [
                ("roe", 0.35),
                ("cashflow_to_profit", 0.30),
                ("gross_margin", 0.20),
                ("debt_ratio", -0.15),
            ],
            "value": [
                ("earnings_yield", 0.35),
                ("operating_cashflow_yield", 0.25),
                ("fcf_yield", 0.10),
                ("book_to_price", 0.20),
                ("dividend_yield_ttm", 0.10),
            ],
            "risk": [
                ("idio_vol20", -0.40),
                ("downside_vol20", -0.35),
                ("drawdown_60", 0.25),
            ],
        }
        for factor, inputs in factor_inputs.items():
            parts: list[tuple[pd.Series, float]] = []
            for column, weight in inputs:
                if column not in df or not df[column].notna().any():
                    continue
                standardized = self._industry_neutral_zscore(df, column)
                parts.append((standardized, weight))
            df[f"factor_{factor}"] = self._weighted_available_average(parts, df.index)
            df.loc[~df["eligible"], f"factor_{factor}"] = np.nan
        return df

    def _industry_neutral_zscore(self, frame: pd.DataFrame, column: str) -> pd.Series:
        values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        daily_median = values.groupby(frame["trade_date"]).transform("median")
        group_keys = [frame["trade_date"], frame["industry"]]
        industry_count = values.groupby(group_keys).transform("count")
        industry_median = values.groupby(group_keys).transform("median")
        center = industry_median.where(industry_count >= self.config.min_industry_members, daily_median)
        residual = values - center
        lower = residual.groupby(frame["trade_date"]).transform(lambda x: x.quantile(0.025))
        upper = residual.groupby(frame["trade_date"]).transform(lambda x: x.quantile(0.975))
        winsorized = residual.clip(lower=lower, upper=upper)
        mean = winsorized.groupby(frame["trade_date"]).transform("mean")
        std = winsorized.groupby(frame["trade_date"]).transform("std").replace(0, np.nan)
        return (winsorized - mean) / std

    @staticmethod
    def _weighted_available_average(parts: list[tuple[pd.Series, float]], index: pd.Index) -> pd.Series:
        if not parts:
            return pd.Series(np.nan, index=index, dtype=float)
        numerator = pd.Series(0.0, index=index)
        denominator = pd.Series(0.0, index=index)
        for values, signed_weight in parts:
            weight = abs(float(signed_weight))
            direction = 1.0 if signed_weight >= 0 else -1.0
            numerator = numerator + values.fillna(0.0) * weight * direction
            denominator = denominator + values.notna().astype(float) * weight
        return numerator / denominator.replace(0, np.nan)

    def _compute_market_context(
        self,
        features: pd.DataFrame,
        benchmark: pd.DataFrame | None,
    ) -> pd.DataFrame:
        liquid = features[
            (features["listed_sessions"] >= min(self.config.min_history_days, 120))
            & (features["amount_ma20"] >= self.config.min_amount_ma20 * 0.5)
            & ~features["is_st"]
        ].copy()
        if liquid.empty:
            return pd.DataFrame(columns=["trade_date", "risk_score", "target_exposure"])
        liquid["above_ma60"] = liquid["close"] > liquid["ma60"]
        liquid["up_amount"] = liquid["amount"].where(liquid["ret_1"] > 0, 0.0)
        grouped = liquid.groupby("trade_date")
        context = grouped.agg(
            breadth=("above_ma60", "mean"),
            high_share=("new_high_120", "mean"),
            low_share=("new_low_120", "mean"),
            up_amount=("up_amount", "sum"),
            total_amount=("amount", "sum"),
            market_return=("ret_1", "median"),
        ).reset_index()
        context["up_amount_share"] = context["up_amount"] / context["total_amount"].replace(0, np.nan)
        context["market_vol20"] = context["market_return"].rolling(20, min_periods=10).std(ddof=1) * math.sqrt(252)

        synthetic = (1.0 + context["market_return"].fillna(0.0)).cumprod()
        trend_close = synthetic
        if benchmark is not None and not benchmark.empty:
            if not {"trade_date", "close"} <= set(benchmark.columns):
                raise ValueError("benchmark must contain trade_date and close")
            bench = benchmark[["trade_date", "close"]].copy()
            bench["trade_date"] = pd.to_datetime(bench["trade_date"], errors="coerce").dt.normalize()
            bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
            bench = bench.dropna().sort_values("trade_date").drop_duplicates("trade_date")
            context = context.merge(bench.rename(columns={"close": "benchmark_close"}), on="trade_date", how="left")
            context["benchmark_close"] = context["benchmark_close"].ffill()
            trend_close = context["benchmark_close"].where(context["benchmark_close"].notna(), synthetic)
        context["trend_close"] = trend_close
        context["trend_ma200"] = context["trend_close"].rolling(200, min_periods=120).mean()
        trend_ma60 = context["trend_close"].rolling(60, min_periods=30).mean()
        level = context["trend_close"] / context["trend_ma200"] - 1.0
        slope = trend_ma60 / trend_ma60.shift(20) - 1.0

        breadth_score = ((context["breadth"] - 0.20) / 0.60).clip(0.0, 1.0)
        trend_score = (0.50 + 8.0 * level.fillna(0.0) + 4.0 * slope.fillna(0.0)).clip(0.0, 1.0)
        high_low_score = (0.50 + 2.5 * (context["high_share"] - context["low_share"])).clip(0.0, 1.0)
        amount_score = context["up_amount_share"].fillna(0.5).clip(0.0, 1.0)
        volatility_score = ((0.40 - context["market_vol20"].fillna(0.25)) / 0.25).clip(0.0, 1.0)
        context["risk_score"] = (
            0.30 * breadth_score
            + 0.20 * trend_score
            + 0.20 * high_low_score
            + 0.15 * amount_score
            + 0.15 * volatility_score
        ).clip(0.0, 1.0)
        floor = self.config.minimum_market_exposure
        ceiling = self.config.maximum_market_exposure
        context["target_exposure"] = floor + (ceiling - floor) * context["risk_score"]
        return context

    def _compute_ic_history(self, features: pd.DataFrame) -> pd.DataFrame:
        horizon = self.config.forward_return_days
        if len(self.calendar) <= horizon:
            return pd.DataFrame(columns=["signal_date", "known_at", "factor", "ic", "stock_count"])
        reverse_date = {self.calendar[index]: self.calendar[index - horizon] for index in range(horizon, len(self.calendar))}
        future = features[["stock_code", "trade_date", "close"]].copy()
        future["signal_date"] = future["trade_date"].map(reverse_date)
        future = future.dropna(subset=["signal_date"]).rename(columns={"close": "future_close"})
        merged = features.merge(
            future[["stock_code", "signal_date", "future_close"]],
            left_on=["stock_code", "trade_date"],
            right_on=["stock_code", "signal_date"],
            how="left",
        )
        merged["forward_return"] = merged["future_close"] / merged["close"] - 1.0
        known_at = {self.calendar[index]: self.calendar[index + horizon] for index in range(len(self.calendar) - horizon)}
        rows: list[dict[str, Any]] = []
        valid = merged[merged["eligible"] & merged["forward_return"].notna()]
        for date, day in valid.groupby("trade_date"):
            for factor in FACTOR_NAMES:
                column = f"factor_{factor}"
                sample = day[[column, "forward_return"]].dropna()
                if len(sample) < self.config.min_ic_stocks:
                    continue
                # Spearman is Pearson correlation of ranks. Compute it directly so the
                # research module does not add scipy as a runtime dependency.
                factor_rank = sample[column].rank(method="average")
                return_rank = sample["forward_return"].rank(method="average")
                ic = factor_rank.corr(return_rank, method="pearson")
                if pd.notna(ic):
                    rows.append(
                        {
                            "signal_date": date,
                            "known_at": known_at.get(date, pd.NaT),
                            "factor": factor,
                            "ic": float(ic),
                            "stock_count": len(sample),
                        }
                    )
        return pd.DataFrame(rows)

    def _available_factors_for_date(self, date: pd.Timestamp) -> list[str]:
        day = self.features[(self.features["trade_date"] == date) & self.features["eligible"]]
        available = []
        minimum = min(self.config.min_ic_stocks, max(2, len(day)))
        for factor in FACTOR_NAMES:
            if day[f"factor_{factor}"].notna().sum() >= minimum:
                available.append(factor)
        return available or [factor for factor in PRICE_FACTOR_NAMES if day[f"factor_{factor}"].notna().any()]

    def _select_candidates(self, scored: pd.DataFrame, held_codes: set[str]) -> pd.DataFrame:
        if scored.empty:
            return scored
        entry_count = max(1, int(math.ceil(len(scored) * self.config.entry_fraction)))
        exit_count = max(entry_count, int(math.ceil(len(scored) * self.config.exit_fraction)))
        entrants = set(scored.head(entry_count)["stock_code"])
        retained = set(scored.head(exit_count)["stock_code"]).intersection(held_codes)
        selected_codes = entrants | retained
        return scored[scored["stock_code"].isin(selected_codes)].head(self.config.max_positions).copy()

    def _build_target_weights(self, selected: pd.DataFrame, target_exposure: float) -> dict[str, float]:
        if selected.empty or target_exposure <= 0:
            return {}
        volatility = selected["idio_vol20"].fillna(selected["idio_vol20"].median()).clip(lower=0.10)
        score_rank = selected["alpha_score"].rank(pct=True).fillna(0.5)
        raw = (0.50 + 0.50 * score_rank) / volatility
        raw = raw.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        if raw.sum() <= 0:
            raw = pd.Series(1.0, index=selected.index)
        raw_by_code = dict(zip(selected["stock_code"], raw / raw.sum()))
        industries = dict(zip(selected["stock_code"], selected["industry"]))
        return self._allocate_with_caps(raw_by_code, industries, target_exposure)

    def _allocate_with_caps(
        self,
        raw: dict[str, float],
        industries: dict[str, str],
        exposure: float,
    ) -> dict[str, float]:
        weights = {code: 0.0 for code in raw}
        group_totals: dict[str, float] = {}
        remaining = float(exposure)
        for _ in range(50):
            if remaining <= 1e-10:
                break
            active = {}
            for code, preference in raw.items():
                stock_capacity = self.config.max_stock_weight - weights[code]
                industry = industries.get(code, "UNKNOWN")
                group_capacity = float("inf")
                if industry and industry != "UNKNOWN":
                    group_capacity = self.config.max_industry_weight - group_totals.get(industry, 0.0)
                if stock_capacity > 1e-12 and group_capacity > 1e-12 and preference > 0:
                    active[code] = preference
            if not active:
                break
            preference_sum = sum(active.values())
            proposals = {code: remaining * value / preference_sum for code, value in active.items()}
            for industry in set(industries.get(code, "UNKNOWN") for code in active):
                if not industry or industry == "UNKNOWN":
                    continue
                members = [code for code in active if industries.get(code) == industry]
                proposal_sum = sum(proposals[code] for code in members)
                group_capacity = self.config.max_industry_weight - group_totals.get(industry, 0.0)
                if proposal_sum > group_capacity > 0:
                    scale = group_capacity / proposal_sum
                    for code in members:
                        proposals[code] *= scale
            allocated = 0.0
            for code, proposal in proposals.items():
                increment = min(proposal, self.config.max_stock_weight - weights[code])
                if increment <= 0:
                    continue
                weights[code] += increment
                industry = industries.get(code, "UNKNOWN")
                if industry and industry != "UNKNOWN":
                    group_totals[industry] = group_totals.get(industry, 0.0) + increment
                allocated += increment
            if allocated <= 1e-12:
                break
            remaining -= allocated
        return {code: weight for code, weight in weights.items() if weight > 1e-10}

    def _regime_exposure(self, date: pd.Timestamp) -> float:
        row = self.market_context[self.market_context["trade_date"] <= date].tail(1)
        return float(row.iloc[0]["target_exposure"]) if not row.empty else self.config.minimum_market_exposure

    def _portfolio_volatility_scale(self, nav: pd.DataFrame) -> float:
        returns = nav["nav"].pct_change(fill_method=None).dropna().tail(self.config.volatility_lookback)
        if len(returns) < max(5, self.config.volatility_lookback // 2):
            return 1.0
        volatility = float(returns.std(ddof=1) * math.sqrt(252))
        if not np.isfinite(volatility) or volatility <= 1e-9:
            return 1.0
        return float(np.clip(self.config.target_annual_volatility / volatility, 0.25, 1.0))

    def _drawdown_scale(self, drawdown: float) -> float:
        loss = -drawdown
        if loss >= self.config.drawdown_stop:
            return 0.20
        if loss >= self.config.drawdown_tier_3:
            return 0.20
        if loss >= self.config.drawdown_tier_2:
            return 0.50
        if loss >= self.config.drawdown_tier_1:
            return 0.75
        return 1.0

    def _execute_rebalance(
        self,
        date: pd.Timestamp,
        rows: pd.DataFrame,
        target_weights: dict[str, float],
        signal_date: pd.Timestamp,
        positions: dict[str, dict[str, Any]],
        cash: float,
        trade_rows: list[dict[str, Any]],
    ) -> tuple[float, set[str]]:
        unresolved_sells: set[str] = set()
        equity = cash
        for code, position in positions.items():
            price = self._price_for_code(rows, code, "open", position.get("last_close", position["entry_price"]))
            equity += float(position["shares"]) * price
        desired_shares = {}
        all_codes = set(positions) | set(target_weights)
        for code in all_codes:
            if code not in rows.index:
                desired_shares[code] = int(positions.get(code, {}).get("shares", 0))
                if code in positions and code not in target_weights:
                    unresolved_sells.add(code)
                continue
            open_price = float(rows.loc[code, "open"])
            target_value = equity * float(target_weights.get(code, 0.0))
            desired_shares[code] = int(target_value / (open_price * (1 + self.config.slippage_rate)) / self.config.board_lot) * self.config.board_lot

        for code in sorted(all_codes):
            current = int(positions.get(code, {}).get("shares", 0))
            desired = desired_shares.get(code, 0)
            if current <= desired or code not in rows.index:
                continue
            if not self._can_trade(rows.loc[code], code, "SELL"):
                if desired == 0:
                    unresolved_sells.add(code)
                continue
            current_weight = current * float(rows.loc[code, "open"]) / max(equity, 1e-9)
            target_weight = float(target_weights.get(code, 0.0))
            if code in target_weights and current_weight - target_weight < self.config.rebalance_threshold:
                continue
            cash = self._record_sell(
                code,
                current - desired,
                date,
                rows.loc[code],
                positions,
                cash,
                trade_rows,
                f"REBALANCE_FROM_{pd.Timestamp(signal_date).date()}",
            )

        for code in sorted(target_weights, key=target_weights.get, reverse=True):
            if code not in rows.index or not self._can_trade(rows.loc[code], code, "BUY"):
                continue
            current = int(positions.get(code, {}).get("shares", 0))
            desired = desired_shares.get(code, 0)
            if desired <= current:
                continue
            current_weight = current * float(rows.loc[code, "open"]) / max(equity, 1e-9)
            if target_weights[code] - current_weight < self.config.rebalance_threshold:
                continue
            cash = self._record_buy(
                code,
                desired - current,
                date,
                rows.loc[code],
                positions,
                cash,
                trade_rows,
                f"REBALANCE_FROM_{pd.Timestamp(signal_date).date()}",
            )
        return cash, unresolved_sells

    def _record_buy(
        self,
        code: str,
        requested_shares: int,
        date: pd.Timestamp,
        row: pd.Series,
        positions: dict[str, dict[str, Any]],
        cash: float,
        trade_rows: list[dict[str, Any]],
        reason: str,
    ) -> float:
        lot = self.config.board_lot
        shares = int(requested_shares / lot) * lot
        fill_price = float(row["open"]) * (1.0 + self.config.slippage_rate)
        while shares >= lot:
            notional = shares * fill_price
            commission = max(self.config.minimum_commission, notional * self.config.commission_rate)
            if notional + commission <= cash + 1e-9:
                break
            shares -= lot
        if shares < lot:
            return cash
        notional = shares * fill_price
        commission = max(self.config.minimum_commission, notional * self.config.commission_rate)
        cash -= notional + commission
        atr = float(row.get("atr20_pct", np.nan))
        atr = atr if np.isfinite(atr) and atr > 0 else 0.04
        if code in positions:
            position = positions[code]
            old_shares = int(position["shares"])
            position["entry_price"] = (position["entry_price"] * old_shares + notional) / (old_shares + shares)
            position["shares"] = old_shares + shares
            position["entry_atr_pct"] = max(float(position.get("entry_atr_pct", atr)), atr)
        else:
            positions[code] = {
                "shares": shares,
                "entry_price": fill_price,
                "entry_date": date,
                "entry_atr_pct": atr,
                "peak_close": float(row["close"]),
                "last_close": float(row["close"]),
                "holding_sessions": 0,
                "below_ma60_days": 0,
            }
        trade_rows.append(
            self._trade_row(date, code, "BUY", shares, fill_price, notional, commission, 0.0, reason)
        )
        return cash

    def _record_sell(
        self,
        code: str,
        requested_shares: int,
        date: pd.Timestamp,
        row: pd.Series,
        positions: dict[str, dict[str, Any]],
        cash: float,
        trade_rows: list[dict[str, Any]],
        reason: str,
    ) -> float:
        position = positions.get(code)
        if not position:
            return cash
        shares = min(int(requested_shares), int(position["shares"]))
        if shares <= 0:
            return cash
        fill_price = float(row["open"]) * (1.0 - self.config.slippage_rate)
        notional = shares * fill_price
        commission = max(self.config.minimum_commission, notional * self.config.commission_rate)
        stamp = notional * self.config.sell_stamp_duty_rate
        cash += notional - commission - stamp
        position["shares"] = int(position["shares"]) - shares
        if position["shares"] <= 0:
            del positions[code]
        trade_rows.append(
            self._trade_row(date, code, "SELL", shares, fill_price, notional, commission, stamp, reason)
        )
        return cash

    def _sell_position(
        self,
        code: str,
        date: pd.Timestamp,
        rows: pd.DataFrame,
        positions: dict[str, dict[str, Any]],
        trade_rows: list[dict[str, Any]],
        reason: str,
        cash: float,
    ) -> tuple[float, bool]:
        if code not in rows.index or not self._can_trade(rows.loc[code], code, "SELL"):
            return cash, False
        shares = int(positions[code]["shares"])
        updated = self._record_sell(code, shares, date, rows.loc[code], positions, cash, trade_rows, reason)
        return updated, True

    def _update_position_risk(
        self,
        date: pd.Timestamp,
        rows: pd.DataFrame,
        positions: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        exits: dict[str, str] = {}
        for code, position in positions.items():
            position["holding_sessions"] = int(position.get("holding_sessions", 0)) + 1
            if code not in rows.index:
                continue
            row = rows.loc[code]
            close = float(row["close"])
            position["last_close"] = close
            position["peak_close"] = max(float(position.get("peak_close", close)), close)
            atr_pct = float(row.get("atr20_pct", position.get("entry_atr_pct", 0.04)))
            if not np.isfinite(atr_pct) or atr_pct <= 0:
                atr_pct = float(position.get("entry_atr_pct", 0.04))
            trailing_stop = float(position["peak_close"]) * (1.0 - self.config.atr_stop_multiple * atr_pct)
            if close <= trailing_stop:
                exits[code] = "ATR_TRAILING_STOP"
                continue
            ma60 = float(row.get("ma60", np.nan))
            if np.isfinite(ma60) and close < ma60:
                position["below_ma60_days"] = int(position.get("below_ma60_days", 0)) + 1
            else:
                position["below_ma60_days"] = 0
            if position["below_ma60_days"] >= self.config.trend_exit_confirm_days:
                exits[code] = "TREND_FAILURE"
                continue
            if (
                position["holding_sessions"] >= self.config.stale_position_days
                and close <= float(position["entry_price"])
            ):
                exits[code] = "STALE_POSITION"
        return exits

    def _can_trade(self, row: pd.Series, code: str, side: str) -> bool:
        if float(row.get("volume", 0.0)) <= 0 or float(row.get("amount", 0.0)) <= 0:
            return False
        open_price = float(row.get("open", np.nan))
        previous_close = float(row.get("pre_close", np.nan))
        if not np.isfinite(open_price) or open_price <= 0:
            return False
        if not np.isfinite(previous_close) or previous_close <= 0:
            return True
        limit = self._price_limit_ratio(code, bool(row.get("is_st", False)))
        tolerance = self.config.price_limit_tolerance
        if side == "BUY" and open_price >= previous_close * (1.0 + limit) * (1.0 - tolerance):
            return False
        if side == "SELL" and open_price <= previous_close * (1.0 - limit) * (1.0 + tolerance):
            return False
        return True

    @staticmethod
    def _price_limit_ratio(code: str, is_st: bool) -> float:
        if code.startswith(("300", "301", "688")):
            return 0.20
        if is_st:
            return 0.05
        return 0.10

    @staticmethod
    def _price_for_code(rows: pd.DataFrame, code: str, column: str, fallback: float) -> float:
        if code not in rows.index:
            return float(fallback)
        value = float(rows.loc[code, column])
        return value if np.isfinite(value) and value > 0 else float(fallback)

    @staticmethod
    def _trade_row(
        date: pd.Timestamp,
        code: str,
        side: str,
        shares: int,
        price: float,
        notional: float,
        commission: float,
        stamp_duty: float,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "trade_date": date,
            "stock_code": code,
            "side": side,
            "shares": shares,
            "price": price,
            "notional": notional,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "reason": reason,
        }

    @staticmethod
    def _mark_to_market(
        rows: pd.DataFrame,
        positions: dict[str, dict[str, Any]],
        cash: float,
    ) -> tuple[float, float]:
        market_value = 0.0
        for code, position in positions.items():
            fallback = float(position.get("last_close", position["entry_price"]))
            close = fallback if code not in rows.index else float(rows.loc[code, "close"])
            if not np.isfinite(close) or close <= 0:
                close = fallback
            position["last_close"] = close
            market_value += int(position["shares"]) * close
        total = cash + market_value
        exposure = market_value / total if total > 0 else 0.0
        return float(total), float(exposure)

    @staticmethod
    def _bounded_normalize(raw: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> np.ndarray:
        raw = np.asarray(raw, dtype=float)
        raw = np.where(np.isfinite(raw) & (raw > 0), raw, 1e-12)
        if lower.sum() > 1.0 + 1e-10 or upper.sum() < 1.0 - 1e-10:
            raise ValueError("infeasible factor-weight bounds")
        low, high = 0.0, 1e6
        for _ in range(100):
            scale = (low + high) / 2.0
            values = np.clip(raw * scale, lower, upper)
            if values.sum() > 1.0:
                high = scale
            else:
                low = scale
        values = np.clip(raw * ((low + high) / 2.0), lower, upper)
        residual = 1.0 - values.sum()
        if abs(residual) > 1e-10:
            capacity = (upper - values) if residual > 0 else (values - lower)
            if capacity.sum() > 0:
                values += np.sign(residual) * abs(residual) * capacity / capacity.sum()
        return values / values.sum()

    @staticmethod
    def _performance_metrics(nav: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float | int]:
        if nav.empty:
            return {}
        values = nav.set_index("trade_date")["nav"].astype(float)
        daily = values.pct_change(fill_method=None).dropna()
        total_return = float(values.iloc[-1] / values.iloc[0] - 1.0)
        elapsed_days = max((values.index[-1] - values.index[0]).days, 1)
        annual_return = float((values.iloc[-1] / values.iloc[0]) ** (365.25 / elapsed_days) - 1.0)
        drawdown = values / values.cummax() - 1.0
        annual_volatility = float(daily.std(ddof=1) * math.sqrt(252)) if len(daily) > 1 else 0.0
        sharpe = (
            float((daily.mean() * 252 - 0.02) / annual_volatility)
            if annual_volatility > 1e-12
            else 0.0
        )
        traded_notional = float(trades["notional"].sum()) if not trades.empty else 0.0
        average_nav = float(values.mean())
        years = max(elapsed_days / 365.25, 1.0 / 365.25)
        annual_turnover = traded_notional / average_nav / years if average_nav > 0 else 0.0
        return {
            "initial_nav": round(float(values.iloc[0]), 2),
            "final_nav": round(float(values.iloc[-1]), 2),
            "total_return": round(total_return, 6),
            "annual_return": round(annual_return, 6),
            "max_drawdown": round(float(drawdown.min()), 6),
            "annual_volatility": round(annual_volatility, 6),
            "sharpe": round(sharpe, 4),
            "annual_turnover": round(float(annual_turnover), 4),
            "average_exposure": round(float(nav["gross_exposure"].mean()), 6),
            "trade_count": int(len(trades)),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent adaptive A-share strategy research backtest")
    parser.add_argument("--cache", required=True, help="A-share pickle cache")
    parser.add_argument("--fundamentals", help="Point-in-time fundamentals CSV with announce_date")
    parser.add_argument("--benchmark", help="Benchmark CSV with trade_date and close")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--out-dir", default=os.path.join("tests", "dynamic_alpha_backtest"))
    parser.add_argument("--universe-limit", type=int, default=800)
    args = parser.parse_args()

    from jobs.common.a_share_panel import load_a_share_panel

    panel = load_a_share_panel(args.cache, min_history_days=0, universe_size=None)
    fundamentals = pd.read_csv(args.fundamentals) if args.fundamentals else None
    benchmark = pd.read_csv(args.benchmark) if args.benchmark else None
    strategy = DynamicAlphaStrategy(DynamicAlphaConfig(universe_limit=args.universe_limit))
    strategy.prepare(panel, fundamentals=fundamentals, benchmark=benchmark)
    result = strategy.run_backtest(args.start, args.end)
    files = result.write(args.out_dir)
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    print(json.dumps(files, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
