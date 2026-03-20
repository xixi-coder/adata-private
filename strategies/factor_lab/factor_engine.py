from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd


CORE_15_FACTORS = [
    "ret_5",
    "ret_20",
    "ret_60",
    "reversal_5",
    "volatility_20",
    "downside_vol_20",
    "turnover_20_mean",
    "volume_ratio_5_20",
    "price_volume_corr_20",
    "bp",
    "ep",
    "roe",
    "net_margin",
    "op_cf_to_profit",
    "revenue_yoy",
]


def _to_datetime(df: pd.DataFrame, col: str) -> None:
    if col in df.columns and not pd.api.types.is_datetime64_any_dtype(df[col]):
        df[col] = pd.to_datetime(df[col], errors="coerce")


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denom = pd.to_numeric(denominator, errors="coerce").replace(0, np.nan)
    num = pd.to_numeric(numerator, errors="coerce")
    return num / denom


def _cross_section_zscore(series: pd.Series) -> pd.Series:
    std = series.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(np.nan, index=series.index)
    return (series - series.mean()) / std


def _board_limit_pct(stock_code: Any) -> float:
    code = str(stock_code)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("430", "8", "92")):
        return 0.30
    return 0.10


def align_financials_to_daily(
    daily_df: pd.DataFrame,
    finance_df: pd.DataFrame,
    stock_col: str = "stock_code",
    daily_date_col: str = "trade_date",
    notice_date_col: str = "notice_date",
) -> pd.DataFrame:
    """
    Align financial snapshots to daily bars by announcement date (no look-ahead).

    说明：
    - 每只股票单独做 `merge_asof`，保证时序匹配稳定。
    - 只允许使用 `notice_date <= trade_date` 的财务快照，避免未来函数。
    """
    if daily_df.empty:
        return daily_df.copy()
    if finance_df is None or finance_df.empty:
        return daily_df.copy()

    left = daily_df.copy()
    right = finance_df.copy()
    _to_datetime(left, daily_date_col)
    _to_datetime(right, notice_date_col)

    left = left.dropna(subset=[stock_col, daily_date_col]).copy()
    right = right.dropna(subset=[stock_col, notice_date_col]).copy()
    left = left.sort_values([stock_col, daily_date_col]).reset_index(drop=True)
    right = right.sort_values([stock_col, notice_date_col]).reset_index(drop=True)

    merged_parts: list[pd.DataFrame] = []
    right_groups = {code: grp for code, grp in right.groupby(stock_col, sort=False)}
    for code, left_sub in left.groupby(stock_col, sort=False):
        left_sub = left_sub.sort_values(daily_date_col)
        right_sub = right_groups.get(code)
        if right_sub is None or right_sub.empty:
            merged_parts.append(left_sub.copy())
            continue
        merged_parts.append(
            pd.merge_asof(
                left_sub,
                # 去掉右侧 stock 列，避免合并后出现重复列名。
                right_sub.drop(columns=[stock_col], errors="ignore").sort_values(notice_date_col),
                left_on=daily_date_col,
                right_on=notice_date_col,
                direction="backward",
                allow_exact_matches=True,
            )
        )

    if not merged_parts:
        return left
    merged = pd.concat(merged_parts, ignore_index=True)
    return merged.sort_values([stock_col, daily_date_col]).reset_index(drop=True)


