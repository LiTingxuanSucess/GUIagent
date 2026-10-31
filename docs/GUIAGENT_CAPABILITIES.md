# GUIAgent 能力全景文档

更新时间：2026-06-01

## 1. 概述

`GUIAgent` 是一个多 Agent 自动化平台，面向 **Web 浏览器自动化** 与 **Windows 桌面 GUI 自动化** 两类场景。各 Agent 独立运行，但共享部分基础设施（CDP 浏览器、OCR 服务、统一控制台、SQLite 数据层）。

### 1.1 设计原则

| 层级 | 能力 | 说明 |
|------|------|------|
| 数据感知 | DOM/CDP | 结构化抓取优先：消息、价格、商品字段 |
| UI 交互 | OCR + 鼠标键盘 | 弹窗、按钮、输入框等高风险操作 |
| 异常兜底 | VLM | 复杂页面语义判断，低频使用 |
| 浏览器 | CDP 连接 | 复用真实 Chrome 登录态，Playwright `connect_over_cdp` |

### 1.2 项目总览

| 项目 | 定位 | 状态 | Web 控制台 |
|------|------|------|-----------|
| **WXWorkAgent** | 企微自动化 + 统一 Agent Console | 生产可用 | 是（:8000 / :5173） |
| **DouyinCSAgent** | 抖音企业号客服自动回复 | 生产可用 | 接入 WXWork Console |
| **ECAgent** | 电商价格追踪 | 淘宝 MVP 可用；JD/Tmall 部分 | 是（:8010 / :5174） |
| **APAgent** | Apple Store 自动下单 + SKU 价格采集 | MVP 可用 | 是（Vue + FastAPI） |
| **PRAgent** | Google News 舆情抓取 | MVP 可用 | 否 |
| **GEO** | AI 对话站点引用抓取 | 可用 | 否 |
| **agent_platform** | 共享平台模块（线索/会话 API） | 脚手架 | 部分（与 WXWork 重复） |
| **webagent** | 共享 Web 自动化核心 | 仅设计文档 | — |
| **UI-TARS-desktop** | 桌面 GUI Agent 集成 | 空目录占位 | — |
| **workflow** | 业务流程说明 | 仅笔记 | — |

---

## 2. 共享基础设施

### 2.1 浏览器运行时（CDP）

各 Agent 通过 Chrome DevTools Protocol 附着已登录浏览器，避免重复登录：

| CDP 端口 | 使用方 | Profile |
|----------|--------|---------|
| **9222** | ECAgent、DouyinCSAgent、WXWorkAgent（朋友圈） | 默认或 `.chrome-webagent-profile` |
| **9223** | APAgent、WXWorkAgent 管理后台 | 独立 admin profile |
| **9224** | DouyinCSAgent 服务脚本（可选） | `.chrome-cdp-profile` |

连接方式统一为 Playwright `chromium.connect_over_cdp(url)`。

### 2.2 OCR 服务

- **服务**：`WXWorkAgent/wxwork_ocr_service.py`
- **地址**：`http://127.0.0.1:8765`
- **技术**：PaddleOCR 3.x + PaddlePaddle
- **用途**：企微桌面 UI 识别；ECAgent 可选用于价格校验
- **调用方**：WXWorkAgent 桌面脚本、`ECAgent/src/ocr_client.py`

### 2.3 统一 Agent Console（WXWorkAgent Web）

当前事实上的平台中枢，聚合多渠道数据：

- **后端**：FastAPI，`http://127.0.0.1:8000`
- **前端**：Vue 3 + Vite，`http://127.0.0.1:5173`
- **数据库**：SQLite `WXWorkAgent/data/agent_platform.db`

### 2.4 外部依赖

| 服务 | 用途 | 使用方 |
|------|------|--------|
| RevAI (`car.revai.cn`) | 汽车领域对话回复 | DouyinCSAgent |
| DashScope Qwen / Qwen-VL | 文本/VLM 决策 | DouyinCSAgent、PRAgent、GEO、WXWorkAgent |
| car-agent（外部仓库） | 本地 LLM 兼容服务基线 | DouyinCSAgent |
| Browser Use Cloud | 云端浏览器 Agent（文档参考） | GEO |
| CloakBrowser | 反指纹浏览器（PoC） | ECAgent |

---

## 3. 各 Agent 详细能力

### 3.1 WXWorkAgent — 企业微信自动化 + 统一控制台

**目标平台**：企业微信 PC 客户端、企微管理后台（`work.weixin.qq.com`）

#### 3.1.1 桌面自动化（OCR 驱动）

