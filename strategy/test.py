# -*- coding: utf-8 -*-

"""
512890 红利低波ETF RSI择时策略

规则:
RSI(16 Wilder)

RSI < 40:
    满仓

RSI > 72:
    80%

RSI > 82:
    50%

RSI > 92:
    清仓


交易:
A股T+1:
今天收盘产生信号
下一交易日开盘调整仓位


收益:
昨日收盘
    ↓
今日开盘 (隔夜)
    ↓
今日收盘 (日内)

本金:
100万元
"""


import akshare as ak
import pandas as pd
import numpy as np



# =====================
# 参数
# =====================


INIT_CAPITAL = 1_000_000

SYMBOL = "sh512890"

START_DATE = "2019-01-18"

END_DATE = "2026-08-07"



# =====================
# 获取数据
# =====================


print("加载行情...")


df = ak.fund_etf_hist_sina(
    symbol=SYMBOL
)


df["open"] = pd.to_numeric(df["open"])

df["close"] = pd.to_numeric(df["close"])

df["date"] = pd.to_datetime(df["date"])


df = (
    df
    .sort_values("date")
    .reset_index(drop=True)
)


df = df[
    (df.date >= START_DATE)
    &
    (df.date <= END_DATE)
]


df = (
    df
    .reset_index(drop=True)
)


print(
    "行情数量:",
    len(df)
)



# =====================
# RSI Wilder
# =====================


def calc_rsi(
        close,
        n=16
):

    delta = close.diff()


    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )


    avg_gain = gain.copy()

    avg_loss = loss.copy()


    avg_gain.iloc[:]=np.nan

    avg_loss.iloc[:]=np.nan



    avg_gain.iloc[n] = (
        gain.iloc[1:n+1]
        .mean()
    )


    avg_loss.iloc[n] = (
        loss.iloc[1:n+1]
        .mean()
    )


    for i in range(n+1,len(close)):

        avg_gain.iloc[i] = (
            avg_gain.iloc[i-1]*(n-1)
            +
            gain.iloc[i]
        ) / n


        avg_loss.iloc[i] = (
            avg_loss.iloc[i-1]*(n-1)
            +
            loss.iloc[i]
        ) / n



    rs = (
        avg_gain /
        avg_loss
    )


    rsi = (
        100 -
        100/(1+rs)
    )


    return rsi



df["rsi"] = calc_rsi(
    df.close,
    16
)




# =====================
# RSI -> 仓位
# =====================


def get_target_weight(r):


    if np.isnan(r):

        return np.nan


    if r < 40:

        return 1.0


    if r > 92:

        return 0


    if r > 82:

        return 0.5


    if r > 72:

        return 0.8


    return np.nan



df["target"] = (
    df.rsi
    .apply(get_target_weight)
)




# =====================
# T+1 回测
# =====================


asset = INIT_CAPITAL

weight = 0


assets = [

    asset

]


trade_count = 0


for i in range(
    1,
    len(df)
):


    # -----------------
    # 隔夜收益
    # -----------------

    overnight = (
        df.open.iloc[i]
        /
        df.close.iloc[i-1]
        -
        1
    )


    asset *= (
        1
        +
        weight *
        overnight
    )



    # -----------------
    # 今日开盘执行昨日信号
    # -----------------

    old_weight = weight


    if not np.isnan(
        df.target.iloc[i-1]
    ):

        weight = (
            df.target.iloc[i-1]
        )


    if weight != old_weight:

        trade_count += 1



    # -----------------
    # 日内收益
    # -----------------

    intraday = (
        df.close.iloc[i]
        /
        df.open.iloc[i]
        -
        1
    )


    asset *= (
        1
        +
        weight *
        intraday
    )


    assets.append(asset)



df["asset"] = assets



# =====================
# 基础统计
# =====================


final_asset = df.asset.iloc[-1]


total_return = (
    final_asset /
    INIT_CAPITAL
    -
    1
)


years = (
    df.date.iloc[-1]
    -
    df.date.iloc[0]
).days / 365



cagr = (
    final_asset /
    INIT_CAPITAL
) ** (
    1/years
) - 1



# 最大回撤

high = (
    df.asset
    .cummax()
)


drawdown = (
    df.asset /
    high
    -
    1
)


max_dd = drawdown.min()



print("\n======================")

print("策略结果")

print("======================")


print(
    f"初始资金: {INIT_CAPITAL:,.0f}"
)


print(
    f"最终资产: {final_asset:,.2f}"
)


print(
    f"累计收益: {total_return:.2%}"
)


print(
    f"年化收益: {cagr:.2%}"
)


print(
    f"最大回撤: {max_dd:.2%}"
)


print(
    f"调仓次数: {trade_count}"
)



# =====================
# 周/月/年收益
# =====================


df = (
    df
    .set_index("date")
)



def period_return(freq):


    nav = (
        df.asset
        .resample(freq)
        .last()
    )


    ret = (
        nav /
        nav.shift(1)
        -
        1
    )


    return ret.dropna()



print("\n========== 年收益 ==========")

print(
    period_return("YE")
)



print("\n========== 月收益 ==========")

print(
    period_return("ME")
)



print("\n========== 最近20周收益 ==========")

print(
    period_return("W")
    .tail(20)
)