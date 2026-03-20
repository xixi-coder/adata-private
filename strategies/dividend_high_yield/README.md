# 高股息策略（Dividend Yield）

策略规则（简版）：

1. 使用本地全市场日线缓存构建流动性股票池（默认前 300）。
2. 每 20 个交易日调仓一次。
3. 候选股票需满足：
   - 价格在趋势均线上方（默认 MA60）；
   - 近 20 日成交额均值达标；
   - 60 日波动率不超过阈值；
   - 过去 365 天股息率不低于阈值（默认 2%）。
4. 按“股息率高优先，波动率低优先”排序，等权买入前 N（默认 12）。
5. 持仓中若遇到除息日，按持仓股数将现金分红直接计入现金账户。

## 运行

```bash
./venv/bin/python strategies/dividend_high_yield/high_dividend_strategy.py
```

可选参数示例：

```bash
./venv/bin/python strategies/dividend_high_yield/high_dividend_strategy.py \
  --start 2025-03-20 \
  --end 2026-03-20 \
  --universe-size 300 \
  --max-positions 12 \
  --min-dividend-yield 0.02 \
  --rebalance-period 20
```

结果默认输出到：`tests/dividend_strategy_backtest_v1`
