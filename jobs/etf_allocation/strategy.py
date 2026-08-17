from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


ETF_UNIVERSE: dict[str, dict[str, str]] = {
    "510050": {"name": "上证50ETF", "group": "宽基"},
    "510300": {"name": "沪深300ETF", "group": "宽基"},
    "510500": {"name": "中证500ETF", "group": "宽基"},
    "512100": {"name": "中证1000ETF", "group": "宽基"},
    "159915": {"name": "创业板ETF", "group": "宽基"},
    "588000": {"name": "科创50ETF", "group": "宽基"},
    "512880": {"name": "证券ETF", "group": "行业"},
    "512800": {"name": "银行ETF", "group": "行业"},
    "159928": {"name": "消费ETF", "group": "行业"},
    "512170": {"name": "医疗ETF", "group": "行业"},
    "512480": {"name": "半导体ETF", "group": "行业"},
    "512660": {"name": "军工ETF", "group": "行业"},
    "515030": {"name": "新能源车ETF", "group": "行业"},
    "515790": {"name": "光伏ETF", "group": "行业"},
    "512400": {"name": "有色金属ETF", "group": "行业"},
    "515220": {"name": "煤炭ETF", "group": "行业"},
}


@dataclass(frozen=True)
class ETFAllocationConfig:
    short_window: int = 20
    medium_window: int = 60
    long_window: int = 120
    volatility_window: int = 20
    max_broad: int = 1
    max_sector: int = 2
    normal_exposure: float = 0.80
    defensive_exposure: float = 0.0
    broad_weight_share: float = 0.50
    min_score: float = 0.0
    min_average_amount: float = 50_000_000
    min_history_days: int = 200


def prepare_snapshot(
    frames: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    config: ETFAllocationConfig | None = None,
) -> pd.DataFrame:
    config = config or ETFAllocationConfig()
    rows: list[dict[str, Any]] = []
    for code, raw in frames.items():
        frame = raw.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame = frame[frame["trade_date"] <= signal_date].sort_values("trade_date")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce")
        frame = frame.dropna(subset=["trade_date", "close"])
        if len(frame) < config.min_history_days:
            continue

        close = frame["close"]
        latest = float(close.iloc[-1])
        ret20 = float(latest / close.iloc[-1 - config.short_window] - 1)
        ret60 = float(latest / close.iloc[-1 - config.medium_window] - 1)
        ret120 = float(latest / close.iloc[-1 - config.long_window] - 1)
        ma60 = float(close.iloc[-60:].mean())
        ma120 = float(close.iloc[-config.long_window :].mean())
        prior_ma60 = float(close.iloc[-80:-20].mean())
        ma200 = float(close.iloc[-200:].mean())
        volatility = float(close.pct_change().iloc[-config.volatility_window :].std(ddof=1) * math.sqrt(252))
        amount20 = float(frame["amount"].iloc[-20:].mean()) if frame["amount"].notna().any() else np.nan
        liquid = pd.isna(amount20) or amount20 >= config.min_average_amount
        score = 0.45 * ret20 + 0.35 * ret60 + 0.20 * ret120 - 0.15 * volatility
        eligible = (
            latest > ma120
            and ma60 > ma120
            and ma60 > prior_ma60
            and ret60 > 0
            and score > config.min_score
            and liquid
        )
        meta = ETF_UNIVERSE.get(code, {"name": code, "group": "其他"})
        rows.append(
            {
                "code": code,
                "name": meta["name"],
                "group": meta["group"],
                "trade_date": frame["trade_date"].iloc[-1],
                "close": latest,
                "ret20": ret20,
                "ret60": ret60,
                "ret120": ret120,
                "ma60": ma60,
                "ma120": ma120,
                "prior_ma60": prior_ma60,
                "ma200": ma200,
                "volatility20": volatility,
                "amount20": amount20,
                "eligible": bool(eligible),
                "score": score,
            }
        )
    return pd.DataFrame(rows).sort_values(["eligible", "score"], ascending=[False, False]).reset_index(drop=True)


def build_target_weights(
    snapshot: pd.DataFrame,
    config: ETFAllocationConfig | None = None,
) -> tuple[dict[str, float], str, bool]:
    config = config or ETFAllocationConfig()
    weights = {code: 0.0 for code in ETF_UNIVERSE}
    if snapshot.empty:
        return weights, "ETF行情不足，保持现金", True

    benchmark = snapshot[snapshot["code"] == "510300"]
    defensive = bool(
        benchmark.empty
        or float(benchmark.iloc[0]["close"]) <= float(benchmark.iloc[0]["ma200"])
        or float(benchmark.iloc[0]["ma60"]) <= float(benchmark.iloc[0]["prior_ma60"])
    )
    exposure = config.defensive_exposure if defensive else config.normal_exposure
    passing = snapshot[snapshot["eligible"]].sort_values("score", ascending=False)
    broad = passing[passing["group"] == "宽基"].head(config.max_broad)
    sector = passing[passing["group"] == "行业"].head(config.max_sector)

    if exposure <= 0:
        return weights, "沪深300未通过长期趋势过滤，保持现金", defensive
    if broad.empty and sector.empty:
        return weights, "没有ETF通过趋势与流动性过滤，保持现金", defensive
    broad_exposure = exposure * config.broad_weight_share
    group_weights = [(broad, broad_exposure), (sector, exposure - broad_exposure)]

    selected_names: list[str] = []
    for group, group_exposure in group_weights:
        if group.empty:
            continue
        per_asset = group_exposure / len(group)
        for _, row in group.iterrows():
            weights[str(row["code"])] = per_asset
            selected_names.append(f"{row['name']} {per_asset:.0%}")
    regime = "防守" if defensive else "正常"
    reason = f"{regime}仓位；趋势与风险调整动量领先：" + "、".join(selected_names)
    return weights, reason, defensive
