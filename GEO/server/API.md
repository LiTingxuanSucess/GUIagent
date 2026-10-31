# GEO 问答 API

面向业务系统对接。服务统一向国内 AI 平台发问并回收回答。

2026-10-21 更新：生产环境支持 `GET /api/v1/jobs?status=queued&all=true` 获取全部排队任务（签名账号仅能读取本租户，详见任务列表章节）。管理看板新增额度与账号限制、短信接续阶段；业务接口原有错误码继续兼容。

- API 地址：`https://YOUR_GEO_HOST:8443`
- 编码：UTF-8 / `application/json`
- 鉴权：AK/SK HMAC-SHA256 签名

---

## 1. 接入约定

### 1.1 站点 ID

| site | 名称 |
|------|------|
| `deepseek` | DeepSeek |
| `doubao` | 豆包 |
| `weixinyiyan` | 文心一言 |
| `yuanbao` | 腾讯元宝 |
| `qianwen` | 通义千问 |
| `kimi` | Kimi |

文心一言的标准 site ID 是 `weixinyiyan`；兼容别名 `wenxinyiyan`。

2026-10-15 生产豆包优先使用已登录网页号池，已按要求恢复 `GEO_DOUBAO_API_FALLBACK=1`：网页失败后继续尝试豆包官方 API，成功则返回 API 正文。当日早先请求返回 HTTP 403 `AccountOverdueError`；随后约 10:36 的两条实际业务请求已由豆包官方 API 成功返回正文和来源。API 回答按实际渠道标记，不具有网页问答分享链接。

### 1.2 调用方式

推荐：**提交任务 → 立刻拿到 `job_id` → 轮询或等回调**。

- `POST /api/v1/ask` 立即返回 `202`，不会同步等回答
- 请求被接受后返回 `status=queued`
- 建议每次提交都携带唯一 `request_id` 或 `Idempotency-Key`，网络重试不会重复创建任务
- 当前部署启用全局官方 API 兜底（`GEO_API_FALLBACK=1`），六个平台优先完成网页路径：可恢复的验证或登录问题先同步调用本站 Cursor CLI，独立复核后续读原任务；实际恢复、允许的重试及可用账号尝试耗尽后，有密钥的平台才使用官方 API。本地等待或准备问题不直接改走 API。`GEO_<站点大写ID>_API_FALLBACK=0` 可单独关闭该站点兜底。API 回答标记 `source=official_api`、`answer_origin=official_api`、`degraded=true`，不具有平台问答页面分享链接
- 新任务返回 `deadline_policy=web_first`：排队期间不设置 `deadline_at`，不因等待时间耗尽而跳过网页。进入执行或新的阶段后更新 `deadline_at`；它是**可延长的阶段预计时间**，不是从提交开始计算的全局硬截止时间。启用 `GEO_REQUIRE_DELIVERY=1` 时，任务同时返回 `delivery_policy=eventual`，只有全部请求的平台取得非空有效正文后才进入 `succeeded`；暂时失败返回等待重试状态，不直接进入 `partial` / `failed`
- 获取账号、浏览器准备及执行容量等待，与网页生成分别计时。实际网页调用在会话和连接准备好后获得完整执行窗口；允许的重试或换号也获得新的执行窗口，不为预留 API 时间提前结束网页。各阶段仍有明确的超时与无进展保护，具体配置以服务环境为准。四个任务 worker 并行调度，当前本地网页采集最多三路同时执行，同一账号 / 站点保持独占
- 当前部署 `GEO_DOUBAO_BACKEND=local-only`，六站优先使用各自已登录的本地号池账号，并按需恢复明确待验证或登录失效的账号。发送时保留用户原问题，通过实际网页控件检查搜索设置；引用仅取自真实采集结果，不保证固定条数
- 发送阶段子预算默认为 60 秒，包含新会话和发送前设置；仍受本次网页执行窗口约束。采集进程在输入/等待阶段已取得页面检查点、但随后持续停止更新时，默认 90 秒后结束该次无进展采集，并按既有规则处理换号；由 `GEO_PAGE_STALL_SEC` 控制，`0` 关闭。慢生成只要检查点正常更新就不触发。号池仅为当前实际进行中的登录/验证或显式保活保留闲置浏览器，历史状态标记不单独保活新运行时
- 号池按任务需要启动浏览器，不在服务重启时巡检全部未知账号。自动未知账号检查每次最多 15 秒，同一账号/站点间隔五分钟；显式后台检查可立即执行。无人处理的验证码状态使用短暂保活规则，显式操作按人工保活规则处理；同步 CLI 恢复、复核和续读始终持有账号租约并保护原页面，不能按闲置验证码窗口回收。闲置运行时每 30 秒检查回收，正在执行的账号不回收
- 同平台进入浏览器前按当前有效账号数量限流，等待本站账号时不占全局浏览器槽位；新增账号实际登录后自动加入并发。游客页面的“API 服务 / 更多”不作为豆包登录证据，仍需真实账号标识。加入号池不等于已完成各平台登录
- 已配置 API 的站点连续三次实际网页超时、未完成或请求失败后记录网页冷却。已有健康本地账号或明确待恢复账号时，非全局限流的旧冷却不能阻断网页或 CLI 恢复；没有可用网页候选及实际全局限流时仍按冷却策略处理。槽位等待和冷却跳过不计入失败次数、不延长冷却；真实网页成功清除连续失败状态。该策略不能恢复上游欠费
- 本地账号触发验证码后从普通提问候选中移出，但可由当前任务持租约交给本站 CLI 恢复；同一账号不得同时被另一任务或人工写操作接管。恢复未通过才考虑其他可用账号，最终真实网页失败后再决定 API 兜底；本地登录不会解除云端浏览器的冷却
- 持续交付模式保留原任务编号、已完成正文和原网页恢复记录，只重试缺少回答的平台。`status=queued` 配合 `retry_at`、`retry_count`、`wait_reason` 表示等待下一轮；未交付的平台结果带 `delivery_pending=true`，`retry_state` 为 `retry_wait`（自动重试）、`operator_wait`（等待账号或配置恢复）、`awaiting_confirmation`（原发送结果待确认）。等待期间不占任务执行线程和网页容量；重试采用30秒起步、最多300秒的退避。人工状态自动定期复查，不能假定验证码、扫码、欠费或缺失密钥已解决
- 单纯网页容量等待保留任务/平台的排队票据，软超时与重新调度不清掉顺序；默认2秒重查，资源释放主动唤醒。离线票据不占浏览器或账号名额，不可用平台不能挡住其他平台。平台真实失败、限流和人工等待继续遵守原退避。原网页续读先检查明确丢失/账号忙/可恢复状态，忙账号不占全局容量空等；取得执行名额后仍重新核实并非阻塞地绑定原账号，竞争失败立即归还名额
- 网页发送不确定时只复核和续读同一账号的原页面，不重新提交；原页面丢失或恢复记录缺失时保留待确认状态，并允许已配置的同平台 API 最终兜底。浏览器纯容量等待不跳过网页；只有实际候选准备失败的完整证据才允许将打开失败视为网页路径耗尽。服务重启继续现有未终态任务，终态历史不自动重放；已有成功平台不再次调用
- 平台目标是持续取得真实有效回答，逐步提高网页交付占比；当前仍保留 API 作为最后兜底，不能把本地回归测试通过写成生产100%成功。调用方应持续轮询原 `job_id`，或显式设置自己的等待上限；客户端停止等待不会取消服务端任务。最终回调只在全部平台取得有效正文后发出
- `prompt` 最长 20000 字符

建议使用清晰、完整的自然语言提问。

回答成功与链接完整性分别检查。网页结果包含 `answer_origin=web`，以及 `share_status`（`verified` / `generation_failed` / `verification_failed`）、`citations_status`（`available` / `missing`）。缺少分享链接时包含 `share_error_code`；有正文但链接不完整时保留正文，并通过 `quality_issues` 和 `diagnostic_id` 关联页面证据。任务成功仅表示取得回答，不保证所有平台提供引用和分享链接。

API 兜底包含原网页失败的 `web_error_code`、`web_elapsed_ms`，以及单独的 API 耗时。`share_status=not_supported`、`share_error_code=api_no_web_share` 表示官方 API 没有网页问答分享。全局 `GEO_API_ENABLE_SEARCH=1` 开启搜索；豆包、元宝、千问、文心使用各自官方搜索。Kimi 使用官方 `/tools/search` 返回结构化来源并交给 Kimi 回答。DeepSeek 使用同一把密钥调用官方 `POST /v1/tools/search`，再由 `deepseek-v4-pro` 回答，标记 `native_web_search_supported=true`、`web_search_mode=native_search_api`、`web_search_provider=deepseek_search`。搜索词和原始问题按官方限制截到 128 个字符。搜索和回答共用 API 总预算，独立搜索最多 20 秒。当前部署 DeepSeek 已开启思考，推理强度为 high、最大输出 8192 token；Kimi K3 的推理强度由 low 改为 high。两站分别由 `GEO_DEEPSEEK_API_REASONING_EFFORT`、`GEO_KIMI_API_REASONING_EFFORT` 配置 low/high/max；结果中的 `api_thinking_enabled`、`api_reasoning_effort` 描述请求设置，不代表推理过程完整性。截断视为失败，正文中的模型生成 URL 不作为检索引用。

Kimi/DeepSeek 的独立检索由 `GEO_STRUCTURED_SEARCH_LIMIT` 配置，当前为 20。Kimi 单次范围是 1–20；DeepSeek 的 `count` 官方范围是 1–50，请求仍使用上述配置值。解析和公开输出保留上游返回的全部有效、去重来源，不再本地截取 8 条。`search_requested_limit` 是本次单次检索的请求上限，`search_result_count` 是实际返回的来源数。这些来源属于独立检索资料，不等于网页产品多轮搜索的全部引用，也不表示最终正文逐一引用了每个来源。网页渠道原有大量引用不受此参数限制；历史任务不回填。思考强度、来源数量和回答字数分别受模型及检索流程影响，不保证开启思考后正文一定更长。

当前部署千问 qwen-plus 显式设置 `enable_thinking=true`（`GEO_QIANWEN_API_THINKING=1`）；文心从普通 ernie-4.5-turbo-128k 调整为 ernie-5.0-thinking-preview，保留百度原生联网搜索。豆包 seed-2.1-lite 和元宝 hy3 采用各自默认思考策略，实测上游均记录思考 token。文心流式返回的多批引用合并后去重，后批不覆盖前批。DeepSeek 网页分别观察搜索与思考开关，仅对明确关闭的开关点击一次；`web_search_setting` / `web_thinking_setting` 的 `on` 表示已确认，`unconfirmed` / `not_present` / `not_observed` 不代表开启。网页状态与 API 思考设置分别记录。

