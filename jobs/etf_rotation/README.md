# 纳指ETF/创业板ETF周频轮动

固定使用两只具有较长场内历史的ETF：

- `513100`：国泰纳指ETF，2013-05-15在上交所上市。
- `159915`：易方达创业板ETF，2011-12-09在深交所上市，也是创业板ETF期权标的。

策略每周最后一个共同交易日收盘后计算信号，下一共同交易日开盘成交，避免用信号日收盘价成交造成前视偏差。程序提供稳健优化版 `optimized`、长期复合动量版 `balanced` 和截图复现版 `screenshot`，便于在完全相同的成交假设下比较。

每次运行还会同步计算“仅纳指ETF择时”对照：只使用 `513100`，沿用所选 profile 的趋势过滤、波动率仓位、调仓阈值和交易成本；不满足趋势条件时持有现金。它不同于报告中的“513100买入持有”基准。

同时生成“关闭20日波动率降仓”对照：保留相同的ETF选择、趋势过滤、成交时点、调仓阈值和交易成本，但合格资产始终使用原始目标仓位。报告中的收益、回撤、年度收益和指标表可直接观察波动率规则的独立影响。

## optimized规则（默认）

- 比较两只ETF的20日收益率，只考虑收盘价高于20日均线的ETF。
- 两只都合格时满仓20日动量更强者；只有一只合格时选择该ETF；均不合格时空仓。
- 使用独立的20日年化波动率控制仓位：不超过30%时100%仓位，30%至40%时80%，超过40%时60%。
- 目标仓位与当前仓位偏差不足2个百分点时不交易。

该结构保留短趋势对创业板急跌的快速响应，同时通过波动仓位控制极端风险。参数选择依据2014–2019与2020–2026分段验证，而非全样本最高收益。

## balanced规则

综合动量为 `20日收益×20% + 60日收益×30% + 120日收益×50%`。ETF须同时满足收盘价高于120日均线、60日收益为正才可持有。

- 两只均不合格：空仓。
- 仅一只合格：该ETF 70%，其余现金。
- 两只均合格且动量接近：各50%。
- 两只均合格且动量差达到3个百分点：强者70%，弱者30%。
- 挑战者只有领先当前主仓3个百分点才切换主次。
- 持仓ETF最高20日年化波动超过30%时，风险仓位乘以0.8；超过40%时乘以0.6。
- 目标仓位与当前仓位偏差不足2个百分点时不交易，避免为微小漂移反复再平衡。

回测默认把佣金、价差和滑点合并为单边0.1%的交易成本，现金收益按0%计算。纳指ETF溢价过滤因缺少可靠的历史分钟级IOPV数据，不纳入历史回测，实盘执行时应单独检查。

## 运行

直接下载同花顺公开接口的前复权场内日线，运行默认稳健优化版：

```bash
python3 -m jobs.etf_rotation.run --profile optimized --start 2014-01-01 --end 2026-07-28
```

盘前生成当天买卖信号：

```bash
python3 -m jobs.etf_rotation.daily_signal --profile optimized
```

生成信号并发送邮件：

```bash
python3 -m jobs.etf_rotation.daily_signal --profile optimized --send-email
```

邮件配置从项目根目录 `.env.local` 读取，需配置 `MAIL_163_USER`、`MAIL_163_PASS` 和 `MAIL_TO`；也兼容 `SMTP_USER`、`SMTP_PASS`、`SMTP_HOST`、`SMTP_PORT`。多个收件人可用逗号或分号分隔。使用 `--send-email` 时，缺少配置或发送失败会令任务返回错误，避免邮件未送达却被误判为执行成功。

盘前信号只使用上一交易日及更早的完整日线，并通过A股交易日历识别上一完整交易周的最后共同交易日。只有当天是该周信号的下一交易日时才给出买卖动作，其余交易日输出“今日无需调仓”。它根据模型历史仓位推算当前持仓，不读取真实账户，也不会连接券商或自动下单。

Codex自动任务 `ETF轮动盘前交易信号` 已设置为每周一至周五 `08:50` 运行，使用Asia/Shanghai本地时区，并将结果发送到 `MAIL_TO`。

## GitHub Actions定时任务

工作流文件为 `.github/workflows/etf-rotation.yml`，默认在北京时间每周一至周五 `08:50` 自动执行：

```bash
python3 -m jobs.etf_rotation.daily_signal --profile optimized --send-email
```

在仓库 `Settings -> Secrets and variables -> Actions` 中配置以下 Repository secrets：

| Secret | 说明 |
|---|---|
| `MAIL_163_USER` | 163邮箱账号，也是发件人地址。 |
| `MAIL_163_PASS` | 163邮箱SMTP授权码，不是邮箱登录密码。 |
| `MAIL_TO` | 收件邮箱；多个地址使用逗号或分号分隔。 |

工作流也支持在 Actions 页面手动运行，可选择 `optimized`、`balanced` 或 `screenshot`，可指定 `YYYY-MM-DD` 格式的复核日期，并可关闭邮件。每次运行都会把 `jobs/etf_rotation/live_outputs/` 上传为 artifact，保留30天。

GitHub Actions的 cron 使用UTC，因此配置中的 `50 0 * * 1-5` 对应北京时间工作日 `08:50`。GitHub可能在任务高峰期延迟启动；脚本会按上海时区识别运行日期，并且只使用上一交易日及更早的行情。

本地Codex任务与GitHub Actions是两套独立调度。如果两者同时启用，它们会分别发送邮件；只需要一封通知时，应停用其中一个定时任务。

## 脚本参数

### 回测脚本 `jobs.etf_rotation.run`

完整命令格式：

