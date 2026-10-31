# GEO cn_ai — Linux 服务化方案（推荐做法）

更新时间：2026-08-15

> 目标：把现在绑在 **Windows + 本机 Chrome CDP** 上的国内六站抓取，迁到 **Linux 服务器**长期跑。  
> 结论先说：**第一阶段不要硬做「纯协议无浏览器」**；最佳路径是 **Linux 常驻 Chromium（持久登录态）+ 现有 Playwright CDP 抓取 + 会话探活/补登**。Viewer 继续只读展示，可与抓取机分离。

---

## 1. 现状与约束

| 项 | 现状 |
|----|------|
| 抓取核心 | `GEO/cn_ai/*` + `batch_scrape_cn.py` |
| 浏览器 | Windows Chrome，`--remote-debugging-port=9222`，profile=`User Data_CDP` |
| 登录 | 人工扫码/验证码进 profile；抓取只「消费」已有登录态 |
| 解析 | 优先网络协议（SSE/history/chain），UI 仅负责发题与保活 |
| 展示 | `GEO/viewer` + frp `40216`（已可上 AutoDL Linux） |

六站网页版普遍：**登录态在 Cookie / localStorage / 短期 Bearer**，且常有设备/IP/风控绑定。  
因此「一上 Linux 就全协议直连、完全无人值守登录」不现实；应分阶段。

---

## 2. 推荐架构（Phase 1）

```text
                    ┌──────────────────────────────────────┐
  任务 API/Cron ──► │  cn_ai Worker (Linux)                │
                    │  batch_scrape_cn / scrape_one         │
                    │         │ CDP ws://127.0.0.1:9222    │
                    │         ▼                            │
                    │  Chromium (headful 或 xvfb)           │
                    │  持久 user-data-dir = 登录态仓库       │
                    └──────────────────────────────────────┘
                                      │
                                      ▼ 写入
                               GEO/exports/*.json
                                      │
                                      ▼
                    ┌──────────────────────────────────────┐
                    │  Viewer (可同机或另一台 Linux)         │
                    │  uvicorn :18765 + frpc → :40216      │
                    └──────────────────────────────────────┘
```

**为什么这是最好的第一步**

1. **代码改动最小**：`scraper.connect_over_cdp` 不变，只把 CDP 从 Windows 挪到 Linux。  
2. **登录可运营**：profile 目录当「账号资产」备份/迁移；失效用远程桌面/noVNC 补登一次。  
3. **与已验证解析兼容**：各站 network_parser 仍吃同一套响应。  
4. **和 Viewer 解耦**：抓取机可内网；公网只暴露只读 viewer（现有 `DEPLOY_AUTODL.md`）。

**不推荐一上来就做的事**

- 六站同时上「无头协议机器人」（维护成本爆炸）  
- 多进程共用同一个 Chrome profile（必锁库、必串会话）  
- 把登录二维码自动化成无人验证码破解（不稳定且有合规风险）

---

## 3. Linux 上浏览器与登录怎么搞

### 3.1 常驻 Chromium + 持久 profile

```bash
# 示例目录
/opt/geo-cn-ai/
  profile/          # user-data-dir（等同现在的 User Data_CDP）
  exports/
  .venv/
  repo/GEO/         # 代码

# 启动调试端口（需显示器或 Xvfb）
xvfb-run -a google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/opt/geo-cn-ai/profile \
  --no-first-run --disable-dev-shm-usage \
  about:blank
```

- 用 **Google Chrome** 或 **Playwright 自带 Chromium** 均可；关键是 **固定 user-data-dir**。  
- 生产建议 **systemd** 管 Chrome + Worker；崩溃自动拉起。  
- 一台机 **一个抓取 profile**（或一站一 profile）；禁止多 Worker 抢同一 profile。

### 3.2 首次登录与补登（会话管家）

| 步骤 | 做法 |
|------|------|
| 首次 | 开 noVNC / 临时 VNC / SSH 端口转发到 `9222` 的浏览器 UI，人工登录六站 |
| 探活 | 定时用 Playwright 打开各站首页，检测是否跳到登录页 / 缺少输入框 |
| 告警 | 探活失败 → 飞书/企微/邮件；标记该站 `auth_expired` |
| 补登 | 运维点开 noVNC 扫码；通过后 Worker 自动恢复 |

可选增强（Phase 1.5）：

- 登录成功后导出 cookie 快照到加密存储（仅备份，仍以 profile 为准）  
- IP 尽量固定（同机房出口）；换 IP 后提高补登频率

### 3.3 与 Windows 开发机的关系

| 环境 | 用途 |
|------|------|
| Windows + CDP | 继续做单站调试、修 parser、验收新题 |
| Linux 服务器 | 定时/队列生产抓取 + 持久登录态 |
| AutoDL Viewer | 只读展示 exports（已有更新脚本） |

