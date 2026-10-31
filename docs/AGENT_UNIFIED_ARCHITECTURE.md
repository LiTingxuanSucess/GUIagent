# 多 Agent 现状与统一架构设计

更新时间：2026-05-10

## 目标

当前仓库里已经形成了几类不同 Agent：

- `ECAgent`：面向电商价格追踪，以淘宝为 MVP，重点是搜索、列表抓取、商品详情抓取、价格/促销/店铺信息沉淀。
- `DouyinCSAgent`：面向抖音线索/客服会话，重点是稳定读取当前会话、监听新消息、调用 RevAI/LLM 自动回复、把消息写入平台。
- `WXWorkAgent`：面向企业微信/企微后台，包含本地 OCR 服务、添加客户、企微会话归档、朋友圈发布、统一 Web Console 后端和前端。

后续不应该继续让每个 Agent 各自扩一套浏览器、OCR、任务状态、数据入库、前端展示。目标是抽象出一个大一统架构：

```text
统一平台层
  Web Console / API / SQLite 或后续数据库 / 任务队列 / 审计日志

Agent 编排层
  任务调度 / 状态机 / 异常恢复 / 人工接管 / 会话记忆清理

平台适配层
  淘宝 Adapter / 抖音客服 Adapter / 企微 Adapter / 企微后台 Adapter

感知与执行层
  CDP/DOM / OCR / VLM / 本地截图 / 鼠标键盘模拟 / 网络事件采集

浏览器运行层
  用户真实 Chrome / CloakBrowser / 独立管理后台 Chrome / 远程浏览器
```

核心原则：**数据抓取用结构化能力，敏感操作用拟人化操作，异常恢复用 OCR/VLM，平台统一落库和展示。**

## 现有 Agent 设计盘点

### ECAgent

定位：产品价格追踪与电商数据采集。

现状：

- `ECAgent/src/browser.py` 已有 `connect_over_cdp("http://127.0.0.1:9222")` 抽象。
- 默认复用本机 Chrome 和登录态。
- 淘宝流程已经验证过搜索、列表抓取、JSON 导入、前端表格展示。
- 文档集中在 `ECAgent/docs/REQ.md`、`TAOBAO_MVP_PROGRESS.md`、`TECH_PLAN.md`。

适合继续保留的方向：

- 商品列表、价格、销量、店铺、SKU、活动信息，优先用 DOM/CDP 抓结构化数据。
- 搜索框输入、筛选、翻页、弹窗关闭、登录验证，用 OCR+鼠标键盘模拟兜底。
- 反爬风险较高时，先减少 JS 注入和批量接口扫，控制操作频率，再考虑替换浏览器运行层。

短板：

- 浏览器运行层、任务状态、抓取结果入库还没有完全统一到 WXWorkAgent 的 Web Console 架构。
- 缺少通用反风控策略配置，例如节流、随机等待、失败冷却、账号健康度。

### DouyinCSAgent

定位：抖音客服自动化。

现状：

- 核心脚本：`DouyinCSAgent/douyin_current_chat_reply.py`。
- 通过 Playwright `connect_over_cdp` 连接已登录 Chrome，当前稳定使用 `9222`。
- DOM/CDP 提取当前会话和消息，监听新消息。
- 调用 RevAI：`https://car.revai.cn/api/project/carzone/compat/chat/respond`。
- 自动回复前会把换行替换为空格，避免抖音输入框 Enter/换行截断问题。
- 会把 incoming/outgoing 消息写入 WXWorkAgent 后端 `/api/conversations`。
- 已接入 RevAI session 清理接口，平台按钮可清 `douyin-ltx`。

适合继续保留的方向：

- 消息读取继续用 DOM/CDP，因为文本结构化、成本低、准确。
- 发送消息可以逐步从 `textarea.fill + Enter` 过渡到鼠标点击输入框 + 原生键盘输入，降低自动化痕迹。
- 新消息监听必须由 Agent 主动完成，不能依赖人工切换页面。