```bash
python3 -m jobs.etf_rotation.run [参数]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--profile` | `optimized` | 策略版本：`optimized` 为20日动量优化版，`balanced` 为20/60/120日复合动量版，`screenshot` 为截图规则复现版。 |
| `--start` | `2014-01-01` | 回测开始日期，格式为 `YYYY-MM-DD`。程序会额外向前取约240个自然日作为指标预热数据。 |
| `--end` | 运行当天 | 回测结束日期，格式为 `YYYY-MM-DD`。建议使用已经收盘且日线完整的日期。 |
| `--csv-dir` | 不设置 | 本地行情目录。设置后不再下载行情，目录内必须包含 `513100.csv` 和 `159915.csv`。 |
| `--output-dir` | `jobs/etf_rotation/outputs` | CSV、JSON和HTML报告的输出目录。重复运行会更新目录内同名结果。 |
| `--capital` | `100000` | 初始模拟资金，单位为元。它影响成交金额、权益和交易成本金额，不改变动量排名规则。 |
| `--cost` | `0.001` | 单边综合交易成本率，包含佣金、滑点和价差的合并假设。填写小数，`0.001` 表示 `0.1%`。 |
| `--rebalance-threshold` | `0.02` | 最小调仓偏离。目标仓位与当前仓位之差小于2个百分点时不交易，填写小数。 |
| `-h`、`--help` | - | 显示代码中最新的参数帮助。 |

例如，用20万元初始资金、单边0.08%成本回测 `balanced`：

```bash
python3 -m jobs.etf_rotation.run \
  --profile balanced \
  --start 2014-01-01 \
  --end 2026-07-28 \
  --capital 200000 \
  --cost 0.0008
```

### 盘前信号脚本 `jobs.etf_rotation.daily_signal`

完整命令格式：

```bash
python3 -m jobs.etf_rotation.daily_signal [参数]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--date` | 上海时区当天 | 任务运行日期，格式为 `YYYY-MM-DD`。正常定时运行无需设置；指定日期主要用于历史信号复核。 |
| `--profile` | `optimized` | 策略版本，含义与回测脚本相同。实盘信号应与采用的回测版本保持一致。 |
| `--start` | `2014-01-01` | 模型历史起点。程序从该区间重建当前模型仓位，不建议在日常任务中随意修改。 |
| `--csv-dir` | 不设置 | 本地行情目录；不设置时下载ETF日线。文件要求与回测脚本相同。 |
| `--output-dir` | `jobs/etf_rotation/live_outputs` | 当日信号归档和 `latest_signal.json/.txt` 的输出目录。 |
| `--capital` | `100000` | 用于模型历史回放的初始模拟资金。当前信号以目标仓位比例为主，不代表真实账户金额。 |
| `--cost` | `0.001` | 模型历史回放使用的单边综合交易成本率，填写小数。 |
| `--rebalance-threshold` | `0.02` | 最小调仓偏离，填写小数；不足阈值时输出无需调仓。 |
| `--send-email` | 关闭 | 开启后把完整信号发送到 `MAIL_TO`；配置缺失或发送失败时命令返回错误。 |
| `-h`、`--help` | - | 显示代码中最新的参数帮助。 |

回测和盘前任务至少应保持 `--profile`、`--start`、`--cost`、`--rebalance-threshold` 一致，否则盘前推算的模型仓位可能与回测报告不同。盘前脚本不会读取券商真实持仓，因此实际账户发生手工交易后，仍需自行核对目标仓位与真实仓位。

## 其他运行方式

运行截图规则复现版：20日收益率排名、收盘站上20日均线、满仓排名第一者，两只都不满足时空仓。

```bash
python3 -m jobs.etf_rotation.run --profile screenshot --start 2014-01-01 --end 2026-07-28
```

也可以使用本地CSV，至少包含 `trade_date`（或 `date`）、`open`、`close`：

```bash
python3 -m jobs.etf_rotation.run \
  --csv-dir /path/to/etf_csv \
  --start 2014-01-01 \
  --end 2026-07-28
```

本地目录应包含 `513100.csv` 和 `159915.csv`。结果写入 `jobs/etf_rotation/outputs/`：

- `summary.json`：收益、回撤、波动率、标准夏普、Calmar和基准对比。
- `report.html`：净值、回撤、年度收益、仓位变化、基准对比，以及轮动策略和仅纳指择时策略各自带买卖标记的交互K线图；可切换1年、3年、5年和完整历史，悬停查看成交明细。
- `nav.csv`：每日净值、仓位及两只ETF的OHLC行情。
- `signals.csv`：每周信号、指标和目标仓位。
- `trades.csv`：下一交易日开盘执行的交易记录及成本。
- `nasdaq_only_*.csv`、`nasdaq_only_summary.json`：仅纳指ETF择时对照的净值、信号、交易和汇总指标。
- `no_volatility_control_*.csv`、`no_volatility_control_summary.json`：关闭波动率降仓对照的净值、信号、交易和汇总指标。

盘前信号写入 `jobs/etf_rotation/live_outputs/`：

- `latest_signal.json`、`latest_signal.txt`：最近一次盘前建议。
- `signal_YYYYMMDD.json`、`signal_YYYYMMDD.txt`：按运行日期归档的建议。

资料来源：

- 国泰基金产品资料概要：https://st.gtfund.com/pis/www/CN_50010000_513100_FA010080_20250001_513100_20250321_090000_01.pdf
- 深交所159915上市通知：https://www.szse.cn/disclosure/notice/fund/t20111207_516231.html
- 深交所创业板ETF期权通知：https://www.szse.cn/www/lawrules/rule/derivative/t20220916_595887.html
