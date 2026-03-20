# A 股日频因子实验模块

这个目录提供了可直接落地的日频多因子工具，核心覆盖：

- 15 个起步因子计算（动量/反转/风险/量价/估值/质量/成长）
- 公告日对齐（`notice_date <= trade_date`）防止未来函数
- 去极值、标准化、行业中性化
- 单因子 IC / RankIC
- 分组测试（默认十组，关注 `group_10 - group_1`）

## 快速使用

```python
import pandas as pd
from strategies.factor_lab import (
    align_financials_to_daily,
    run_core_15_pipeline,
)

# 1) 你的日频行情面板（长表）
# 必要字段:
# trade_date, stock_code, open, high, low, close, volume, amount
daily_df = pd.read_parquet("daily_panel.parquet")

# 2) 财务快照（可选，但建议）
# 至少包含 stock_code, notice_date，以及 pe/pb/ps/roe/revenue/profit 等
finance_df = pd.read_parquet("finance_snapshot.parquet")
panel_df = align_financials_to_daily(daily_df, finance_df)

# 3) 指数（可选，用于 excess_ret_20）
index_df = pd.read_parquet("hs300_daily.parquet")  # trade_date, close

# 4) 一键跑起步15因子 + 验证
result = run_core_15_pipeline(
    panel_df=panel_df,
    index_df=index_df,
    horizon=5,
    min_market_cap=3e9,
    min_amount=2e8,
    neutralize=True,
)

factor_panel = result["panel"]          # 包含因子和 future_ret_5
ic_summary = result["ic_summary"]       # 每个因子的 IC 汇总
group_summary = result["group_summary"] # 每个因子的分组汇总
```

## 直接跑本地缓存（推荐）

项目里已经提供了批量入口脚本，可直接读取：

- `data/cache/full_data_v3_5year.pkl`
- `data/cache/finance/*.csv`
- `data/cache/benchmark_000300.csv`

运行示例（先小样本试跑）：

```bash
./venv/bin/python jobs/factor_lab/run_core_15_from_cache.py \
  --max-stocks 300 \
  --start-date 2025-01-01 \
  --out-dir tests/factor_lab_outputs
```

全市场运行示例：

```bash
./venv/bin/python jobs/factor_lab/run_core_15_from_cache.py \
  --start-date 2025-01-01 \
  --out-dir tests/factor_lab_outputs
```

默认输出：

- `ic_summary.csv`
- `group_summary.csv`
- `run_meta.json`

## 自动入模（LightGBM / XGBoost）

提供了训练入口脚本，会：

- 自动计算核心 15 因子并产出 `ic_summary`
- 自动选择 `ic_summary` 前 `N` 个因子（默认 10）
- 训练 LightGBM / XGBoost（`--model auto` 会自动选择可用库）
- 输出特征重要性与测试期分层多空表现

```bash
./venv/bin/python jobs/factor_lab/train_boost_from_cache.py \
  --start-date 2025-01-01 \
  --n-factors 10 \
  --model auto \
  --out-dir tests/factor_lab_model_outputs
```

主要输出文件：

- `feature_importance.csv`
- `test_daily_group_returns.csv`
- `test_group_summary.csv`
- `model_metrics.json`
- `run_meta.json`

## 滚动训练（Walk-Forward）

为减少一次性切分偏差，推荐使用滚动训练/测试：

```bash
./venv/bin/python jobs/factor_lab/train_boost_walkforward.py \
  --start-date 2025-01-01 \
  --train-days 126 \
  --test-days 21 \
  --step-days 21 \
  --min-folds 3 \
  --n-factors 10 \
  --model auto \
  --out-dir tests/factor_lab_walkforward_outputs
```

主要输出文件：

- `wf_fold_metrics.csv`：每个滚动窗口的模型与分层表现
- `wf_daily_group_returns.csv`：测试期逐日分组收益（跨窗口拼接）
- `wf_feature_importance_mean.csv`：跨窗口平均特征重要性
- `wf_summary.json`：总体汇总（含全测试日 long-short 统计）

## 关键函数

- `align_financials_to_daily`: 按公告日对齐财务字段
- `compute_a_share_factors`: 计算因子字段
- `apply_universe_filters`: 过滤小盘/低流动性/停牌/涨跌停难成交
- `preprocess_factors`: 去极值 + 标准化 + 行业中性化
- `evaluate_factor_ic`: 计算 IC/RankIC
- `quantile_group_test`: 分组测试
- `run_core_15_pipeline`: 从因子计算到验证的整链路
