# 抖音/企业微信私域转化与多渠道客服 PRD

Last updated: 2026-05-27

## 1. 背景

当前 `GUIAgent` 已经具备抖音企业号客服监听、自动回复、手机号留资上报，以及企业微信按手机号添加外部客户的能力。新的业务目标是把抖音获客从“聊天自动回复 + 手机号识别”升级为“探需、留资、区域识别、自动入企微、自动打标签、可分层运营”的私域闭环，并继续扩展到视频号后台和小红书。

本 PRD 只描述产品和技术方案，不包含本轮代码实现。

## 2. 目标

1. 抖音客服对话支持全品类探需，不局限于保养。
2. 在客户留资后，自动推进企业微信加人，并在客户进入企微后自动打标签。
3. 标签可用于维修等营销活动的定向群发。
4. 管理端可以按业务板块查看私域客户数量和激活程度。
5. 自动回复支持 emoji 表情，且发送链路不丢失 emoji。
6. 支持后续接入视频号后台与小红书，统一进入同一套线索、标签、会话和运营数据模型。

## 3. 需求拆解

### 3.1 抖音：探需、留资、区域

抖音会话需要产出结构化客户画像：

| 字段 | 说明 | 示例 |
|---|---|---|
| `source_channel` | 来源渠道 | `douyin` |
| `source_account` | 来源账号/会话 | 抖音企业号 A、某条视频、某客服会话 |
| `need_category` | 需求品类 | 维修、保养、钣喷、美容洗车、轮胎、电瓶、保险/年检、救援、二手车、精品改装、其他 |
| `need_intent` | 具体意图 | 补漆报价、换轮胎、故障灯、事故维修 |
| `vehicle_info` | 车型/车系/里程/年款 | 宝马 325i、2 万公里 |
| `region` | 区域 | 北京/朝阳，或门店覆盖区 |
| `urgency` | 紧急程度 | 今天、三天内、随便看看 |
| `lead_status` | 留资状态 | 未留资、已留手机号、已加企微、企微已通过 |
| `phone` | 手机号 | 13800138000 |
| `confidence` | 抽取置信度 | 0.0-1.0 |

探需策略：

- 首轮识别客户是否有明确需求。
- 信息不足时，只追问一个关键问题，优先级：品类 -> 区域 -> 车型/车况 -> 手机号。
- 不再把“保养”写死成默认场景；品类词表必须配置化。
- 对低置信度分类，允许打 `need_category=other`，并进入人工复核队列。

### 3.2 留资后自动加企微并打标签

留资触发条件：

- 客户消息中出现有效手机号。
- 抖音官方留资卡片完成态回传手机号/城市时，直接创建线索。
- 人工在控制台录入手机号。

企微动作：

1. 创建/更新线索。
2. 触发企业微信按手机号添加外部联系人。
3. 加人成功或待通过时，记录任务状态。
4. 客户成为外部联系人后，自动打企业客户标签。

基础标签组建议：

| 标签组 | 标签示例 | 用途 |
|---|---|---|
| 来源渠道 | 抖音、视频号、小红书、手工录入 | 渠道归因 |
| 来源账号/内容 | 抖音账号 A、视频 ID、笔记 ID | 内容归因 |
| 需求品类 | 维修、保养、钣喷、轮胎、电瓶、美容洗车、保险/年检、救援 | 分板块运营 |
| 区域 | 北京-朝阳、北京-海淀、天津-滨海 | 区域门店承接 |
| 生命周期 | 新线索、已加企微、已激活、沉默、成交、流失 | 私域状态 |
| 活跃度 | 7日活跃、30日活跃、沉默30天、沉默90天 | 激活运营 |
| 营销偏好 | 可发维修活动、可发保养活动、仅服务通知 | 合规触达 |

自动打标优先级：

1. API 打标：通过企业微信客户联系能力维护企业客户标签。
2. RPA 打标：如果没有 API 权限，使用企业微信 PC/管理后台自动化进入客户详情页打标签。
3. 本地影子标签：即使企微打标失败，也先在 Agent Console 内保存标签，便于筛选和补偿重试。

### 3.3 标签用途

营销群发：

- 支持按标签组合筛选客户，例如：`需求品类=维修 AND 区域=北京-朝阳 AND 活跃度=30日活跃`。
- 支持活动内容模板、发送预览、任务审批、发送结果回写。
- 企业微信官方群发一般需要员工确认，不应把“创建群发任务”等同于“已送达客户”。

