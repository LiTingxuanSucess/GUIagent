# WXWorkAgent Progress

Last updated: 2026-05-05 +08:00

## Current Goal

Build a WeWork PC automation agent that can:

- Export recent WeWork conversations with structured message data.
- Track incremental updates across repeated scans.
- Detect new incoming messages from selected contacts.
- Generate dry-run replies first, then optionally send replies only when explicitly enabled.
- Support local web visualization of exported conversation data.

The current priority is stable new-message detection and high-quality structured data. Reply sending remains gated behind explicit `--send`.

## Main Files

- `wxwork_export_recent.py`: structured export pipeline.
- `wxwork_agent_loop.py`: new monitoring/reply orchestrator.
- `tools/wechat_send_test.py`: personal WeChat test sender.
- `src/contact_list.py`: WeWork left conversation-list OCR/parser.
- `src/ocr_client.py`: resident OCR service client.
- `src/text_encoding.py`: OCR mojibake repair helpers.
- `web/backend/main.py`, `web/frontend/*`: local export viewer.
- `legacy/`: historical probes and experiments that are not part of the active reply/add-customer workflows.

## Encoding Guardrails

Encoding issues have caused repeated confusion. The repository now has explicit guardrails:

- `../AGENTS.md`
- `AGENTS.md`
- `.editorconfig`
- `.gitattributes`
- `tools/check_text_encoding.py`

Rules for future work:

- Treat all source/config/docs as UTF-8.
- Do not trust PowerShell `Get-Content` rendering for Chinese text.
- Verify suspicious text with Python UTF-8 reads before editing.
- Run this after text-heavy edits:

```powershell
py -3.11 tools\check_text_encoding.py
```

Current verification:

```powershell
py -3.11 tools\check_text_encoding.py
py -3.11 -m py_compile wxwork_agent_loop.py wxwork_export_recent.py
```

Both passed during the latest update.

## OCR Service

The resident OCR service is still the preferred path.

Detailed startup and troubleshooting doc:

- `docs/ocr_service_startup.md`

Preferred GPU command:

```powershell
D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe wxwork_ocr_service.py --port 8765 --backend paddle --device gpu:0 --max-side 960
```

Observed state in latest tests:

- OCR service listens on `http://127.0.0.1:8765`.
- System Python does not have `paddle`; use `D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe`.
- A real PaddleOCR screenshot test will fail with `ModuleNotFoundError: No module named 'paddle'` if it is run under `py -3.11`; this is expected for the current environment.
- Business scripts should call the resident OCR service through `src/ocr_client.py` instead of importing Paddle directly.
- OCR output can still contain mojibake; `src/text_encoding.py` repairs common cases such as `ÁõÍ¢êØ@Î¢ÐÅ -> 刘廷曦@微信`.

## Structured Export

`wxwork_export_recent.py` now exports structured messages with fields such as:

- `sender`
- `direction`
- `timestamp`
- `message_type`
- `text`
- `screen_point`
- `ocr_center`
- `source_screenshot`
- `hover_screenshot`
- `cursor_delta`

Message categories include text plus special cases such as recalled messages, voice/video calls, images, expired images, files, and links.

Default master merge output:

```text
exports\wxwork_master\recent_chats.json
```

Incremental merge tracks:

- `conversation_key`
- `message_key`
- `first_seen_at`
- `last_seen_at`
- `seen_count`
- `reply_status`
- `reply_text`
- `reply_reason`
- `reply_model`

## Hover Timestamp Status

Important conclusion:

- Automated hover works with `SendInput` absolute virtual-desktop coordinates.
- Manual hover also works.
- `WM_MOUSEMOVE`/relative movement were not reliable.

Known verified hover example:

- Message: `明天上午拉个群`
- Captured hidden timestamp: `2025/12/27 00:10:30`

Current performance issue:

- Full hover over every visible historical message is too slow.
- The loop has been changed so direct-contact monitoring first does OCR-only capture and compares with history.
- Only new candidate messages should be hovered to fill exact timestamp.

This is the desired model:

1. OCR visible recent messages.
2. Compare OCR message fingerprints with historical fingerprints.
3. Only hover incrementally for messages not seen before.
4. Merge updated timestamped messages into master.

## New Message Detection

Left-list OCR diff was tested but is not stable enough by itself.

Observed issues:

- The left list parser sometimes mixes navigation/search text with contacts.
- OCR may split one row into incorrect pairs, for example:
  - `name='文'`, `preview='刘廷曦('`
  - `name='PING8'`, `preview='张卫星'`
- Hash-based skipping can miss correction opportunities after one bad OCR pass.

Current preferred monitoring path:

- If `--contact` is provided, `wxwork_agent_loop.py` uses direct contact scan.
- It directly opens each configured contact and captures recent messages.
- It does not rely on the left conversation-list diff.
- Left-list diff remains as fallback for broad monitoring without explicit contacts.

Direct-scan command shape:

