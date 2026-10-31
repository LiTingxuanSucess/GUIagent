# 国内六平台抓取研究日志

> 阶段性记录：DOM、网络 API、profiling 耗时、踩坑、优化思路。
> 研究数据：`GEO/.scrape_cache/research/{site}/{timestamp}/`
> 抓取结果：`GEO/.scrape_cache/cn_ai/{site}/{timestamp}/` + `GEO/exports/`
>
> **工作方式（2026-08-11 调整）**：不再一锅端批量；**一站一题做稳 → 记文档 → 再下一站**。  
> **Linux 服务化（2026-08-15）**：推荐架构与登录方案见 [`LINUX_CN_AI_SERVICE.md`](LINUX_CN_AI_SERVICE.md)。  
> **六站手机号登录（2026-08-15）**：DeepSeek 已走通，经验教训见 [`LOGIN_LESSONS.md`](LOGIN_LESSONS.md)。

更新时间：2026-08-15

## 结果展示（只读）

- 展示部署：[`DEPLOY_AUTODL.md`](DEPLOY_AUTODL.md) → http://proxy.revai.cn:40216/
- 抓取上 Linux：[`LINUX_CN_AI_SERVICE.md`](LINUX_CN_AI_SERVICE.md)
- 方案说明：[`VIEWER_PLAN.md`](VIEWER_PLAN.md)

---

## 总览进度

| 站点 | DOM/流程 | 网络 API | 协议直抓 | 单题验收 | 状态 |
|------|----------|----------|----------|----------|------|
| DeepSeek | ✅ | ✅ completion SSE + history | ✅ sticky SSE / Bearer history | ✅ solver_vendors | **已稳** |
| 豆包 | ✅ | ✅ completion SSE + chain/single | ✅ text_card.url | ✅ solver_vendors | **已稳** |
| 文心一言 | ✅ | ✅ history/list + searchresult | ✅ generator.value | ✅ solver_vendors | **已稳** |
| 腾讯元宝 | ✅ | ✅ chat SSE + detail | ✅ detail 优先 | ✅ solver_vendors | **已稳** |
| 通义千问 | ✅ 关弹窗+Enter | ✅ `/api/v2/chat` SSE | ✅ web_source | ✅ solver_vendors | **已稳** |
| Kimi | ✅ 新会话+联网自动 | ✅ ChatService/Chat connect+json | ✅ searchChunks/references | ✅ solver_vendors | **已稳** |

**登录后实测 URL：**

| 站点 | 配置 URL | 实测 |
|------|----------|------|
| DeepSeek | chat.deepseek.com | `https://chat.deepseek.com/a/chat/s/{uuid}` |
| Kimi | kimi.moonshot.cn | `https://www.kimi.com/` |
| 千问 | tongyi.aliyun.com/qianwen | `https://www.qianwen.com/` |
| 文心 | yiyan.baidu.com | `https://wenxin.baidu.com/` → search 页 |
| 豆包 | doubao.com | `https://www.doubao.com/chat/{conv_id}` |
| 元宝 | yuanbao.tencent.com | `https://yuanbao.tencent.com/chat/{cid}` |

---

## 方法论

1. **Profiling**：每步 `elapsed_ms` + `dom_*.html`（`cn_ai/profiler.py`）
2. **网络捕获**：Playwright `response` → `network_captures.json`
3. **优先协议**：能从 Network/API 解析则 `source=network|api:...`
4. **回退 DOM**：站点特化选择器（如 DeepSeek `.ds-markdown`）
5. **单站验收标准**：`ok=True` + `answer` 长度合理（>200）+ `citations` 为真实网页（非静态/导航噪声）

```bat
cd GEO
set CDP_URL=http://127.0.0.1:9222
set NO_PROXY=*
py -3.11 research_all_sites.py --site deepseek
py -3.11 batch_scrape_cn.py --site deepseek --question-id solver_vendors
```

---

## 踩坑（全局）

1. **千问首页弹窗**：`role=dialog` 挡住输入 → prep 关窗 + **Enter 发送**（勿点侧栏「关闭」）
2. **Kimi 域名**：`kimi.moonshot.cn` → `www.kimi.com`，输入 `.chat-input-editor`
3. **文心跳转**：`wenxin.baidu.com/search/{id}`，引用在「共参考 N 篇资料」
4. **DeepSeek PoW**：`create_pow_challenge` 后才 `completion` SSE
5. **豆包 IM**：`/im/chain/single`，引用在 `search_references`，DOM 几乎无外链
6. **Playwright `res.body()` 竞态**：profiler + capture 同时读 body → `CancelledError`；抓取时 profiler 关 `capture_bodies`
7. **批量串题**：同 tab 多题会混 DOM；每站应新对话，且 answer/citations 取**最后一条助手消息**
8. **引用过脏**：递归 walk JSON 会吸入头像/CDN/静态资源；必须站点白名单字段

