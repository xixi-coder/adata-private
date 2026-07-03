# Requirements Document

## Introduction

本功能为 adata 金融数据库项目新增一个「雪球关注用户监听」模块，命名为 `XueqiuMonitor`。用户在雪球（xueqiu.com）上关注了若干其他用户，本模块负责周期性采集这些「被监控用户」的自选股列表与发布动态，将采集结果与上一次的历史快照进行比对，识别出「新增自选股」与「新发布动态」两类变化，并在检测到变化时通过通知渠道推送给使用者。

本模块延续 adata 现有模块化风格：通过统一的请求工具访问雪球公开接口，使用 pandas 封装数据，采集与解析、比对、通知职责分离。本文档只描述功能需要「做什么」，不涉及具体实现方案。

## Glossary

- **XueqiuMonitor**：本功能的核心系统，负责调度采集、比对与通知的整体流程。
- **Collector（采集器）**：负责调用雪球接口，获取被监控用户的自选股与动态原始数据的组件。
- **Watchlist（自选股列表）**：某个被监控用户在雪球上创建的自选股集合，每只股票包含股票代码与名称。
- **Post（动态）**：被监控用户在雪球上发布的一条消息，包含唯一标识、发布时间、正文内容与来源链接。
- **Monitored_User（被监控用户）**：使用者指定的、需要被监听的雪球用户，由雪球用户 ID 唯一标识。
- **Snapshot（快照）**：某一次采集所得的某被监控用户的自选股列表或动态列表的持久化记录，用于下一次比对。
- **Change_Event（变化事件）**：比对新采集数据与历史快照后识别出的一次变化，类型为「新增自选股」或「新发布动态」。
- **Notifier（通知器）**：负责将 Change_Event 通过某种通知渠道发送给使用者的组件。
- **Notification_Channel（通知渠道）**：接收通知的目标方式，例如控制台输出、文件、Webhook 或即时消息机器人。
- **Credential（凭证）**：访问雪球接口所需的身份信息，例如 Cookie。
- **User（使用者）**：配置并运行本监听工具的人。

## Requirements

### Requirement 1: 管理被监控用户列表

**User Story:** 作为使用者，我想要配置一份需要监听的雪球用户名单，以便系统只关注我在意的那些用户。

#### Acceptance Criteria

1. WHEN 使用者提供一个包含一个或多个雪球用户 ID 的名单，THE XueqiuMonitor SHALL 将该名单中的每个用户 ID 登记为 Monitored_User。
2. IF 使用者提供的名单为空，THEN THE XueqiuMonitor SHALL 返回「监控名单为空」的错误提示并终止本次监听。
3. IF 名单中存在重复的雪球用户 ID，THEN THE XueqiuMonitor SHALL 对重复的用户 ID 去重后仅保留一个 Monitored_User。
4. IF 名单中的某个雪球用户 ID 格式非法（非数字字符串），THEN THE XueqiuMonitor SHALL 跳过该用户 ID 并记录一条「用户 ID 格式非法」的警告。

### Requirement 2: 采集被监控用户的自选股列表

**User Story:** 作为使用者，我想要系统抓取每个被监控用户当前的自选股列表，以便后续检测其自选股的变化。

#### Acceptance Criteria

1. WHEN 对某个 Monitored_User 执行采集，THE Collector SHALL 请求该用户的自选股数据并返回一个包含股票代码与股票名称的 Watchlist。
2. WHEN Collector 成功获取 Watchlist，THE XueqiuMonitor SHALL 将该 Watchlist 中每只股票的股票代码进行标准化，使同一只股票在不同采集批次中具有一致的股票代码表示。
3. IF 某个 Monitored_User 的自选股为非公开状态或无法访问，THEN THE Collector SHALL 返回「自选股不可访问」的错误并跳过该用户的自选股比对。
4. WHILE 使用者未提供有效 Credential，THE Collector SHALL 使用匿名方式访问雪球公开接口。

### Requirement 3: 采集被监控用户的发布动态

**User Story:** 作为使用者，我想要系统抓取每个被监控用户最近发布的动态，以便后续检测其是否发布了新消息。

#### Acceptance Criteria

1. WHEN 对某个 Monitored_User 执行采集，THE Collector SHALL 请求该用户最近发布的动态并返回一个 Post 列表，每个 Post 包含唯一标识、发布时间、正文内容与来源链接。
2. THE Collector SHALL 按发布时间将采集到的 Post 列表从新到旧排序。
3. IF 某个 Monitored_User 没有任何可访问的动态，THEN THE Collector SHALL 返回空的 Post 列表。
4. IF 动态原始数据缺少唯一标识字段，THEN THE Collector SHALL 跳过该条动态并记录一条「动态缺少标识」的警告。

### Requirement 4: 持久化并读取采集快照

**User Story:** 作为使用者，我想要系统保存每次采集的结果，以便下一次运行时能与上一次结果比对出变化。

#### Acceptance Criteria

