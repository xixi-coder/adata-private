# Implementation Plan: 雪球关注用户监听（xueqiu-user-monitor）

## Overview

本实现计划将设计文档拆解为一系列增量式编码任务。整体思路是「先纯函数、后 I/O、最后编排」：

1. 先搭建包结构与数据模型，为后续所有组件提供基础。
2. 优先实现无副作用的纯函数（代码标准化、变化比对、名单去重、快照序列化），这些是最核心、最易出错的逻辑，用属性测试大规模验证。
3. 再实现有副作用的组件（采集、持久化、通知），用示例测试与 mock 验证。
4. 最后由 `XueqiuMonitor` 编排各组件，并接入周期调度，完成整体串联。

每个任务都在前序任务基础上构建，最终在 monitor 与顶层 `__init__.py` 中把所有部件接线，不留孤立代码。

## Tasks

- [x] 1. 搭建 `adata/xueqiu` 包结构与数据模型
  - [x] 1.1 创建包骨架与请求头
    - 创建 `adata/xueqiu/__init__.py` 及 `config.py`、`collector.py`、`snapshot.py`、`differ.py`、`notifier.py`、`monitor.py` 空模块占位
    - 新增 `adata/common/headers/xueqiu_headers.py`，参照现有 `sina_headers.py`、`ths_headers.py` 风格定义雪球请求头
    - _需求：整体架构基础_

  - [x] 1.2 定义核心数据模型
    - 在 `snapshot.py` 中定义 `Snapshot` dataclass（uid、watchlist、posts、collected_at）
    - 在 `differ.py`（或新建 `models.py`）中定义 `ChangeEvent` dataclass（uid、change_type 及自选股/动态两类可选字段）
    - _需求：4.1、5.4、6.3_

- [x] 2. 实现变化检测纯函数（differ.py）
  - [x] 2.1 实现股票代码标准化 `normalize_stock_code`
    - 复用 `code_utils.compile_exchange_by_stock_code` 思路，使同一只股票跨采集批次具有一致的代码表示
    - _需求：2.2_

  - [ ]* 2.2 为 `normalize_stock_code` 编写属性测试
    - **属性：幂等性与一致性** —— 对同一原始代码多次标准化结果一致；等价的原始代码标准化后相等
    - **校验：需求 2.2**

  - [x] 2.3 实现自选股新增比对 `diff_watchlist`
    - 按标准化代码计算「在新列表中但不在历史快照中」的差集，生成「新增自选股」类型 `ChangeEvent`，含 uid、stock_code、short_name
    - 历史中存在但新列表中不存在的股票不算新增
    - _需求：5.1、5.2、5.3、5.4_

  - [ ]* 2.4 为 `diff_watchlist` 编写属性测试
    - **属性：集合差集正确性** —— 结果恰为新旧标准化代码集合的差集；两列表一致时结果为空；删除项不产生事件
    - **校验：需求 5.1、5.2、5.3**

  - [x] 2.5 实现动态新增比对 `diff_posts`
    - 按 `post_id` 计算「在新列表中但不在历史快照中」的差集，生成「新发布动态」类型 `ChangeEvent`，含 uid、post_id、publish_time、content、source_url
    - _需求：6.1、6.2、6.3_

  - [ ]* 2.6 为 `diff_posts` 编写属性测试
    - **属性：按唯一标识去重差集** —— 结果恰为新旧 `post_id` 集合的差集；全部 post_id 已存在时结果为空；事件字段完整
    - **校验：需求 6.1、6.2、6.3**

- [x] 3. 实现被监控用户名单管理（config.py）
  - [x] 3.1 实现 `MonitorConfig` 与 `normalize_user_ids`
    - 校验用户 ID 为数字字符串，非法者跳过并生成「用户 ID 格式非法」警告
    - 对重复 ID 去重，保留首次出现顺序
    - 集中管理 interval_seconds、channels、credential
    - _需求：1.1、1.3、1.4_

  - [ ]* 3.2 为 `normalize_user_ids` 编写属性测试
    - **属性：去重与保序** —— 输出无重复且保留首次出现顺序；非数字 ID 一定被剔除并对应一条警告
    - **校验：需求 1.3、1.4**

  - [ ]* 3.3 为 `MonitorConfig` 编写单元测试
    - 覆盖空名单、含非法 ID、含重复 ID 等边界
    - _需求：1.1、1.3、1.4_

- [x] 4. 实现快照持久化（snapshot.py）
  - [x] 4.1 实现 `SnapshotStore.save` 与 `load`
    - 将 `Snapshot` 序列化为 `{base_dir}/{uid}.json` 写入；`load` 读取最近一次快照，不存在返回 `None`
    - _需求：4.1、4.2_

  - [ ]* 4.2 为快照序列化编写属性测试
    - **属性：往返一致性** —— 任意 `Snapshot` 经 save 后 load 得到内容与原始一致
    - **校验：需求 4.4**

  - [ ]* 4.3 为 `SnapshotStore` 编写单元测试
    - 覆盖首次读取（无文件返回 None）、覆盖写入、中文内容读写
    - _需求：4.1、4.2_

