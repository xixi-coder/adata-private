# -*- coding: utf-8 -*-
"""
512890 红利低波 ETF RSI 策略回测。

回测口径：
1. 使用日线收盘价计算 Wilder RSI(16)。
2. 当日收盘生成信号，下一交易日开盘成交。
3. 使用现金和 ETF 份额逐日记账，隔夜收益自然计入账户净值。
4. ETF 按 100 份一手买入，持仓只能在后续交易日卖出，符合 A 股 ETF 的 T+1 习惯。
5. 输出每日净值以及每周、每月、每年收益报告。

注意：历史接口可能滞后。脚本在收盘后尝试用新浪行情接口补充最新交易日。
"""
from pathlib import Path
import re
from datetime import datetime

import akshare as ak
import matplotlib
import numpy as np
import pandas as pd
import requests

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


SYMBOL = "512890"
SINA_SYMBOL = "sh512890"
START_DATE = pd.Timestamp("2019-01-18")
INITIAL_CAPITAL = 1_000_000.0
LOT_SIZE = 100

# 参数优化采用滚动样本外结果稳定的区域，而非全历史单点最优值。
RSI_PERIOD = 12
FIRST_ENTRY_RSI = 25
RECOVER_RSI = 35
TAKE_PROFIT_1_RSI = 76
TAKE_PROFIT_2_RSI = 80
TAKE_PROFIT_3_RSI = 92

# ETF 通常没有印花税；佣金按参数计入，按实际账户情况调整。
COMMISSION_RATE = 0.0003
OUTPUT_DIR = Path("backtest_512890_output")

# 512890 在 2021-10-25 实施过 1 拆 2。价格序列需要前复权，账本份额需要翻倍。
# 若未来发生新的份额拆分，在这里追加日期和倍数。
SPLITS = {pd.Timestamp("2021-10-25"): 2.0}


def _append_latest_quote(df):
    """历史接口滞后且已收盘时，用新浪行情补充最新交易日。"""
    try:
        response = requests.get(
            f"https://hq.sinajs.cn/list={SINA_SYMBOL}",
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        )
        match = re.search(r'var hq_str_sh512890="([^"]*)"', response.text)
        fields = match.group(1).split(",") if match else []
        if len(fields) < 32:
            return df

        quote_date = pd.Timestamp(fields[30])
        quote_time = fields[31]
        quote_close = float(fields[3])
        if quote_time < "15:00:00" or quote_close <= 0:
            return df
        if quote_date <= df["date"].iloc[-1]:
            return df

        latest = pd.DataFrame(
            {
                "date": [quote_date],
                "open": [float(fields[1])],
                "close": [quote_close],
                "high": [float(fields[4])],
                "low": [float(fields[5])],
            }
        )
        return pd.concat([df, latest], ignore_index=True)
    except (requests.RequestException, ValueError, IndexError):
        return df


