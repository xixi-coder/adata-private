# Dynamic Alpha：A 股自适应研究策略 V1

这是一个从零实现的独立研究框架，不复用仓库中已有策略的选股、打分或仓位逻辑。它的目标是验证“市场状态、因子有效性和个股风险同时动态变化”的组合方法，不承诺年化收益。

## V1 包含什么

- 股票池：上市时长、20 日平均成交额、停牌、ST/退市风险和流动性排名过滤。
- 五类因子：中期动量、趋势质量、基本面质量、估值、低风险。
- 行业中性：因子先在行业内去中心，再按交易日缩尾和标准化。
- 自适应权重：只使用已经完成未来收益观察窗口的历史 IC；60% 长期先验加 40% 滚动有效性。
- 市场状态：市场广度、长趋势、新高/新低、上涨成交额占比和市场波动共同决定连续仓位。
- 组合约束：单票、行业、最大持仓数和逆波动率配置。
- 风控：组合波动率目标、分层回撤降仓、ATR 移动退出、趋势确认退出和时间退出。
- 执行模拟：收盘形成信号，下一交易日开盘成交；包含整手、佣金最低收费、卖出印花税、滑点、涨跌停和停牌约束。

## 输入数据

日线面板必须包含：

```text
stock_code, trade_date, open, high, low, close, volume, amount
```

推荐额外提供：

```text
pre_close, stock_name, industry, is_st, market_cap
```

财务数据必须包含 `stock_code` 和 `announce_date`。策略把财务数据安排在公告日之后的第一个交易日生效，避免把收盘后公告提前用于当天信号。可识别字段包括：

```text
roe, cashflow_to_profit, gross_margin, debt_ratio,
earnings_yield, fcf_yield, book_to_price
```

如果不提供财务数据，策略只使用动量、趋势和风险三类价格因子，并重新归一化权重，不会用虚构值填充基本面因子。

## Python 调用

```python
from strategies.dynamic_alpha import DynamicAlphaStrategy

strategy = DynamicAlphaStrategy()
strategy.prepare(panel, fundamentals=fundamentals, benchmark=benchmark)
result = strategy.run_backtest(start_date="2015-01-01", end_date="2025-12-31")
result.write("tests/dynamic_alpha_backtest")
```

基准数据可选，格式为 `trade_date, close`。没有基准时使用股票池的截面中位数收益构建市场代理。

## 命令行

```bash
python3 -m strategies.dynamic_alpha.strategy \
  --cache data/cache/full_data_v3_5year.pkl \
  --fundamentals path/to/point_in_time_fundamentals.csv \
  --benchmark data/cache/benchmark_000300.csv \
  --start 2015-01-01 \
  --end 2025-12-31 \
  --out-dir tests/dynamic_alpha_backtest
```

输出包括：

- `nav.csv`
- `trades.csv`
- `signals.csv`
- `factor_weights.csv`
- `metrics.json`

## 当前边界

V1 是研究回测器，还不是实盘系统。正式评估前仍需补齐：

- 包含退市股票的历史全量股票池，避免幸存者偏差。
- 历史 ST、停牌、涨跌停规则和历史证券交易税费。
- 复权价格与真实成交价格的一致处理。
- 财报更正、行业历史归属、IPO 前五日无涨跌幅限制。
- 分红、送转、配股等公司行动的现金和持仓调整。
- 成交量参与率与资金规模相关的冲击成本模型。

因此，短样本中出现高年化不能视为达到目标。至少应做滚动训练/测试和最终冻结盲测，并对成本、参数与股票池做压力测试。