def compute_a_share_factors(
    panel_df: pd.DataFrame,
    index_df: pd.DataFrame | None = None,
    stock_col: str = "stock_code",
    date_col: str = "trade_date",
    close_col: str = "close",
    high_col: str = "high",
    low_col: str = "low",
    volume_col: str = "volume",
    amount_col: str = "amount",
    keep_intermediate: bool = False,
) -> pd.DataFrame:
    """
    Compute daily A-share factors on a long-format panel.

    输入应为“长表”：
    - 每行是一只股票在一个交易日的记录
    - 至少包含 OHLCV + amount
    """
    required = {stock_col, date_col, close_col, high_col, low_col, volume_col, amount_col}
    missing = [c for c in required if c not in panel_df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = panel_df.copy()
    _to_datetime(df, date_col)
    df = df.sort_values([stock_col, date_col]).reset_index(drop=True)

    numeric_cols = [
        close_col,
        high_col,
        low_col,
        volume_col,
        amount_col,
        "pe",
        "pb",
        "ps",
        "roe",
        "revenue",
        "profit",
        "net_profit",
        "operating_cashflow",
        "gross_profit",
        "float_shares",
        "turnover",
        "market_cap",
        "total_liability",
        "total_assets",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 后续时序滚动都按股票分组计算，避免跨股票污染。
    g = df.groupby(stock_col, sort=False, group_keys=False)

    # Momentum and reversal
    df["ret_5"] = _safe_divide(df[close_col], g[close_col].shift(5)) - 1.0
    df["ret_20"] = _safe_divide(df[close_col], g[close_col].shift(20)) - 1.0
    df["ret_60"] = _safe_divide(df[close_col], g[close_col].shift(60)) - 1.0
    df["reversal_5"] = -df["ret_5"]

    # Return volatility factors
    df["ret_1d"] = g[close_col].pct_change()
    df["volatility_20"] = g["ret_1d"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True)
    df["volatility_60"] = g["ret_1d"].rolling(60, min_periods=20).std().reset_index(level=0, drop=True)
    df["_down_ret"] = df["ret_1d"].where(df["ret_1d"] < 0)
    df["downside_vol_20"] = g["_down_ret"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True)

    # Drawdown
    df["_roll_max_60"] = g[close_col].rolling(60, min_periods=20).max().reset_index(level=0, drop=True)
    df["_drawdown"] = _safe_divide(df[close_col], df["_roll_max_60"]) - 1.0
    df["max_drawdown_60"] = g["_drawdown"].rolling(60, min_periods=20).min().reset_index(level=0, drop=True)

    # Turnover and volume/amount behavior
    if "turnover" not in df.columns and "float_shares" in df.columns:
        df["turnover"] = _safe_divide(df[volume_col], df["float_shares"])
    if "turnover" in df.columns:
        df["turnover_20_mean"] = g["turnover"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
        df["turnover_std_20"] = g["turnover"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True)

    df["volume_ratio_5_20"] = _safe_divide(
        g[volume_col].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True),
        g[volume_col].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True),
    )
    df["_volume_ret"] = g[volume_col].pct_change()
    # `rolling.corr` 在大表上直接 groupby 赋值容易索引错位，逐股票拼接更稳。
    pv_corr_parts = []
    for _, sub in df.groupby(stock_col, sort=False):
        pv_corr_parts.append(sub["ret_1d"].rolling(20, min_periods=10).corr(sub["_volume_ret"]))
    if pv_corr_parts:
        df["price_volume_corr_20"] = pd.concat(pv_corr_parts).sort_index()
    else:
        df["price_volume_corr_20"] = np.nan
    df["amt_trend_20"] = _safe_divide(
        df[amount_col], g[amount_col].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    )

    # Relative return vs index
    if index_df is not None and not index_df.empty:
        idx = index_df.copy()
        if date_col not in idx.columns:
            raise ValueError(f"Index dataframe must contain `{date_col}`.")
        if close_col not in idx.columns:
            raise ValueError(f"Index dataframe must contain `{close_col}`.")
        _to_datetime(idx, date_col)
        idx = idx[[date_col, close_col]].dropna().sort_values(date_col).drop_duplicates(date_col)
        idx["index_ret_20"] = idx[close_col].pct_change(20)
        df = df.merge(idx[[date_col, "index_ret_20"]], on=date_col, how="left")
        df["excess_ret_20"] = df["ret_20"] - df["index_ret_20"]

    # Valuation (inverse to align direction)
    if "pb" in df.columns:
        df["bp"] = np.where(df["pb"] > 0, 1.0 / df["pb"], np.nan)
    if "pe" in df.columns:
        df["ep"] = np.where(df["pe"] > 0, 1.0 / df["pe"], np.nan)
    if "ps" in df.columns:
        df["sp"] = np.where(df["ps"] > 0, 1.0 / df["ps"], np.nan)
    if "operating_cashflow" in df.columns and "market_cap" in df.columns:
        df["ocfp"] = _safe_divide(df["operating_cashflow"], df["market_cap"])

    # Quality and growth
    if "gross_profit" in df.columns and "revenue" in df.columns:
        df["gross_margin"] = _safe_divide(df["gross_profit"], df["revenue"])
    if "net_profit" in df.columns and "revenue" in df.columns:
        df["net_margin"] = _safe_divide(df["net_profit"], df["revenue"])
    elif "profit" in df.columns and "revenue" in df.columns:
        df["net_margin"] = _safe_divide(df["profit"], df["revenue"])

    profit_col = "net_profit" if "net_profit" in df.columns else ("profit" if "profit" in df.columns else "")
    if "operating_cashflow" in df.columns and profit_col:
        df["op_cf_to_profit"] = _safe_divide(df["operating_cashflow"], df[profit_col])

    if "total_liability" in df.columns and "total_assets" in df.columns:
        df["asset_liability_ratio"] = _safe_divide(df["total_liability"], df["total_assets"])

    if "revenue" in df.columns:
        df["revenue_yoy"] = _safe_divide(df["revenue"], g["revenue"].shift(4)) - 1.0
    if "net_profit" in df.columns:
        df["profit_yoy"] = _safe_divide(df["net_profit"], g["net_profit"].shift(4)) - 1.0
    elif "profit" in df.columns:
        df["profit_yoy"] = _safe_divide(df["profit"], g["profit"].shift(4)) - 1.0

    if not keep_intermediate:
        drop_cols = ["ret_1d", "_down_ret", "_roll_max_60", "_drawdown", "_volume_ret"]
        existing = [c for c in drop_cols if c in df.columns]
        if existing:
            df = df.drop(columns=existing)

    return df


def apply_universe_filters(
    panel_df: pd.DataFrame,
    stock_col: str = "stock_code",
    date_col: str = "trade_date",
    close_col: str = "close",
    volume_col: str = "volume",
    amount_col: str = "amount",
    min_market_cap: float | None = None,
    min_amount: float | None = None,
    exclude_suspended: bool = True,
    exclude_limit_hits: bool = True,
    pre_close_col: str = "pre_close",
    keep_flag: bool = True,
) -> pd.DataFrame:
    """
    Apply practical A-share universe filters (liquidity, suspension, limit-hit days).
    """
    df = panel_df.copy()
    _to_datetime(df, date_col)
    df = df.sort_values([stock_col, date_col]).reset_index(drop=True)

    mask = pd.Series(True, index=df.index)

    if min_market_cap is not None and "market_cap" in df.columns:
        mask &= pd.to_numeric(df["market_cap"], errors="coerce") >= float(min_market_cap)

    if min_amount is not None and amount_col in df.columns:
        mask &= pd.to_numeric(df[amount_col], errors="coerce") >= float(min_amount)

    if exclude_suspended:
        if volume_col in df.columns:
            mask &= pd.to_numeric(df[volume_col], errors="coerce").fillna(0.0) > 0
        if amount_col in df.columns:
            mask &= pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0) > 0
        if close_col in df.columns:
            mask &= pd.to_numeric(df[close_col], errors="coerce").fillna(0.0) > 0

    if exclude_limit_hits and close_col in df.columns:
        g = df.groupby(stock_col, sort=False, group_keys=False)
        if pre_close_col in df.columns:
            pre_close = pd.to_numeric(df[pre_close_col], errors="coerce")
        else:
            pre_close = g[close_col].shift(1)
        limit_pct = df[stock_col].map(_board_limit_pct).astype(float)
        chg = _safe_divide(df[close_col], pre_close) - 1.0
        limit_hit = chg.abs() >= (limit_pct * 0.995)
        mask &= ~limit_hit.fillna(False)

    if keep_flag:
        df["is_tradeable"] = mask
        return df
    return df.loc[mask].copy()