def load_data():
    df = ak.fund_etf_hist_sina(symbol=SINA_SYMBOL)
    numeric_columns = ["open", "close", "high", "low"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").dropna(subset=numeric_columns).reset_index(drop=True)
    df = _append_latest_quote(df)
    end_date = pd.Timestamp(datetime.now().date())
    return df[(df["date"] >= START_DATE) & (df["date"] <= end_date)].reset_index(drop=True)


def calculate_wilder_rsi(close, period):
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[0] = loss[0] = np.nan

    average_gain = np.full(len(close), np.nan)
    average_loss = np.full(len(close), np.nan)
    average_gain[period] = np.nanmean(gain[1 : period + 1])
    average_loss[period] = np.nanmean(loss[1 : period + 1])
    for index in range(period + 1, len(close)):
        average_gain[index] = (
            average_gain[index - 1] * (period - 1) + gain[index]
        ) / period
        average_loss[index] = (
            average_loss[index - 1] * (period - 1) + loss[index]
        ) / period

    relative_strength = np.divide(
        average_gain,
        average_loss,
        out=np.full(len(close), np.inf),
        where=average_loss != 0,
    )
    rsi = 100 - 100 / (1 + relative_strength)
    rsi[np.isnan(average_gain) | np.isnan(average_loss)] = np.nan
    return rsi


def adjust_close_for_splits(df):
    """将拆分日前价格前复权，避免拆分造成虚假的 RSI 极端信号。"""
    adjusted = df["close"].to_numpy(float).copy()
    for split_date, ratio in sorted(SPLITS.items()):
        adjusted[df["date"].to_numpy() < split_date] /= ratio
    return adjusted


def build_target_weights(rsi):
    """生成收盘信号对应的目标仓位；实际交易在下一交易日开盘执行。"""
    target = np.zeros(len(rsi), dtype=float)
    current = 0.0
    has_entered = False

    for index, value in enumerate(rsi):
        if np.isnan(value):
            target[index] = current
            continue

        if current == 0.0:
            # 首次入场使用文档的 RSI<30；清仓后再次回补使用 RSI<40。
            if (not has_entered and value < FIRST_ENTRY_RSI) or (
                has_entered and value < RECOVER_RSI
            ):
                current = 1.0
                has_entered = True
        elif value < RECOVER_RSI:
            current = 1.0
        elif value > TAKE_PROFIT_3_RSI:
            current = 0.0
        elif value > TAKE_PROFIT_2_RSI:
            current = 0.5
        elif value > TAKE_PROFIT_1_RSI:
            current = 0.8

        target[index] = current

    return target


def trade_to_target(cash, shares, target_weight, open_price):
    """在开盘将仓位调整到目标比例，返回交易后的现金和份额。"""
    equity_at_open = cash + shares * open_price
    target_shares = int(
        equity_at_open * target_weight / open_price / LOT_SIZE
    ) * LOT_SIZE

    if target_shares < shares:
        sold_shares = shares - target_shares
        proceeds = sold_shares * open_price
        cash += proceeds * (1 - COMMISSION_RATE)
        shares = target_shares
    elif target_shares > shares:
        requested_shares = target_shares - shares
        affordable_shares = int(
            cash / (open_price * (1 + COMMISSION_RATE)) / LOT_SIZE
        ) * LOT_SIZE
        bought_shares = min(requested_shares, affordable_shares)
        cost = bought_shares * open_price
        cash -= cost * (1 + COMMISSION_RATE)
        shares += bought_shares

    return cash, shares


def run_backtest(df):
    close = df["close"].to_numpy(float)
    open_price = df["open"].to_numpy(float)
    adjusted_close = adjust_close_for_splits(df)
    rsi = calculate_wilder_rsi(adjusted_close, RSI_PERIOD)
    target = build_target_weights(rsi)

    cash = INITIAL_CAPITAL
    shares = 0
    equity = np.full(len(df), np.nan)
    executed_weight = np.zeros(len(df), dtype=float)
    cash_history = np.full(len(df), np.nan)
    shares_history = np.zeros(len(df), dtype=int)

    for index in range(len(df)):
        split_ratio = SPLITS.get(df["date"].iloc[index], 1.0)
        if split_ratio != 1.0:
            shares = int(shares * split_ratio)
        # 使用前一日收盘信号在今日开盘交易，保证当天新买入份额不再卖出。
        if index > 0:
            cash, shares = trade_to_target(
                cash, shares, target[index - 1], open_price[index]
            )
        equity[index] = cash + shares * close[index]
        cash_history[index] = cash
        shares_history[index] = shares
        executed_weight[index] = (
            shares * close[index] / equity[index] if equity[index] else 0.0
        )

    result = df[["date", "open", "close"]].copy()
    result["adjusted_close"] = adjusted_close
    result["rsi"] = rsi
    result["target_weight"] = target
    result["shares"] = shares_history
    result["cash"] = cash_history
    result["equity"] = equity
    result["executed_weight"] = executed_weight
    return result


def period_report(result, frequency):
    grouped = result.assign(
        period=result["date"].dt.to_period(frequency)
    ).groupby("period", sort=True)
    period_end = grouped["date"].last().reset_index(drop=True)
    equity = grouped["equity"].last().reset_index(drop=True)
    previous = equity.shift(1).fillna(INITIAL_CAPITAL)
    report = pd.DataFrame(
        {
            "period_end": period_end,
            "start_equity": previous,
            "end_equity": equity,
        }
    )
    report["profit"] = report["end_equity"] - report["start_equity"]
    report["return"] = report["end_equity"] / report["start_equity"] - 1
    return report


def print_report(name, report, tail=None):
    if tail is not None:
        report = report.tail(tail)
    display = report.copy()
    display["period_end"] = display["period_end"].dt.strftime("%Y-%m-%d")
    display["start_equity"] = display["start_equity"].map(lambda x: f"{x:,.2f}")
    display["end_equity"] = display["end_equity"].map(lambda x: f"{x:,.2f}")
    display["profit"] = display["profit"].map(lambda x: f"{x:+,.2f}")
    display["return"] = display["return"].map(lambda x: f"{x:+.2%}")
    print(f"\n{name}")
    print(display.to_string(index=False))


def _set_chart_style():
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["PingFang SC", "Arial Unicode MS", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "axes.titlesize": 14,
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
        }
    )