短板：

- 当前监听的是“当前/可见会话”为主，完整会话列表、历史消息、增量同步还需要更强的列表遍历和去重机制。
- 发送层对页面输入框行为依赖较强，后续应增加 OCR 定位输入框和发送按钮的兜底路径。
- RevAI session_id 规则必须统一，否则清理记忆会清错会话。当前实际规则是 `douyin-{会话标题}`。

### WXWorkAgent

定位：企微自动化能力和统一平台雏形。

现状：

- 有本地 OCR HTTP 服务：`wxwork_ocr_service.py`，默认 `127.0.0.1:8765`。
- OCR 推荐用常驻服务，业务脚本通过 `src/ocr_client.py` 调用，不直接 import Paddle。
- Web 后端：`WXWorkAgent/web/backend/main.py`。
- Web 前端：`WXWorkAgent/web/frontend/src/App.vue`。
- SQLite：`WXWorkAgent/data/agent_platform.db`。
- 已有功能：
  - 线索 `leads`
  - 会话 `conversations`
  - 抖音归档导入
  - 朋友圈发布 `moments`
  - RevAI session 清理
- 企微朋友圈脚本 `wxwork_pengyouquan.py` 已改为默认复用 `9222`，和抖音/淘宝同一 CDP 浏览器一致。

适合继续保留的方向：

- 把 WXWorkAgent 的 Web Console 升级成统一 Agent Console。
- OCR 服务作为所有 Agent 的共享能力，而不是企微私有能力。
- 低频、高风险、强 UI 依赖流程，如加客户、发朋友圈、弹窗确认，优先使用 OCR+模拟点击。

短板：

- 后端目前是单文件 FastAPI，功能继续增加后需要拆模块。
- SQLite 可以支撑 MVP，但长期需要任务表、事件表、抓取批次表、幂等键、附件表。
- 任务执行目前多是线程/子进程，后续需要统一 runner 和任务队列。

## 统一架构方向

### 1. Browser Runtime 层

统一抽象：

```text
BrowserRuntime
  provider: chrome_cdp | cloak_cdp | admin_chrome | remote_cdp
  cdp_url
  user_data_dir
  profile_name
  health_check()
  start()
  attach()
```

当前默认：

```yaml
browser:
  provider: chrome_cdp
  cdp_url: http://127.0.0.1:9222
```

设计原因：

- 我们现有三个核心方向都已经基于 CDP。
- 只要浏览器暴露 CDP，业务脚本可以继续用 Playwright `connect_over_cdp`。
- 未来替换 CloakBrowser 时，优先替换运行层，不改业务 Adapter。

### 2. Perception 感知层

统一抽象：

```text
Perception
  DOMExtractor       结构化 DOM 数据
  NetworkObserver    XHR/WebSocket/接口响应
  OCRService         屏幕文字和坐标
  VLMAnalyzer        复杂截图理解，低频兜底
  ScreenshotStore    截图、标注、问题复盘
```

推荐分工：

- DOM/CDP：列表、消息、价格、商品字段、结构化内容。
- OCR：按钮、弹窗、输入框、验证码提示、页面状态。
- VLM：OCR 和 DOM 都无法判断时，用于复杂页面语义判断，不作为高频链路。
- Network：仅做辅助观测和复核，不做高频批量扫接口，避免风险。

### 3. Action 执行层

统一抽象：

```text
ActionExecutor
  click_text(text)
  click_point(x, y)
  type_text(text)
  hotkey(keys)
  scroll(direction, amount)
  wait_for_state(predicate)
```

执行策略：

- 普通结构化采集：可以用 Playwright selector，但要限制频率。
- 高风控页面操作：优先鼠标轨迹 + 原生键盘输入。
- 弹窗和确认按钮：优先 OCR 定位文字后点击。
- 发送消息：推荐“点击输入框 -> 粘贴/键入 -> 点击发送或 Enter”，并保留 DOM selector 兜底。

