# WebAgent Core 架构设计

更新时间：2026-05-18

## 目标

在 `GUIAgent` 里抽出一个可复用的 `WebAgent` 核心，让现有 `ECAgent` 和 `DouyinCSAgent` 不再各自维护浏览器连接、页面查找、登录态检查、网络监听、截图、异常识别和任务状态。

短期目标：

- 继续复用本机已登录 Chrome，通过 CDP `http://127.0.0.1:9222` 连接。
- 支持 `ECAgent` 从当前淘宝 MVP 扩展到 `jd.com` 和 `tmall.com`。
- 支持 `DouyinCSAgent` 的客服会话监听、当前会话回复、会话导出归档。
- 形成平台 Adapter 边界，业务项目只写平台规则，不重复写 Web 自动化底座。

非目标：

- 不自动处理扫码登录、短信验证码或强风控验证码。
- 不直接替换现有 ECAgent/Douyin 脚本为大重构。
- 不把 OCR/VLM 作为主链路，结构化 DOM/CDP 仍然优先。

## 当前 Web 相关代码盘点

### ECAgent

核心链路是电商价格采集：

- `ECAgent/src/browser.py`
  - CDP health check。
  - 启动 Chrome。
  - `chromium.connect_over_cdp`。
  - 查找或打开页面。
- `ECAgent/src/taobao.py`
  - 淘宝首页打开、搜索框定位、关键词输入。
  - 销量排序点击。
  - 搜索结果商品卡片 DOM 抽取。
  - 商品详情页字段抽取、截图、SKU/片数/价格解析。
- `ECAgent/eca_taobao.py`
  - 单平台 runner。
  - 读取配置、连接浏览器、执行搜索、导出 JSON/CSV。
- `ECAgent/web/backend/main.py`
  - FastAPI 控制台。
  - 启动淘宝任务子进程。
  - 读取 export 结果并标准化给前端。
- `ECAgent/web/frontend/*`
  - 电商结果展示。

问题：

- 浏览器 runtime 只在 ECAgent 内部实现，Douyin 不能复用。
- `start_chrome_with_cdp()` 默认使用 Chrome 默认用户目录，新版 Chrome 可能限制 remote debugging，应改成专用 profile。
- `taobao.py` 同时包含平台动作、字段抽取、领域评分和详情解析，扩展 JD/Tmall 时会复制逻辑。
- Web backend 的平台接口仍是 `/api/taobao/*`，需要升到 `/api/ec/*` 或 `/api/tasks/*`。

### DouyinCSAgent

核心链路是客服会话自动化：

- `DouyinCSAgent/start_chrome.bat`
  - 使用独立 `.chrome-cdp-profile` 启动 CDP Chrome。
- `DouyinCSAgent/douyin_watch.py`
  - 监听网络 response。
  - 解析 IM 消息。
  - 调用 Qwen。
  - 通过 Playwright 填输入框发送。
- `DouyinCSAgent/douyin_export_recent.py`
  - 监听 response 和 websocket。
  - 保存 raw payload。
  - DOM fallback 读取当前会话。
  - 归并会话和消息。
- `DouyinCSAgent/douyin_current_chat_reply.py`
  - 读取当前选中会话。
  - 调用 Qwen/RevAI。
  - dry-run 或发送。
  - 上报 Agent Console。

问题：

- 每个脚本自己连接 CDP、自己找页面。
- response/websocket 监听逻辑和导出逻辑绑在一起，难以复用到其他站点。
- 消息幂等状态是本地 JSON，未来需要接统一 task/message 表。
- 发送动作目前以 selector 为主，需要加 OCR/坐标 fallback。

### 其他可复用 Web 能力

- `GEO/start_chrome_cdp.bat`：专用 Chrome profile + CDP 启动思路可复用。
- `WXWorkAgent/web/backend/main.py`：已有统一控制台雏形。
- `WXWorkAgent/src/ocr_client.py` 和 OCR 服务：可作为 WebAgent 的 `OCRService` fallback。
- `agent_platform/*`：已有平台配置、数据库、路由雏形，适合承接统一任务状态。