私域规模和激活把控：

- 按来源、品类、区域、生命周期统计客户数。
- 按最近互动时间、回复次数、企微通过状态计算激活程度。
- 支持查看板块漏斗：抖音咨询 -> 留资 -> 加企微 -> 通过 -> 7日活跃 -> 成交/待成交。

### 3.4 回复支持 emoji

需求：

- 生成回复允许少量 emoji，但不能影响专业感。
- 支持人工配置是否启用 emoji。
- 发送链路必须保留 Unicode emoji。

建议策略：

- 默认开启轻量 emoji：例如 `😊`、`👌`、`🚗`、`🔧`，每条最多 1 个。
- 投诉、事故、价格争议、售后纠纷等严肃场景禁用 emoji。
- 对抖音、企微分别做发送回归测试，确认输入框、保存状态、归档导入和控制台展示不丢失表情。

### 3.5 视频号后台介入

目标：

- 将视频号后台的私信/评论/留资进入同一套 `conversations`、`leads`、`contact_tags` 数据模型。
- 支持人工接管和自动回复策略一致。

一期建议：

- 走网页后台 CDP/RPA 接入，先打通会话读取、自动回复、留资上报。
- 若官方 API 权限可申请，再替换为 webhook/API 方式。

### 3.6 小红书接入

目标：

- 接入小红书专业号/聚光等后台的私信、评论线索和留资数据。
- 统一抽取探需、区域、手机号，进入企微转化流程。

一期建议：

- 先做浏览器后台适配器：登录态复用、会话列表抓取、当前会话消息解析、回复发送、归档上报。
- 后续根据官方权限情况接入开放平台或聚光平台接口。

## 4. 当前实现评估

### 4.1 已实现

抖音侧：

- `DouyinCSAgent/douyin_watch.py` 已通过 CDP 连接抖音企业号客服页，并监听 `leads.cluerich.com`、`imapi`、`server-realtime` 等接口响应。
- 已能解析消息列表、识别访客最新消息、调用 Qwen/RevAI 生成回复并通过输入框发送。
- 已有手机号正则 `_PHONE_RE`，检测到手机号后通过 `PlatformReporter.report_lead()` 写入线索。
- 已通过 `PlatformReporter.report_conversation()` 上报抖音入站/出站消息到统一控制台。
- `DouyinCSAgent/douyin_current_chat_reply.py` 已有当前会话 dry-run/send 模式、回复去重状态、RevAI/Qwen provider 切换、回复发送前换行归一化。
- `DouyinCSAgent/douyin_export_recent.py` 已能导出 `recent_chats.json` 归档；控制台可导入抖音归档。

企业微信侧：

- `WXWorkAgent/wxwork_add_customer.py` + `WXWorkAgent/src/add_customer.py` 已实现按手机号添加外部客户的 GUI 自动化流程：通讯录 -> 新的客户 -> 添加 -> 搜手机号 -> 添加 -> 发送邀请。
- 添加客户流程支持 OCR 定位、比例回退、步骤事件上报。
- `WXWorkAgent/src/actions.py` 通过 `WM_CHAR` 逐字发送 Unicode 文本，理论上可支持 emoji。
- `WXWorkAgent/wxwork_watch.py`、`wxwork_multi_watch.py`、`wxwork_agent_loop.py` 已具备企业微信消息 OCR、自动回复、多会话扫描、联系人白名单、消息类型过滤等能力。
- `WXWorkAgent/wxwork_export_recent.py` 和 `wxwork_export_external_contacts.py` 已能导出近期会话和外部联系人列表。
- `WXWorkAgent/wxwork_pengyouquan.py` 与 `WXWorkAgent/web/backend/main.py` 的 `/api/moments` 已支持企业朋友圈内容发布任务。

统一控制台：

- `agent_platform` 与 `WXWorkAgent/web/backend/main.py` 已有 `leads`、`conversations` 两类核心表。
- 已有 `/api/leads`、`/api/leads/{id}/trigger`、`/api/conversations`、`/api/conversation_threads`。
- 前端 `WXWorkAgent/web/frontend/src/App.vue` 已有“客户线索”“抖音对话记录”“企微归档”“发朋友圈”页面。
- 当前线索看板有 pending/running/done/failed 状态统计。

### 4.2 缺口