| 能力 | 脚本 | 状态 |
|------|------|------|
| 单联系人监听 + 自动回复 | `wxwork_watch.py` | 可用 |
| 多会话并行监听 | `wxwork_multi_watch.py` | 可用 |
| 结构化监听 + 主数据合并 | `wxwork_agent_loop.py` | 可用 |
| 聊天记录 OCR 导出（含滚动） | `wxwork_export_recent.py` | 可用 |
| 按手机号添加外部客户 | `wxwork_add_customer.py` | 可用 |
| 外部联系人导出 | `wxwork_export_external_contacts.py` | 可用 |
| 朋友圈发布（管理后台 CDP） | `wxwork_pengyouquan.py` | 可用 |

#### 3.1.2 Agent Console API

| 端点 | 功能 |
|------|------|
| `GET/POST /api/leads` | 线索 CRUD |
| `POST /api/leads/{id}/trigger` | 触发企微加客户 |
| `POST /api/leads/{id}/reset` | 重置线索状态 |
| `GET/POST /api/conversations` | 统一会话（抖音 + 企微） |
| `GET /api/conversation_threads` | 会话线程列表 |
| `POST /api/douyin/revai/session/clear` | 清理 RevAI 会话记忆 |
| `GET/POST /api/import/douyin-*` | 抖音归档导入 |
| `GET/POST /api/moments` | 朋友圈任务 |
| `GET /api/wxwork/exports` | 企微导出浏览 |
| `GET /api/wxwork/chats` | 企微会话详情 |

#### 3.1.3 已知短板

- 后端为单文件 `main.py`，待模块化
- 任务队列 / 事件审计表尚未统一
- OCR 服务概念上应提升为全平台公共服务

---

### 3.2 DouyinCSAgent — 抖音企业号客服

**目标平台**：抖音线索/客服后台（`leads.cluerich.com`）

#### 3.2.1 核心能力

| 能力 | 脚本 | 说明 |
|------|------|------|
| 当前会话读取 + 自动回复 | `douyin_current_chat_reply.py` | 支持 dry-run / `--send` / watch 模式 |
| 网络监听 + Qwen 回复循环 | `douyin_watch.py` | 拦截 IM response |
| 会话导出与合并 | `douyin_export_recent.py` | response + WebSocket + DOM fallback |
| Windows 服务编排 | `douyin_service.ps1` | 本地 LLM + watcher |
| 本地 RevAI 兼容服务 | `local_carzone_compat_server.py` | 端口 8787 |

#### 3.2.2 回复引擎

- **RevAI**：`https://car.revai.cn/api/project/carzone/compat/chat/respond`
- **Qwen**：DashScope 文本模型
- **local_carzone**：本地兼容端点

#### 3.2.3 数据流

```
抖音客服页面 (CDP)
  → DOM/CDP 读取消息
  → LLM 生成回复
  → Playwright 填入输入框发送
  → POST /api/conversations (WXWork Console)
  → 导出 JSON 归档
```

#### 3.2.4 已实现细节

- 换行替换为空格，避免抖音输入框 Enter 截断
- 手机号检测与留资上报
- RevAI session_id 规则：`douyin-{会话标题}`
- 平台按钮可清理指定会话记忆

#### 3.2.5 待完善

- 完整会话列表遍历与增量同步
- OCR 兜底输入框/发送按钮定位
- 独立 `messages` 表与幂等去重
- 私域转化闭环（见 PRD）

---

### 3.3 ECAgent — 电商价格追踪

**目标平台**：淘宝（MVP）、天猫、京东（部分）

#### 3.3.1 数据采集

| 能力 | 入口 | 状态 |
|------|------|------|
| 淘宝搜索 + 销量排序 + 列表抓取 | `eca_taobao.py` | MVP 可用 |
| 商品详情页字段解析 | `src/taobao.py` | MVP 可用 |
| 多平台采集 | `eca_collect_web.py` | 部分可用 |
| CloakBrowser 反爬 PoC | `cloak_*_probe.py` | 实验性 |
| JSON/CSV 导出 + 截图 | `src/exporter.py` | 可用 |

#### 3.3.2 采集字段

- 标题、价格、付款/收货人数、店铺名
- SKU 规格、片数、片单价
- 搜索页/详情页截图
- 品牌/系列匹配与候选排序

#### 3.3.3 Web 控制台

- **后端**：`:8010`
- **前端**：`:5174`（Vue + ECharts 价格趋势）

| API | 功能 |
|-----|------|
| `/api/brands`, `/api/series`, `/api/platforms` | 品牌/系列/平台 CRUD |
| `POST /api/runs` | 触发采集任务 |
| `GET /api/products` | 商品列表 |
| `GET /api/trends` | 价格趋势 |

#### 3.3.4 待完善

