# 国内 AI 客户端问答批量抓取方案

> 需求来源：求解器厂商 / 决策式 AI / 纯电家用车 三类问题，覆盖 DeepSeek、豆包、文心一言、腾讯元宝、千问、Kimi 六个平台，输出问答正文 + 参考信源链接（打包 JSON 或 API）。

更新时间：2026-08-11

---

## 一、需求摘要

| 维度 | 内容 |
|------|------|
| **问题** | ① 求解器厂商推荐？ ② 决策式 AI 厂商代表有哪些？ ③ 全球纯电家用车推荐？ |
| **平台** | DeepSeek（Ds）、豆包、文心一言、腾讯元宝、通义千问、Kimi |
| **数据** | 客户端问答正文 + citations（参考信源 URL / 域名） |
| **交付** | 批量 JSON 打包，或 HTTP API 返回（与现有 GEO 结果结构对齐） |

问题清单见：`questions_cn.json`

---

## 二、与现有 GEO 代码的关系

当前 `test.py` 已验证的链路：

```
run_with_cdp.bat → Chrome User Data_CDP → browser-use Agent → extract → .scrape_cache/{site}/
```

已支持站点：`kimi`、`chatgpt`、`perplexity`、`gemini`（见 `SITE_CONFIGS`）。

**本需求是同一套架构的「国内六平台 + 三问题批量」扩展**，不重写底层，只做：

1. **站点配置扩展** — `chinese_ai_sites.py` + 合并进 `SITE_CONFIGS`
2. **批量编排** — `batch_scrape_cn.py`（按 site × question 矩阵跑）
3. **浏览器运行时** — 优先 **CloakBrowser CDP**（反检测），回退 **Chrome CDP**（与现网一致）
4. **输出聚合** — `exports/cn_ai_batch_{timestamp}.json` + 可选 FastAPI 只读接口

### 复用清单

| 模块 | 文件 | 复用方式 |
|------|------|----------|
| CDP 连接 / 代理绕过 | `test.py` 顶部 patch | 原样复用 |
| Agent task 模板 | `test.py` `_scrape_via_browser` | 按站点复制并改 UI 步骤 |
| citations 提取 | `_extract_citations_from_html`、`_merge_citations` | 通用 + 站点 parser |
| HTML 落盘 | `_save_html`、`_get_page_html_via_direct_cdp` | 原样复用 |
| 缓存结构 | `.scrape_cache/{site}/results|runs|html` | 原样复用 |
| LLM | DashScope `qwen-plus` 驱动 Agent | 原样复用 |

### 待新增

| 模块 | 说明 |
|------|------|
| `batch_scrape_cn.py` | 读取 `questions_cn.json`，循环 site × question |
| `api_server.py`（可选） | `GET /batch/{id}`、`GET /results?site=&question=` |
| 各站 UI 步骤 | 豆包/文心/元宝/千问的 input、发送、联网开关（参考 ChatGPT Web search 做法） |

---

## 三、为什么用 CloakBrowser

仓库架构文档（`docs/AGENT_UNIFIED_ARCHITECTURE.md`）已规划 `cloak_cdp` 作为 Browser Runtime 选项。

| 对比 | Chrome CDP | CloakBrowser CDP |
|------|------------|------------------|
| 登录态 | `User Data_CDP`，已验证 | 独立 profile `CloakBrowser/User Data_CN_AI` |
| 反检测 | 一般 | stealth Chromium，降低自动化指纹 |
| 与 browser-use | `BrowserProfile(cdp_url=...)` | **相同**，仅换 CDP 端口 |
| 端口建议 | 9222 | **9242**（避免与 Chrome 冲突） |

**推荐策略：**

- **登录阶段**：Cloak 或 Chrome 均可（`open_login_sites_cloak.bat` / `open_login_sites_cdp.bat`）
- **批量抓取**：优先 Cloak `9242`；若某站 Cloak 异常再切 Chrome `9222` 对比

安装：`py -3.11 -m pip install cloakbrowser`

---

## 四、平台 URL 与登录