---

## DeepSeek（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` / 求解器厂商推荐？ |
| export | `exports/cn-ai-20260811-220026.json` |
| answer | ~1645 字，含 Gurobi/CPLEX/COPT 等 |
| citations | 12 条真实网页 |
| source | `network:/api/v0/chat/completion` |
| profiling | send ~5.8s，wait ~16s |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/api/v0/chat/create_pow_challenge` | PoW，completion 前置 |
| POST | `/api/v0/chat/completion` | **主通道** SSE |
| GET | `/api/v0/chat/history_messages?chat_session_id=` | 兜底全文；需 `Authorization: Bearer {userToken}` |

### SSE 协议（关键）

DeepSeek 不是完整 message 帧，而是 **JSON Patch + sticky path**：

1. 首包 `v.response.fragments`：含 `SEARCH`（results.url）与 `RESPONSE` 起始字
2. `{"p":"response/fragments/-1/results","v":[...]}` → citations
3. `{"p":"response/fragments/-1/content","o":"APPEND","v":"器"}` 设当前 path
4. 之后约 700+ 条仅 `{"v":"片段"}`，**沿用当前 content path 拼接**

错误实现只取 FRAG 首字 → answer 只剩「求解器」。已修：`parse_deepseek_sse` sticky path。

### DOM

| 用途 | 选择器 |
|------|--------|
| 助手回答正文 | `.ds-markdown.ds-assistant-message-main-content`（取最后一个） |
| 引用角标 | `.ds-markdown-cite` |
| 输入框 | `textarea`（首页/会话页均有） |
| 滚动容器 | `.ds-scroll-*`（勿当正文） |

DOM 外链混有风控 CSS（fengkongcloud）、站点内链，需过滤 `deepseek.com` / `volces` / `fengkongcloud`。

### Auth 踩坑

- `page.context.request.get(history)` → `INVALID_TOKEN`
- 页内 `fetch` 不带 Bearer → 同样失败
- 正确：`localStorage.userToken` → `Authorization: Bearer ...` + `credentials:'include'`

### 实现路径（推荐优先级）

1. **UI 发问**（CDP）→ 捕获 completion SSE → sticky 拼 answer + SEARCH urls  
2. **可选**：Bearer `history_messages` 取最后一条 ASSISTANT（更完整，防流式截断）  
3. **回退**：`.ds-markdown` innerText + 过滤后的 a[href]

### 优化建议

| 方向 | 说明 | 收益 |
|------|------|------|
| A. 纯协议发问 | 复用 PoW + POST completion（不点 UI） | 去掉 send 5s + 输入不稳定 |
| B. 只拉 history | UI 发完只等 session_id，再 GET history | 解析最简单、全文最稳 |
| C. wait 条件 | 等 SSE `quasi_status=FINISHED` / DOM 长度稳定 | 比固定 sleep 更准 |
| D. 新对话 | 当前「新对话」按钮有时未跳转，session_id 复用 | 需点稳定 selector 或 `goto /` |
| E. 引用结构化 | 保留 title/snippet/cite_index，不止 url | GEO 分析更有用 |

**下一刀优先**：B（history 优先）已在 scraper 接好；若 Bearer 失败则 SSE。再考虑 A。

---

## 豆包（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` |
| export | `exports/cn-ai-20260811-220659.json` |
| answer | ~1.5–2k 字 markdown，运筹求解器分类 |
| citations | ~17–21（对齐 UI「参考 N 篇资料」） |
| source | `network:/im/chain/single` |
| profiling | send ~6s，wait ~40–45s（联网搜索偏慢） |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/chat/completion` | **流式主通道** `text/event-stream`（CHUNK_DELTA / STREAM_CHUNK） |
| POST | `/im/chain/single` | **推荐读结果**：完成后消息链，结构最完整 |
| POST | `/im/conversation/info` | 会话元数据，**勿当正文/引用源** |

### 消息结构（DOM/协议）

助手 `user_type != 1`，`content_block`：

| block_type | content 字段 | 用途 |
|------------|--------------|------|
| 10025 | `search_query_result_block.results[].text_card` | 引用：`url` / `title` / `sitename` / `summary` |
| 10000 | `text_block.text` | **回答正文** |
| 10050 | `rich_media_block` | 相关视频（非 GEO 主引用） |

### DOM

- 外链 `a[href]` **几乎为 0**（引用不渲染成 `<a>`）
- 输入：`textarea`；「新对话」可点
- 勿全树扫 HTML/`_ROUTER_DATA`：会吸入 `byteimg` CDN（曾误标 90–150 条 citation）

### 踩坑

1. 研究阶段 `body_preview` 截断 8KB，completion 不完整 → 抓取用 `NetworkCapture` 存 500KB
2. `chain/single` 发送瞬间只有 user 消息；须等生成结束后再读（或依赖 wait 后的第二次 pull）
3. 通用 `_walk_json_for_text_links` 对豆包 **禁用**
4. page state walk 已从 scraper 移除

### 优化建议

| 方向 | 说明 |
|------|------|
| A. 结束后主动 pull chain | UI 发完 → 用 cookie POST `/im/chain/single` 拉最后消息，不依赖监听 |
| B. 只解析 text_card | 已实现；可导出 title/sitename 结构化 |
| C. wait 条件 | 等 `SSE_REPLY_END` 或 UI「参考 N 篇」出现，缩短 40s 盲等 |
| D. 过滤短视频 | GEO 可丢掉 `iesdouyin.com`，只留网页/PDF |

---


## 文心一言（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` |
| export | `exports/cn-ai-20260811-221210.json` |
| answer | ~1550 字，国产/国际厂商分类 |
| citations | ~33（history `referenceList`） |
| source | `api:history/list` |
| profiling | send ~3.5s，wait ~12s |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/csaitab/searchresult` | 搜索结果卡 `data.results[].url`（约 20 条，无完整正文） |
| GET | `/csaitab/history/list` | **推荐**：`generator[].data.value` 正文 + `referenceList[].url` |
| 其它 | `aitabserver` / sidebar | **忽略**（噪声极大） |

### 结构

```
historyData[].msgList[].chatResponse[].data.message.content.generator[]
  gen[1].data.referenceList[].url   # 引用
  gen[2].data.value                 # AI 正文 markdown
