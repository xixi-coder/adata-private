# A 股主线轮动 Workflow

## 作用

主线轮动 workflow 把盘面主题雷达进一步归并成可执行的组合篮子，用于回答“科技、创新药、高股息、消费、周期里，本周应该主攻谁、谁做副线、谁只观察”。

它不是买卖点系统，也不承诺收益。它的目标是把主线判断流程化：

- 先识别强主题；
- 再给战略篮子评分；
- 最后输出动作标签和目标仓位区间。

## 入口文件

- `jobs/theme_rotation_workflow/run.py`
- `strategies/theme_rotation_workflow/strategy.py`
- `.github/workflows/theme-rotation-workflow.yml`

## GitHub Actions

独立 workflow 会在北京时间工作日 15:20 运行：

1. 先执行 `jobs/theme_monitor/run.py` 生成当日主题雷达，且关闭主题雷达邮件。
2. 再执行 `jobs/theme_rotation_workflow/run.py` 生成主线轮动计划。
3. 上传主题雷达和主线轮动报告 artifact。

手动触发时可以传入当前篮子仓位 JSON，也可以配置仓库密钥 `THEME_ROTATION_POSITIONS_JSON`：

```json
{
  "科技成长": 0.2,
  "创新药": 0.1,
  "高股息": 0.3
}
```

## 输入

默认读取主题雷达已有输出：

- `jobs/theme_monitor/outputs/latest_theme_radar.csv`
- `jobs/theme_monitor/outputs/latest_market_context.json`

如果主题雷达文件不存在，会自动在线抓取热榜、概念榜、行业榜、人气榜和市场环境。

也可以强制在线抓取：

```bash
python jobs/theme_rotation_workflow/run.py --fetch-live
```

可选传入当前仓位，用于拥挤度惩罚：

```json
{
  "科技成长": 0.25,
  "创新药": 0.12,
  "高股息": 0.30
}
```

运行：

```bash
python jobs/theme_rotation_workflow/run.py --positions positions.json
```

## 输出

目录：`jobs/theme_rotation_workflow/outputs/`

- `latest_theme_rotation_plan.csv`
- `latest_summary.json`
- `latest_report.md`

## 篮子

第一版内置五个篮子：

- 科技成长：AI、算力、CPO、半导体、芯片、机器人、软件、信创等。
- 创新药：创新药、生物医药、CRO、CXO、医疗器械、ADC、License-out 等。
- 高股息：银行、保险、煤炭、电力、公用事业、运营商、红利、央企等。
- 消费修复：白酒、食品饮料、旅游、酒店、家电、零售等。
- 周期资源：有色、黄金、铜、铝、稀土、化工、钢铁、石油、航运等。

## 评分逻辑

综合分由五部分组成：

- 趋势强度：30%，来自匹配主题的雷达分。
- 资金强度：22%，来自热股数量、人气共振和热度值。
- 催化强度：18%，来自篮子基础政策分和市场环境调整。
- 兑现强度：20%，来自产业兑现基础分、主题广度和涨幅。
- 拥挤惩罚：10%，来自过高趋势、资金热度、短期涨幅和当前仓位。

动作标签：

- `主线`：综合分排名第一且不低于 68。
- `副主线`：综合分不低于 58。
- `观察`：综合分不低于 48。
- `回避`：综合分低于 48。

## 注意事项

- 目标仓位是纪律提示，不是自动交易指令。
- 拥挤度高时，workflow 会压低主线目标仓位并提示“只低吸不追高”。
- 后续可以继续接 ETF 资金流、估值分位和真实持仓文件。