| 需求 | 当前状态 | 缺口 |
|---|---|---|
| 抖音探需 | 仅把最新客户消息发给 Qwen/RevAI 生成回复 | 没有结构化需求抽取、品类、区域、车型、紧急度 |
| 全品类 | RevAI/示例主要围绕 carzone、保养/套餐 | 没有配置化品类体系和跨品类提示词 |
| 留资 | 仅手机号正则触发线索 | 没有留资卡片完成态解析；没有姓名/城市/来源内容归因 |
| 区域 | 无结构化字段 | 不能按区域分配、筛选、统计 |
| 企微自动打标签 | 未实现 | 只有加好友任务；没有 tag 表、tag API/RPA、补偿任务 |
| 营销群发 | 只有朋友圈发布 | 没有按标签筛选客户、群发任务、发送结果回写 |
| 私域数量/激活 | 只有线索状态数量 | 没有按板块、区域、活跃度、生命周期的 dashboard |
| emoji | 技术链路可能支持 Unicode | 没有产品开关、提示词约束、平台回归测试 |
| 视频号 | 无适配器 | 需要后台会话/评论/留资采集与回复链路 |
| 小红书 | 无适配器 | 需要后台会话/评论/留资采集与回复链路 |

## 5. 方案设计

### 5.1 统一数据模型

新增或扩展表：

```text
channels
- id, platform, account_name, external_account_id, status

contacts
- id, platform, platform_user_id, union_key, display_name, phone, region, created_at, updated_at

conversations
- 现有表扩展：channel_id, message_id, raw_payload, intent_snapshot_id

leads
- 现有表扩展：contact_id, name, region, need_category, need_intent, vehicle_info,
  urgency, confidence, lifecycle_status, wxwork_external_userid, owner_userid

tags
- id, group_name, name, source, wxwork_tag_id, status

contact_tags
- contact_id, tag_id, source, confidence, applied_to_wxwork, applied_at, error

campaigns
- id, name, target_filter_json, content, channel, status, created_at

campaign_sends
- campaign_id, contact_id, external_userid, status, sent_at, clicked_at, replied_at

activation_metrics
- contact_id, last_inbound_at, last_outbound_at, inbound_count_7d, inbound_count_30d,
  outbound_count_30d, activation_level
```

### 5.2 抽取与回复链路

每条客户入站消息进入统一 pipeline：

1. `MessageIngestor` 保存原始消息。
2. `NeedExtractor` 输出结构化 `need_profile`。
3. `LeadDetector` 判断是否留资、是否需要追问手机号。
4. `TagPlanner` 根据结构化字段生成候选标签。
5. `ReplyPolicy` 结合客户状态生成回复；控制 emoji、追问、活动推荐。
6. `ActionRunner` 执行发送、建线索、加企微、打标。

结构化抽取输出示例：

```json
{
  "need_category": "repair",
  "need_intent": "事故维修报价",
  "region": "北京-朝阳",
  "vehicle_info": "宝马325i",
  "urgency": "三天内",
  "lead_stage": "phone_missing",
  "suggested_question": "您方便说下在哪个区，以及留个手机号方便顾问给您确认报价吗？",
  "confidence": 0.82
}
```

### 5.3 企微打标方案

推荐优先走 API：

1. 在企微后台开通客户联系能力，配置客户联系 secret、回调 URL。
2. 同步企业标签库到本地 `tags`。
3. 客户添加成功后，通过回调或客户列表同步获取 `external_userid`。
4. 调用客户标签能力给 `external_userid` 增加标签。
5. 失败写入 `contact_tags.error`，后台补偿重试。

RPA fallback：

1. 用手机号或昵称在企微外部联系人中定位客户。
2. 打开客户详情。
3. 点击标签区域，输入并选择标签。
4. OCR 验证标签已出现。

### 5.4 私域看板

看板页建议：

- 总览：新增线索、已加企微、已通过、7日活跃、30日沉默。
- 品类矩阵：按 `need_category` 展示客户数、留资率、加微率、激活率。
- 区域矩阵：按区域展示线索数、服务承接状态、待跟进数。
- 渠道矩阵：抖音/视频号/小红书/手工录入的转化漏斗。
- 活动效果：按 campaign 展示触达人数、确认发送人数、回复人数、转化线索。

激活定义建议：

| 激活等级 | 规则 |
|---|---|
| 高活跃 | 7 天内有客户入站或 2 次以上互动 |
| 中活跃 | 30 天内有互动 |
| 沉默 | 30 天无互动 |
| 流失风险 | 90 天无互动且未成交 |

### 5.5 多渠道适配器

定义统一接口：