```

query 在 `msg.query[].data.text.query`。

### DOM

- 页面像「搜索结果页」：`共参考 N 篇资料` + 列表
- `body.innerText` 含侧栏历史，**不能**直接当 answer
- 输入后跳 `wenxin.baidu.com/search/...`

### 踩坑

1. 全树 walk → 2万字垃圾 + 百级 CDN citation
2. `json.dumps(captures)[:2000000]` 会截断坏 JSON → 改为截断单条 body
3. `res.body()` CancelledError 需 `except BaseException`
4. history 不一定自动出现在 Network → 结束后 **页内 fetch** history/list

### 优化建议

| 方向 | 说明 |
|------|------|
| A. 按 prompt 匹配会话 | 已做 score；可再按 createTime 取最新 |
| B. 结构化引用 | 保留 title/abstract |
| C. 等待条件 | 等「共参考」或 history 含当前 query |

---


## 腾讯元宝（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` |
| export | `exports/cn-ai-20260811-221654.json` |
| answer | ~2k 字，厂商表格式推荐 |
| citations | ~18（`searchGuid.docs`） |
| source | `api:conversation_detail` |
| profiling | send ~4–5s，wait ~30s |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/api/chat/{conversationId}` | SSE 流式；**偶发正文编码脏**，慎用 |
| POST | `/api/user/agent/conversation/v1/detail` | **推荐**：`{conversationId}` |

### URL

`https://yuanbao.tencent.com/chat/{agentId}/{conversationId}`  
取 **最后一段** 为 cid（曾误用 agentId 导致 detail 空）。

### 结构

```
convs[speaker=ai][-1].speechesV2[].content[]
  type=searchGuid → docs[].url
  type=text/markdown → msg/text 正文
```

### 踩坑

1. SSE 捕获正文曾出现 UTF-8 被当单字节的乱码 → **以 detail 为准**
2. 全树 walk 吸入 `cdn-yb.icon.qq.com` / privacy 链接
3. 内部标记 `[](@mark_underline=N)` / `[citation:N]` 需剥离

### 优化建议

| 方向 | 说明 |
|------|------|
| A. 发完只拉 detail | 可不解析 SSE |
| B. 确认联网开关 | UI 有「联网搜索」时 citations 更稳 |
| C. 结构化 docs | 保留 title/index |

---


