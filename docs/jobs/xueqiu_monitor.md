# 雪球自选股监听 Job

## 作用

雪球自选股监听用于定时采集指定雪球用户的自选股列表，与上一次快照比较，发现新增自选股后发送通知。当前部署形态只监听自选股，不采集雪球动态。

## 入口文件

- `jobs/xueqiu_monitor/run.py`

核心模块在：

- `adata/xueqiu/collector.py`
- `adata/xueqiu/monitor.py`
- `adata/xueqiu/notifier.py`
- `adata/xueqiu/snapshot.py`

## Workflow

- `.github/workflows/xueqiu-monitor.yml`
  - 名称：`雪球关注用户自选股监听`
  - 当前 cron：UTC `03:00`、`06:00`、`09:00`、`14:00`
  - 对应北京时间 `11:00`、`14:00`、`17:00`、`22:00`
  - workflow 会把最新快照提交回仓库

## 输入

环境变量：

- `XUEQIU_UIDS`：被监控用户，逗号分隔，支持 `uid` 或 `uid:名称`
- `XUEQIU_COOKIE`：雪球登录 Cookie，可选但建议配置
- `MAIL_163_USER` 或 `SMTP_USER`
- `MAIL_163_PASS` 或 `SMTP_PASS`
- `MAIL_TO`
- `SMTP_HOST`
- `SMTP_PORT`
- `XUEQIU_SNAPSHOT_DIR`

## 输出

快照目录：

- `jobs/xueqiu_monitor/snapshots/*.json`

通知：

- 控制台日志
- 配置邮件参数时发送 163 邮件

## 运行流程

1. 加载本地 `.env.local` 或 GitHub Actions 注入的环境变量。
2. 解析被监控用户列表和备注名。
3. 构造只采自选股的 collector。
4. 读取上一次快照。
5. 采集最新自选股并对比新增项。
6. 有新增变化时发送通知。
7. workflow 将快照 JSON 强制加入 git 并提交回仓库。

## 注意事项

- GitHub 托管 runner 对雪球动态接口容易受 WAF 影响，所以当前只做自选股监听。
- Cookie 失效会导致部分用户自选股不可见。
- workflow 需要 `contents: write` 权限提交快照。
