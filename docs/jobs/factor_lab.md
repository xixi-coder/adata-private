# 因子实验室 Job

## 作用

`jobs/factor_lab/` 是因子研究和模型实验脚本集合，用于从本地缓存计算核心因子、做 IC 和分层评估、训练提升树模型、做 walk-forward 评估，以及评估 Qlib 预训练模型信号。

这组脚本偏研究实验，不是稳定线上定时任务。

## 入口文件

- `jobs/factor_lab/run_core_15_from_cache.py`
- `jobs/factor_lab/train_boost_from_cache.py`
- `jobs/factor_lab/train_boost_walkforward.py`
- `jobs/factor_lab/eval_qlib_pretrained_on_cache.py`
- `jobs/factor_lab/analyze_pretrained_signal.py`
- `jobs/factor_lab/geminitest.py`

核心因子逻辑在：

- `strategies/factor_lab/factor_engine.py`

## Workflow

当前没有单独配置 GitHub Actions workflow，主要用于本地研究运行。

## 主要脚本说明

### `run_core_15_from_cache.py`

从本地行情、财务和指数缓存计算 A 股核心 15 因子，并输出：

- `ic_summary.csv`
- `group_summary.csv`
- `run_meta.json`

### `train_boost_from_cache.py`

基于缓存数据训练 LightGBM/XGBoost 或线性回退模型，自动从 IC 摘要中选择因子，并输出：

- `selected_factors.json`
- `feature_importance.csv`
- `test_predictions.csv`
- `test_daily_group_returns.csv`
- `test_group_summary.csv`
- `model_metrics.json`
- `run_meta.json`

### `train_boost_walkforward.py`

滚动训练、滚动测试版本。每个窗口只使用历史数据训练，再对后续测试窗口打分，避免未来函数。

### `eval_qlib_pretrained_on_cache.py`

用本地 adata 缓存复现 Alpha360 风格输入，调用 Qlib LSTM/GRU 预训练权重做 inference-only 评估。

### `analyze_pretrained_signal.py`

对预训练模型预测和因子模型预测做信号诊断、分组、基准对比和组合分析。

### `geminitest.py`

Google Gemini SDK 测试脚本，不属于核心因子流程。

## 输入

- `data/cache/full_data_v3_5year.pkl`
- `data/cache/finance/*.csv`
- `data/cache/benchmark_000300.csv`
- 可选模型权重或预测文件

## 输出

默认输出目录通常由命令参数指定，例如：

- `tests/factor_lab_outputs/`
- `ic_summary.csv`
- `group_summary.csv`
- `model_metrics.json`
- `test_predictions.csv`
- `run_meta.json`

## 注意事项

- 这组 job 用于研究，不建议直接接入交易流程。
- 训练和评估脚本要特别关注时间切分，避免未来数据泄漏。
- 依赖 LightGBM、XGBoost、PyTorch、Qlib 权重时，本地环境需要额外准备。