def _neutralize_single_factor(
    panel_df: pd.DataFrame,
    factor_col: str,
    date_col: str,
    industry_col: str,
    style_cols: Iterable[str] | None = None,
) -> pd.Series:
    out = pd.Series(np.nan, index=panel_df.index)
    style_cols = list(style_cols or [])

    for _, cross in panel_df.groupby(date_col, sort=False):
        y = pd.to_numeric(cross[factor_col], errors="coerce")
        if y.notna().sum() < 5:
            continue

        if industry_col not in cross.columns:
            continue
        industry = cross[industry_col].astype(str).replace("nan", np.nan)
        valid = y.notna() & industry.notna()
        if valid.sum() < 5:
            continue

        sub = cross.loc[valid].copy()
        sub_y = y.loc[valid]
        ind_dummies = pd.get_dummies(sub[industry_col].astype(str), prefix="ind", drop_first=True, dtype=float)

        x_parts = [pd.Series(1.0, index=sub.index, name="const"), ind_dummies]
        for style in style_cols:
            if style not in sub.columns:
                continue
            s = pd.to_numeric(sub[style], errors="coerce")
            s = s.replace([np.inf, -np.inf], np.nan)
            s_std = s.std(ddof=0)
            if pd.notna(s_std) and s_std > 0:
                s = (s - s.mean()) / s_std
            s = s.fillna(0.0)
            x_parts.append(s.rename(style))

        x = pd.concat(x_parts, axis=1).astype(float)
        if x.shape[1] == 1:
            # Only constant, equivalent to de-mean.
            out.loc[sub.index] = sub_y - sub_y.mean()
            continue

        if len(sub_y) <= x.shape[1] + 1:
            # Sample too small for OLS: fallback to industry de-mean.
            out.loc[sub.index] = sub_y - sub.groupby(industry_col)[factor_col].transform("mean")
            continue

        beta, *_ = np.linalg.lstsq(x.to_numpy(), sub_y.to_numpy(), rcond=None)
        fitted = x.to_numpy() @ beta
        residual = sub_y.to_numpy() - fitted
        out.loc[sub.index] = residual

    return out


