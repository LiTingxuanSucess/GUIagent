# Issues

| Date | Area | Issue | Root Cause | Fix | Verification | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-06-07 | WXWorkAgent | 企业微信出现自问自答和重复回复 | 仅按 OCR 文本中心点判断消息方向，换行后的蓝色己方气泡可能被误判为客户消息 | 新增像素级气泡版面解析，识别蓝色己方气泡和灰色客户气泡，并用发送后 OCR 确认己方最新气泡 | `wxwork_layout_smoke.py`、现场 dry-run 和 SouthIsland 实测 | Done |
| 2026-06-07 | WXWorkAgent | 群聊、内部账号和无关对话存在误回复风险 | 可见列表 OCR 只按文本变化触发，缺少会话类型和业务相关性保护 | 增加群聊跳过、业务关键词过滤、已关注联系人状态和回复质量门禁 | 群聊样本跳过、通识问答样本复测 | Done |
| 2026-06-07 | WXWorkAgent | 联系人列表名称和预览串行错配 | 旧逻辑按相邻 OCR 行两两配对，OCR 顺序抖动时会跨行配错 | 用头像/行带检测联系人列表行，再把 OCR 文本挂到对应行内 | 联系人列表 smoke test | Done |
| 2026-06-07 | WXWorkAgent | SouthIsland 生成了回复但没有真正发出 | 发送动作未做送达确认，仍提前写入 `sent_keys`，后续被当作已回复 | 发送后重新 OCR 最新己方气泡，确认包含回复文本后才标记已发送 | SouthIsland 现场截图和日志确认 | Done |
| 2026-06-07 | WXWorkAgent | 两个账号同时咨询时只回复一个 | 旧循环单次处理一个 diff，发送期间的新消息没有排队，联系人坐标可能过期 | 引入 pending 队列、点击前重定位联系人行、发送后重扫列表，并跳过自发消息预览回流 | 两账号并发场景分析和 live 服务重启 | Done |
| 2026-06-07 | WXWorkAgent | Paddle 版面识别对企微 UI 帮助有限 | PPStructure 面向文档版面，会把聊天 UI 识别成 `figure/table`，不能稳定切出气泡和联系人行 | 保留离线探针，正式链路采用企微 UI 专用像素版面解析 | `paddle_layout_probe.py` 输出对比 | Decision |
| 2026-06-07 | OCR | 本机 GPU OCR 启动不稳定 | NVIDIA DLL 路径缺失，且 PaddleOCR 2.x/3.x API 不兼容 | 启动脚本补 cudnn/cublas/nvrtc 路径，OCR runtime 同时兼容 `predict()` 和 `ocr()` | OCR health、warmup 和编译检查 | Done |
| 2026-06-07 | QA Engine | 企业微信未复用 car-agent 本地通识问答基线 | 本地兼容服务偏抖音链路，未显式传递企微 channel，环境变量也未兼容 DashScope | `local_carzone_compat_server.py` 加载 car-agent baseline，映射 DashScope 环境变量，传入 `channel=wxwork` 并 fail closed | `127.0.0.1:8787/health` 和企微 dry-run | Done |
| 2026-06-07 | DouyinCSAgent | 新来客 `life.douyin.com` 页面无法兼容旧 cluerich 登录页逻辑 | 页面 URL、DOM 结构和消息节点类名与旧链路不同 | 增加 `LIFE_SESSION_URL`、页面 marker 和 life 私信 DOM 抽取逻辑 | 代码编译检查 | Done |