开发仍可在 Windows；**生产登录态以 Linux profile 为准**，不要两边抢同一套 cookie。

---

## 4. 进程与接口建议

### 4.1 最小可跑（先不用上大框架）

```bash
export CDP_URL=http://127.0.0.1:9222
export NO_PROXY=*
cd /opt/geo-cn-ai/repo/GEO
.venv/bin/python batch_scrape_cn.py
# 或单站单题
.venv/bin/python batch_scrape_cn.py --site deepseek --question-id solver_vendors
```

用 cron / systemd timer 触发即可。

### 4.2 下一步再服务化（Phase 2）

在不变 CDP 模型的前提下加薄 API：

| API | 含义 |
|-----|------|
| `POST /jobs` | 提交 `{sites[], question_ids[]}` |
| `GET /jobs/{id}` | 状态 / 日志 |
| `GET /sessions` | 各站登录探活结果 |
| `POST /sessions/{site}/mark-ok` | 补登完成后人工确认 |

Worker 串行或「一站一浏览器」并行；结果仍落 `exports/cn-ai-*.json`，Viewer 原样读。

---

## 5. 分站登录态策略（务实）

| 站点 | Phase 1 | Phase 2+（可选） |
|------|---------|------------------|
| DeepSeek | CDP + profile | Bearer/`history_messages` 可加强备份 |
| 豆包 | CDP + profile | chain/single 依赖登录 cookie，慎拆 UI |
| 文心 | CDP + profile | `history/list` token 可缓存，仍要浏览器发题 |
| 元宝 | CDP + profile | `conversation/detail` 可加强 |
| 千问 | CDP + profile | 注意弹窗/Enter 发送流程 |
| Kimi | CDP + profile | 联网搜索依赖 UI 开关，短期难纯协议 |

**原则**：发题与「联网/新会话」仍走 `site_flows`；正文与引用尽量走已稳定的 `network_parser`。

---

## 6. 部署清单（Linux 抓取机）

1. 安装 Chrome/Chromium、Xvfb（无显示器时）、Python 3.11+、Playwright 依赖  
2. 创建 `/opt/geo-cn-ai/profile`，首次人工登录六站  
3. 拉代码，`pip install -r`（viewer 与抓取依赖分开也行）  
4. `playwright install chromium`（若用 PW 浏览器）  
5. systemd：`geo-chrome.service` + `geo-cn-ai-worker.service`（或 timer）  
6. 探活脚本 cron（每 30～60 分钟）  
7. exports 同步到 Viewer 机（rsync / 共享盘 / 现有 `update_autodl_geo.ps1` 思路）

Viewer 公网部署继续看：[`DEPLOY_AUTODL.md`](DEPLOY_AUTODL.md)。

---

## 7. 风险与合规

- 账号封禁、验证码升级、页面改版 → 需要「补登 + 单站热修 parser」的运营节奏  
- 自动化访问可能违反站点 ToS；仅限自有账号、内网研究/业务用途  
- profile 含登录态，备份需加密，权限收紧到运行用户  
- 同一 `remotePort`（如 40216）全局只能一个 frpc；抓取机与展示机穿透不要抢端口

---

## 8. 路线图（建议顺序）

| 阶段 | 交付 | 验收 |
|------|------|------|
| **P0** | Linux Chrome + 持久 profile + CDP 跑通单站单题 | `ok=True` 且有 citations |
| **P1** | systemd + 六站定时批跑 + 探活告警 + exports→Viewer | 连续 3 天无人工盯屏（仅偶发补登） |
| **P2** | Job API 队列化；一站一浏览器隔离 | 并发多题不串会话 |
| **P3** | 单站协议增强（DeepSeek/文心/元宝先行） | 部分站 UI 仅发题或可降级 |

---

## 9. 和本仓库文件的对应关系

| 路径 | 角色 |
|------|------|
| `cn_ai/scraper.py` | CDP 抓取主流程（Linux 上改 `CDP_URL` 即可） |
| `cn_ai/site_flows.py` | 各站 UI 发题 |
| `cn_ai/network_parser.py` | 协议解析 |
| `batch_scrape_cn.py` | 批量入口 |
| `viewer/` + `frp-geo-40216/` | 只读展示与穿透 |
| `tools/update_autodl_geo.ps1` | 展示机更新（非抓取机） |
| 本文 | Linux 抓取服务化总纲 |

---

## 10. 一句话决策

**Linux 上最好的搞法：把「已登录的 Chrome」当成有状态服务常驻，cn_ai 继续 CDP 驱动；登录问题用「持久 profile + 探活 + 远程补登」解决，而不是先赌六站纯协议。**