def preprocess_factors(
    panel_df: pd.DataFrame,
    factor_cols: Iterable[str],
    date_col: str = "trade_date",
    industry_col: str = "industry",
    winsorize: bool = True,
    lower_q: float = 0.01,
    upper_q: float = 0.99,
    standardize: bool = True,
    neutralize: bool = False,
    neutralize_style_cols: Iterable[str] | None = ("market_cap",),
) -> pd.DataFrame:
    """
    Cross-sectional preprocessing: winsorize, z-score, industry neutralization.
    """
    df = panel_df.copy()
    _to_datetime(df, date_col)

    for col in factor_cols:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        df[col] = series

        if winsorize:
            df[col] = df.groupby(date_col, sort=False)[col].transform(
                lambda s: s.clip(lower=s.quantile(lower_q), upper=s.quantile(upper_q))
            )

        if standardize:
            df[col] = df.groupby(date_col, sort=False)[col].transform(_cross_section_zscore)

        if neutralize and industry_col in df.columns:
            df[col] = _neutralize_single_factor(
                df,
                factor_col=col,
                date_col=date_col,
                industry_col=industry_col,
                style_cols=neutralize_style_cols,
            )

    return df


def add_forward_returns(
    panel_df: pd.DataFrame,
    horizons: Iterable[int] = (5,),
    stock_col: str = "stock_code",
    close_col: str = "close",
) -> pd.DataFrame:
    """
    Add forward return columns: future_ret_{h}.
    """
    df = panel_df.copy()
    g = df.groupby(stock_col, sort=False, group_keys=False)
    for h in sorted({int(x) for x in horizons if int(x) > 0}):
        df[f"future_ret_{h}"] = _safe_divide(g[close_col].shift(-h), df[close_col]) - 1.0
    return df