## 总体架构

```text
Web Console / API
  tasks, events, exports, browser health, manual handoff

Agent Orchestrator
  task lifecycle, retry policy, idempotency, audit log

WebAgent Core
  BrowserRuntime
  PageRegistry
  NetworkObserver
  DOMExtractor
  ActionExecutor
  ScreenshotStore
  RiskDetector
  OCR/VLM fallback adapters

Platform Adapters
  EC: taobao, tmall, jd
  CS: douyin

Existing Projects
  ECAgent runner/web
  DouyinCSAgent watcher/reply/export
```

建议新增包：

```text
GUIAgent/
  webagent/
    __init__.py
    browser_runtime.py
    page_registry.py
    network_observer.py
    dom.py
    actions.py
    screenshots.py
    risk.py
    tasks.py
    types.py
    adapters/
      base.py
      ec_base.py
      taobao.py
      tmall.py
      jd.py
      douyin_cs.py
```

## WebAgent Core 模块设计

### BrowserRuntime

职责：

- 管理 CDP 连接和 Chrome 启动。
- 使用专用 profile 保存登录态。
- 暴露统一 health check。
- 允许未来替换为 CloakBrowser、远程 CDP 或 browser-use 云端 CDP。

接口：

```python
class BrowserRuntime:
    def health(self) -> BrowserHealth: ...
    def start(self, start_url: str | list[str]) -> None: ...
    async def attach(self) -> BrowserSession: ...
```

配置：

```yaml
browser:
  provider: chrome_cdp
  cdp_url: http://127.0.0.1:9222
  user_data_dir: C:\workspace\GUIAgent\.chrome-webagent-profile
  start_urls:
    - https://login.taobao.com/
    - https://passport.jd.com/new/login.aspx
    - https://leads.cluerich.com/pc/cs/chat/session
```

当前已启动：

- CDP URL: `http://127.0.0.1:9222`
- Profile: `C:\workspace\GUIAgent\.chrome-webagent-profile`

### PageRegistry

职责：

- 在所有 browser contexts/pages 中查找目标页面。
- 没有页面时打开 fallback URL。
- 对平台做 URL 归属判断。

接口：

```python
class PageRegistry:
    async def get_or_open(platform: str, url_patterns: list[str], fallback_url: str) -> Page: ...
    async def list_pages() -> list[PageInfo]: ...
```

示例归属：

- `taobao`: `taobao.com`, `tmall.com`, `world.taobao.com`
- `tmall`: `tmall.com`, `detail.tmall.com`, `list.tmall.com`
- `jd`: `jd.com`, `item.jd.com`, `search.jd.com`, `mall.jd.com`
- `douyin_cs`: `leads.cluerich.com`, `zijieapi.com`

### NetworkObserver

职责：

- 统一注册 `page.on("response")` 和 `page.on("websocket")`。
- 支持按 host/path 过滤。
- 保存 raw payload。
- 向 Adapter 发送结构化事件。

接口：

```python
class NetworkObserver:
    def add_rule(name: str, url_markers: list[str], parse_json: bool = True) -> None: ...
    async def attach(page: Page) -> None: ...
    def subscribe(handler: Callable[[NetworkEvent], Awaitable[None]]) -> None: ...
```

ECAgent 用途：

- 淘宝/Tmall/JD 搜索页 XHR 中抽商品字段。
- 详情页 price/SKU 接口字段辅助 DOM 结果。

Douyin 用途：

- IM response/websocket 捕获。
- raw payload 留档。
- 会话和消息幂等归并。

### DOMExtractor

职责：

- 封装通用 JS evaluate。
- 标准化文本、链接、图片、表格、可点击元素。
- 不包含平台领域逻辑，平台规则放 Adapter。

接口：

```python
class DOMExtractor:
    async def text_lines(page: Page) -> list[str]: ...
    async def links(page: Page, url_patterns: list[str]) -> list[LinkNode]: ...
    async def images(page: Page) -> list[ImageNode]: ...
    async def visible_elements(page: Page) -> list[VisibleElement]: ...
```