定义见 `chinese_ai_sites.py`：

| ID | 名称 | URL |
|----|------|-----|
| `deepseek` | DeepSeek | https://chat.deepseek.com |
| `doubao` | 豆包 | https://www.doubao.com/chat |
| `yiyan` | 文心一言 | https://yiyan.baidu.com |
| `yuanbao` | 腾讯元宝 | https://yuanbao.tencent.com/chat |
| `qianwen` | 通义千问 | https://tongyi.aliyun.com/qianwen |
| `kimi` | Kimi | https://kimi.moonshot.cn |

### 一次性登录（已实现）

```bat
REM 方式 A：Chrome CDP（与现有 GEO 一致，profile 在 User Data_CDP）
cd GEO
open_login_sites_cdp.bat

REM 方式 B：CloakBrowser stealth（端口 9242）
open_login_sites_cloak.bat

REM 方式 C：Chrome 已开 CDP 时仅开标签
set CDP_URL=http://127.0.0.1:9222
py -3.11 open_login_sites.py
```

登录完成后 **不要清 profile**，后续抓取自动复用 cookies。

---

## 五、数据输出格式

### 5.1 单条结果（与现有 `ChatGPTResult` 一致）

```json
{
  "site": "kimi",
  "site_name": "Kimi",
  "question_id": "solver_vendors",
  "prompt": "求解器厂商推荐？",
  "answer": "……",
  "citations": ["https://example.com/..."],
  "citation_domains": ["example.com"],
  "brand_mentioned": null,
  "brand_rank": null,
  "scraped_at": "2026-08-11T21:00:00+08:00",
  "meta": {
    "browser": "cloak_cdp",
    "cdp_url": "http://127.0.0.1:9242",
    "html_path": ".scrape_cache/kimi/html/{cache_key}.html"
  }
}
```

`brand_*` 字段在「品牌监测」类问题时可填；当前三题可为 `null`。

### 5.2 批量打包

```json
{
  "batch_id": "cn-ai-20260811-001",
  "questions": ["solver_vendors", "decision_ai_vendors", "ev_cars_global"],
  "sites": ["deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"],
  "results": [ /* 18 条，site × question */ ],
  "summary": {
    "total": 18,
    "ok": 16,
    "failed": 2,
    "with_citations": 14
  }
}
```

输出路径建议：`GEO/exports/cn_ai_batch_{batch_id}.json`

### 5.3 API（可选 Phase 2）

```
POST /api/v1/scrape/batch     # body: { questions, sites } → batch_id
GET  /api/v1/batch/{id}       # 完整打包
GET  /api/v1/results          # ?site=kimi&question_id=solver_vendors
```

可与 `WXWorkAgent` FastAPI 合并，或 `GEO/api_server.py` 独立起服务。

---

## 六、抓取流程（单 site × 单 question）

与 `IMPLEMENTATION_NOTES.md` 保持一致：

1. **连接 CDP**（Cloak 9242 或 Chrome 9222），`NO_PROXY=*`
2. **导航**到站点 URL（CDP 模式 `initial_actions=[]`，由 `run_with_cdp` 或 batch 脚本打开）
3. **Agent 步骤**（各站 UI 不同，需单独 task 模板）：
   - 开启联网 / 深度思考（若需要 citations）
   - `input` 问题
   - `click` 发送
   - `wait(30~45)` 等待回答与信源加载
   - `extract` **最多 2 次**
   - `done()` 返回 JSON
4. **后处理**：
   - `ast.literal_eval` / JSON 解析 Agent 输出
   - 从 HTML 补充 citations（`_extract_citations_from_html`）
   - 保存 HTML 到 `html/`
5. **缓存**：`results/{cache_key}.json`，`cache_key = hash(site + question_id + prompt)`

### 各站差异（预估，需实测补 task）

| 站点 | 联网/citations | 备注 |
|------|----------------|------|
| Kimi | icon-cache 域名图标 | 已有 `_parse_answer_kimi` |
| DeepSeek | 需开「联网搜索」 | 类似 ChatGPT Web search |
| 豆包 | 可能有引用卡片 | 待录 DOM |
| 文心一言 | 文末引用 | 待录 DOM |
| 元宝 | 混元/DS 切换 + 联网 | 待录 DOM |
| 千问 | 通义联网 | 待录 DOM |

