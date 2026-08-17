from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs.dynamic_alpha.data_adapter import load_drive_research_data, sync_drive_cache, write_json
from strategies.dynamic_alpha import DynamicAlphaConfig, DynamicAlphaStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Dynamic Alpha research from the Google Drive cache")
    parser.add_argument("--no-sync", action="store_true", help="Use existing local data/cache without Drive download")
    parser.add_argument("--start", default="", help="Backtest start date; empty uses available history")
    parser.add_argument("--end", default="", help="Requested end date; incomplete cross-sections are clamped")
    parser.add_argument("--max-stocks", type=int, default=800, help="Top liquid stocks at effective end; 0 loads all")
    parser.add_argument("--complete-ratio", type=float, default=0.95)
    parser.add_argument("--coverage-lookback", type=int, default=120)
    parser.add_argument("--min-history-days", type=int, default=250)
    parser.add_argument("--min-amount-ma20", type=float, default=100_000_000)
    parser.add_argument("--max-positions", type=int, default=15)
    parser.add_argument("--no-dividends", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Refuse formal run when long-history data gate fails")
    parser.add_argument("--out-dir", default=str(CURRENT_DIR / "outputs"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    synced = sync_drive_cache(str(PROJECT_ROOT), enabled=not args.no_sync)
    data = load_drive_research_data(
        str(PROJECT_ROOT),
        requested_end_date=args.end or None,
        max_stocks=args.max_stocks if args.max_stocks > 0 else None,
        complete_ratio=args.complete_ratio,
        coverage_lookback=args.coverage_lookback,
        attach_dividends=not args.no_dividends,
    )
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.strict and not data.audit["formal_long_horizon_ready"]:
        strict_audit = dict(data.audit)
        strict_audit["drive_sync_performed"] = bool(synced)
        write_json(output_dir / "data_audit.json", strict_audit)
        raise RuntimeError(f"Drive data failed the formal long-horizon quality gate: {output_dir / 'data_audit.json'}")

    config = DynamicAlphaConfig(
        min_history_days=args.min_history_days,
        min_amount_ma20=args.min_amount_ma20,
        max_positions=args.max_positions,
        universe_limit=args.max_stocks if args.max_stocks > 0 else 800,
    )
    strategy = DynamicAlphaStrategy(config)
    strategy.prepare(data.panel, fundamentals=data.fundamentals, benchmark=data.benchmark)
    eligible = strategy.features[strategy.features["eligible"]]
    latest_features = strategy.features[strategy.features["trade_date"] == data.effective_end_date]
    data.audit["factor_coverage"] = {
        "eligible_row_count": int(len(eligible)),
        "eligible_stock_count": int(eligible["stock_code"].nunique()),
        "latest_eligible_stock_count": int(latest_features["eligible"].sum()),
        **{
            factor: round(float(eligible[f"factor_{factor}"].notna().mean()), 6) if len(eligible) else 0.0
            for factor in ["momentum", "trend", "quality", "value", "risk"]
        },
    }
    result = strategy.run_backtest(
        start_date=args.start or None,
        end_date=data.effective_end_date.strftime("%Y-%m-%d"),
    )

    files = result.write(output_dir)
    audit = dict(data.audit)
    audit["drive_sync_performed"] = bool(synced)
    audit["strategy_input"] = {
        "min_history_days": args.min_history_days,
        "min_amount_ma20": args.min_amount_ma20,
        "max_positions": args.max_positions,
        "backtest_start": args.start or data.panel["trade_date"].min().strftime("%Y-%m-%d"),
        "backtest_end": data.effective_end_date.strftime("%Y-%m-%d"),
    }
    audit_file = output_dir / "data_audit.json"
    write_json(audit_file, audit)

    latest_signals = pd.DataFrame()
    if not result.signals.empty:
        latest_signal_date = result.signals["signal_date"].max()
        latest_signals = result.signals[result.signals["signal_date"] == latest_signal_date].copy()
    latest_signal_file = output_dir / "latest_signals.csv"
    latest_signals.to_csv(latest_signal_file, index=False, encoding="utf-8-sig")
    summary = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "research_only",
        "effective_end_date": data.effective_end_date.strftime("%Y-%m-%d"),
        "formal_long_horizon_ready": audit["formal_long_horizon_ready"],
        "warnings": audit["research_limitations"],
        "metrics": result.metrics,
        "latest_signal_count": int(len(latest_signals)),
        "files": {**files, "data_audit": str(audit_file), "latest_signals": str(latest_signal_file)},
    }
    write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