发送网页或 API 问题时保留用户原文，不再追加“请联网搜索后回答，并列出参考来源链接。”或类似提示。网页在发送前检查实际搜索控件；DeepSeek 同时识别新版“智能搜索”。`web_search_setting` 的 `on` / `auto` 描述已观察到的控件状态，`web_search_setting_confirmed` 说明是否确认；`not_present` / `unconfirmed` 不代表已开启，也不把历史搜索入口或隐藏的引用标题当作开关。搜索设置独立于思考设置。

结果的 `web_search_status` 为 `used`（返回结构化检索来源）、`not_confirmed`（未取得实际搜索证据）、`failed`（搜索失败，可在剩余预算内尝试普通回答）、`disabled` 或 `not_supported`。搜索失败附带 `web_search_error_code`；`citations_origin=search_results` 表示来自搜索返回，`none` 表示无引用。`not_confirmed` 无来源时增加 `search_not_confirmed` 质量标记并触发告警，不把正文 URL、控件开启或已点击当作实际搜索证据。网页请求观察只保留搜索标志、请求路径及原问题哈希，不将请求凭据写入该证据字段。

API 的 `web_search_policy` 区分 `forced`（豆包 Responses 的 `tool_choice=required`、千问的 `forced_search=true`）、`automatic`（元宝、文心原生搜索由模型决策）及 `required_retrieval`（先取得结构化检索资料，再由原平台模型回答）。元宝没有添加未文档化的强制搜索参数；原生回答无来源时，尝试检索再由同一元宝模型回答（`GEO_YUANBAO_API_REQUIRE_SEARCH=1`、`GEO_YUANBAO_EXTERNAL_SEARCH=1`）。不保证每题固定返回来源数量。

文心 API 默认要求真实联网检索（`GEO_YIYAN_API_REQUIRE_SEARCH=1`）。部署开启 `GEO_YIYAN_EXTERNAL_SEARCH=1`：原生回答缺少结构化来源时，尝试 Kimi 搜索资料，再由原文心模型依据资料重新回答，来源标记为外部检索。检索或重新回答失败时保留已完成的原文心正文，并显式标记缺引用，不编造链接。

网页已取得完整正文时，`web_search_status=not_confirmed` 或缺少分享链接仅增加质量标记，保留原网页正文、来源渠道、真实引用及分享状态，不调用 API 补搜索或替换回答。官方 API 的检索和生成仅发生在网页路径实际失败后的兜底阶段。历史结果及缺失的搜索证据不回填。

API 自身失败保留原网页错误，并增加 `api_fallback_failed`、`api_error_code`、`api_error`、`api_elapsed_ms`。`api_quota_exhausted` 包括配额耗尽和账户欠费（例如火山 HTTP 403 AccountOverdueError），`api_auth_failed` 为认证/权限失败，`api_risk_control` 为明确的风控拒绝，`api_rate_limited` 为限流。`api_error` 是固定的中文说明，不回传上游响应、密钥或凭据。欠费、鉴权和明确风控拒绝后不再重复提交同一问题到普通 API；瞬时或搜索工具错误仍保留原普通回答兜底路径。历史任务错误码不回填。

`completion_detection=dom_state` 表示观察到网页完成状态后确认正文稳定；`text_stability` 表示当前版本未提供可识别的完成状态，使用完整文本稳定检查。正在生成时不会仅因文字暂停就提前结束。健康浏览器在最近一次成功后的 150 秒内可复用，云端空闲释放仍为 180 秒。匿名分享验证失败的诊断增加 `public-share.html`、`public-share.txt`、`public-share.png` 和 `public_share_probe`。

文心任务模式引用还会从当前问题匹配的 `task_mode` / `canvas/search` 网页响应读取。仅提取实际 `referenceList`，不把任务进度或其他问题的来源混入当前回答。

已发送问题但平台生成未完成时可返回 `platform_busy` 且 `retryable=false`，程序不会重新提交同一个问题。部署交接期间新增浏览器尝试可在发送前短暂等待，任务原 request_id 保持不变，阶段预计时间可更新；已经开始网页操作的任务不会因交接自动重放。

文心、元宝使用编辑器回车提交，避免发送控件跳转导致点击超时；文心分享复制使用实际指针点击并等待地址，不把仅取得分享 token 当作取得分享链接。程序保留正文并单独报告分享失败。

Kimi 新任务首次发送前优先使用网页的新会话控件，必须确认旧回答已清空；控件无效才重新访问首页。已绑定任务的恢复和续读只沿用原页面及轮次，不另开首页或清空旧回答；生成中编辑器禁用不视为失败。只存在统计类 cookie 的档案不作为 Kimi 登录恢复来源。同平台等待时间独立于网页执行窗口记录，号池的已登录标记不等于已验证多个可并发的云端登录会话。

六个平台均有独立的 Cursor CLI Skill 和 SOP。服务端为需要恢复的当前任务保留账号租约，提供唯一 CDP/target 和只读私有 journal；同步等待 CLI 结束，再独立检查同一标签的账号、验证层及原问题上下文。CLI 只能返回 `recovery-candidate` 或 `recovery-stopped: 原因`，不自行发送问题、不操作其他账号，也不调用会与租约冲突的后台 check/click/drag。已有回答就提取，已发送仍生成就等待，明确未发送才由服务端在原页发送一次。待验证或掉线账号的恢复候选失败后，才继续其他可用网页账号及最终 API 兜底。

| 平台 | Cursor CLI Skill | SOP |
|------|------------------|-----|
| DeepSeek | [geo-deepseek-web-recovery](../.cursor/skills/geo-deepseek-web-recovery/SKILL.md) | [DEEPSEEK_WEB_RECOVERY_SOP.md](../docs/DEEPSEEK_WEB_RECOVERY_SOP.md) |
| 豆包 | [geo-doubao-web-recovery](../.cursor/skills/geo-doubao-web-recovery/SKILL.md) | [DOUBAO_WEB_RECOVERY_SOP.md](../docs/DOUBAO_WEB_RECOVERY_SOP.md)、[语义验证细节](../docs/DOUBAO_CAPTCHA_SOP.md) |
| 元宝 | [geo-yuanbao-web-recovery](../.cursor/skills/geo-yuanbao-web-recovery/SKILL.md) | [YUANBAO_WEB_RECOVERY_SOP.md](../docs/YUANBAO_WEB_RECOVERY_SOP.md) |
| Kimi | [geo-kimi-web-recovery](../.cursor/skills/geo-kimi-web-recovery/SKILL.md) | [KIMI_WEB_RECOVERY_SOP.md](../docs/KIMI_WEB_RECOVERY_SOP.md) |
| 千问 | [geo-qianwen-web-recovery](../.cursor/skills/geo-qianwen-web-recovery/SKILL.md) | [QIANWEN_WEB_RECOVERY_SOP.md](../docs/QIANWEN_WEB_RECOVERY_SOP.md) |
| 文心 | [geo-yiyan-web-recovery](../.cursor/skills/geo-yiyan-web-recovery/SKILL.md) | [YIYAN_WEB_RECOVERY_SOP.md](../docs/YIYAN_WEB_RECOVERY_SOP.md) |

后台截图可只读查看恢复中的原页面；人工点击、拖动及登录写操作受恢复独占保护。独立的人工异常队列或公开续跑 API 不在本次接口范围内；已经结束、已回调的旧任务不会因人工操作重写终态。现有本地后台不能直接操作 AgentBay 云端验证。

分享链接必须由网页真实分享操作取得，再以匿名访问检查回答内容；不会根据会话 ID 拼接链接。验证预算最多 25 秒，并受请求剩余时间约束。Kimi 发送前关闭已观察到的知识库开关，并保持联网搜索开启；`kimi_knowledge_base=off` 表示确认关闭，`not_present` 表示当前菜单未显示开关，不能解释为确认关闭。已显示开关但不能确认状态时返回 `platform_setting_unavailable`，停止发送。

### 1.3 任务状态

| status | 含义 |
|--------|------|
| `queued` | 已入队，尚未开始 |
| `running` | 正在发问 |
| `succeeded` | 列出的站点都拿到了回答 |
| `partial` | 部分站点成功 |
| `failed` | 所请求的平台都没拿到回答，或任务执行被中断 |
| `cancelled` | 调用方主动取消，停止后续处理，保留已有结果 |

终态是 `succeeded`、`partial`、`failed`、`cancelled`，轮询到即可停。`running` 期间已经完成的站点会先出现在 `results` 里，这时顺序不保证与 `sites` 一致；正常完成后，`results` 与 `sites` 顺序一致。取消保留当时已有结果，可能未包含全部站点。

`failed` 返回任务级 `error_code`；`cancelled` 返回 `request_cancelled`。`succeeded` 和 `partial` 没有任务级 `error_code`。`partial` 时保留已经成功的回答。

---

## 2. 签名

每个请求必须带齐下面 4 个请求头。

| 头 | 说明 |
|----|------|
| `X-Geo-AK` | 访问密钥 |
| `X-Geo-Timestamp` | Unix 秒，与服务器相差超过 5 分钟会拒 |
| `X-Geo-Nonce` | 8–64 字符随机串；同一 AK 下 10 分钟内不可重复 |
| `X-Geo-Signature` | 小写 hex 的 HMAC-SHA256 |

待签字符串（5 行，末行无多余换行）：

```text
METHOD
PATH
TIMESTAMP
NONCE
SHA256_HEX(body)
```

- `METHOD`：大写，如 `GET` / `POST`
- `PATH`：只要路径，不要 query。例如 `/api/v1/ask`、`/api/v1/jobs/ab12cd34`
- `GET` 的 body 按空字节算，即 `SHA256("")` = `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `POST` 的 body 必须与实际发送的 JSON 字节完全一致（建议 `ensure_ascii=False`，不要 pretty-print）

```
signature = hex( HMAC_SHA256(key=SK, msg=canonical) )
```

### Python 示例

```python
import hashlib, hmac, json, time, uuid, urllib.request
from urllib.parse import unquote, urlsplit

BASE = "https://YOUR_GEO_HOST:8443"
AK = "geo_xxxxxxxx"
SK = "请换成我们提供的 SK"