### 4. Platform Adapter 层

每个平台一个 Adapter，统一接口：

```text
Adapter
  identify()
  open()
  observe()
  extract()
  act()
  normalize()
  health()
```

淘宝 Adapter：

- `search(keyword)`
- `extract_list()`
- `extract_detail()`
- `normalize_product()`
- `detect_risk_state()`

抖音客服 Adapter：

- `list_conversations()`
- `get_current_chat()`
- `watch_new_messages()`
- `send_reply()`
- `normalize_message()`
- `clear_memory(session_id)`

企微 Adapter：

- `export_recent_chats()`
- `add_customer(phone)`
- `publish_moment(content, image)`
- `ocr_locate()`

### 5. Agent Orchestrator 层

统一处理任务生命周期：

```text
Task
  id
  type
  platform
  status: pending | running | waiting_user | done | failed
  input
  output
  error
  created_at
  updated_at

TaskEvent
  task_id
  step
  level
  message
  screenshot_path
  payload
```

关键能力：

- 幂等：同一个抓取批次、同一条消息、同一个商品不要重复写入。
- 重试：网络/页面加载失败可重试，风控/验证码不自动硬闯。
- 人工接管：出现登录、验证码、权限弹窗时暂停并提示。
- 审计：每次自动回复、每次发布朋友圈、每次价格抓取都要有日志。
- 记忆管理：LLM/RevAI session 可查询、清理、重置。

### 6. Data Model 层

建议统一表模型：

```text
platform_accounts
browser_sessions
tasks
task_events
conversations
messages
products
product_snapshots
captures
attachments
llm_sessions
```

当前已有：

- `conversations`
- `leads`
- `moments`

需要补：

- `messages` 独立表，避免全部塞在 conversations。
- `products` 和 `product_snapshots`，支撑价格追踪。
- `task_events`，支撑统一日志和错误复盘。
- `captures`，保存截图/OCR/VLM结果。
- `llm_sessions`，统一管理 RevAI/LLM 会话清理。

## 关于 bb-browser 和 CloakBrowser

### bb-browser

判断：不适合作为无缝替换浏览器。

原因：

- 它的价值偏“真实浏览器登录态 + CDP/daemon + adapter/CLI/MCP”。
- 我们现在已经是“真实 Chrome + 登录态 + CDP + Playwright connect_over_cdp”。
- 它可参考 adapter 组织方式，但不是反指纹浏览器。

适合借鉴：

- 站点 adapter 结构。
- 通过浏览器上下文做 authenticated fetch。
- 调试和 MCP/CLI 工具化思路。

### CloakBrowser

判断：更值得做 PoC，但不建议直接替换生产链路。

原因：

- 它是偏 stealth browser 的方向，目标是降低自动化/fingerprint 暴露。
- 如果它稳定暴露 CDP，我们可以继续复用 `connect_over_cdp`。
- 对淘宝这类强风控场景更有价值。

推荐落地：

1. 先在 `ECAgent` 淘宝流程做 PoC。
2. 新增 `browser.provider = cloak_cdp`。
3. 用独立 profile 登录淘宝。
4. 对比 Chrome CDP 与 CloakBrowser 的验证码率、页面限流率、连续抓取成功率。
5. 不要先动 DouyinCSAgent，因为客服监听和回复链路已经在线使用。

## 关于 OCR + 模拟点击

结论：**有帮助，但不能替代结构化抓取。**

推荐定位：

```text
DOM/CDP = 高效率数据感知
OCR     = 可见 UI 状态和按钮定位
VLM     = 复杂异常兜底
鼠标键盘 = 低检测操作执行
```

有帮助的地方：

- 减少直接 DOM 操作、JS 注入、批量接口调用带来的自动化痕迹。
- 页面结构变动时，按钮文字和视觉位置往往比 class/selector 更稳定。
- 登录、验证码、弹窗、发送消息、发布朋友圈等高风险操作更接近真人。

不适合的地方：

