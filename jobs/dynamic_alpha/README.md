# Dynamic Alpha 云盘研究入口

这个 job 把 Google Drive 中的 `three_dim_cache_bundle.tar.gz` 接入独立的 Dynamic Alpha 策略。它只从云盘下载，不上传、替换或删除云端文件。

## 执行

```bash
.venv/bin/python -m jobs.dynamic_alpha.run_research --max-stocks 800 --start 2025-03-14
```

快速验证可以使用 `--max-stocks 200`。已有本地缓存时可加 `--no-sync`。`--strict` 会启用长期研究门禁；当前云盘主体行情历史不足五年，因此严格模式会拒绝运行。

## 数据处理

- 自动寻找最近一个达到截面完整率要求的交易日，默认要求达到近期峰值股票数的 95%。
- 财务数据按 `notice_date` 对齐，并从公告后的下一交易日开始可用。
- 季报累计 EPS 和每股经营现金流按报告期做年化代理，再与每日价格结合生成动态估值。
- 分红因子只使用已经发生的除权事件，计算过去 365 天现金分红率。
- 如果没有行业文件，显式退化为全市场标准化，不伪造行业分类。

## 输出

默认目录为 `jobs/dynamic_alpha/outputs/`，包含 `data_audit.json`、`summary.json`、净值、交易、信号、因子权重和回测指标。

当前默认的结束日流动性截取适合近期信号研究，但会产生股票池选择偏差，不能把短样本年化数值当成正式业绩。
