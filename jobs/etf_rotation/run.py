from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pandas as pd

from jobs.etf_rotation.backtest import RotationConfig, download_ths_etf, load_etf_csv, run_backtest


ETF_CODES = ("513100", "159915")


def build_config(args: argparse.Namespace) -> RotationConfig:
    common = {
        "initial_capital": args.capital,
        "trading_cost_rate": args.cost,
        "rebalance_threshold": args.rebalance_threshold,
    }
    if args.profile == "screenshot":
        return RotationConfig(
            profile_name="screenshot",
            short_window=5,
            medium_window=10,
            long_window=20,
            momentum_weights=(0.0, 0.0, 1.0),
            switch_buffer=0.0,
            single_asset_weight=1.0,
            leader_weight=1.0,
            follower_weight=0.0,
            medium_volatility=9.0,
            high_volatility=10.0,
            require_medium_return_positive=False,
            **common,
        )
    if args.profile == "optimized":
        return RotationConfig(
            profile_name="optimized",
            short_window=5,
            medium_window=10,
            long_window=20,
            momentum_weights=(0.0, 0.0, 1.0),
            switch_buffer=0.0,
            single_asset_weight=1.0,
            leader_weight=1.0,
            follower_weight=0.0,
            medium_volatility=0.30,
            high_volatility=0.40,
            medium_vol_scale=0.80,
            high_vol_scale=0.60,
            volatility_window=20,
            require_medium_return_positive=False,
            **common,
        )
    return RotationConfig(profile_name="balanced", **common)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="纳指ETF/创业板ETF周频轮动回测")
    parser.add_argument("--start", default="2014-01-01", help="回测开始日期，默认 2014-01-01")
    parser.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"), help="回测结束日期")
    parser.add_argument("--csv-dir", help="可选的本地CSV目录，文件名应为 513100.csv 和 159915.csv")
    parser.add_argument("--output-dir", default="jobs/etf_rotation/outputs", help="结果目录")
    parser.add_argument("--capital", type=float, default=100_000.0, help="初始资金")
    parser.add_argument("--cost", type=float, default=0.001, help="单边综合交易成本率")
    parser.add_argument("--rebalance-threshold", type=float, default=0.02, help="最小调仓偏离，默认2%%")
    parser.add_argument(
        "--profile",
        choices=("optimized", "balanced", "screenshot"),
        default="optimized",
        help="optimized为稳健优化版，balanced为长期复合动量版，screenshot为截图规则复现版",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config(args)
    warmup_start = (pd.Timestamp(args.start) - timedelta(days=240)).strftime("%Y-%m-%d")
    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
        frames = {code: load_etf_csv(csv_dir / f"{code}.csv") for code in ETF_CODES}
    else:
        frames = {code: download_ths_etf(code, warmup_start, args.end) for code in ETF_CODES}

    result = run_backtest(frames, config=config, start_date=args.start, end_date=args.end)
    nasdaq_config = replace(config, profile_name=f"{config.profile_name}_nasdaq_only")
    nasdaq_only = run_backtest(
        {"513100": frames["513100"]},
        config=nasdaq_config,
        start_date=args.start,
        end_date=args.end,
    )
    no_volatility_config = replace(
        config,
        profile_name=f"{config.profile_name}_no_volatility_control",
        medium_volatility=9.0,
        high_volatility=10.0,
        medium_vol_scale=1.0,
        high_vol_scale=1.0,
    )
    no_volatility_control = run_backtest(
        frames,
        config=no_volatility_config,
        start_date=args.start,
        end_date=args.end,
    )
    result.write(
        args.output_dir,
        comparisons={
            "nasdaq_only": nasdaq_only,
            "no_volatility_control": no_volatility_control,
        },
    )
    summary = result.summary
    print(f"纳指ETF/创业板ETF周频轮动回测 ({args.profile})")
    print(f"区间: {summary['start_date']} 至 {summary['end_date']}")
    print(f"期末权益: {summary['ending_equity']:,.2f}")
    print(f"累计收益: {summary['total_return']:.2%}")
    print(f"年化收益: {summary['annual_return']:.2%}")
    print(f"最大回撤: {summary['max_drawdown']:.2%}")
    print(f"年化波动: {summary['annual_volatility']:.2%}")
    print(f"夏普(无风险利率0): {summary['sharpe_zero_rate']:.2f}")
    print(f"Calmar: {summary['calmar_ratio']:.2f}")
    print(f"交易笔数: {summary['trade_count']}")
    print(f"调仓次数: {summary['rebalance_count']}")
    print(f"累计交易成本: {summary['transaction_cost']:,.2f}")
    for code in ETF_CODES:
        benchmark = summary["benchmarks"][code]
        print(
            f"{code}同期持有: 累计{benchmark['total_return']:.2%}，"
            f"年化{benchmark['annual_return']:.2%}，最大回撤{benchmark['max_drawdown']:.2%}"
        )
    nasdaq_summary = nasdaq_only.summary
    print(
        f"仅513100择时: 累计{nasdaq_summary['total_return']:.2%}，"
        f"年化{nasdaq_summary['annual_return']:.2%}，"
        f"最大回撤{nasdaq_summary['max_drawdown']:.2%}，"
        f"夏普{nasdaq_summary['sharpe_zero_rate']:.2f}"
    )
    no_volatility_summary = no_volatility_control.summary
    print(
        f"关闭波动率降仓: 累计{no_volatility_summary['total_return']:.2%}，"
        f"年化{no_volatility_summary['annual_return']:.2%}，"
        f"最大回撤{no_volatility_summary['max_drawdown']:.2%}，"
        f"夏普{no_volatility_summary['sharpe_zero_rate']:.2f}"
    )
    print(f"结果目录: {Path(args.output_dir).resolve()}")
    print(f"图表报告: {(Path(args.output_dir) / 'report.html').resolve()}")


if __name__ == "__main__":
    main()