### ActionExecutor

职责：

- 将动作执行统一封装，减少各脚本直接散落 `locator.click/fill/press`。
- 提供 selector、text、point、keyboard 四类执行方式。
- 注入节流、随机等待、失败截图。

接口：

```python
class ActionExecutor:
    async def fill_and_submit(locator, text: str) -> None: ...
    async def click_selector(selector: str) -> bool: ...
    async def click_text(text: str) -> bool: ...
    async def type_native(text: str) -> None: ...
    async def wait_human(reason: str) -> None: ...
```

策略：

- 数据采集优先 DOM/CDP。
- 登录、验证码、强风控进入 `NeedHuman`。
- 发送消息、弹窗确认、筛选按钮可以逐步加入 OCR/坐标 fallback。

### RiskDetector

职责：

- 检测登录失效、验证码、滑块、访问受限、页面空白、频控。
- 给 Orchestrator 返回可恢复或需要人工接管的状态。

统一异常：

```text
LoginRequired
CaptchaRequired
RateLimited
AccessDenied
SelectorChanged
NetworkBlocked
NeedHuman
```

## Platform Adapter 接口

所有 Adapter 继承统一接口：

```python
class PlatformAdapter:
    id: str
    async def open(self, session: BrowserSession) -> Page: ...
    async def health(self, page: Page) -> PlatformHealth: ...
    async def run_task(self, task: WebTask, ctx: WebAgentContext) -> WebTaskResult: ...
```

电商 Adapter 继承：

```python
class ECPlatformAdapter(PlatformAdapter):
    async def search(self, target: ProductTarget) -> SearchResult: ...
    async def sort_by_sales(self, page: Page) -> SortResult: ...
    async def extract_list(self, page: Page) -> list[ProductCandidate]: ...
    async def extract_detail(self, candidate: ProductCandidate) -> ProductSnapshot: ...
    async def normalize(self, raw: dict) -> ProductSnapshot: ...
```

客服 Adapter 继承：

```python
class CSPlatformAdapter(PlatformAdapter):
    async def list_conversations(self) -> list[Conversation]: ...
    async def get_current_chat(self) -> ConversationSnapshot: ...
    async def watch_messages(self) -> AsyncIterator[MessageEvent]: ...
    async def send_reply(self, conversation_id: str, text: str) -> SendResult: ...
```

## ECAgent 扩展设计

### 统一电商领域模型

```python
class ProductTarget:
    platform: str
    brand: str
    series: str
    keyword: str
    official_store_names: list[str]

class ProductSnapshot:
    platform: str
    product_id: str
    product_url: str
    title: str
    shop_name: str
    shop_type: str
    official_store_flag: bool
    sku_name: str
    spec_text: str
    piece_count: int | None
    final_price: float | None
    unit_price: float | None
    sales_text: str
    rank_position: int | None
    image_url: str
    screenshot_path: str
    source: list[str]
    confidence: float
    notes: str
```

### Taobao/Tmall

实现方式：

- 短期保留 `TaobaoAdapter` 覆盖 `taobao.com` 和 `tmall.com`。
- 中期拆 `TmallAdapter`，专门支持：
  - `list.tmall.com/search_product.htm`
  - `detail.tmall.com/item.htm`
  - 天猫旗舰店/官方店识别。
- 现有 `ECAgent/src/taobao.py` 的代码先搬进 adapter，不急着重写。

关键增强：

- 登录态检测：淘宝首页/搜索页是否出现登录入口、nick、验证码。
- 搜索结果网络监听：优先从 XHR JSON 提取商品卡片。
- 店铺白名单：品牌官方旗舰店、天猫国际自营、官方自营。
- 系列匹配规则：品牌词、系列词、排除词、SKU 词。
- 详情页价格源：DOM price、接口 price、OCR price 三方记录。

### JD

新增 `JDAdapter`。

页面范围：

- 首页：`https://www.jd.com`
- 搜索：`https://search.jd.com/Search`
- 商品详情：`https://item.jd.com/{sku}.html`
- 店铺：`https://mall.jd.com/*`