- JD/Tmall 生产级 Adapter
- 抖音电商 Adapter
- 官方旗舰店白名单
- 搜索页 XHR 结构化抓取
- 接入统一任务模型

---

### 3.4 APAgent — Apple Store 自动下单

**目标平台**：Apple Store 官网（`apple.com/shop`）

#### 3.4.1 核心能力

| 能力 | 说明 | 状态 |
|------|------|------|
| SKU 配置管理 | iPhone 17 Pro/Pro Max、HomePod mini 等 | 可用 |
| 买家/身份 Profile | 收货人、地址、支付方式 | 可用 |
| 自动结账流程 | 加购 → 配送 → 账单 → 审核页 | MVP 可用 |
| 支付 | 信用卡或礼品卡 | 可用 |
| 安全停止 | 停在 `ready_for_submit`，不点「Place Order」 | 已实现 |
| SKU 价格采集 | 全目录价格抓取 + QA | 可用 |

#### 3.4.2 Web 控制台 API

| 端点 | 功能 |
|------|------|
| `/api/skus` | SKU Profile CRUD |
| `/api/identities` | 买家身份 CRUD |
| `/api/tasks` | 下单任务 CRUD |
| `POST /api/tasks/{id}/run` | 执行下单 |
| `/api/sku-price-runs` | 价格采集批次 |
| `/api/sku-price-records` | 价格记录查询 |

#### 3.4.3 配套文档与数据

- `docs/APPLE_PRODUCT_CATALOG.*` — Apple 产品目录
- `docs/APAGENT_PRODUCTION_ARCHITECTURE.md` — 生产安全架构
- `docs/APAGENT_SKU_*` — SKU 采集策略与 QA

#### 3.4.4 待完善

- PCI 合规支付保险库
- 生产级任务队列与 Worker 池
- 礼品卡 + 信用卡组合支付

---

### 3.5 PRAgent — Google News 舆情抓取

**目标平台**：Google News（`news.google.com`）

#### 3.5.1 能力

| 组件 | 作用 |
|------|------|
| Playwright | 浏览器自动化 |
| OmniParser（可选） | 本地屏幕元素定位，减少 API 调用 |
| Qwen-VL | 导航决策 |

#### 3.5.2 使用

```bash
python main.py --keyword "XREAL" --pages 30 --output xreal_news.csv
```

输出 CSV/JSON，支持增量保存。

#### 3.5.3 状态

- 独立 CLI 工具，未接入 Agent Console
- 需要 `DASHSCOPE_API_KEY`

---

### 3.6 GEO — AI 对话站点引用抓取

**目标平台**：Kimi、ChatGPT、Perplexity、Gemini

#### 3.6.1 能力

- 使用 browser-use + CDP 驱动 AI 对话页面
- 提取回答文本 + 引用 URL
- 站点特定 citation 解析器
- 缓存目录：`.scrape_cache/{site}/results|runs|html`
- 多站点批量运行（`run_all_sites.bat`）

#### 3.6.2 状态

- 功能可用，独立运行
- 未接入统一平台
- 详细运维说明见 `GEO/IMPLEMENTATION_NOTES.md`

---

### 3.7 agent_platform — 共享平台模块

**定位**：从 WXWorkAgent 抽出的共享 API 层雏形

| 模块 | 内容 |
|------|------|
| `router.py` | FastAPI 路由：leads、conversations、conversation_threads |
| `db.py` | SQLite 操作 |
| `config.py` | 平台配置 |

**状态**：脚手架阶段；WXWorkAgent 后端仍保留更完整实现，两者尚未完全合并。

---

## 4. 跨 Agent 业务闭环

### 4.1 当前已打通的链路

```text
抖音客服 (DouyinCSAgent)
  ├─ 自动回复（RevAI/Qwen）
  ├─ 手机号识别 → 线索上报
  └─ 会话写入 Agent Console

Agent Console (WXWorkAgent)
  ├─ 统一查看抖音 + 企微会话
  ├─ 线索管理 → 触发企微加客户
  ├─ 朋友圈发布
  └─ RevAI 会话记忆清理

企业微信 (WXWorkAgent)
  ├─ OCR 监听 + 自动回复
  ├─ 按手机号加外部客户
  └─ 聊天记录导出
```

### 4.2 规划中的私域闭环（PRD，未实现）

详见 `docs/DOUYIN_WXWORK_PRIVATE_DOMAIN_PRD.md`：

1. 抖音探需（品类、区域、车型）→ 结构化画像
2. 留资后自动加企微 + 打标签
3. 标签分层运营（维修/保养活动群发）
4. 扩展视频号、小红书

### 4.3 业务流程笔记

`workflow/RAW.md` 描述目标流程：

