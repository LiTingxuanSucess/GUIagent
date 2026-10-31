# 网页多平台采集参考与本轮改动（2026-10-12）

目标仍是六个平台真实网页回答、真实引用和可匿名访问的分享页。官方模型 API 的回复不能替代网页产品回答；单平台总预算 120 秒、多平台总预算 300 秒。

| 参考 | 已核对的公开实现 | 对当前程序的取舍 |
| --- | --- | --- |
| [Weiming3/browser-ai](https://github.com/Weiming3/browser-ai) | Playwright、持久化登录资料、多个网页源并发分发 | 保留现有按平台隔离和并发调度；登录资料复用适用，不需要再引入一个 Agent 执行框架 |
| [lhaovo/geo-monitoring](https://github.com/lhaovo/geo-monitoring) | Kimi 结合停止按钮和回答操作区判断完成；元宝使用 finished 状态；引用从平台页面提取 | 独立实现按平台观察生成状态，避免仅凭文本长度稳定判断完成；未知状态保留文本稳定检查 |
| [GunGig/GEOApp](https://github.com/GunGig/GEOApp) | 产品说明提供预登录管理、网页回答、引用及分享采集 | 克隆仓库只有说明与图片，无法验证采集代码和速度；不把宣传当作性能证据 |
| [AgentBay 会话文档](https://github.com/agentbay-ai/wuying-agentbay-sdk/blob/main/docs/guides/common-features/basics/session-management.md) | get 恢复会话、keep_alive、持久化上下文和空闲释放 | 保留已有会话健康检查；复用窗口 90→150 秒，云端空闲释放仍为 180 秒 |
| [Browserless 会话文档](https://docs.browserless.io/baas/session-management/standard-sessions) | 有效期内重连可保留页面状态；该 reconnect 工作流要求 Puppeteer | 采用减少重复初始化的思路，继续使用 AgentBay SDK；不把 Puppeteer 专用命令搬到 Playwright |

代码核对版本：browser-ai `85cfb6d6778569ce547605b895e0e4c4daf963e8`；geo-monitoring `c7e12b509dff0f549a9dc3b08a38558e6e41b5f3`；GEOApp `b9600854ce7dd58e97370804b68875dd76478930`。外部仓库仅作阅读参考，没有安装或执行其代码。

本轮实现：

- Kimi 的停止生成按钮仍可见时不判完成；回答操作区出现后，正文稳定确认缩短至约 1 秒。
- 元宝可观察到 `finished` 时采用同样的短确认。豆包正在流式生成的块仍会阻止提前结束；块结束不等同于整条回答结束，因此保留整条文本稳定检查。
- 文本稳定比较完整内容，修正长度相等但内容变化的误判。等待使用 Playwright 的事件循环，让网络响应和界面更新在等待期间持续处理。
- 结果和诊断包含 `completion_detection=dom_state` 或 `text_stability`，便于确认实际使用了哪种路径。
- 匿名分享验证失败时独立保存 `public-share.html`、`public-share.txt`、`public-share.png` 和 `public_share_probe`，不覆盖已登录回答页证据；输入及敏感参数继续脱敏。
- 文心的实际匿名证据显示旧分享流程分享了任务确认消息。改为选择最终历史回答的可见分享操作，跳过隐藏控件；实际复测取得最终正文、10 条引用与通过匿名校验的分享链接。
- Kimi 观察到发送后停止生成按钮出现即可确认网页接收，不必再等待首段正文才开始回答等待；已有生成状态不作为新请求确认。
- 网络回调只登记已完成响应，正文在主流程中读取一次，避免同步 Playwright 操作嵌套及重复读取。历史和聊天关键 JSON 的内存上限与保存上限统一为 2 MB，普通响应仍限 500 KB，避免大型历史 JSON 在 500 KB 处截断导致解析失败。
- 已登录浏览器复用窗口延长到 150 秒，仍检查 SDK 会话有效性，保留云端 180 秒空闲释放和同平台串行锁。
- 冷启动先打开 `about:blank`，待 SDK 取得 CDP 后再恢复登录资料并打开平台网页，避免站点首次加载影响浏览器初始化。在同一份隔离豆包上下文中，两次仅启动验证分别为 13.4 秒、12.0 秒；仍需实际问答确认，不能把启动成功当成平台回答成功。

不能靠增加并发解决失效登录或验证码。DeepSeek 的已知登录拒绝仍需重新登录；不清空生产登录资料，不自动尝试绕过验证码。实测速率、耗时与链接完整性以本轮隔离测试及生产复核文件为准；小样本不能推导长期成功率或承诺所有平台在 1–2 分钟内完成。

阶段实测：首轮六平台 148 秒收尾，4 个平台有网页回答；空白页启动后的一轮六平台 180 秒收尾，5 个平台有网页回答（DeepSeek 登录失效），其中 4 个分享通过匿名校验。两轮 Kimi / 元宝热复用均成功，浏览器建立连接约 4–5 秒；整条请求仍受平台生成时间影响。修正文心分享操作后的三平台复测为 Kimi 91 秒、文心 119 秒、豆包 130 秒，均有引用和有效分享。这些是不同请求的阶段样本，不能据此计算因果意义上的性能提升百分比。

生产复核进一步发现文心任务模式的引用位于 `task_mode` SSE 和 `canvas/search` JSON 的嵌套 `referenceList` 中，旧解析器把这两个接口作为噪声忽略。新增只返回引用的解析器，必须由顶层或 `data.query` 精确匹配当前问题，再读取该任务的引用列表；任务进度不会覆盖最终回答。用同一条实际生产响应离线回放恢复 9 条引用，换成不同问题时返回 0 条，避免混入历史会话。

后续复用检查发现 `Page.evaluate` 不支持 `timeout` 参数，之前健康页面也会判失败，导致元宝额外导航及多标签时关闭原页面。改用支持超时的 `wait_for_function`，唤醒页面后释放临时 CDP session；豆包先唤醒再检测登录。真实 Chromium 回归覆盖两标签复用和冻结豆包标签恢复，不依赖放宽签名的模拟对象掩盖错误。

短期人工候选接入参考 [AgentBay Call For User](https://github.com/agentbay-ai/wuying-agentbay-sdk/blob/main/docs/guides/browser-use/core-features/call-for-user.md)。SDK 提供同会话人工接管入口及完成通知，本地安装也确认相应接口存在；当前采集流程没有完成集成，回调与 Playwright 的配合需要隔离实测。[完整人工方案](IMPLEMENTATION_NOTES.md#2026-10-12-页面复用修复与短期人工方案)区分本地登录维护与云端接管、自动截止时间与人工补齐时间，设计有限试运行和退出门槛。

页面复用修复后的两轮豆包 / 元宝隔离请求，首次分别 115.8 秒、103.6 秒；复用浏览器的第二轮分别 63.8 秒、63.6 秒，四条网页回答均有引用和匿名分享通过。复用登录检查分别为 2.27 秒、2.07 秒；冷启动仍分别为 44.9 秒、20.5 秒，需要继续按阶段排查加载耗时。这两轮不是六平台并发样本，也不足以宣称稳定达到 1–2 分钟。

2026-10-12 夜间生产检查：21:00 后的快照中 DeepSeek 17 次均为网页登录失败；诊断中的 `/api/v0/users/current` 返回 `40003 / invalid token`。豆包同期 28 次中 10 次取得网页回答、15 次风控、2 次超时、1 次繁忙，最近的真实 DOM 显示验证码。API 密钥已有配置，但之前为网页回答要求关闭了全局兜底。本轮按用户最新指示恢复六平台全局兜底，并增加按站点独立开关；不把 API 输出混称为网页回答。

依据 [DeepSeek Responses API 文档](https://api-docs.deepseek.com/guides/responses_api/)，内置 `web_search` 被忽略；删除该站点无效搜索尝试，直接调用 chat/completions，正文 URL 不冒充搜索引用。[Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)支持 `thinking.type=disabled`，兜底采用该模式并限制输出 token，接口报告截断时返回失败。豆包仍使用 [火山方舟官方联网搜索](https://docs.volcengine.com/docs/ark/web-search?lang=en) 的 Responses 工具；搜索不可用时可在剩余总预算内退回普通回答。DeepSeek、豆包网页与 API 分别最多 60 秒，原始单平台 120 秒和多平台 300 秒总截止时间保持有效。

## 2026-10-13：REA 生产证据分析与风控处置建议

本轮实际运行 [morluto/rea](https://github.com/morluto/rea) 的缓存 CLI 6.4.0，对六平台的 8 份生产 HAR 做离线解析，8 次均成功，共 909 条保留的网络记录。结合对应 manifest、DOM 和进程退出信息判断故障类型，未重放请求。REA 原始输出、文件 SHA-256、记录 ordinal 和脱敏摘要保存在 `/root/workspace/test/GEO/server/data/rea-analysis-20261013/`，摘要为 `summary.json`。REA 对 HAR 的定位是历史生产者证据，不能据此推断服务端真实风控模型或部署真实性，见[网络捕获说明](https://github.com/morluto/rea/blob/main/docs/web-network-captures.md)。

可复核运行方式：

```sh
node /root/.npm/_npx/dea9475797100b20/node_modules/rea-agents/scripts/rea.mjs \
  inspect-web-network-capture /absolute/path/network.har har --json --full-output
```

### 已确认与尚不能确认的结论

| 平台 / 样本 | 现场证据 | 结论与处置 |
| --- | --- | --- |
| 千问 / `qianwen-challenge`，55 条 | `/api/v2/chat` HTTP 200，随后出现 `_____tmd_____/punish` 验证 iframe；标题“验证码拦截”，正文要求拖动滑块 | 确认安全验证。现有 `platform_risk_control` 分类正确；应暂停对应网页会话并转 API，人工验证完成后再恢复 |
| 千问 / `qianwen-success-control`，90 条 | 同样 `/api/v2/chat` HTTP 200，现场无验证 iframe，进程成功，经过分享创建和匿名验证 | 同一聊天接口的 HTTP 200 无法区分回答成功与验证拦截，必须结合业务响应、当前页面和完成状态 |
| 豆包 / `doubao-challenge`，150 条 | `/chat/completion` HTTP 200；REA ordinal 120 为 `rmc.bytedance.com/verifycenter/captcha/v2` HTTP 200，同一 checkpoint 的 frame 1 标题“验证码”；最终进程 60 秒超时 | 确认该现场存在验证页面，同时暴露验证识别与最终错误分类的缺口。不能仅凭现有数据证明验证是超时的唯一原因 |
| 豆包 / `doubao-stale-checkpoint`，150 条 | 最新 manifest 仅列主页面与 drive iframe，两个 DOM 快照均 TimeoutError；目录旧 frame 文件却残留验证码文字 | 不将旧快照与新 manifest 拼接成当前验证码证据。诊断文件应按 checkpoint 世代写入，失败快照明确不可用 |
| DeepSeek / `deepseek-auth`，47 条 | `/api/v0/users/current` HTTP 200，但业务 `code=40003`、`Authorization Failed (invalid token)`；页面“登录信息已失效，请重新登录” | 确认登录令牌失效。登录表单的“发送验证码”不是滑块风控证据。停止恢复被拒绝的令牌，待重新登录后再开放网页 |
| Kimi / `kimi-latest`，150 条 | ChatService/Chat HTTP 200，`send_acknowledgement=generation_started`；等待 40.46 秒后返回“已发送问题，但平台回答尚未生成完成” | 该样本是未取得完整回答；保留记录无 HTTP 429、无明确验证页面，也无“聊天的人太多了”文字，不能称为已确认限流或验证码风控 |
| 元宝 / `yuanbao-latest`，150 条 | `/api/chat/...` HTTP 200，登录阶段 31.83 秒，发送阶段 28.54 秒，等待 11.90 秒后未取得完整回答 | 优先核对登录耗时、流式完成与页面状态；该样本没有明确验证码或 HTTP 429 证据 |
| 文心 / `yiyan-latest`，117 条 | 会话接口 HTTP 200，等待阶段 73 秒，进入 DOM 提取后进程 120 秒超时 | 先查完成判定、提取与预算耗尽；目前不能确认为风控 |

注意：这些 HAR 均未保留完整响应正文，部分只剩最近 150 条记录；采集从 CDP 连接后开始。HTTP 200 可能只说明流式响应已开始，HTTP 0 表示状态未知，不能当作服务端明确拒绝。豆包图片资源的 403 也不能直接归因为聊天账号封禁。

当次诊断目录扫描有 301 份 manifest/process，豆包 73 份 `site_timeout`、DeepSeek 63 份 `platform_auth_unavailable`、Kimi 53 份 `platform_busy`、元宝 51 份 `platform_busy`。这是滚动保留的诊断样本；失败大多保留、成功仅抽样约 5%，且目录随请求更新，因此不能由这些数量计算平台成功率或验证码发生率，更不能把豆包 73 个超时全算作验证码。

### 现有策略与建议优先级

代码和 `optimization.env` 已配置站点串行门禁：同一站点同时只有一个网页执行，6 是跨站点总并发，不是同平台 6 个网页请求。验证码、配额、登录失效均不做网页重试；验证码冷却 600 秒、配额 1800 秒、云端登录失效 120 秒、短信超时 300 秒；官方 API 在网页冷却期间仍可用。全局 API 兜底与联网搜索已开启；豆包 API 预算已增加到 120 秒、豆包单平台总预算 210 秒。上文 10-12 的 60/120 秒描述属于历史配置。

建议按以下顺序处理，以下是分析建议，尚未在本轮部署：

1. **先修分类及诊断一致性。** 给当前可见验证 iframe、业务错误码、等待/提取失败分别记录时间和证据类型；豆包在提取与截止前也检查验证状态。单纯加载验证码 SDK 不算验证拦截，通用“验证码”文字不算风控。每个 checkpoint 使用独立世代与明确文件清单，避免旧 frame 文件被当成本次快照。
2. **登录失败改为账号熔断。** 明确拒绝的账号保持不可用，重新登录并验证成功后恢复，避免固定 120 秒一到继续恢复同一失效令牌。站点冷却与账号状态分别管理；已有按站点串行门禁应保留。
3. **验证拦截走人工处理和 API 兜底。** 网页识别到验证后停止发送、暂停对应会话；时间冷却结束不代表验证已通过。恢复需要验证已消失且登录健康。真实网页结果、官方 API 结果、外部搜索来源继续标明渠道，API 的来源引用不等于网页产品的分享链接。
4. **未完成生成与限流分别处理。** Kimi/元宝记录发送确认、首段回答、流式终止、DOM 完成和提取结果；明确 429 才按 Retry-After 或退避处理，已确认生成开始则避免重复提交同一问题。冷启动、登录、等待、提取都计入总预算，提前保留 API 完成时间。
5. **补充指标并做单变量对照。** 看板区分确认验证码、鉴权拒绝、明确限流、生成未完成、页面无响应和纯预算超时，保留每次实际处置及恢复原因。在同一有效账号、相近问题和同一出口条件下，对照浏览器复用/冷启动、请求间隔、低并发/现有总并发，衡量网页成功率、验证率、P95 和 API 兜底率。现有 HAR 缺少 TLS、HTTP/2 指纹和完整响应，不能直接断言 IP 信誉或浏览器指纹是根因。

### 其他已查阅工具及适配性

| 工具 | 官方能力与本项目用途 | 推荐顺序 |
| --- | --- | --- |
| [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer) | 将操作、DOM 快照、控制台、网络和时间线对齐，适合定位“请求已发出，但等待/提取失败”；本项目已使用 Playwright | 优先，在隔离复现中启用，证据控制在本机并限制留存 |
| [Chrome DevTools / CDP](https://developer.chrome.com/docs/devtools/network/reference) | 查看 initiator、frame、网络 Timing、协议与导入 HAR，补充关键请求由哪个页面/脚本发起及耗时分解 | 优先，与 REA 的离线结构化结果互补 |
| [mitmproxy / mitmdump](https://docs.mitmproxy.org/stable/addons/examples/) | 保存流量并通过 Python 分析离线 flows，便于跨请求汇总；流式响应的正文保留受捕获设置影响 | 在隔离环境需要更完整网络证据时使用；代理会改变连接路径，不能把代理条件下的差异直接当作生产根因 |
| [Fiddler Everywhere MCP](https://www.telerik.com/fiddler/fiddler-everywhere/documentation/agent-tools/fiddler-mcp-server) | GUI 与 MCP 可联查 HTTPS 会话，支持浏览器、终端、反向代理和应用捕获 | 人工排障候选，当前服务端已有 REA/CDP，无需为本轮另接一套生产捕获链 |

当前更适合采用 REA 离线归纳 + Playwright/CDP 补齐现场。工具可以把证据整理清楚，但无法从少量失败 HAR 还原平台私有的评分模型与触发阈值。