- [x] 5. 检查点 - 确保纯函数与持久化测试通过
  - 确保所有测试通过，如有疑问请询问使用者。

- [x] 6. 实现雪球采集器（collector.py）
  - [x] 6.1 实现凭证与匿名会话处理
    - 未提供有效 Credential 时先匿名访问 `https://xueqiu.com` 获取会话 Cookie，再携带请求数据接口；提供 Cookie 时优先使用
    - 统一经 `adata.common.requests` 访问，使用 `xueqiu_headers`
    - _需求：2.4_

  - [x] 6.2 实现 `get_watchlist`
    - 请求自选股接口，解析返回 `[stock_code, short_name]` 的 DataFrame，代码经 `normalize_stock_code` 标准化
    - 非公开/不可访问抛 `WatchlistNotAccessibleError`；接口非成功/超时抛 `CollectRequestError(uid)`；无法解析抛 `ResponseParseError(uid, raw)`
    - _需求：2.1、2.2、2.3、9.1、9.2_

  - [x] 6.3 实现 `get_posts`
    - 请求用户动态接口，解析返回 `[post_id, publish_time, content, source_url]` 的 DataFrame，按 publish_time 从新到旧排序
    - 无可访问动态返回空列表；缺少 post_id 的动态跳过并记录「动态缺少标识」警告
    - _需求：3.1、3.2、3.3、3.4_

  - [ ]* 6.4 为采集器编写单元测试（mock 响应）
    - 覆盖正常解析、不可访问、接口失败、解析失败、缺标识跳过、匿名会话
    - _需求：2.1、2.3、2.4、3.2、3.4、9.1、9.2_

- [x] 7. 实现通知器（notifier.py）
  - [x] 7.1 实现通知渠道与 `Notifier`
    - 定义 `NotificationChannel` 抽象、`ConsoleChannel`、`FileChannel`
    - `Notifier.notify`：有事件时向每个渠道发送摘要；单渠道失败记录「通知发送失败」并继续其余渠道；未配置渠道时默认使用 `ConsoleChannel`；无事件时不发送
    - _需求：7.1、7.2、7.3、7.4、7.5_

  - [ ]* 7.2 为通知器编写单元测试
    - 覆盖多渠道发送、单渠道失败隔离、默认渠道、无事件不发送
    - _需求：7.1、7.2、7.3、7.4、7.5_

- [x] 8. 实现监听编排与调度（monitor.py）
  - [x] 8.1 实现 `run_once` 单轮编排
    - 逐个 Monitored_User 采集→读取历史快照→比对→写新快照，汇总全部 `ChangeEvent` 后调用 `Notifier`
    - 无历史快照时存初始快照并视为无变化；单用户采集异常时记录错误、保留旧快照、跳过并继续下一个用户
    - 名单为空时返回「监控名单为空」错误并终止本轮
    - _需求：1.2、4.3、8.2、8.4、9.3_

  - [x] 8.2 实现 `start` 周期调度
    - 校验 interval_seconds 为正整数，≤0 时返回「执行间隔非法」错误并终止启动；否则每经间隔时长执行一轮 `run_once`
    - _需求：8.1、8.3_

  - [ ]* 8.3 为 monitor 编写集成测试（mock 采集器与渠道）
    - 覆盖首轮初始化无变化、次轮检测新增自选股与新动态、单用户失败隔离、间隔非法终止
    - _需求：4.3、8.1、8.3、8.4、9.3_

- [x] 9. 接线与聚合导出
  - [x] 9.1 在顶层暴露 `xueqiu` 实例
    - 在 `adata/xueqiu/__init__.py` 聚合导出 `XueqiuMonitor` 并构造 `xueqiu` 实例
    - 在 `adata/__init__.py` 增加 `from adata.xueqiu import xueqiu`，与 `sentiment`、`fund` 等保持一致
    - _需求：整体接线_

- [x] 10. 最终检查点 - 确保全部测试通过
  - 确保所有测试通过，如有疑问请询问使用者。

## Notes

- 标注 `*` 的子任务为可选测试任务，可为快速交付 MVP 而跳过。
- 每个任务都引用了对应的需求条款，便于追溯。
- 检查点用于分阶段增量验证。
- 属性测试用于验证纯函数的通用正确性（标准化、比对差集、名单去重、快照往返）。
- 单元测试与集成测试用于验证具体示例、边界与带副作用组件的行为。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "3.1", "4.1", "7.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "2.5", "3.2", "3.3", "4.2", "4.3", "7.2"] },
    { "id": 4, "tasks": ["2.4", "2.6", "6.1"] },
    { "id": 5, "tasks": ["6.2", "6.3"] },
    { "id": 6, "tasks": ["6.4", "8.1"] },
    { "id": 7, "tasks": ["8.2"] },
    { "id": 8, "tasks": ["8.3", "9.1"] }
  ]
}
```