1. WHEN 一次采集成功完成，THE XueqiuMonitor SHALL 将该 Monitored_User 的 Watchlist 与 Post 列表作为 Snapshot 持久化存储。
2. WHEN 对某个 Monitored_User 开始比对，THE XueqiuMonitor SHALL 读取该用户最近一次持久化的 Snapshot。
3. IF 某个 Monitored_User 不存在任何历史 Snapshot，THEN THE XueqiuMonitor SHALL 将本次采集结果作为初始 Snapshot 存储，并将本次采集视为无变化。
4. THE XueqiuMonitor SHALL 使读取到的 Snapshot 内容与其被存储时的内容保持一致（存储后读取的往返一致性）。

### Requirement 5: 检测自选股新增变化

**User Story:** 作为使用者，我想要知道被监控用户往自选股里新加了哪些股票，以便跟踪他们的操作。

#### Acceptance Criteria

1. WHEN 新采集的 Watchlist 与历史 Snapshot 中的 Watchlist 比对，THE XueqiuMonitor SHALL 将出现在新 Watchlist 中但不在历史 Snapshot 中的每只股票识别为一个「新增自选股」类型的 Change_Event。
2. IF 新采集的 Watchlist 与历史 Snapshot 中的 Watchlist 完全一致，THEN THE XueqiuMonitor SHALL 不产生任何「新增自选股」类型的 Change_Event。
3. WHEN 某只股票在历史 Snapshot 中存在但在新 Watchlist 中不存在，THE XueqiuMonitor SHALL 不将该股票识别为「新增自选股」类型的 Change_Event。
4. THE XueqiuMonitor SHALL 使每个「新增自选股」类型的 Change_Event 包含被监控用户 ID、股票代码与股票名称。

### Requirement 6: 检测新发布动态变化

**User Story:** 作为使用者，我想要知道被监控用户发布了哪些新动态，以便及时了解他们的观点。

#### Acceptance Criteria

1. WHEN 新采集的 Post 列表与历史 Snapshot 中的 Post 列表比对，THE XueqiuMonitor SHALL 将唯一标识出现在新 Post 列表中但不在历史 Snapshot 中的每条动态识别为一个「新发布动态」类型的 Change_Event。
2. IF 新采集的 Post 列表中所有动态的唯一标识都已存在于历史 Snapshot 中，THEN THE XueqiuMonitor SHALL 不产生任何「新发布动态」类型的 Change_Event。
3. THE XueqiuMonitor SHALL 使每个「新发布动态」类型的 Change_Event 包含被监控用户 ID、动态唯一标识、发布时间、正文内容与来源链接。

### Requirement 7: 发送变化通知

**User Story:** 作为使用者，我想要在被监控用户产生变化时收到通知，以便无需手动查看即可获知动向。

#### Acceptance Criteria

1. WHEN 一轮监听产生了一个或多个 Change_Event，THE Notifier SHALL 通过已配置的 Notification_Channel 发送包含全部 Change_Event 摘要的通知。
2. IF 一轮监听未产生任何 Change_Event，THEN THE Notifier SHALL 不发送通知。
3. WHERE 使用者配置了多个 Notification_Channel，THE Notifier SHALL 向每个已配置的 Notification_Channel 发送通知。
4. IF 某个 Notification_Channel 发送失败，THEN THE Notifier SHALL 记录一条「通知发送失败」的错误并继续向其余 Notification_Channel 发送。
5. WHERE 使用者未配置任何 Notification_Channel，THE Notifier SHALL 使用控制台作为默认 Notification_Channel 输出通知。

### Requirement 8: 周期性执行监听

**User Story:** 作为使用者，我想要工具能按固定间隔自动运行监听，以便持续获取更新而无需每次手动触发。

#### Acceptance Criteria

1. WHERE 使用者启用了周期执行并配置了一个以秒为单位的正整数间隔，THE XueqiuMonitor SHALL 每经过该间隔时长执行一轮完整监听。
2. WHEN 一轮监听正在执行，THE XueqiuMonitor SHALL 依次对每个 Monitored_User 执行采集、比对与通知。
3. IF 使用者配置的执行间隔小于或等于 0，THEN THE XueqiuMonitor SHALL 返回「执行间隔非法」的错误并终止启动。
4. IF 单个 Monitored_User 在采集过程中发生错误，THEN THE XueqiuMonitor SHALL 记录该错误并继续处理下一个 Monitored_User。

### Requirement 9: 处理接口访问异常

**User Story:** 作为使用者，我想要工具在雪球接口异常时保持稳定，以便偶发的网络或限流问题不会导致整个监听崩溃。

#### Acceptance Criteria

1. IF 对雪球接口的请求返回非成功状态或超时，THEN THE Collector SHALL 返回「接口请求失败」的错误并附带被监控用户 ID。
2. IF 雪球接口返回的响应无法解析为预期的数据结构，THEN THE Collector SHALL 返回「响应解析失败」的错误并保留本次原始响应内容用于排查。
3. WHEN 某个 Monitored_User 的采集因接口异常失败，THE XueqiuMonitor SHALL 保留该用户上一次的 Snapshot 不做更新。