- 大规模商品字段抓取不能全靠 OCR，成本高、漏字段、排序容易错。
- 历史消息和表格数据不能只靠视觉识别，应该用 DOM/CDP 结构化提取。
- OCR 依赖分辨率、缩放、窗口遮挡，必须配合截图和坐标校验。

具体应用：

- 淘宝：DOM 抓商品数据，OCR+鼠标键盘做搜索、筛选、翻页、弹窗处理。
- 抖音：DOM 读消息，OCR+原生输入做发送消息兜底。
- 企微：加客户、发朋友圈、确认弹窗优先 OCR+模拟点击。

## 反爬/风控策略

优先级从低成本到高成本：

1. 复用真实用户浏览器和稳定登录态。
2. 控制频率、并发、翻页速度、页面停留时间。
3. 避免无意义批量接口扫描。
4. 操作层改为鼠标键盘模拟，减少直接 DOM 操作。
5. OCR/VLM 做弹窗和异常识别，出现验证码时进入人工接管。
6. 引入 BrowserRuntime 抽象，允许切换 CloakBrowser。
7. 账号、代理、环境隔离，这部分只有业务必要时再做。

不建议：

- 一上来就全量替换浏览器。
- 全链路只用 OCR。
- 所有站点共用一套激进抓取节奏。
- 遇到验证码继续自动硬闯。

## 推荐实施路线

### Phase 1：统一平台底座

- 把 WXWorkAgent Web Console 升级为根级 `agent_platform`。
- 抽出公共 API：任务、事件、会话、消息、截图、配置。
- 所有 Agent 写入同一套任务和消息表。
- 保留现有脚本，但统一通过平台触发和展示。

### Phase 2：统一 BrowserRuntime

- 抽出 `BrowserRuntime` 模块。
- 所有项目统一读取：

```yaml
browser:
  provider: chrome_cdp
  cdp_url: http://127.0.0.1:9222
```

- 保留 `start_chrome.bat`，但沉淀成平台里的“浏览器健康检查”和“启动说明”。
- 加 `/api/browser/health`，返回 CDP、页面、登录状态。

### Phase 3：统一 OCR 服务

- WXWork OCR 服务升级为公共服务。
- 接口标准化：

```text
POST /ocr/screen
POST /ocr/image
POST /ocr/find_text
POST /ocr/click_text_plan
```

- 输出统一包括文本、坐标、置信度、截图路径。

### Phase 4：平台 Adapter 化

- 淘宝、抖音、企微分别实现 Adapter。
- Adapter 只负责平台细节，不直接决定任务状态和入库结构。
- 统一异常类型：

```text
LoginRequired
CaptchaRequired
RateLimited
SelectorChanged
NetworkBlocked
NeedHuman
```

### Phase 5：CloakBrowser PoC

- 只在淘宝流程做。
- 指标：
  - 100 次搜索成功率
  - 验证码出现次数
  - 页面空白/限流次数
  - 平均抓取耗时
  - 登录态保持时间
- 成功后再考虑抖音和其他平台。

## 当前最应该做的下一步

1. 把 `WXWorkAgent/web/backend/main.py` 拆成模块：
   - `api/conversations.py`
   - `api/douyin.py`
   - `api/moments.py`
   - `api/imports.py`
   - `db.py`
2. 新增统一 `tasks` 和 `task_events` 表。
3. 把 `ECAgent` 淘宝抓取结果接入统一任务模型。
4. 把 `DouyinCSAgent` 新消息监听写入独立 `messages` 表，并做幂等。
5. 抽 `browser_runtime.py`，先支持 `chrome_cdp`，预留 `cloak_cdp`。
6. 把 OCR 服务从 WXWorkAgent 概念上提升为公共服务。

## 参考链接

- bb-browser: https://github.com/epiral/bb-browser
- CloakBrowser: https://github.com/CloakHQ/CloakBrowser
- Playwright CDP 连接能力：当前本地代码统一使用 `chromium.connect_over_cdp`