字段映射：

- `product_id`: JD SKU id。
- `shop_type`: `self_operated` when 京东自营/京东超市/京东大药房自营。
- `sales_text`: 评论数、销量或评价数，JD 可见字段不一定等同付款人数。
- `final_price`: DOM 可见到手价，必要时 OCR 校验。
- `sku_name`: 默认选中的规格/颜色/套餐。

JD 风险点：

- 搜索页懒加载明显，需要滚动触发。
- 价格可能异步渲染，详情页需等待价格区域稳定。
- 自营与旗舰店判断要分开，不能简单把旗舰店当自营。
- 促销价、券后价、PLUS 价需要标记来源，不能混成一个价格。

建议 JD MVP 流程：

1. 打开 `jd.com`，检测登录态。
2. 搜索 keyword。
3. 抽取搜索结果前 N 个 sku。
4. 优先保留自营/官方旗舰店候选。
5. 打开详情页，抽标题、店铺、价格、SKU、规格、主图、截图。
6. 输出同一 `ProductSnapshot`。

### ECAgent Runner

新增统一入口：

```text
ECAgent/ec_run.py
  --platform taobao|tmall|jd
  --keyword "杜蕾斯 001"
  --brand "杜蕾斯"
  --series "001"
  --max-candidates 12
  --detail-limit 5
```

旧入口 `eca_taobao.py` 保留，内部调用新 runner。

Web API 从：

```text
/api/taobao/runs
/api/taobao/results
```

升级为：

```text
/api/ec/runs
/api/ec/results?platform=jd
/api/ec/exports
/api/browser/health
```

## Douyin 支持设计

### Douyin Adapter

`DouyinCSAdapter` 负责平台细节：

- 找到客服页面。
- 监听 response/websocket。
- 从 DOM fallback 读取当前会话。
- 识别最新 incoming 消息。
- 发送回复。
- 输出标准 `Conversation`、`Message`、`MessageEvent`。

保留现有脚本入口：

- `douyin_export_recent.py` 调用 adapter 的 capture API。
- `douyin_current_chat_reply.py` 调用 adapter 的 current chat + send API。
- `douyin_watch.py` 后续可以替换为 Orchestrator 长驻任务。

### 消息幂等

统一 message key：

```text
platform + conversation_key + message_id
```

没有 message_id 时 fallback：

```text
platform + conversation_key + timestamp + direction + sender + sha256(text)
```

发送记录必须落库或落状态文件：

- incoming reported
- reply generated
- reply sent
- outgoing reported

### 人工接管

出现以下状态时停止发送，只记录事件：

- 未登录。
- 找不到输入框。
- 页面切到非客服会话。
- 最新消息不是 incoming。
- 回复文本为空或包含异常换行。
- 页面出现验证码、权限弹窗、网络错误。

## 任务和数据模型

建议最小表结构：

```text
tasks
  id, type, platform, status, input_json, output_json, error, created_at, updated_at

task_events
  id, task_id, level, step, message, payload_json, screenshot_path, created_at

browser_sessions
  id, provider, cdp_url, user_data_dir, status, last_health_json, updated_at

products
  id, platform, product_id, product_url, title, shop_name, shop_type

product_snapshots
  id, product_ref, task_id, brand, series, sku_name, final_price, unit_price,
  sales_text, screenshot_path, source_json, confidence, captured_at

conversations
  id, platform, conversation_key, title, contact_id, last_message_at

messages
  id, platform, conversation_key, message_key, direction, sender, text,
  raw_json, first_seen_at, last_seen_at

captures
  id, task_id, platform, kind, path, raw_path, screenshot_path, created_at
```

MVP 可以先继续写 JSON/CSV，但字段按这个模型对齐，便于后续迁移。

## 配置设计

统一配置示例：

