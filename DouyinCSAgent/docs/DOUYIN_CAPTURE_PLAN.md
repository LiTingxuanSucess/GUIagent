# Douyin CS Capture Plan

Last updated: 2026-05-03

## Current Position

The current `DouyinCSAgent` is still centered on auto-reply. It already knows how to:

- attach to the logged-in Chrome session through CDP
- intercept `imapi3-normal.zijieapi.com` and `imapi-oth.zijieapi.com` responses
- parse message-like payloads
- send replies back through the browser

What is still missing for a stable data pipeline:

- a persistent conversation list snapshot
- a merged history store
- incremental update merge across runs
- raw payload retention for schema drift

## Chrome/CDP Note

Verified on 2026-05-03:

- Launching Chrome 147 with `--remote-debugging-port=9222` against the default profile path did not open the CDP port.
- Switching `start_chrome.bat` to a dedicated profile directory `.chrome-cdp-profile` opened `http://127.0.0.1:9222/json/version` correctly.
- Because this is a new profile, the user must log in once in that CDP Chrome window before conversation capture can see the chat session page.

Current test result:

- CDP connection: OK
- Active page: `https://leads.cluerich.com/pc/auth/login`
- Captured raw responses: login/gray/ttwid only
- Captured conversations: 0

## Direction

Use the same pattern that was stabilized in `WXWorkAgent`:

- keep raw payloads
- normalize into run-level JSON
- merge into a master JSON
- dedupe by stable keys, but keep raw data for reprocessing

## What the New Collector Does

The new `douyin_export_recent.py` is built around three layers:

1. Network capture
   - watch JSON/XHR responses
   - watch WebSocket frames when present
   - store all raw payloads under `exports/douyin_recent/raw/`

2. Normalization
   - extract conversation-like objects
   - extract message-like objects
   - derive stable keys from IDs, timestamps, sender, and text

3. Merge
   - write the current run to `recent_chats.json`
   - merge into `exports/douyin_master/recent_chats.json`

## Why This Is Safer Than DOM-Only Scraping

- conversation list and history loading are usually backed by API traffic
- UI text can be virtualized, truncated, or paginated
- API payloads survive layout changes better
- raw payloads let us re-run extraction after schema changes

## How History and Incremental Updates Are Handled

History:

- when the chat pane loads older messages, the collector keeps the new response payloads
- if the page needs manual scrolling to trigger the fetch, the collector keeps a light wheel poke loop

Incremental updates:

- new responses and WebSocket frames are merged into the same in-memory store
- message dedupe prefers `message_id`
- fallback keys use `timestamp + sender + text`

## What We Learned from Recent WXWork Updates

The latest `WXWorkAgent` changes are the right model to follow:

- `wxwork_export_recent.py` now exports structured conversations and messages
- it has a master merge step, not just a one-off dump
- it keeps screenshots/raw artifacts for audit
- hover-time capture proved that some UI-only metadata needs a slower, dedicated probe

The main takeaway for Douyin:

- keep the network layer as the primary source
- keep the UI as a trigger and fallback
- do not tie the export to a single visible layout

## Next Steps

1. Run the new collector against the open Douyin CS page.
2. Check whether the response payloads already expose conversation list and message history.
3. If list fields are missing, add a focused probe for the session list endpoint.
4. If history is paginated, add a conversation-by-conversation replay step.
5. After the schema is stable, wire the normalized export into a customer-facing web view, using the same pattern as `WXWorkAgent`.

## RevAI Auto Reply

Verified against the WXWork implementation:

- `https://car.revai.cn/docs` is a SPA documentation route.
- Machine-readable API schema is available at `https://car.revai.cn/api/openapi.json`.
- Swagger UI is available at `https://car.revai.cn/api/docs`.
- The text reply endpoint is `POST /api/project/carzone/compat/chat/respond`.
- Request body uses `session_id`, `message`, `message_type`, and optional `image_url`.

Douyin integration:

- Added `src/revai_qa.py`.
- Added `tools/revai_qa_smoke.py`.
- Added `douyin_current_chat_reply.py` for selected-chat dry-run/send.
- Added `reply_engine.provider` and `revai` config.
- Default provider is currently `revai`.

Safe run:

```powershell
py -3.11 douyin_current_chat_reply.py --cdp http://127.0.0.1:9222
```

Send mode:

```powershell
py -3.11 douyin_current_chat_reply.py --cdp http://127.0.0.1:9222 --send
```

### 常驻自动回复服务

Windows 下优先用服务脚本管理本地兼容接口和抖音 watch 进程：

```powershell
# 真实发送：启动本地 carzone 兼容接口 + 抖音左侧可见会话扫描 watcher
powershell -ExecutionPolicy Bypass -File .\DouyinCSAgent\douyin_service.ps1 start

# 测试模式：不发送，只 dry-run 生成回复
powershell -ExecutionPolicy Bypass -File .\DouyinCSAgent\douyin_service.ps1 start -NoSend

# 查看状态、日志、停止
powershell -ExecutionPolicy Bypass -File .\DouyinCSAgent\douyin_service.ps1 status
powershell -ExecutionPolicy Bypass -File .\DouyinCSAgent\douyin_service.ps1 logs
powershell -ExecutionPolicy Bypass -File .\DouyinCSAgent\douyin_service.ps1 stop
```

当前远端 `car.revai.cn` 暂不可用时，`config.yaml` 默认 `reply_engine.provider=local_carzone`，本地接口监听 `http://127.0.0.1:8787`。如需切回远端，把 provider 改为 `revai` 即可。

watch 模式默认扫描左侧可见会话；如果客户连续发送多条消息，会合并成一条回复。测试单次请求时可显式加 `--no-send`，避免误发。

The script reads the currently selected Douyin conversation through DOM fallback. It does not navigate or reload. A conversation must be selected before it can generate a reply.