def plot_equity_and_drawdown(result):
    _set_chart_style()
    dates = result["date"]
    equity = result["equity"]
    drawdown = equity / equity.cummax() - 1

    figure, axes = plt.subplots(
        2,
        1,
        figsize=(14, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]},
    )
    axes[0].plot(dates, equity / 10000, color="#155e75", linewidth=1.8)
    axes[0].axhline(
        INITIAL_CAPITAL / 10000,
        color="#64748b",
        linestyle="--",
        linewidth=1,
        label="Initial capital",
    )
    axes[0].set_ylabel("Equity (10k CNY)")
    axes[0].set_title(f"512890 RSI({RSI_PERIOD}) Strategy: Equity and Drawdown")
    axes[0].legend(loc="upper left", frameon=False)

    axes[1].fill_between(dates, drawdown * 100, 0, color="#dc2626", alpha=0.25)
    axes[1].plot(dates, drawdown * 100, color="#b91c1c", linewidth=1)
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}%"))
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    figure.tight_layout()
    path = OUTPUT_DIR / "equity_drawdown.png"
    figure.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(figure)
    return path


def plot_annual_returns(report):
    _set_chart_style()
    report = report.copy()
    labels = report["period_end"].dt.year.astype(str)
    returns = report["return"] * 100
    colors = np.where(returns >= 0, "#0f766e", "#dc2626")

    figure, axis = plt.subplots(figsize=(12, 5.5))
    bars = axis.bar(labels, returns, color=colors, width=0.68)
    axis.axhline(0, color="#334155", linewidth=0.8)
    axis.set_title(f"512890 RSI({RSI_PERIOD}) Strategy: Annual Returns")
    axis.set_ylabel("Return (%)")
    axis.set_ylim(
        min(-3, float(returns.min()) - 3),
        max(3, float(returns.max()) + 3),
    )
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}%"))
    for bar, value in zip(bars, returns):
        offset = 0.7 if value >= 0 else -0.7
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value + offset,
            f"{value:+.1f}%",
            ha="center",
            va="bottom" if value >= 0 else "top",
            fontsize=9,
        )
    figure.tight_layout()
    path = OUTPUT_DIR / "annual_returns.png"
    figure.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(figure)
    return path