```powershell
py -3.11 wxwork_agent_loop.py --dry-run --allow-background-visible --interval-sec 5 --baseline-scans 2 --contact 刘廷曦 --run-dir exports\wxwork_agent_loop_direct --master-dir exports\wxwork_master_direct --messages 10 --max-scrolls 0
```

Direct-scan behavior:

- Baseline scans are OCR-only and mark current visible messages as seen.
- Loop scans are OCR-only first.
- Fingerprint comparison is count-based, not just set-based.
- Fingerprint shape:
  - `conversation + sender + direction + message_type + text`
- Count-based comparison allows repeated identical text to be detected if the count increases.
- New candidates can then receive incremental hover timestamps.

## Personal WeChat Test Sender

Added:

```text
tools\wechat_send_test.py
```

Purpose:

- Operate personal WeChat independently from the WeWork loop.
- Search/open contact `刘廷曦`.
- OCR the chat header before sending.
- Abort if the header does not contain the expected contact name.

Example:

```powershell
py -3.11 tools\wechat_send_test.py --contact 刘廷曦 --text "PING9 企业微信闭环 21:31:30"
```

Verified:

- Personal WeChat was found as `Weixin`, class `Qt51514QWindowIcon`, title `微信`.
- Header OCR correctly recognized `刘廷曦`.
- Test messages such as `PING5` through `PING9` appeared in WeWork.

## Latest Test Results

Successful:

- Personal WeChat can send to `刘廷曦` with OCR header confirmation.
- WeWork direct scan can open `刘廷曦` and OCR recent messages.
- Master output correctly contains conversation `刘廷曦`.
- Historical visible messages are loaded into master.
- Direct baseline and repeated direct scans no longer hover all historical messages.

Latest observed master sample:

```text
Conversation: 刘廷曦
Recent messages:
- 企业微信自动化闭环测试1 11:11:54
- 企业微信自动化闭环测试2 11:15:13
- 企业微信自动化闭环测试3 11:18:47
- PING5 企业微信闭环 21:22:46
- PING6 企业微信闭环 21:24:24
- PING7 企业微信闭环 21:26:44
- PING8 企业微信闭环 21:29:23
- PING9 企业微信闭环 21:31:30
```

Still needs retest:

- Full direct-loop sequence where a message is sent after baseline and then detected as a new candidate.
- Incremental hover on only that new candidate.
- Dry-run reply generation after incremental detection.

## Reply Engine Status

Current reply engines:

- `QwenTextClient` via DashScope config.
- `RevaiQaClient` via `https://car.revai.cn/api/project/carzone/compat/chat/respond`.
- Dry-run mode is the default safe test mode.
- Actual sending requires `--send` and not `--dry-run`.
- RevAI client sends the detected new customer message by default instead of the entire local reply prompt.

RevAI docs were read from `https://car.revai.cn/docs` and cross-checked with `https://car.revai.cn/api/docs`.

Important docs-reading note:

- `https://car.revai.cn/docs` is a frontend SPA route. Raw HTTP only returns the HTML shell.
- For machine-readable API shape, use `https://car.revai.cn/api/openapi.json`.
- For human docs text, render `/docs` in a browser/Playwright and read `body.innerText`.
- Swagger UI is available at `https://car.revai.cn/api/docs`.

Implemented RevAI integration:

- Added `src/revai_qa.py`.
- Added `tools/revai_qa_smoke.py`.
- Added `reply_engine.provider` config switch with `qwen` as the default.
- `wxwork_agent_loop.py` can now call either Qwen or RevAI through the same reply flow.

RevAI request shape:

```yaml
reply_engine:
  provider: revai
revai:
  base_url: "https://car.revai.cn"
  endpoint: "/api/project/carzone/compat/chat/respond"
  api_key: null
  message_mode: "latest_new"
```

Smoke test command:

```powershell
py -3.11 tools\revai_qa_smoke.py --message "宝马325i想做小保养，帮我看下机油机滤"
```

Latest smoke result:

- Request format matches OpenAPI `ChatRequest`.
- `/health` returns OK.
- Remote 500 root cause was found in `backend_data/logs/carzone.log`: internal DashScope-compatible LLM calls returned `401 Unauthorized`, then `stage reply generation failed` bubbled up as HTTP 500.
- Remote service was patched so `backend/stage_agent.py` falls back to deterministic `facts.draft_reply` when LLM generation fails.
- Remote `restart.sh` was patched to prefer `/root/miniconda3/bin/python3`; system `python3` lacks `dateutil`, and bare `python` is not available in non-interactive SSH.
- `https://car.revai.cn/health` returns 200.
- `tools\revai_qa_smoke.py --message "325i"` returns a valid candidate-confirmation reply.

Latest dialogue test:

```text
session_id: wxwork-doc-dialogue-test-numeric-unicode-20260503
1. user: 325i
   reply: asks user to confirm 2026 BMW 3 Series 325i candidate.
2. user: 1
   reply: confirms model and asks for mileage / last service time.
3. user: 20000公里，想做小保养
   reply: reports oil usage 5.75L and brands such as Bosch / Fuchs.
4. user: 机油机滤多少钱
   reply: still answers fitment usage, not a full price quote.
```