def evaluate_factor_ic(
    panel_df: pd.DataFrame,
    factor_col: str,
    date_col: str = "trade_date",
    stock_col: str = "stock_code",
    horizon: int = 5,
    forward_ret_col: str | None = None,
    min_obs: int = 30,
) -> dict[str, Any]:
    """
    Daily cross-sectional IC + RankIC summary.
    """
    if factor_col not in panel_df.columns:
        raise ValueError(f"Factor column not found: {factor_col}")

    target_col = forward_ret_col or f"future_ret_{horizon}"
    df = panel_df if target_col in panel_df.columns else add_forward_returns(panel_df, horizons=(horizon,), stock_col=stock_col)
    if target_col not in df.columns:
        raise ValueError(f"Forward return column not found: {target_col}")

    use = df[[date_col, factor_col, target_col]].replace([np.inf, -np.inf], np.nan)

    ic_rows: list[dict[str, Any]] = []
    for dt_value, cross in use.groupby(date_col, sort=False):
        cross = cross.dropna(subset=[factor_col, target_col])
        if len(cross) < min_obs:
            continue
        ic = cross[factor_col].corr(cross[target_col], method="pearson")
        rank_ic = cross[factor_col].rank(method="average").corr(
            cross[target_col].rank(method="average"), method="pearson"
        )
        ic_rows.append({"trade_date": dt_value, "ic": ic, "rank_ic": rank_ic, "count": len(cross)})

    ic_df = pd.DataFrame(ic_rows)
    if ic_df.empty:
        return {
            "factor": factor_col,
            "forward_col": target_col,
            "obs_days": 0,
            "ic_mean": np.nan,
            "ic_std": np.nan,
            "ic_ir": np.nan,
            "ic_positive_rate": np.nan,
            "rank_ic_mean": np.nan,
            "rank_ic_std": np.nan,
            "rank_ic_ir": np.nan,
            "ic_series": ic_df,
        }

    ic_mean = float(ic_df["ic"].mean())
    ic_std = float(ic_df["ic"].std(ddof=0))
    rank_ic_mean = float(ic_df["rank_ic"].mean())
    rank_ic_std = float(ic_df["rank_ic"].std(ddof=0))
    ic_ir = ic_mean / ic_std * np.sqrt(252) if ic_std > 0 else np.nan
    rank_ic_ir = rank_ic_mean / rank_ic_std * np.sqrt(252) if rank_ic_std > 0 else np.nan

    return {
        "factor": factor_col,
        "forward_col": target_col,
        "obs_days": int(len(ic_df)),
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "ic_ir": float(ic_ir) if pd.notna(ic_ir) else np.nan,
        "ic_positive_rate": float((ic_df["ic"] > 0).mean()),
        "rank_ic_mean": rank_ic_mean,
        "rank_ic_std": rank_ic_std,
        "rank_ic_ir": float(rank_ic_ir) if pd.notna(rank_ic_ir) else np.nan,
        "ic_series": ic_df,
    }