```python
class ChannelAdapter:
    platform: str
    def list_threads(self) -> list[Thread]: ...
    def fetch_messages(self, thread_id: str) -> list[Message]: ...
    def send_message(self, thread_id: str, text: str) -> SendResult: ...
    def fetch_leads(self) -> list[LeadPayload]: ...
```

已有适配器：

- `douyin_cdp_adapter`：基于当前 `DouyinCSAgent`。
- `wxwork_desktop_adapter`：基于当前 `WXWorkAgent`。

新增适配器：

- `wechat_channels_adapter`：视频号后台网页/CDP。
- `xiaohongshu_adapter`：小红书专业号/聚光后台网页/CDP。

## 6. 实施计划

### P0：当前链路补齐（1-2 周）

- 扩展 `leads` 表：`need_category`、`need_intent`、`region`、`vehicle_info`、`lifecycle_status`。
- 新增 `NeedExtractor`，对抖音入站消息做结构化抽取。
- 调整抖音/企微 system prompt：全品类探需，允许配置 emoji。
- 抖音手机号留资时写入结构化上下文。
- 控制台线索页展示品类、区域、来源、最近互动。
- 增加 emoji 发送回归测试：抖音、企微各 5 条。

### P1：企微标签闭环（2-3 周）

- 新增 `tags`、`contact_tags`、`tag_apply_tasks`。
- 标签词表配置化，先支持来源、品类、区域、生命周期。
- 完成企微 API 凭据配置和标签库同步。
- 接入外部联系人回调或定时同步，拿到 `external_userid`。
- 实现自动打标任务与失败重试。
- 控制台支持查看标签和手工修正标签。

### P2：营销与激活看板（2-3 周）

- 新增私域看板页面。
- 实现标签组合筛选器。
- 实现营销活动草稿、预览、审批、发送任务。
- 先接企业微信群发任务创建；没有 API 权限时提供导出名单 + 人工群发助手。
- 回写 campaign 结果，计算回复率和激活率。

### P3：视频号/小红书（3-5 周）

- 视频号后台 CDP 适配：会话/评论读取、回复、留资上报。
- 小红书后台 CDP 适配：私信/评论读取、回复、留资上报。
- 两个平台统一进入 `ChannelAdapter`。
- 控制台增加渠道过滤、来源账号管理、渠道健康状态。

## 7. 验收标准

P0：

- 抖音 100 条真实会话中，`need_category` 有效分类率 >= 85%。
- `region` 在用户明确提到时抽取准确率 >= 90%。
- 手机号留资创建线索无重复率 >= 99%。
- emoji 在抖音和企微发送、归档、控制台展示均不乱码。

P1：

- 客户加企微成功后 5 分钟内自动打标签成功率 >= 90%。
- 打标失败任务有错误原因和重试入口。
- 同一客户重复留资不会重复打无意义标签。

P2：

- 可按品类/区域/来源/活跃度筛选客户。
- 可创建维修活动群发任务并记录任务状态。
- 看板可展示各板块客户数、7日活跃、30日沉默、加微漏斗。

P3：

- 视频号和小红书至少各跑通 20 条真实会话的读取、回复、留资上报。
- 多渠道会话在控制台统一展示且可按平台筛选。

## 8. 风险与注意事项

- 平台权限：抖音企业私信、企微客户联系、视频号、小红书的官方 API 权限都可能需要企业主体审核。
- 群发合规：营销触达必须遵守平台频控、客户授权和退订/免打扰要求。
- RPA 稳定性：网页/客户端 UI 改版会导致定位失效，必须保留截图诊断和人工接管。
- 数据合规：手机号、地域、聊天内容属于敏感客户数据，需要限制访问、记录操作日志、避免导出滥用。
- 标签污染：LLM 抽取可能误判，必须提供置信度、人工修正和规则回滚。

## 9. 外部能力参考

- 抖音开放平台“企业私信接入方案”说明官方企业私信 open_api 可用于企业号收发消息、webhook 监听私信和会话动作。
- 抖音开放平台“接收私信消息事件”列出了 `text`、`image`、`emoji`、`video`、`retain_consult_card` 等消息类型，且留资卡片完成态可携带手机号、城市等字段。
- 企业微信客户联系能力需要重点验证企业标签、编辑客户企业标签、创建企业群发消息、获取群发结果等权限。
- 小红书/视频号一期建议先用后台 CDP/RPA，官方 API 以实际企业资质和权限申请结果为准。