Latest local WeChat -> WeWork safe test:

```text
Mode: safe read-only on WeWork after user manually opened the target chat.
No WeWork activation/search/click/scroll/hover was used during the safe read.
Personal WeChat sender:
  contact: 刘廷曦
  text: LOCAL_REVAI_TEST_2245 325i机油机滤多少钱
  result: sent, header OCR confirmed 刘廷曦.
WeWork read-only OCR:
  screenshot: exports/local_safe_read/chat_lower.png
  observed text includes LOCAL_REVAI_TEST_2245 / 325 / 机油机滤多少钱.
RevAI dry-run:
  session_id: liutingxi-local-safe-read-unicode-20260503
  reply: 您好，针对2026款 宝马 3系 325i，目前有以下保养套餐可选：
         1. 基础保养版｜团购价338元
         2. 升级养护版｜团购价458元
```

Latest current-session auto QA test, 2026-05-03 23:14:

```text
Tool:
  tools/current_dialogue_qa_test.py

Safe operating mode:
  - User manually kept personal WeChat and WeWork target chats open.
  - No WeWork launch, search, conversation switch, foreground activation, scroll, or hover.
  - Personal WeChat send used PostMessage to the current Weixin window.
  - WeWork reply used current composer PostMessage/WM_CHAR only.
  - WeWork verification used screenshot + OCR on the current visible chat.

Command:
  py -3.11 tools\current_dialogue_qa_test.py --single-price-turn --session-id wxwork-current-visible-20260503-single-price --wait-sec 24 --poll-sec 2

Verified flow:
  1. RevAI seeded with "325i" and "1" in one deterministic session.
  2. Personal WeChat current chat sent: 机油机滤多少钱
  3. WeWork OCR confirmed the incoming customer text: 机油机滤多少钱
  4. RevAI returned the price package answer:
     - 基础保养版 | 团购价338元
     - 升级养护版 | 团购价458元
  5. WeWork current chat sent the RevAI reply.
  6. WeWork OCR confirmed the outgoing reply contained 338 and 458.

Artifacts:
  - exports/current_dialogue_qa/20260503_231420_after_single_turn.png
  - exports/current_dialogue_qa/20260503_231423_after_single_reply.png

Implementation update:
  - Added tools/current_dialogue_qa_test.py.
  - Expanded WeWork OCR region to include the lower chat area.
  - Added automatic WeChat fanout retry when the first current-window send is not confirmed by OCR.
```

Important safety note:

- Do not use automatic WeWork launch/activation/search during local safety-sensitive tests.
- `activate_hwnd()` can restore/foreground the client and may fall back to OS mouse events; this can trigger WeWork security warnings.
- Preferred test mode is: user manually opens the target WeWork chat, automation only captures screenshots/OCR and calls RevAI in dry-run.

Current RevAI quality caveat:

- The HTTP API is stable again.
- Remote LLM key selection was hardened so a generic `OPENAI_API_KEY` is not sent to DashScope's default endpoint.
- The fallback avoids 500s but may not answer price and follow-up questions as well as the intended LLM-backed engine.
- Remote `package_answer` routing now has higher priority than fitment usage when the user asks for prices or package details.
- Keep WeWork reply sending in `--dry-run` until a valid remote LLM provider/key is configured and reply quality is rechecked.

## Known Issues

- OCR still sometimes corrupts Chinese in logs, though file content is usually valid UTF-8.
- `config.yaml` currently contains a real-looking DashScope key; avoid committing secrets.
- `wxwork_agent_loop.py` is still untracked in git in the current worktree.
- `exports/` and `logs/` are ignored and contain test artifacts only.
- Direct scan is slower than list diff but much more reliable for 1-2 specified contacts.
- Left-list broad monitoring still needs more parser hardening before relying on it unattended.
- Incremental hover code has been added conceptually but needs one clean end-to-end proof after baseline.
- RevAI remote service currently returns 200 through deterministic fallback when no valid LLM provider/key is configured; quality is usable for smoke tests but not final.

## Recommended Next Steps

1. Run one clean direct-loop test:

```powershell
py -3.11 wxwork_agent_loop.py --dry-run --allow-background-visible --interval-sec 5 --baseline-scans 2 --contact 刘廷曦 --run-dir exports\wxwork_agent_loop_incremental_hover --master-dir exports\wxwork_master_incremental_hover --messages 10 --max-scrolls 0
```

2. Wait for:

```text
>>> direct baseline contacts=1 ...
```

3. Send from personal WeChat:

```powershell
py -3.11 tools\wechat_send_test.py --contact 刘廷曦 --text "PING_INCREMENTAL_TEST"
```

4. Confirm loop logs:

```text
>>> direct new messages=1
>>> incremental hover time hit: ...
>>> generate reply for 刘廷曦 | PING_INCREMENTAL_TEST
```

5. After that, switch `reply_engine.provider` to `revai` and keep the first tests in `--dry-run`.