def quantile_group_test(
    panel_df: pd.DataFrame,
    factor_col: str,
    date_col: str = "trade_date",
    stock_col: str = "stock_code",
    horizon: int = 5,
    forward_ret_col: str | None = None,
    n_groups: int = 10,
    min_obs: int = 30,
) -> dict[str, Any]:
    """
    Group test by daily cross-sectional quantiles.
    """
    if factor_col not in panel_df.columns:
        raise ValueError(f"Factor column not found: {factor_col}")

    target_col = forward_ret_col or f"future_ret_{horizon}"
    df = panel_df if target_col in panel_df.columns else add_forward_returns(panel_df, horizons=(horizon,), stock_col=stock_col)
    if target_col not in df.columns:
        raise ValueError(f"Forward return column not found: {target_col}")

    rows: list[dict[str, Any]] = []
    for dt_value, cross in df.groupby(date_col, sort=False):
        cross = cross[[factor_col, target_col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(cross) < max(min_obs, n_groups):
            continue
        ranked = cross[factor_col].rank(method="first")
        try:
            bucket = pd.qcut(ranked, q=n_groups, labels=False, duplicates="drop") + 1
        except ValueError:
            continue
        cross = cross.assign(_group=bucket.values)
        grp_ret = cross.groupby("_group")[target_col].mean()
        row: dict[str, Any] = {"trade_date": dt_value, "count": len(cross)}
        for i in range(1, n_groups + 1):
            row[f"group_{i}"] = float(grp_ret.get(i, np.nan))
        row["long_short"] = row.get(f"group_{n_groups}", np.nan) - row.get("group_1", np.nan)
        rows.append(row)

    group_df = pd.DataFrame(rows)
    if group_df.empty:
        return {
            "factor": factor_col,
            "forward_col": target_col,
            "obs_days": 0,
            "long_short_mean": np.nan,
            "long_short_std": np.nan,
            "long_short_ir": np.nan,
            "group_return_series": group_df,
        }

    ls_mean = float(group_df["long_short"].mean())
    ls_std = float(group_df["long_short"].std(ddof=0))
    ls_ir = ls_mean / ls_std * np.sqrt(252) if ls_std > 0 else np.nan

    return {
        "factor": factor_col,
        "forward_col": target_col,
        "obs_days": int(len(group_df)),
        "long_short_mean": ls_mean,
        "long_short_std": ls_std,
        "long_short_ir": float(ls_ir) if pd.notna(ls_ir) else np.nan,
        "group_return_series": group_df,
    }


def run_core_15_pipeline(
    panel_df: pd.DataFrame,
    index_df: pd.DataFrame | None = None,
    stock_col: str = "stock_code",
    date_col: str = "trade_date",
    industry_col: str = "industry",
    horizon: int = 5,
    min_market_cap: float | None = None,
    min_amount: float | None = None,
    neutralize: bool = True,
) -> dict[str, Any]:
    """
    End-to-end helper for the 15-factor starter set:
    1) factor calculation
    2) universe filtering
    3) winsorize/standardize/(optional) industry neutralization
    4) IC and quantile-group diagnostics
    """
    panel = compute_a_share_factors(
        panel_df=panel_df,
        index_df=index_df,
        stock_col=stock_col,
        date_col=date_col,
    )
    panel = apply_universe_filters(
        panel,
        stock_col=stock_col,
        date_col=date_col,
        min_market_cap=min_market_cap,
        min_amount=min_amount,
        keep_flag=False,
    )

    factors = [f for f in CORE_15_FACTORS if f in panel.columns]
    panel = preprocess_factors(
        panel,
        factor_cols=factors,
        date_col=date_col,
        industry_col=industry_col,
        winsorize=True,
        standardize=True,
        neutralize=neutralize and (industry_col in panel.columns),
    )
    panel = add_forward_returns(panel, horizons=(horizon,), stock_col=stock_col)

    ic_summary: list[dict[str, Any]] = []
    group_summary: list[dict[str, Any]] = []
    for factor in factors:
        ic_result = evaluate_factor_ic(
            panel_df=panel,
            factor_col=factor,
            date_col=date_col,
            stock_col=stock_col,
            horizon=horizon,
        )
        group_result = quantile_group_test(
            panel_df=panel,
            factor_col=factor,
            date_col=date_col,
            stock_col=stock_col,
            horizon=horizon,
            n_groups=10,
        )
        ic_summary.append({k: v for k, v in ic_result.items() if k != "ic_series"})
        group_summary.append({k: v for k, v in group_result.items() if k != "group_return_series"})

    return {
        "panel": panel,
        "factors": factors,
        "ic_summary": pd.DataFrame(ic_summary).sort_values("ic_mean", ascending=False),
        "group_summary": pd.DataFrame(group_summary).sort_values("long_short_mean", ascending=False),
    }