def plot_monthly_heatmap(report):
    _set_chart_style()
    monthly = report.copy()
    monthly["year"] = monthly["period_end"].dt.year
    monthly["month"] = monthly["period_end"].dt.month
    table = monthly.pivot(index="year", columns="month", values="return")
    table = table.reindex(columns=range(1, 13))
    values = table.to_numpy(dtype=float) * 100
    limit = max(5, float(np.nanmax(np.abs(values))))

    figure, axis = plt.subplots(figsize=(13, 5.8))
    image = axis.imshow(
        values,
        cmap="RdYlGn",
        aspect="auto",
        vmin=-limit,
        vmax=limit,
    )
    axis.set_title(f"512890 RSI({RSI_PERIOD}) Strategy: Monthly Returns")
    axis.set_xlabel("Month")
    axis.set_ylabel("Year")
    axis.set_xticks(range(12), [str(month) for month in range(1, 13)])
    axis.set_yticks(range(len(table.index)), [str(year) for year in table.index])
    figure.colorbar(image, ax=axis, label="Return (%)", pad=0.02)

    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            value = values[row, column]
            if np.isnan(value):
                label = "-"
            else:
                label = f"{value:+.1f}%"
            axis.text(column, row, label, ha="center", va="center", fontsize=8)

    figure.tight_layout()
    path = OUTPUT_DIR / "monthly_heatmap.png"
    figure.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(figure)
    return path


def plot_weekly_returns(report):
    _set_chart_style()
    report = report.tail(52).copy()
    labels = report["period_end"].dt.strftime("%m-%d")
    returns = report["return"] * 100
    colors = np.where(returns >= 0, "#0f766e", "#dc2626")

    figure, axis = plt.subplots(figsize=(14, 5.5))
    axis.bar(np.arange(len(report)), returns, color=colors, width=0.8)
    axis.axhline(0, color="#334155", linewidth=0.8)
    axis.set_title(f"512890 RSI({RSI_PERIOD}) Strategy: Last 52 Weekly Returns")
    axis.set_ylabel("Return (%)")
    axis.set_xticks(np.arange(len(report)), labels, rotation=60, ha="right")
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}%"))
    figure.tight_layout()
    path = OUTPUT_DIR / "weekly_returns_last_52.png"
    figure.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(figure)
    return path


def plot_charts(result, reports):
    return [
        plot_equity_and_drawdown(result),
        plot_annual_returns(reports["yearly"]),
        plot_monthly_heatmap(reports["monthly"]),
        plot_weekly_returns(reports["weekly"]),
    ]


def main():
    df = load_data()
    if len(df) <= RSI_PERIOD:
        raise RuntimeError("历史数据不足，无法计算 RSI")

    result = run_backtest(df)
    final_equity = result["equity"].iloc[-1]
    total_return = final_equity / INITIAL_CAPITAL - 1
    print(
        f"数据区间: {result['date'].iloc[0].date()} ~ {result['date'].iloc[-1].date()}"
    )
    print(f"最新收盘价: {result['close'].iloc[-1]:.4f}")
    print(f"最新 RSI({RSI_PERIOD}): {result['rsi'].iloc[-1]:.2f}")
    print(f"初始本金: {INITIAL_CAPITAL:,.2f}")
    print(f"期末资产: {final_equity:,.2f}")
    print(f"累计收益: {final_equity - INITIAL_CAPITAL:+,.2f} ({total_return:+.2%})")
    print(f"平均实际仓位: {result['executed_weight'].mean():.2%}")
    print(f"佣金率: {COMMISSION_RATE:.4%}，不含印花税")

    reports = {
        "weekly": period_report(result, "W-FRI"),
        "monthly": period_report(result, "M"),
        "yearly": period_report(result, "Y"),
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    result.to_csv(OUTPUT_DIR / "daily_equity.csv", index=False)
    for name, report in reports.items():
        report.to_csv(OUTPUT_DIR / f"{name}_returns.csv", index=False)
        print_report(name, report, tail=20 if name == "weekly" else None)
    chart_paths = plot_charts(result, reports)
    print(f"\n报告已写入: {OUTPUT_DIR.resolve()}")
    print("图表文件:")
    for path in chart_paths:
        print(f"- {path.resolve()}")


if __name__ == "__main__":
    main()