## 通义千问（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` |
| export | `exports/cn-ai-20260811-221909.json` |
| answer | ~1.4k 字分类推荐 |
| citations | ~7–8（web_source） |
| source | `network:/api/v2/chat` |
| profiling | send ~5.5s（含关弹窗），wait ~30s |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `chat2.qianwen.com/api/v2/chat` | **主通道** SSE，帧内多为全量快照 |
| 忽略 | `cms-sdk-server.qianwen.com/cms` | 运营配置，曾污染 answer/citations |

### 结构

```
data.messages[].content          # 取最长字符串为正文
data.messages[].meta_data.list[] # type=web_source → url/title
```

### DOM / 流程

- 输入：`contenteditable`，`data-placeholder="向千问提问"`
- 首页 Radix Dialog → `run_qianwen_prep` + **Enter 发送**（勿点侧栏关闭）
- 域名：`www.qianwen.com`

### 优化建议

| 方向 | 说明 |
|------|------|
| A. 只吃最后一帧 SSE | 全量快照，不必拼增量 |
| B. 结构化 cite | 保留 title/name/publish_time |
| C. session API | `chat2-api` session 列表可做历史回读 |

---


## Kimi（已稳）

### 验收

| 项 | 结果 |
|----|------|
| 题 | `solver_vendors` |
| export | `exports/cn-ai-20260811-230311.json` |
| answer | ~2.7k 字（Gurobi/CPLEX 等分类） | ✅ |
| citations | ~28（`searchChunks`/`references` 的 `base.url`） | ✅ |
| source | `network:.../ChatService/Chat` |
| profiling | send ~16s（含联网开关），wait ~65s |

### 关键 API

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/apiv2/kimi.gateway.chat.v1.ChatService/Chat` | **主通道** `application/connect+json` |
| 忽略 | `FeedService/ListFeeds`、`ListMessages`、`GetChat` | 历史/推荐流，易污染 answer |

### Connect 协议

长度前缀帧内嵌 JSON patch：

- `op=append` + `mask=block.text.content` → 正文增量
- `mask=message.refs.searchChunks` → 检索卡 `base.url`（主引用源）
- `mask=message.references` → 句内 cite → `items[].search.base.url`
- `mask=block.tool...` → `searchResult.base.url`
- 过滤 `iconUrl` / `coverUrl` / `kimi-web-img` / `icon-cache`

### DOM / 流程

- 输入：`.chat-input-editor`；优先点 `.send-button-container:not(.disabled)`，确认 URL 进入 `/chat/`
- 新会话：先 `goto kimi.com` 再点「新建会话」，避免停在 `chat_enter_method=history`
- 联网：`+`（`.toolkit-trigger-btn`）→ 悬停「联网搜索」→ 选「自动」
- 知识题附加一句「请联网搜索…」提高 searchChunks 出现率
- `wait` 必须先看到 `/chat/`，再等 markdown 节点稳定（侧栏文本会骗过旧逻辑）
- 网络：流式响应勿在 `response` 回调里立刻 `body()`；`requestfinished` + `flush()` 再读

### 踩坑

1. ListFeeds / ListMessages 比 Chat 更长 → merge 盖掉正确正文
2. 工具箱弹层未关时 Enter 不发送，或停在旧 history 会话抽到天气答案
3. Connect 流在 `response` 事件读 body → `CancelledError`，表现为「无 Chat 捕获」
4. 旧正则扫 https 会吸入 icon-cache，且截断真实 URL

---


## 跨站优化优先级（协议优先）

| 优先级 | 动作 | 影响站点 |
|--------|------|----------|
| P0 | 站点专用解析器，禁止全树 walk / 原始 body 回退 | 全部 |
| P0 | 每题新会话 + 按 prompt 取最后一条助手消息 | 全部 |
| P1 | 发完主动拉 history/detail（少依赖 SSE） | DeepSeek / 文心 / 元宝 |
| P1 | ~~Kimi 打开联网搜索拿到 references~~ ✅ | Kimi |
| P2 | wait 条件改为协议 FINISHED / UI「参考 N 篇」 | 豆包/千问/元宝 |
| P2 | 引用结构化（title/snippet/domain） | 全部 |
| P3 | 纯协议发问（复用 cookie + PoW/token） | DeepSeek 优先试点 |

单站验收命令：

```bat
cd GEO
set CDP_URL=http://127.0.0.1:9222
set NO_PROXY=*
py -3.11 batch_scrape_cn.py --site deepseek --question-id solver_vendors
```

## 输出格式

```json
{
  "site_id": "deepseek",
  "question_id": "solver_vendors",
  "prompt": "求解器厂商推荐？",
  "answer": "...",
  "citations": ["https://..."],
  "source": "network:/api/v0/chat/completion",
  "ok": true
}
```