ChatGPT 已验证技巧（仅 ChatGPT/Gemini 用）：prompt 追加 `Give me all the links you searched.` — **国内站默认不加**，除非实测有效。

---

## 七、实施步骤

### Phase 0 — 登录准备（当前）

- [x] `chinese_ai_sites.py`、`questions_cn.json`
- [x] `open_login_sites.py` + CDP/Cloak 启动脚本
- [ ] **你方**：运行 `open_login_sites_cdp.bat` 或 `open_login_sites_cloak.bat`，完成六平台登录

### Phase 1 — Kimi 三题回归

- [ ] `batch_scrape_cn.py` 骨架：读 questions，调 `ChatGPTScraper.scrape`
- [ ] Kimi × 3 题端到端，确认 citations + HTML

### Phase 2 — 扩展 SITE_CONFIGS + task 模板

- [ ] 将 `chinese_ai_sites` 并入 `SITE_CONFIGS`
- [ ] 逐站录 UI：DeepSeek → 豆包 → 文心 → 元宝 → 千问
- [ ] 每站至少 1 题 `--test-extract` 验证

### Phase 3 — 全量批量 + 导出

- [ ] 18 条矩阵跑通，失败重试与 skip
- [ ] `exports/` 打包 + summary
- [ ] 更新 `IMPLEMENTATION_NOTES.md`

### Phase 4 — API（按需）

- [ ] `api_server.py` 或接入统一 Console

---

## 八、运行命令速查

```bat
cd GEO

REM 1. 登录六平台
open_login_sites_cdp.bat          REM Chrome 9222
REM 或
open_login_sites_cloak.bat        REM Cloak 9242

REM 2. 单站测试（现有）
set CDP_URL=http://127.0.0.1:9222
set SKIP_KILL_CHROME=1
py -3.11 test.py --site kimi

REM 3. 批量（待实现）
set CDP_URL=http://127.0.0.1:9242
set SKIP_KILL_CHROME=1
py -3.11 batch_scrape_cn.py

REM 4. 列出平台 URL
py -3.11 open_login_sites.py --list
```

环境变量（与现网一致）：

- `CDP_URL` — CDP HTTP 地址
- `SKIP_KILL_CHROME=1` — 不杀 Chrome/Cloak
- `NO_PROXY=*` — 本地 CDP 必设
- `PYTHONIOENCODING=utf-8` — Windows 日志不乱码

---

## 九、风险与注意

1. **账号与合规**：仅抓取己方账号可见内容；频率控制在人工可接受范围。
2. **UI 变更**：国内站迭代快，task 模板需版本化（`task_templates/{site}.md`）。
3. **Citations 质量**：Agent 可能幻觉 URL；必须以 HTML fallback + 域名白名单过滤（沿用 `_is_valid_citation_url`）。
4. **Cloak 首次下载**：约 200MB，需留时间；失败可回退 Chrome CDP。
5. **并发**：建议单浏览器串行 site×question，避免同 profile 多 Agent 争用。

---

## 十、相关文件

| 文件 | 作用 |
|------|------|
| `CHINESE_AI_BATCH_PLAN.md` | 本文档 |
| `chinese_ai_sites.py` | 六平台定义 |
| `questions_cn.json` | 三问题 |
| `open_login_sites.py` | CDP 开多标签 |
| `open_login_sites_cdp.bat` | Chrome 登录流程 |
| `open_login_sites_cloak.bat` | Cloak 登录流程 |
| `start_cloak_browser.py` | Cloak CDP 守护 |
| `test.py` | 核心 Scraper（待扩展 SITE_CONFIGS） |
| `IMPLEMENTATION_NOTES.md` | CDP/Agent 踩坑 |
| `docs/AGENT_UNIFIED_ARCHITECTURE.md` | Cloak 架构位 |