> 抖音客服 → 自动回复 → 收集手机号 → 企微加好友 → 企微自动对话

---

## 5. 技术栈汇总

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.11 |
| Web 自动化 | Playwright (CDP)、browser-use |
| 桌面自动化 | PaddleOCR、mss、pywin32 |
| 后端 | FastAPI |
| 前端 | Vue 3 + Vite + ECharts |
| 数据库 | SQLite |
| LLM | DashScope Qwen/Qwen-VL、RevAI |
| 配置 | YAML |

---

## 6. 架构演进方向

### 6.1 已文档化、待实现

| 模块 | 文档 | 现状 |
|------|------|------|
| 统一架构 | `docs/AGENT_UNIFIED_ARCHITECTURE.md` | 设计完成 |
| WebAgent Core | `docs/WEBAGENT_CORE_ARCHITECTURE.md` | 代码未建 |
| APAgent 生产架构 | `docs/APAGENT_PRODUCTION_ARCHITECTURE.md` | 设计完成 |

### 6.2 推荐实施优先级

1. **Phase 1**：WXWork Console 升级为根级 `agent_platform`，统一 tasks/events 表
2. **Phase 2**：抽取 `BrowserRuntime`，所有 Agent 共用 CDP 连接层
3. **Phase 3**：OCR 服务标准化为公共 HTTP API
4. **Phase 4**：平台 Adapter 化（淘宝/抖音/企微）
5. **Phase 5**：CloakBrowser PoC（仅 ECAgent 淘宝流程）

### 6.3 空占位

- **`webagent/`** — 共享 Web 自动化包，目录不存在
- **`UI-TARS-desktop/`** — 桌面 GUI Agent 集成，空目录

---

## 7. 快速启动参考

### 7.1 启动 Chrome CDP

```powershell
# 通用（9222）
& "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="C:\workspace\GUIAgent\.chrome-webagent-profile" `
  --no-first-run "https://www.taobao.com"
```

### 7.2 启动各控制台

| 服务 | 命令 | 端口 |
|------|------|------|
| Agent Console 后端 | `uvicorn web.backend.main:app --port 8000` | 8000 |
| Agent Console 前端 | `npm run dev` (WXWorkAgent/web/frontend) | 5173 |
| ECAgent 后端 | `uvicorn web.backend.main:app --port 8010` | 8010 |
| ECAgent 前端 | `npm run dev` (ECAgent/web/frontend) | 5174 |
| OCR 服务 | `py wxwork_ocr_service.py` | 8765 |

### 7.3 典型 Agent 运行

```powershell
# 淘宝价格采集
py -3.11 eca_taobao.py --keyword "杜蕾斯 001" --max-candidates 12

# 抖音客服自动回复（dry-run）
py -3.11 douyin_current_chat_reply.py --watch

# 企微监听
py -3.11 wxwork_watch.py

# Google News 抓取
py -3.11 main.py --keyword "XREAL" --pages 30
```

---

## 8. 能力矩阵（一览）

| 能力域 | WXWork | Douyin | EC | AP | PR | GEO |
|--------|--------|--------|----|----|----|----|
| Web CDP 自动化 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 桌面 OCR 自动化 | ✓ | — | — | — | — | — |
| 自动回复/对话 | ✓ | ✓ | — | — | — | — |
| 数据采集/导出 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Web 管理控制台 | ✓ | 接入 | ✓ | ✓ | — | — |
| LLM 集成 | ✓ | ✓ | — | — | ✓ | ✓ |
| 线索/CRM | ✓ | 部分 | — | — | — | — |
| 任务队列/审计 | 部分 | 部分 | 部分 | 部分 | — | — |
| 生产级安全 | — | — | — | 设计中 | — | — |

---

## 9. 相关文档索引

| 文档 | 内容 |
|------|------|
| `AGENTS.md` | Windows UTF-8 编码规范 |
| `docs/AGENT_UNIFIED_ARCHITECTURE.md` | 多 Agent 统一架构设计 |
| `docs/WEBAGENT_CORE_ARCHITECTURE.md` | WebAgent 核心模块设计 |
| `docs/DOUYIN_WXWORK_PRIVATE_DOMAIN_PRD.md` | 私域转化产品需求 |
| `docs/APAGENT_PRODUCTION_ARCHITECTURE.md` | APAgent 生产架构 |
| `docs/产品价格追踪需求文档.md` | ECAgent 业务需求 |
| `browse-use.md` | Browser Use Cloud API 参考 |
| `ECAgent/README.md` | 电商 Agent 使用说明 |
| `PRAgent/README.md` | 舆情 Agent 使用说明 |
| `WXWorkAgent/web/README.md` | Agent Console 启动说明 |