```yaml
browser:
  provider: chrome_cdp
  cdp_url: http://127.0.0.1:9222
  user_data_dir: C:\workspace\GUIAgent\.chrome-webagent-profile

webagent:
  action_delay_ms:
    min: 300
    max: 1200
  screenshot_on_error: true
  raw_capture: true

ec:
  default_platforms: [tmall, jd]
  max_candidates: 12
  detail_limit: 5
  use_ocr: false
  targets:
    - platform: tmall
      brand: 杜蕾斯
      series: "001"
      keyword: 杜蕾斯 001
      official_store_names:
        - durex杜蕾斯官方旗舰店
        - 杜蕾斯官方旗舰店
    - platform: jd
      brand: 杜蕾斯
      series: "001"
      keyword: 杜蕾斯 001
      official_store_names:
        - 杜蕾斯京东自营旗舰店

douyin:
  session_url: https://leads.cluerich.com/pc/cs/chat/session
  reply_engine: revai
  watch_interval_sec: 3
```

## 迁移路线

### Phase 1: 抽 BrowserRuntime

- 新增 `webagent/browser_runtime.py`。
- ECAgent `src/browser.py` 改为薄 wrapper。
- Douyin 脚本统一使用 BrowserRuntime attach。
- 增加 `/api/browser/health`。

验收：

- `eca_taobao.py` 和 `douyin_current_chat_reply.py` 都能连接同一个 `9222`。
- Chrome 使用专用 profile，登录态可复用。

### Phase 2: 抽 PageRegistry 和 NetworkObserver

- ECAgent 使用 PageRegistry 找淘宝/Tmall/JD tab。
- Douyin 使用 PageRegistry 找客服 tab。
- Douyin 导出逻辑改用 NetworkObserver。
- ECAgent 增加搜索页 response 捕获，先只落 raw，不强依赖接口字段。

验收：

- 能列出当前 CDP 里所有页面。
- 能区分淘宝、天猫、京东、抖音客服页面。
- Douyin raw payload 保存逻辑不回退。

### Phase 3: 抽 EC Adapter

- 搬迁现有 `taobao.py` 到 `webagent/adapters/taobao.py`。
- 新增 `tmall.py`，先复用 Taobao 详情逻辑。
- 新增 `jd.py`，实现搜索页和详情页 MVP。
- 新增 `ec_run.py`。

验收：

- `--platform tmall` 能产出 `ProductSnapshot`。
- `--platform jd` 能产出同结构 JSON/CSV。
- Web 前端能按平台筛选结果。

### Phase 4: 抽 Douyin Adapter

- `douyin_export_recent.py` 只保留 CLI 和文件输出。
- `douyin_current_chat_reply.py` 只保留 CLI、LLM 调用和 send 开关。
- 监听、DOM fallback、send 进入 `DouyinCSAdapter`。

验收：

- dry-run 回复仍可用。
- `--send` 仍只回复最新 incoming。
- 导出 recent chats 仍包含 raw payload 和 DOM fallback。

### Phase 5: 统一 Orchestrator 和 Console

- 引入 tasks/task_events。
- ECAgent Web backend 改为通用 task runner。
- Douyin 长驻任务在 Console 中显示状态。
- 人工接管状态可以在 Console 看到。

## 关键设计取舍

- CDP 是核心边界。只要 provider 暴露 CDP，业务 Adapter 不关心是 Chrome、CloakBrowser 还是远程浏览器。
- DOM/CDP 是主感知链路。OCR/VLM 只做登录/弹窗/价格可视校验/低置信度兜底。
- 平台 Adapter 只管平台差异，不直接决定任务状态和存储模型。
- ECAgent 和 Douyin 的迁移要薄切，不做一次性大重构。
- JD/Tmall 扩展先追求同结构输出，再追求店铺内搜索、官方店白名单和价格源精细化。

## 下一步实施建议

优先级最高的三件事：

1. 新增 `webagent/browser_runtime.py`，并让 ECAgent/Douyin 都复用它。
2. 新增 `webagent/adapters/ec_base.py`，把当前 `ProductRecord` 升级成跨平台 `ProductSnapshot`。
3. 新增 `JDAdapter` MVP，只做搜索结果前 N 个和详情页基础字段，先跑通同结构导出。