def call(method, path, payload=None):
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    canonical_path = unquote(urlsplit(path).path)
    canon = f"{method.upper()}\n{canonical_path}\n{ts}\n{nonce}\n{body_hash}"
    sig = hmac.new(SK.encode(), canon.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        BASE + path,
        data=body if method != "GET" else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Geo-AK": AK,
            "X-Geo-Timestamp": ts,
            "X-Geo-Nonce": nonce,
            "X-Geo-Signature": sig,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))
```

---

## 3. 接口

### 3.1 提交发问

`POST /api/v1/ask` → **202 Accepted**

```json
{
  "request_id": "order-20260928-0001",
  "site": "deepseek",
  "sites": ["weixinyiyan"],
  "prompt": "帮我用一句话解释什么是哈希，别列提纲。",
  "callback_url": "https://your.example.com/geo/callback"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `prompt` | 是 | 自然语言问题，最长 20000 字符 |
| `site` | 二选一 | 单站 |
| `sites` | 二选一 | 多站；可与 `site` 同时给，会去重 |
| `request_id` | 建议 | 调用方生成的幂等键，最长 128 字符；同一 AK 下重复提交返回原任务 |
| `callback_url` | 否 | 公网 HTTP 或 HTTPS，最长 2048 字符。不能带账号口令或 `#fragment`，也不能指向本机或内网。任务结束后 POST 任务结果 |

也可以用标准请求头 `Idempotency-Key` 代替 body 的 `request_id`。两者同时提供时必须完全一致。
同一幂等键再次提交时，`prompt`、站点和回调地址也必须与首次请求一致，否则返回 `400`。

成功响应：

```json
{
  "job_id": "cfe7cefe587999f8",
  "request_id": "order-20260928-0001",
  "status": "queued",
  "prompt": "帮我用一句话解释什么是哈希，别列提纲。",
  "sites": ["deepseek", "weixinyiyan"],
  "callback_url": "https://your.example.com/geo/callback",
  "poll_url": "/api/v1/jobs/cfe7cefe587999f8",
  "completed_sites": 0,
  "queue_pos": 0,
  "queue_len": 1,
  "queued_ms": 0,
  "created_at": "2026-08-16T04:50:01+08:00",
  "updated_at": "2026-08-16T04:50:01+08:00"
}
```

首次创建返回 `202 Accepted`。相同 AK 使用相同幂等键重试时返回原任务：

- HTTP `200 OK`
- 响应头 `X-Geo-Idempotent-Replay: true`
- 不会再次入队，也不会重复向 AI 站点提问

建议轮询间隔 2–5 秒。`deadline_policy=web_first` 的 `deadline_at` 是可更新的阶段预计时间，不得将它作为客户端停止轮询或重发任务的条件。排队任务可能暂时没有该字段，预计时间后移也不代表任务失败。没有显式等待限制时，配套客户端持续等待终态；`geo_ask.py --poll-sec N`、`geo_callback_client.py --wait-sec N`、`geo_queue_client.py --wait-sec N` 和 `load_test.py --wait-sec N` 可限制客户端等待，但不会取消或改写服务端任务。

同一请求的网络重试继续使用原 `request_id`；用户主动重新生成或重跑旧的失败/缺来源结果，应生成新的 `request_id`。重复读取或复用原幂等键会返回原任务，不会重新调用平台。业务端应轮询到终态后展示各站 `answer` 和 `citations[].url`；固定客户端等待结束时保留 `job_id` 并提示“仍未完成”，不要重新提交同一问题。

服务端只读验收可运行 `python3 scripts/verify_job.py JOB_ID --require-citations --saved-dir data/jobs`；外部客户端省略 `--saved-dir`，通过 `--base`、`--keys` 选择自己的入口与凭据。此命令不发送新问题，退出码 0 为通过、1 为失败、2 为尚未完成。普通不需要引用的问题可省略 `--require-citations`。

### 3.2 查询任务

`GET /api/v1/jobs/{job_id}`

`GET /api/v1/jobs` 返回**当前 AK** 的任务列表（看不到其他租户），默认最近 100 条。

可选查询参数：

| 参数 | 说明 |
|------|------|
| `limit` | 1–200，默认 100 |
| `status` | `queued` / `running` / `succeeded` / `partial` / `failed` / `cancelled` |
| `request_id` | 按调用方幂等键查询 |
| `all` | `true` / `1`：一次返回当前 AK 的全部排队任务，仅支持与 `status=queued` 同时使用；不受默认 100 条及 200 条上限截断。默认 `false`，保留原有限量查询行为 |

列表响应：

```json
{
  "jobs": [],
  "total": 0,
  "limit": 100
}
```

#### 一次获取全部排队任务（生产已支持）

```text
GET https://YOUR_GEO_HOST:8443/api/v1/jobs?status=queued&all=true
```

继续使用第 2 节的 AK/SK 签名，请求体为空。签名里的路径只写 `/api/v1/jobs`，不包含 `?status=queued&all=true`。普通调用方只能读取当前 AK 的任务；“全部”不跨租户。管理员使用已登录的管理员会话访问时，可查看全部租户的排队任务。

```python
# 复用第 2 节的 call()，不需要反复翻页或增大 limit。
code, data = call("GET", "/api/v1/jobs?status=queued&all=true")
queued_jobs = data["jobs"]
assert len(queued_jobs) == data["total"]
```

响应示例（无排队任务时 `jobs` 为 `[]`）：

```json
{
  "jobs": [],
  "total": 0,
  "limit": 0,
  "all": true
}
```

`limit=0` 仅在本模式下表示不截断；传入合法 `limit` 也不会限制全部排队查询的条数。返回的是查询瞬间的排队快照，仍按创建时间从新到旧排列；已领取执行的 `running`、已结束任务不包含在内。持续交付任务等待重试、等待资源或人工确认且当前状态为 `queued` 时也会返回，原因见 `retry_at`、`retry_count`、`wait_reason` 及平台结果中的 `retry_state`。需要未完成任务的另一部分时，使用普通查询 `status=running`；排队不等于执行中。

完成后示例：

```json
{
  "job_id": "cfe7cefe587999f8",
  "status": "succeeded",
  "prompt": "帮我用一句话解释什么是哈希，别列提纲。",
  "sites": ["deepseek", "weixinyiyan"],
  "poll_url": "/api/v1/jobs/cfe7cefe587999f8",
  "completed_sites": 2,
  "queue_pos": 0,
  "queue_len": 0,
  "queued_ms": 12,
  "elapsed_ms": 21400,
  "created_at": "2026-08-16T04:50:01+08:00",
  "started_at": "2026-08-16T04:50:01+08:00",
  "finished_at": "2026-08-16T04:50:22+08:00",
  "callback_status": "ok",
  "callback_ms": 5,
  "results": [
    {
      "site_id": "deepseek",
      "site_name": "DeepSeek",
      "ok": true,
      "prompt": "帮我用一句话解释什么是哈希，别列提纲。",
      "answer": "哈希就是把任意长度的数据映射成固定长度指纹的算法。",
      "citations": [
        {
          "icon": "https://www.shanshu.ai/favicon.ico",
          "index": 1,
          "site_name": "杉数科技官网",
          "title": "杉数科技 COPT 介绍",
          "url": "https://www.shanshu.ai/solver"
        }
      ],
      "citation_details": [
        {
          "icon": "https://www.shanshu.ai/favicon.ico",
          "index": 1,
          "site_name": "杉数科技官网",
          "title": "杉数科技 COPT 介绍",
          "url": "https://www.shanshu.ai/solver"
        }
      ],
      "url": "https://chat.deepseek.com/share/abc123_xyz",
      "source": "history_messages",
      "elapsed_ms": 18120,
      "error": ""
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `completed_sites` | 已经写入 `results` 的站点数，成功和失败都计入。运行中会随各站完成增加 |
| `queued_ms` | 从提交到真正开跑的等待毫秒 |
| `queue_pos` | 当前活动队列中的零基位置；随着前序任务完成动态变化 |
| `queue_len` | 当前 `queued + running` 任务数；终态任务查询时通常为 0 |
| `elapsed_ms` | 开跑到结束的墙钟毫秒（多站并行时接近最慢一站） |
| `total_ms` | 从提交被接受到当前进度或终态的墙钟毫秒，包含排队 |
| `deadline_policy` | 新任务为 `web_first`，网页优先；不设从提交开始计算的全局执行硬截止。无此字段的历史终态记录保留原语义 |
| `deadline_at` | `web_first` 的阶段预计时间，RFC3339 格式，执行阶段开始后更新且可延长；排队时可缺省。历史无 policy 的任务仍按原截止时间理解 |
| `results[].ok` | 该站是否抽到有效回答 |
| `results[].answer` | 可直接展示的回答正文。平台内部的 `[citation:n]` / `[reference:n]` 已去掉。元宝配图占位符 `[](@replace=media_...)`、千问视频占位符 `[[...]]` / `[(...)]` 会换成 Markdown 图片 `[![说明](图片地址)](来源页)` |
| `results[].url` | 该次问答的公开分享链接，必须能在未登录状态下打开并看到对应问答。无法生成或无法核验时返回空字符串；不会返回需要登录才能查看的原始会话地址。 |
| `results[].citations` | 引用详情数组；没有引用时为空数组 |
| `results[].citations[].icon` | 站点图标的绝对 URL；无法获取时为空字符串 |
| `results[].citations[].index` | 引用序号；按最终数组顺序从 1 开始 |
| `results[].citations[].site_name` | 站点名称；无法获取时回退为域名 |
| `results[].citations[].title` | 引用页面标题；无法获取时为空字符串 |
| `results[].citations[].url` | 引用页面 URL |
| `results[].citation_details` | 兼容字段，内容与 `citations` 相同 |
| 引用兼容转换 | 合并已有 `citations` 与 `citation_details` 后按 URL 去重；详情为 null、空或不完整时不丢弃原始来源。轮询与回调行为一致，不为真正缺失的来源补造链接 |
| `results[].images` | 回答中的配图。没有配图时不返回该字段 |
| `results[].images[].description` | 图片说明，例如「特斯拉 Model Y」；没有时为空字符串 |
| `results[].images[].url` | 图片地址，可直接展示 |
| `results[].images[].jump_url` | 图片对应的来源页面；没有时为空字符串 |
| `results[].elapsed_ms` | 该站耗时 |
| `results[].total_ms` | 该站开始调度到完成的耗时，包含槽位等待及 API 兜底，不含任务 worker 排队 |
| `results[].source` | 结果来源；`official_api` 表示官方 API 兜底，其他值标识网页采集路径 |
| `results[].web_error_code` | 官方 API 兜底成功前的网页失败原因 |
| `results[].account_retries` | 可选。当前任务中切换号池账号的次数，官方 API 兜底成功后也保留 |
| `results[].web_skip_reason` | 可选的跳过原因，历史记录可见 `browser_admission`、`site_cooldown`。新任务等待网页执行机会；本地调度或准备失败不直接触发 API，恢复、重试及可用账号实际失败后才进入兜底 |
| `results[].web_answer_recovered` | 可选。`true` 表示已确认完整且匹配当前问题的正文已保存，后续 DOM 补充或分享异常时保留该回答；未验证分享链接为空。未完成正文不会作为成功检查点 |
| `results[].optional_stage_error_code` / `share_error_code` | 可选阶段和分享阶段的具体原因。它们可与 `ok=true` 同时出现，不代表回答失败；如 `answer_extract_timeout`、`share_timeout`、`share_not_attempted` |
| `results[].web_recovery` | 本次网页恢复的明确证据：`status` 为 `succeeded` / `failed` / `cancelled` / `skipped`；`attempts` 是真实启动 CLI 的次数；`verified` 表示页面复核是否通过，`resumed` 表示原网页续跑是否取得回答，`cli_assisted` 仅在最终成功续跑的同账号实际经过 CLI 修复并复核时为 true；未记录时为 null。`elapsed_ms` 为恢复耗时，`error_code` 为稳定原因。CLI 完成不等于网页恢复完成；无需 CLI 即续读成功时 `attempts=0`。跨账号汇总可累加 `attempts`，但不能继承前一账号的 `cli_assisted=true` |
| `results[].stage_timings` | 各阶段耗时，单位毫秒：`pool_wait_ms`、`site_gate_wait_ms`、`cloud_slot_wait_ms`、`web_slot_wait_ms`、`session_acquire_ms`、`session_setup_ms`、`web_execution_ms`、`api_wait_ms`、`api_execution_ms`。没有观测到的阶段不返回；0 表示已测量但不足 1 毫秒。任务 worker 等待仍由顶层 `queued_ms` 表示 |
| `results[].api_wait_ms` | 等待官方 API 并发槽位的耗时，毫秒 |
| `results[].api_execution_ms` | 实际 API 阶段耗时，不含槽位等待；包含该阶段的思考、搜索与上游调用 |
| `results[].api_skipped` / `api_skip_reason` | API 未实际启动时的跳过标记与原因，不能计为一次上游 API 失败 |
| `results[].api_error_code` | API 未完成时的分类：`request_deadline_exceeded` 为调用前已过任务截止；`insufficient_api_budget` 为有效执行窗口不足；`api_admission_timeout` 为等待 API 槽位超时；`api_timeout` 为调用超时；`api_request_failed` 为其他 API 调用错误。与网页 `error_code` 分开判断 |
| `results[].browser_reused` | 网页成功结果是否复用了近期成功的 AgentBay 浏览器 |
| `results[].browser_setup_ms` / `login_ms` / `browser_flow_ms` | 网页成功结果的云浏览器连接、登录恢复、完整浏览器流程耗时；未取得网页结果时可能不返回 |
| `results[].diagnostic_id` | 可选的内部现场编号，供运维关联失败记录或抽样成功记录。官方 API 兜底时继续保留网页失败的编号；不提供公开下载地址，现场文件可能已按保留策略清理 |
| `error_code` | 仅 `failed` 时出现的任务级错误码，取值见第 5 节 |
| `error` | 失败说明。不要解析这段文案，也不要把它当作错误码 |
| `retryable` | 是否建议稍后使用新的 `request_id` 重试。任务级为 `false` 时，这个字段可能不出现 |
| `results[].error_code` | 单个平台失败时的错误码，取值见第 5 节 |
| `results[].error` | 单个平台的失败说明。不要解析这段文案 |
| `results[].retryable` | 该平台失败是否可重试。失败结果里会带上，包括 `false` |
| `callback_status` | `pending` / `delivering` / `ok` / `failed` / `skipped`（主动取消，不回调）；未配回调则无此字段 |
| `callback_attempts` | 已执行的回调次数 |

引用详情优先保留平台搜索结果中的标题、站点名和图标；缺失字段会再尝试从引用网页元数据和站点 favicon 补齐。`index` 始终按最终 `citations` 数组顺序从 1 连续编号。

`partial` 时请按 `results[].ok` 逐站处理，不要整单丢弃。

网页已取得完整回答时，缺少搜索来源或分享链接作为质量信息展示，保留原网页正文、来源渠道、分享状态和真实引用，不再调用 API 替换该回答。官方 API 只用于网页路径实际失败后的兜底。

文心网页完成快照会补入当前问题的网络 `referenceList`，保留标题、站点名称、图标并按 URL 去重。仅采纳本轮实际发出的问题所对应的响应，或当前会话内最后一条精确匹配问题的历史响应；其他问题、其他会话和旧请求不会混入。补齐引用时更新 `citations_status`、`web_search_status`、`search_result_count`，正文和已验证的分享地址继续保留。补齐后的完成快照也用于分享或后续 DOM 提取超时的兜底，避免退回早期的空引用。

管理员请求看板可查看 `recovery_states` 中等待 CLI、等待短信、页面复核和原网页续跑的进度，并关联内部 `cli_call_id`；这些运行态字段和内部调用标识不提供给公开结果。CLI 页面复核成功率与最终网页恢复成功率分别统计：页面复核通过但续跑失败，不抹掉已复核的 CLI 恢复结果。

看板 `delivery.web_share` / `delivery.api_fallback_rate` 分别为网页 / API 最终取得回答的平台结果数除以已终态平台结果数，包括 `partial` 请求中的平台结果。`recovery.web_success_rate` 的分子要求网页实际交付、`resumed=true` 且 `cli_assisted=true`；分母 `known_completed` 仅使用真实启动过 CLI、恢复结束、续跑及 CLI 归因明确的记录。`cli_assisted=false` 的跨账号直接续读不计为 CLI 恢复成功；缺失该字段的历史记录单列 `unknown`，不推断归因、不当作失败。`completed` 仍保留恢复终态总量。取消、跳过、执行中及完全未调用 CLI 的续读不计入分母。

如果因为平台认证状态异常导致失败，状态查询和回调都会返回：

```json
{
  "job_id": "cfe7cefe587999f8",
  "status": "failed",
  "completed_sites": 1,
  "error_code": "platform_auth_unavailable",
  "error": "平台会话暂不可用，请稍后重试",
  "retryable": true,
  "results": [
    {
      "site_id": "deepseek",
      "ok": false,
      "error_code": "platform_auth_unavailable",
      "error": "平台会话暂不可用，请稍后重试",
      "retryable": true,
      "elapsed_ms": 0
    }
  ]
}
```

调用方无需执行登录操作；建议稍后重试，持续发生时联系 GEO 服务支持。

---

### 取消任务

`POST /api/v1/jobs/{job_id}/cancel`

使用与提交任务相同的 AK/SK 签名，签名路径包含完整的 `/cancel`；请求体可为空。只能取消当前 AK 所属任务，其他租户与不存在的任务均返回 HTTP 404；缺少或错误签名返回 401，其他请求方法返回 405。

```python
# 复用第 2 节的 call(method, path, payload=None)。
code, cancelled = call("POST", f"/api/v1/jobs/{job_id}/cancel")
assert code == 200
print(cancelled["status"])
```

HTTP 200 返回最新任务对象。排队、执行中或等待重试的任务变为 `status="cancelled"`、`error_code="request_cancelled"`，记录 `finished_at`，`retryable=false`，清除后续重试时间。保留取消时已经持久化的各平台结果；取消后的迟到结果不会覆盖终态。重复取消幂等；已成功、部分成功或失败的任务原样返回，不重写历史。持久化失败返回 503，本地执行仍会停止，当前进程的查询也会显示 `cancelled`；必须重试同一取消请求直至 200，才能确认取消状态已保存并可跨服务重启保留。

取消会通知本地浏览器流程、API 请求和 CLI 结束并释放资源；HTTP 响应不等待全部子进程退出。已经提交给外部平台的问题或产生的费用无法撤回。取消只停止服务端继续处理，不退出平台账号。已取消任务不发送结果回调；配置过回调的任务标记 `callback_status="skipped"`。客户端轮询遇到 `cancelled` 应立即停止。原 `request_id` 仍绑定原任务，幂等重放不会重新执行；确需再次提问时使用新的 `request_id`。

任务列表支持 `GET /api/v1/jobs?status=cancelled`。

## 4. 回调

任务进入终态后，若提交时带了 `callback_url`，我们会 **POST** 任务结果。结构与任务查询响应一致，但不包含 `callback_status`、`callback_attempts`、`callback_error` 和 `callback_ms`。

额外请求头：

| 头 | 说明 |
|----|------|
| `X-Geo-AK` | 你们的 AK |
| `X-Geo-Timestamp` | Unix 秒 |
| `X-Geo-Nonce` | 随机串 |
| `X-Geo-Signature` | 与请求相同的算法 |
| `X-Geo-Job-Id` | 任务 ID |
| `Content-Type` | `application/json; charset=utf-8` |

验签时：

- `METHOD` 固定 `POST`
- `PATH` 用回调 URL 的 **实际 path**（例如 `/geo/callback`），不是写死的 `/callback`
- body 按收到的原始字节做 SHA256

请在 **8 秒内** 返回 2xx。总计最多尝试 3 次，失败后分别等待约 1 秒、2 秒重试。

- 每次重试使用相同 `X-Geo-Job-Id`，接收方应按它做幂等去重
- 最终投递结果可通过任务查询中的 `callback_status`、`callback_attempts`、`callback_error` 查看

```python
def verify_callback(sk, path, headers, raw_body: bytes) -> bool:
    ts = headers["X-Geo-Timestamp"]
    nonce = headers["X-Geo-Nonce"]
    sig = headers["X-Geo-Signature"].lower()
    body_hash = hashlib.sha256(raw_body).hexdigest()
    canon = f"POST\n{path}\n{ts}\n{nonce}\n{body_hash}"
    want = hmac.new(sk.encode(), canon.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)
```

---

## 5. 错误码

业务错误按 `error_code` 处理。HTTP 错误先看状态码，再用响应里的 `detail` 定位原因。`error` 和 `detail` 只是说明，不要把这两段文案当成稳定错误码。主动取消的终态为 `cancelled`，对应 `request_cancelled`，不可重试原任务。

覆盖 `POST /api/v1/ask`、`GET /api/v1/jobs`、`GET /api/v1/jobs/{job_id}`，以及任务终态回调。回调正文与单任务查询一致，但不包含 `callback_status`、`callback_attempts`、`callback_error`、`callback_ms`。短信回调见第 7 节，不使用本节错误码。

先看 HTTP 状态，再看任务状态，最后看各平台结果。

| 层级 | 字段 | 说明 |
|------|------|------|
| HTTP | 状态码、`detail` | 请求有没有被接受。失败时响应体为 `{"detail": "..."}` |
| 任务 | `status`、`error_code`、`error`、`retryable` | 任务整体结果。`failed` 有失败错误码，`cancelled` 为 `request_cancelled` |
| 平台 | `results[].ok`、`results[].error_code`、`results[].error`、`results[].retryable` | 单个平台的结果 |

`retryable` 为 `true` 时，稍后使用新的 `request_id` 重新提交。同一个 `request_id` 只会返回原任务，不会重新提问。

### 5.1 HTTP 状态

| HTTP | 适用 | 说明 |
|------|------|------|
| 200 | `POST /api/v1/ask` | 幂等重放。响应头 `X-Geo-Idempotent-Replay: true`，返回的是原任务 |
| 200 | `GET /api/v1/jobs`、`GET /api/v1/jobs/{job_id}` | 查询成功 |
| 202 | `POST /api/v1/ask` | 新任务已入队。用 `poll_url` 查询，或等待回调 |
| 400 | 见下表 | 请求参数错误 |
| 401 | 见下表 | 签名校验失败 |
| 404 | `GET /api/v1/jobs/{job_id}` | `detail` 为 `job not found`。任务不存在，或不属于当前 AK |
| 405 | 上述三个路径 | `detail` 为 `method not allowed`。提问用 POST，查询用 GET |

#### 401 签名校验失败

四个签名头是 `X-Geo-AK`、`X-Geo-Timestamp`、`X-Geo-Nonce`、`X-Geo-Signature`。时间戳是 Unix 秒，与服务器相差超过 5 分钟会被拒绝。`nonce` 长度为 8–64，同一 AK 下 10 分钟内不可重复。每次请求都要换新的 `nonce`。

| detail | 处理 |
|--------|------|
| 缺少 AK/SK 签名头 | 补齐上述四个请求头 |
| nonce 无效 | 使用长度为 8–64 的新 nonce |
| 时间戳无效 | `X-Geo-Timestamp` 必须是十进制 Unix 秒 |
| 时间戳过期 | 校准时钟后重新签名 |
| nonce 已使用 | 更换 nonce 后重新请求 |
| AK 无效 | 核对 AK |
| 签名错误 | 用本次请求体的原始字节重新计算签名 |

#### 400 请求参数错误

| detail | 适用 | 处理 |
|--------|------|------|
| 读 body 失败 | 需要签名的接口 | 重新发送请求 |
| invalid json | `POST /api/v1/ask` | 修正 JSON |
| Idempotency-Key 与 request_id 不一致 | `POST /api/v1/ask` | 让两者一致，或只传其中一个 |
| prompt 不能为空 | `POST /api/v1/ask` | 填写提问内容 |
| prompt 不能超过 20000 字符 | `POST /api/v1/ask` | 缩短提问内容 |
| 需要 site 或 sites | `POST /api/v1/ask` | 至少指定一个平台 |
| 未知站点: {site_id} | `POST /api/v1/ask` | 改用第 1.1 节的平台编号 |
| request_id 不能超过 128 字符 | `POST /api/v1/ask` | 缩短幂等键 |
| request_id 已用于不同请求 | `POST /api/v1/ask` | 该键已经绑定了别的提问、平台或回调地址。更换 `request_id` |
| callback_url 过长 | `POST /api/v1/ask` | 缩短回调地址，上限 2048 字符 |
| callback_url 无效 | `POST /api/v1/ask` | 提供合法 URL |
| callback_url 仅支持 http/https | `POST /api/v1/ask` | 使用 HTTP 或 HTTPS |
| callback_url 不允许包含用户信息 | `POST /api/v1/ask` | 去掉 URL 里的账号和口令 |
| callback_url 不允许包含 fragment | `POST /api/v1/ask` | 去掉 `#` 及其后内容 |
| callback_url 不允许指向本机或内网 | `POST /api/v1/ask` | 改为公网可访问地址 |
| callback_url 域名无法解析 | `POST /api/v1/ask` | 检查域名 |
| limit 需为 1–200 | `GET /api/v1/jobs` | 将 `limit` 设为 1–200 |
| all 需为 true/false 或 1/0 | `GET /api/v1/jobs` | 将 `all` 设为合法布尔值 |
| all=true 仅支持 status=queued | `GET /api/v1/jobs` | 全部排队查询使用 `status=queued&all=true`；其他状态保留限量查询 |
| status 无效 | `GET /api/v1/jobs` | `status` 只能是 `queued`、`running`、`succeeded`、`partial`、`failed`、`cancelled` |

### 5.2 任务级错误码

只在 `status` 为 `failed` 时出现在任务根对象上。查询和回调使用同一组错误码。

| error_code | error | retryable | 处理 |
|------------|-------|-----------|------|
| `platform_auth_unavailable` | 平台会话暂不可用，请稍后重试 | `true` | 所请求平台都是这个错误。调用方不用登录。稍后用新的 `request_id` 重试。持续出现时联系 GEO 服务支持 |
| `all_sites_failed` | 全部站点未拿到回答 | `false` | 全部平台都没成功。任务级 `retryable` 可能不出现。是否重试，看 `results[].retryable` |
| `service_interrupted` | 服务重启，任务中断 | `true` | 任务执行被中断。用新的 `request_id` 重新提交。已写入的 `results[]` 仍可核对 |
| `job_timeout` | 请求总时间预算耗尽，请稍后重试 | `true` | 历史硬截止策略的任务级错误；新 `web_first` 任务不会仅因排队或超过阶段预计时间产生此错误。按实际终态核对平台结果 |

### 5.3 平台级错误码

出现在 `results[]` 里，且 `ok` 为 `false`。查询和回调使用同一组错误码。

| error_code | error | retryable | 处理 |
|------------|-------|-----------|------|
| `platform_auth_unavailable` | 平台会话暂不可用，请稍后重试 | `true` | 该平台会话不可用。调用方不用登录。稍后用新的 `request_id` 重试该平台。持续出现时联系 GEO 服务支持 |
| `platform_session_unavailable` | 平台会话暂不可用，请稍后重试 | `true` | 该平台调用通道暂不可用。稍后重试该平台 |
| `platform_busy` | 平台当前请求过多，请稍后重试 | `true` | 稍后重试该平台 |
| `platform_risk_control` | 平台触发安全验证，请完成验证后重试 | `false` | 不要立刻重试。持续出现时联系 GEO 服务支持 |
| `platform_quota` | Kimi 免费额度已用完，请等待额度重置后再试 | `false` | 出现在 Kimi。等额度重置后再请求该平台 |
| `sms_timeout` | 元宝验证码没有送达，请稍后重试 | `true` | 登录短信没有走完。`error` 文案不固定，请只认 `error_code`。稍后重试该平台 |
| `site_timeout` | 平台响应超时，请稍后重试 | `true` | 稍后重试该平台。也可能是「站点执行等待超时，请稍后重试」或「站点执行预算耗尽，请稍后重试」 |
| `site_request_failed` | 平台请求失败，请稍后重试 | `true` | 稍后重试该平台 |

`platform_auth_unavailable` 的 `error` 也可能是「平台会话暂不可用，请重新登录」，或带有账号标识的其他说明。不要解析 `error`，也不要把其中的账号标识展示给终端用户。`site_timeout` 和 `sms_timeout` 同样只认 `error_code`。

### 5.4 处理顺序

1. HTTP 状态不是 200 或 202：先按状态码处理，再用上表的 `detail` 修正请求。重新请求时使用新的 `nonce`。
2. `queued` 或 `running`：继续轮询，或等待回调。`running` 时可以先使用已经返回且 `ok` 为 `true` 的结果，终态仍以最后一次查询或回调为准。
3. `succeeded`：使用全部 `results[]`。
4. `partial`：保留 `ok` 为 `true` 的回答。对 `ok` 为 `false` 且 `retryable` 为 `true` 的平台，稍后用新的 `request_id` 单独重试。
5. `failed`：先看任务级 `error_code`。若是 `all_sites_failed`，再按各平台的 `retryable` 决定是否重试。
6. `retryable` 为 `false` 时不要立刻重试。`platform_risk_control` 和 `platform_quota` 的 `retryable` 为 `false`。

---

## 6. 建议对接流程

1. 生成唯一 `request_id`，`POST /api/v1/ask`，带 `prompt` + `sites`，可选 `callback_url`
2. 用返回的 `poll_url` 轮询，或等回调
3. 按第 5.4 节处理终态。`partial` 先保留成功回答，再重试失败的站
4. `retryable` 为 `true` 时，稍后使用新的 `request_id` 重试。不要复用已经用过的 `request_id`

最小调用：

```python
code, job = call("POST", "/api/v1/ask", {
    "request_id": "order-20260928-0001",
    "sites": ["deepseek", "weixinyiyan"],
    "prompt": "帮我用一句话解释什么是哈希，别列提纲。",
})
assert code == 202
job_id = job["job_id"]

while True:
    time.sleep(3)
    _, job = call("GET", f"/api/v1/jobs/{job_id}")
    if job["status"] in ("succeeded", "partial", "failed", "cancelled"):
        break
```

---

## 7. 短信验证码回调

短信服务商将收到的短信推送到：

`POST http://YOUR_GEO_HOST:18080/api/sms/inbound?token=<SMS_TOKEN>`

也可以不使用查询参数，改传请求头 `X-Sms-Token: <SMS_TOKEN>`。

```json
{
  "type": "recieve",
  "sender": "10690000",
  "receiver": "YOUR_PHONE_NUMBER",
  "requestid": "ed3815eefd61",
  "result": "【平台】您的验证码是 <SMS_CODE>，5分钟内有效",
  "time": "2026-09-30 14:50:00"
}
```

服务会提取 4–8 位验证码，并按 `receiver` 匹配正在等待短信的登录账号：

- 仅有一个匹配中的登录时，自动填写并提交验证码。
- 同一号码有多个登录同时等待时，只保存短信，不自动提交。
- 非空 `requestid` 是幂等键；重复回调返回 `duplicate: true`，不会重复提交。

网页恢复发现短信页时，CLI 立即返回 `recovery-stopped: sms_required`，不主动发送新短信、不填写验证码。只有原页面已明确显示短信已发送/倒计时且存在可关联的既有服务端短信回调流程时，服务端才登记本账号/站点等待该次短信，收到匹配回调后重新独立复核并续读原任务。仅有短信输入框不视为已发送；不能自动确认的短信或二维码步骤交人工处理，不切换到其他账号接收验证码。

正常响应：

```json
{
  "ok": true,
  "requestid": "ed3815eefd61",
  "code_detected": true,
  "pending": ["acct_xxx:kimi"]
}
```


## 8. 管理后台请求监控与飞书告警

管理入口为 `/admin/monitor`，号池管理页提供“请求看板”入口；需要管理员登录。只读汇总接口 `GET /api/admin/monitor` 同样要求管理员会话，无登录返回 401。汇总不返回客户问题、回答正文、租户 AK/SK、平台 Cookie 或机器人地址。

“各平台有效登录”模块提供“检测并辅助登录”按钮。`POST /api/admin/login-check`（可选 JSON `site_id`，缺省为全部平台）返回 202，在后台逐项检测已启用账号；重复提交复用当前执行中的任务。`GET /api/admin/login-check` 返回本轮任务编号、`status`（`idle` / `running` / `completed` / `failed`）、`total`、`completed` 和逐项 `results`。只返回账号编号、手机号尾号、平台、检测状态及 CLI 辅助结果，不返回完整手机号、验证码、Cookie、CDP 或 CLI 日志。页面刷新后可继续查看同一服务进程中的任务；服务重启后需重新触发。检测与 CLI 辅助共享业务浏览器和 CLI 容量，忙账号、原任务待恢复页面及容量不足的项目显示为跳过，可稍后重试。未登录时使用原绑定手机号的现有服务端发码流程，CLI 处理当前页面的登录入口和安全验证；短信由已有回调或人工确认，CLI 自报完成不作为登录成功。完成仅表示本轮处理结束，待短信、验证未通过和跳过项仍单独展示；页面会重新读取有效登录数量。

页面使用 `GET /api/admin/monitor?window=today` 等所选范围的轻量汇总，省略已由分页接口提供的 `recent` 详情；不带 `window` 的原汇总接口保持兼容。响应包含服务端 `server_time`、`snapshot_age_seconds` 和 `poll_interval_seconds`。页面依据服务端数据年龄及浏览器单调计时判断过期，不依赖电脑时钟；请求在 15 秒后超时，刷新完成后再安排下一轮，避免挂起或积压。切换范围会取消旧请求，返回页面时主动刷新。

监控使用单调时钟维持约 10 秒的生成周期。成功发布数据后向 systemd 发送心跳；卡住超过 45 秒由 watchdog 结束并按原重启策略恢复。前端仍显示真正超过 40 秒未更新的数据，另行显示网络失败或读取超时，不掩盖真实故障。

看板每 10 秒更新，支持北京时间今天、今天下午（12:00 起）、指定日历日，以及最近 15 分钟、1 小时、6 小时及 24 小时。请求按提交时间归入范围；最终请求成功率只以已结束请求为分母，部分成功单列。网页、API 和 Cursor CLI 分别展示调用次数与成功率，账号问题表可直接筛选对应失败请求。每日统计提供任务和各渠道的成功率、P50/P95 延迟；渠道延迟只采用有记录的已完成实际调用，跳过、未知及未记录耗时不作为零延迟样本。

渠道成功率为 `成功 / (成功 + 失败)`，每次网页执行（含换号及同账号重试）分别计数；API 按一次逻辑调用阶段计数，阶段内部的搜索与 HTTP 重试不另计。取得回答即为渠道调用成功，搜索、引用和分享质量独立展示。网页失败后 API 成功时，网页失败仍保留；未实际调用的排队、冷却、登录不可用等跳过，以及无法从历史数据确定的尝试，不进入渠道分母。新任务保存逐次账号记录；历史记录只依据已有字段还原，未保存的早期重试账号和结果显示为未知，不改写原任务文件。

新增管理员只读接口：

- `GET /api/admin/monitor?window=day&date=2026-10-15`：按北京时间指定日期统计，返回 `windows.day`；`available_dates` 和轻量 `daily` 序列供日期选择和每日比较使用。没有记录的合法日期返回空统计和空成功率。
- `GET /api/admin/monitor/requests?window=today&page=1&page_size=25`：返回 `updated_at`、`total`、`page`、`page_size`、`rows`。每页默认 25、最多 100；`window` 支持 `today`、`afternoon`、`15m`、`1h`、`6h`、`24h`，也支持 `window=day&date=YYYY-MM-DD`。日期严格校验，日期参数仅用于 `day`。
- 列表可追加 `site`（平台）、`channel=web|api|cursor`、`account`（账号 ID，`unknown` 表示历史账号未知）、`outcome=success|failed|skipped|unknown|running`、`error`（稳定错误码）、`status`（请求最终状态）。账号、渠道、结果与错误必须匹配同一次调用，避免把同任务其他平台的问题归给该账号。CLI 范围依据调用开始时间，允许追溯前一天提交的任务。
- `GET /api/admin/monitor/requests/{job_id}`：按需读取完整 `prompt`、`request_id`，各平台 `answer`、`share_url`、`citation_details`（搜索来源链接、标题和站点），并保留最终来源、账号、搜索/分享状态和逐次调用记录。`content_available=false` 表示原任务正文不可读取；没有分享链接时不伪造链接。

API 凭据以 `api:<平台>` 标识，网页登录账号只展示 ID 和手机尾号；API 失败不归到网页手机号。所有新接口要求管理员会话，响应 `Cache-Control: no-store`，不返回完整手机号、API 密钥、Cookie、租户 AK/SK。问题和回答仅通过管理员详情按需返回，不进入周期汇总或分页索引。请求索引 `data/monitor-requests.json` 保留现存历史任务的脱敏元数据，`data/monitor-daily.json` 保存按日聚合；文件权限均为 0600，原始任务不改写。

Cursor CLI 是独立统计渠道，六个平台分别通过本站 Skill/SOP 同步恢复当前任务的原网页，不能等同网页回答成功。`data/cursor-calls/*.json` 持久化调用开始、结果、耗时、平台与账号；调用数包含已启动且运行中或结果未知的任务。CLI 自报 `recovery-candidate` 仍为待复核，只有服务端独立页面复核通过才记该 CLI 恢复成功，`resume_ok` 单独记录原网页续读结果；页面复核通过但续读失败不抹掉 CLI 恢复成功。无法启动计为跳过，旧记录缺少复核证据时不推断。服务重启后，已验证的 CLI 结果保留成功、续读中断标记为 `cli_resume_interrupted`；尚未复核且对应任务确认中断时标记 `service_interrupted`。旧业务日志只还原有依据的调用，未统一记录的手工 CLI 操作不计入，平台无记录时显示零调用且成功率为空。

已通过独立页面复核的成功 CLI 会话会记录 `session_id`，保存在私有 `data/cursor-sessions/`，后续相同账号、平台、任务类型及模型的调用使用 `--resume <session_id>`；调用记录中的 `resumed_session_id` 可用于核对实际复用。每轮仍重新提供当前 CDP、唯一 target 与恢复状态文件，不沿用历史页面地址。会话缓存跨服务重启保留；复用失败后清除对应缓存，下轮创建新会话。调用方主动取消产生的 `cli_canceled` 保留此前已验证成功的缓存。CLI 会话 Resume 与原网页续跑的 `resume_ok` 是两件独立的事。

CLI 执行名额暂满时，原网页修复最多等待 30 秒（`GEO_CURSOR_CAPACITY_WAIT_SEC`，范围 1–120 秒），不因瞬时繁忙立即跳过修复。等待不启动额外 CLI，可由取消接口中断；超时仍记录 `cli_busy`，取消记录 `cli_canceled`。当前 8 GB 主机最多同时执行三路本地网页和两路 CLI。

独立 `geo-monitor.service` 读取持久化任务并检测业务服务，可在 `geo-server` 不可达时发送告警。请求失败、API 额度/认证/限流/超时、网页验证与登录失败、搜索失败、分享失败及结果回调失败分别记录。已有 API 回答但网页失败标为提醒；实际调用失败标为告警。近 5 分钟某平台至少 5 次调用且失败率不低于 40% 时告警，降到 20% 以下且样本仍不少于 5 次时恢复；连续 3 次健康检查不可达时发送服务告警。

新任务采用 `web_first`：worker 排队、部署等待和浏览器资源排队不消耗网页执行预算。启用 `GEO_PLATFORM_DISPATCH=1` 后，问题由轻量协调器汇总各平台结果，最多保留 `GEO_DISPATCH_PENDING_MAX=64` 个协调器；等待中的平台不占调度 worker。账号容量、站点、云资源及全局网页槽位一起分配，按等待任务 / 平台轮换。资源等待默认 300 秒（`GEO_DISPATCH_RESOURCE_WAIT_SEC`），保留公平排队位置，超时仍按资源等待重新排队，不作为平台失败或 API 替代的依据。

取得网页名额后，一轮会话准备、换号、同号重试、CLI 和原页面续读共用固定网页窗口，生产默认最多 240 秒（`GEO_SITE_TIMEOUT_SEC`、`GEO_SINGLE_WEB_BUDGET_SEC`、`GEO_MULTI_WEB_BUDGET_SEC` 及平台覆盖配置取较短值）。换号不再刷新该窗口；账号准备本身仍受 `GEO_SESSION_ACQUIRE_WAIT_SEC=120` 限制。窗口到期保留发送 journal 和已完成正文，已发送问题继续绑定原账号及原 target，后续只核实、续读，不重新提交。API 使用独立预算，取得网页名额后的完整失败才按原有规则兜底；准入默认最多等 5 秒、最小执行窗口 15 秒（`GEO_API_ADMISSION_WAIT_SEC`、`GEO_API_MIN_EXECUTION_SEC`）。

历史 `insufficient_web_budget`、`browser_admission_timeout` 以及 `session_setup_timeout`、`browser_start_failed` 属于本地调度或准备问题，不记入实际平台调用失败，不归责账号，也不触发平台冷却。CDP 不可用仍可尝试其他账号并保留故障账号记录。对于要求最终交付的任务，账号准备超时或没有已登录账号的等待累计达到两次（`GEO_DELIVERY_SETUP_WAIT_LIMIT=2`），或从首次准备等待起累计三分钟（`GEO_DELIVERY_SETUP_WAIT_SEC=180`），且持久化 journal 或最新调度记录能够确认问题未发送时，允许使用已配置、已启用的官方 API 兜底；结果记录 `fallback_reason=web_setup_wait_exhausted`、等待次数及耗时。等待证据跨重试和重启保留，达到上限后不重复启动 Chrome/CLI。浏览器资源排队不消耗该准备预算；已发送、发送中、journal 损坏或发送状态不确定时仍核实原网页，不凭等待次数重复提问。显式调用方取消继续有效。实际执行后的 `generation_timeout`、`answer_extract_timeout`、`browser_unresponsive` 分别表示生成未完成、提取超时、浏览器无进展。详情展示各段耗时，可选阶段失败仍保留已完成正文和回答成功率；本地问题与质量提醒复用原告警去重，不新增逐请求推送。

Python 与 Go 共享同一绝对截止时间，即使关闭诊断采样也有效。DeepSeek、文心优先提交严格绑定当前问题并确认完成的协议结果，默认留 5 秒收尾（`GEO_PROTOCOL_FINALIZE_RESERVE_SEC`）；需要 DOM 提取的其他流程默认留 10 秒（`GEO_WEB_EXTRACTION_RESERVE_SEC`）。可选 DOM 补充默认最多 10 秒（`GEO_DOM_EXTRACT_BUDGET_SEC`），分享创建默认最多 20 秒（`GEO_SHARE_CREATE_BUDGET_SEC`），均受任务总截止时间约束。已确认完整回答后，可选 DOM 补充超时仍会在剩余预算内继续创建及验证分享；预算不足时保留回答并记录分享失败原因。

分享链接只返回通过匿名正文校验的原生公开链接，字段为 `url`，`share_status=verified`。API 直连或兜底回答没有对应平台网页会话，返回 `url=""`、`share_status=not_supported`、`share_error_code=api_no_web_share`。千问支持全量快照与增量补丁正文，公开分享可通过匿名分享正文接口校验；豆包等平台在复制动作后继续收集异步分享响应，避免漏掉已生成链接。历史任务不会因部署自动重跑或生成分享。

实际失败告警默认 15 分钟去重，已由 API 补齐回答的网页异常明确写为提醒，默认每小时提醒一次。同平台同渠道保留当前原因，原因变化继承去重时间；上游 API 最终失败只推送该根因，避免网页、搜索和失败率再次重复推送。对应渠道取得成功后发恢复通知；旧原因被替代或六小时没有新观测时退出活跃列表，保留历史，不伪报恢复。推送失败退避重试，状态及去重信息持久化，监控重启后继续。飞书 HTTP 成功但业务返回码非 0 仍视为发送失败。

服务器私有 `.monitoring.json` 保存机器人地址及可选签名密钥，不写入前端或 Git；状态文件 `data/monitor-state.json` 与汇总文件 `data/monitor-dashboard.json` 均为 0600。推送只含平台、错误类型、累计次数、任务编号和需要登录的看板入口。启动：`systemctl enable --now geo-monitor`；检查：`systemctl status geo-monitor`。上线验证可运行 `python3 scripts/request_monitor.py --test`，该命令会实际发送一条接入测试消息。

## 9. 服务器桌面人工登录

服务器已安装官方向日葵 Linux 客户端，操作桌面使用独立的 `DISPLAY=:0`，与业务自动化显示器隔离。识别码与单次验证码在向日葵窗口查看，不写入项目文件。相关服务为 `runawesun`、`geo-operator-x11`、`geo-operator-desktop`。

1. 连接桌面，在空白处右键，选择“豆包：人工登录账号”或“DeepSeek：人工检查账号”。已有登录终端时直接使用该终端。
2. 在终端输入账号序号。工具会暂停该账号所有平台的自动化，再打开该账号原有 Chrome 档案；若账号正在执行任务，后台会拒绝暂停，等待任务完成后重新操作。
3. 在 Chrome 中人工完成登录、短信或安全验证。保持浏览器打开，返回终端按回车保存。
4. 工具将该平台 Cookie 和 localStorage 保存到账号原有 `.session-store.json`（0600），保留其他平台的会话，然后关闭人工浏览器、恢复账号并执行登录检查。无需复制或发送 Cookie。

同一时间只接管一个账号。若中途退出而 Chrome 仍打开，账号保持暂停；先结束人工操作并关闭该浏览器，再在号池管理页恢复账号并检查登录。登录检查成功不代表后续提问不会再次触发验证码；恢复后仍需用真实问题验证回答及来源。

命令入口为 `server/scripts/manual_account_login.py --site doubao`，运行时需要操作桌面的 `DISPLAY=:0` 和 `XAUTHORITY=/run/geo-operator/Xauthority`。只读自检可运行 `python3 server/scripts/manual_account_login.py --self-check`。桌面服务、菜单及向日葵显示器配置保存在 `server/geo-operator-*.service` 和 `server/desktop/`。

## 10. 控制台任务列表分页

根页面 `/` 的任务列表使用已登录会话读取 `GET /api/console/jobs?limit=50&offset=0`，默认每页 50 条、最多 100 条，`offset` 为非负整数；管理员可追加 `ak` 筛选租户，普通租户始终只读自身数据。响应包含 `jobs`、`total`、`limit`、`offset`。

该列表只返回表格摘要（问题前 160 字符、各站成功状态、耗时等），不返回回答正文、引用详情或回调地址。点击任务后通过原有 `GET /api/console/jobs/{job_id}` 获取完整问题、回答与引用；历史任务保留。公开调用方的 `/api/v1/jobs` 与任务详情接口保持原协议。

页面提供上一页、下一页、刷新和加载状态；刷新回到最新任务，切换租户从第一页加载。请求超时或网络失败时明确显示错误并允许刷新，失败翻页不改变已显示数据的页码；401 返回登录界面并提示重新登录。服务切换会使原内存登录会话失效，需要重新登录。

## 11. 豆包并发与验证窗口

2026-10-20 已确认生产主机为 4 vCPU、约 7.7 GiB 物理总内存（8 GB 规格），扩容后的配置为 `GEO_QUEUE_WORKERS=4`、`GEO_SITE_CONCURRENCY=3`、`GEO_API_CONCURRENCY=6`、`GEO_CURSOR_CONCURRENCY=2`。四路网页叠加批量账号登录的试运行仍出现持续回收与换页，交换空间一度接近 2 GiB 上限，因此最终采用三路网页、两路 CLI。启用平台调度后，四个 worker 负责领取问题并交给有界协调器，实际网页流程全局最多三路，API 独立最多六路。只有持有实际网页或 API 执行名额的问题显示 `running`，仅等待资源的问题显示 `queued`；网页和 API 同时推进时，`running` 问题数可能超过三。浏览器 cgroup 的节流线为 4000 MiB、硬上限为 4400 MiB（`GEO_BROWSER_MEMORY_HIGH_MB` / `GEO_BROWSER_MEMORY_MAX_MB`），所有网页流程共享此内存预算；主服务及 Cursor CLI 的 systemd 内存节流线为 2300 MiB、硬上限为 2800 MiB，为系统和操作桌面保留空间。

Cursor CLI 使用独立的全局容量 `GEO_CURSOR_CONCURRENCY=2`；原任务的网页恢复等待容量最多 30 秒，超时才记录 `skipped/cli_busy`，不把跳过当作已完成的恢复尝试。人工接管和后台登录检查在名额已满时仍直接返回忙。生产单次执行预算为 180 秒，首次工具调用等待上限 75 秒（`GEO_CURSOR_STARTUP_SEC`），此后连续 45 秒无输出会结束进程组；初始输出不会取消后续静默检查。每轮最多 24 个工具调用（`GEO_CURSOR_MAX_TOOLS`），仅在工具动作全部完成后终止无进展循环。同账号接管最多等待 5 秒。CLI 的 `eval` 由浏览器强制只读，读取坐标允许，改写 DOM 登录标记或验证码状态会被阻止。

Cursor 恢复记录使用 schema 2：`process_status/process_error_code` 保留原始执行结论，`verified/resume_ok` 分别表示同一原页面复核及原任务续跑，`cli_assisted` 只有实际执行工具且通过复核才为真。账单或进程错误不能被后来的页面恢复覆盖为成功；`recovery_origin=external` 标识这类随后恢复。`resume_error_code` 保留续跑的具体失败原因。`build_id/config_hash/model/tool_calls/stage_timings` 用于按版本、任务及阶段比较。指标分别展示页面复核、原任务续跑、明确归因于 CLI 的交付；短信和人工交接使用 `waiting_sms/action_required`，不计入已明确成功或失败的分母。

管理员 GET `/api/admin/cursor-health` 返回 `state/error_code/retry_at/updated_at`，监控概况也包含 `cursor_health`。账单、认证、额度、限流和上游连接错误分别记为 `cli_billing_required/cli_auth_failed/cli_quota_exhausted/cli_rate_limited/cli_transport_failed`。所有平台、人工辅助和后台登录共用持久化熔断；账单冷却 15 分钟、认证和额度 5 分钟、限流 1 分钟、连接错误 30 秒，到期仅放行一个真实待执行调用复查。失败的旧调用不会被其他并行调用的成功重新放行。账单支付和账号认证由账号持有人处理。

同一账号、平台的未变化验证状态共享恢复冷却，状态持久化在 `data/cursor-retry-state`。换任务、换 target、倒计时文本变化和服务重启不能绕过冷却；首次失败冷却默认 120 秒，10 分钟内两次失败后停止重复尝试直至窗口结束或验证状态发生变化。CLI 只准备原页面和处理验证层，服务端独占负责绑定手机号、发码和短信提交；发码后出现的新验证最多追加一次 CLI。继续原任务前需再次确认同一 target 已稳定就绪，仍按 journal 避免重复发送问题。短信回调在页面独占阶段有限重试，并拒绝早于当前发码轮次的旧验证码。

历史账单失败可用 `scripts/reconcile_cursor_billing.py` 先预览，再通过 `--apply --backup-dir <私有备份目录>` 校正。只依据原始 provider 错误修正 CLI 结论，保留页面复核和续跑事实，逐条备份原始记录；不根据模型引用的账单文字推断错误。

CLI 调用从 `stream-json` 的初始化/结果事件记录实际 `session_id`。只有服务端独立复核成功的调用才保存为可复用会话；下一次同账号、同平台、同任务类型及同模型调用通过 `--resume <session_id>` 续用。每轮仍提供完整的新任务、CDP、唯一 target 和 journal，明确禁止沿用历史页面地址或重复发问。失败的 Resume 会移除该缓存，后续调用创建新会话；调用记录保留 `session_id` 与 `resumed_session_id`，不在公共问答结果中暴露会话 ID。

网页恢复复核同时识别豆包新版侧栏用户名按钮，以及元宝 `.yb-nav__user` 内的账号昵称控件；必须存在可见的实际用户标识，游客头像、隐藏节点和对话正文中的相同文字不能作为已登录证据。

DeepSeek 的 CLI 页面复核在缺少稳定账号控件、且没有验证码或登录提示时，通过平台当前用户接口核实实际认证状态；只有有效业务响应才作为账号证据。明确的登录层、短信等待或验证码仍然阻止通过，凭据和账户详情不离开浏览器。

后台重登在本次检测或发送短信时刚发现 `need_captcha`，会在没有客户租约时调用 `login_check_assist`，只接管该账号已存在的页面，不新建或切换标签。CLI 结束后独立复核页面；已发送短信的等待状态交既有短信回调。不会扫描历史提问验证码并将其重新解释为登录验证。`manual_page_assist` 也会复核同一页面：复核通过才记录成功，短信交接记录 `skipped/sms_required`，验证码仍在或证据不足记录复核失败。

有可用账号时优先网页；多个账号可以在浏览器容量内并行使用。`web_first` 任务等待网页槽位时不消耗实际网页执行窗口；取得名额后换号和重试共用一轮固定窗口。异常账号按平台分别冷却，短暂浏览器故障默认 120 秒，明确的平台禁言或额度重置日期按提示处理；冷却和原页面所属任务保存在 `data/jobs/account-protection/state.json`，重启后继续生效，不会影响其他平台的正常账号。

提问恢复使用独立的单路恢复通道，并继续占用三路网页名额之一；该通道与后台登录处理共享全局两路 CLI 容量。通道忙时持久化 `recovery_queue_busy` 后让出当前执行机会，按资源等待短暂退避，不累计为新的恢复失败，也不因此调用 API。等待中的已发送问题会保留原页面所属任务，暂不接收其他问题；该任务后续仍从原 journal 续读。原页面保活默认 360 秒（`GEO_RECOVERY_PAGE_RETAIN_SEC`），不会因为释放调度 worker 而放开浏览器总并发。

恢复流程的发码只操作已绑定 target 中现有的手机号表单，不打开首页或切换登录流程。该操作最多 25 秒，且受本轮更早的截止时间约束；取消后保留发码冷却，避免确认结果丢失时重复发送短信。部署暂停也会阻止已经登记的平台等待者取得新网页名额，现有发送记录继续保留。

提问时触发验证码的账号会从普通提问候选中移除，当前任务仍可持同一租约同步调用本站 CLI，再独立复核并续读原页面。后台首页登录检查不代表已经完成该验证。恢复中的账号和页面受独占保护，不会被闲置回收或人工写操作打断；恢复结束后，无人处理的旧验证页仍按容量和保活策略回收，账号档案及待验证状态保留。CLI 需要短信时仅交既有回调，不主动发新短信。已发送问题的生成中页面继续等待，不用再次发问来验证账号。

人工登录工具遇到服务重启导致的管理员 401 时会重新认证一次，再重试该次被拒绝的操作。认证失败或其他错误会停止。更新前已打开的旧终端需要从桌面菜单重新打开，文件更新不会替换旧进程中的代码。

## 12. 额度、短信接续与网页失败证据

### 请求渠道开关

管理看板“各平台网页请求成功率”的六张平台卡片各有一个“网页优先”开关，分别控制豆包、DeepSeek、Kimi、千问、文心一言和元宝。开启为该平台先网页、失败后 API 兜底（`web_first`）；关闭为该平台直接调用 API（`api_only`），不改变其他平台。例如，只关闭 DeepSeek，同一个多平台任务仍可在豆包走网页、DeepSeek 走 API。

设置持久化在 `data/jobs/request-routing/state.json`，重启后保留。升级时六个平台继承旧总开关的状态，不主动启停任何平台。已受理、排队、执行和等待重试的任务均保留受理时的策略；重复 `request_id` 仍返回原任务，不改变策略。新任务响应的 `site_execution_modes` 保存本任务各平台的实际策略，如 `{ "deepseek": "api_only", "doubao": "web_first" }`；`execution_mode` 在各平台相同时为对应模式，不同时为 `mixed`。旧任务没有平台策略映射时继续使用原 `execution_mode`，两者都没有时按原网页优先策略执行。

管理员 Cookie 鉴权接口：

- `GET /api/admin/request-routing`：返回 `sites` 中六个平台的独立状态，例如 `sites.deepseek = { "web_first": false, "mode": "api_only" }`，以及整体 `mode`、`revision` 和 `updated_at`。整体 `mode` 可以为 `mixed`；顶层 `web_first` 仅保留旧总开关的兼容默认值，读取平台状态请使用 `sites`。
- `PUT /api/admin/request-routing`：发送 `{ "site_id": "deepseek", "web_first": false, "revision": 1 }`，只修改指定平台，返回完整设置。文心一言使用 `wenxinyiyan`，兼容 `yiyan`；未知平台、空值或错误类型返回 400。必须带当前修订号；其他管理员已修改时返回 409，重新读取后再操作。写入失败返回 503，所有平台运行模式保持不变。
- 为兼容旧客户端，不传 `site_id` 的 `{ "web_first": false, "revision": 1 }` 仍表示将全部六个平台统一设置为直接 API；管理看板的独立开关始终传递 `site_id`。

`api_only` 直接进入原 API 执行通道，使用原有并发限制、执行预算、取消和持续交付重试策略；无需浏览器、登录或 CLI，不生成虚假的网页失败 / 跳过记录。直接 API 模式不受“是否允许网页失败后的 API 兜底”配置开关影响，仍需有效的平台 API 凭据。凭据未配置时明确返回 / 等待 `api_not_configured`，不会擅自改走网页。

管理员登录检测 `POST /api/admin/login-check` 新增可选 `account_ids` 数组，用于只检测 / 辅助登录指定的启用账号；省略时保持全号池检测。可同时提供 `site_id` 限定平台，不存在或停用的账号返回 400。单轮检测仍使用已有浏览器和 CLI 并发预算，不抢占正在执行的业务页面。

显式登录检测结束后，仍在验证码或短信步骤的原页面保留 20 分钟，避免 CLI 处理时间耗尽后台登录的短期保活窗口。短信倒计时消失后，只有同一发码轮次、同一原页面、仍有可见验证码输入框且发码未满 5 分钟，才继续判为等待短信；首次打开的表单和替换页面不据此判为已发码。验证码回调仍按实际接收时间限制 3 分钟提交有效期。

### 额度与短信接续

管理员登录后可通过 `GET /api/admin/monitor/actions` 读取当前人工待办、自动等待及 `account_limits`。该接口只读取状态，不触发发码、CLI、提问或额度探测。普通 AK/SK 无权读取管理员看板。

`account_limits` 每项按账号与平台记录以下字段，账号仅显示脱敏标签：

| 字段 | 含义 |
| --- | --- |
| `kind` | `quota_exhausted` 额度耗尽、`account_muted` 禁言、`quota_or_restriction` 历史记录尚不能区分 |
| `first_detected_at` / `last_detected_at` | 本次限制首次及最近一次网页观测时间，不能推断实际耗尽的更早时刻 |
| `reset_at` / `reset_precision` | 平台明确提示的恢复时间；`date` 表示平台只给出日期，具体时刻未知；零值表示未说明 |
| `next_probe_at` | 最早允许后续网页任务复查的时间，不保证届时有任务，也不表示额度已经恢复 |
| `recovered_at` | 网页成功后的恢复确认时间，登录成功或 API 兜底成功不会清除限制 |
| `status` | `blocked` 暂停调用、`pending_recheck` 到期待复查、`recovered` 网页成功已确认恢复 |
| `source` / `detection_count` | `live` 当前观测、`history` 历史网页尝试回填；同次限制发现次数 |

未知恢复日期的额度限制默认复查间隔为 30 / 60 / 120 分钟，后续保持 120 分钟。基础值 `GEO_QUOTA_COOLDOWN_SEC`、上限 `GEO_QUOTA_MAX_COOLDOWN_SEC`。平台给出的未来禁言截止时间、额度重置日期和元宝“次月 1 日零点刷新”优先生效。到期前跳过该账号，额度限制不会交给 CLI 修复。相关元数据保存在 `data/jobs/account-protection/state.json`，兼容旧停用时间及原页面所属任务，重启后保留。

当前待办的 `handoff_stage` / `handoff_updated_at` 表示短信回调接续证据，包括回调匹配、等待原页面、等待页面操作权、提交中、提交后未确认、登录已确认，以及过期或原页面丢失。接续绑定账号、平台、本轮发码时间和原 target，使用短信实际接收时间检查有效期；重复回调和同轮验证码不会重复提交。仅提交前的未完成记录可以在重启后恢复；提交期间中断的结果标记为未知，转交人工检查，不自动重放验证码。无已确认发码轮次或原页面时同样转交人工，不重新打开登录页或发送短信。状态文件 `data/sms-handoffs.json` 不保存完整手机号、短信内容或验证码；现有私有短信收件箱另行保留。

新增网页失败尝试可带 `failure_stage`、`failure_reason`、`diagnostic_id`；例如原有 `site_request_failed` 可以进一步说明为 `browser_page_crashed`，不改变原业务错误码。私有诊断 `process.json` 记录子进程 PID / 退出码及浏览器 cgroup 内存前后快照、事件计数差值。该 cgroup 由多个浏览器共享，OOM 计数变化仅是关联证据，不能直接归因到某个账号或问题。历史记录缺少的证据不会伪造补齐。

## 13. HTTPS 证书与续期

HTTPS 监听采用证书热加载：`certs/cert.pem`、`certs/key.pem` 更新后，新连接自动使用新证书，无需重启业务；文件短暂不完整时沿用上一组已加载证书。`/.well-known/acme-challenge/{token}` 仅提供受限的公开验证文件，不提供目录列表。

`scripts/install_public_certificate.py` 为 Certbot deploy hook，仅安装主机 IP 匹配、密钥配对且证书链受信任的有效证书。`geo-certificate-renew.service` / `.timer` 用于每天两次检查续期；正式签发并验证访问后才启用 timer。当前 Certbot 位于 `/opt/geo-certbot/bin/certbot`。

2026-10-16 上线热加载后，staging 验证仍因公网 TCP 80 连接超时失败，须先在上游云防火墙/安全组开放证书校验入口。正式证书尚未签发，续期 timer 尚未启用；原自签名证书继续提供 8443 服务。不要将“首页 200”当作浏览器已信任证书。
