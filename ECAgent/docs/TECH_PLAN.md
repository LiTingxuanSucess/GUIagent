# ECAgent 产品价格追踪技术方案

更新时间：2026-05-03

## 1. 需求范围

根据 `docs/产品价格追踪需求文档.pptx` 和 `docs/REQ.md`，当前目标是通过自动化方式追踪电商平台重点品牌和系列的商品价格，并输出可对标的表格。

平台：

- 天猫
- 京东
- 抖音

品牌：

- 杜蕾斯
- 杰士邦
- 冈本
- 名流
- 赤尾
- beU

系列：

- 001
- 003
- AiR
- 超薄
- 经典
- 持久
- 玻尿酸
- 情趣

目标结果：

- 进入品牌官方旗舰店、京东自营或抖音旗舰店。
- 在店铺内按系列词搜索。
- 按销量排行锁定代表产品。
- 进入商品详情页，记录链接主图产品、到手价、截图。
- 解析规格和片数，计算片单。
- 输出结构化表格，用于竞品对标和定价策略。

## 2. 已验证进度

### 2.1 Chrome CDP 接管方式

已确认可以复用 `DouyinCSAgent` 的浏览器接管方式。核心是用用户自己的 Chrome 用户数据目录启动带 CDP 端口的 Chrome：

```powershell
& "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
  --no-first-run `
  --no-default-browser-check `
  "https://www.taobao.com"
```

验证方式：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9222/json/version
```

返回 Chrome 版本和 `webSocketDebuggerUrl` 即表示 Playwright 可以通过 CDP 附着。

### 2.2 淘宝搜索验证

已通过 Playwright `chromium.connect_over_cdp("http://127.0.0.1:9222")` 附着当前 Chrome，并在淘宝搜索框输入关键词 `杜蕾斯 001` 后触发搜索。

验证结果：

- 搜索 URL 中的关键词正确编码为 `q=%E6%9D%9C%E8%95%BE%E6%96%AF%20001`。
- 页面标题变为 `杜蕾斯 001_淘宝搜索`。
- 页面正文能看到商品结果、价格、券后价、付款人数、店铺等信息。
- 搜索链路已通，可以继续做商品卡片结构化抽取。

### 2.3 发现的问题

PowerShell 默认编码会导致内联 Python 脚本里的中文变成 `???`。第一次搜索实际变成了 `??? 001`。

解决方案：

- 临时脚本里设置 `PYTHONIOENCODING=utf-8`。
- Python 内部调用 `sys.stdout.reconfigure(encoding="utf-8")`。
- 对关键词使用 Unicode 转义或从 UTF-8 配置文件读取，避免 PowerShell here-string 编码污染。

示例：

```python
KW = "\u675c\u857e\u65af 001"  # 杜蕾斯 001
```

后续正式代码应从 YAML/JSON 配置文件读取关键词，不在 PowerShell 命令里直接传中文。

### 2.4 登录态观察

这次搜索页面顶部仍出现过“亲，请登录”。用户确认已登录过。这里需要在后续实现中区分：

- Chrome 是否使用了正确的用户数据目录。
- 淘宝是否在当前 Tab 内识别登录态。
- 页面是否局部渲染了未登录文案，但接口/搜索结果仍可访问。

后续采集前应增加登录态检测，不满足时暂停并提示人工登录。

## 3. 技术路线

整体采用“浏览器数据优先 + 本地 OCR 补盲 + VLM/LLM 兜底”的低成本方案。

优先级：

```text
DOM/API 抽取 -> 本地 OCR 校验/补盲 -> VLM 复杂截图判断 -> LLM 归一化和策略判断
```

不建议把 VLM 作为主流程来全屏看图和点击。原因：

- 成本高。
- 高分辨率电商页面 grounding 不稳定。
- 商品标题、价格、销量、SKU 大多可通过 DOM 或接口获得。
- OCR/VLM 更适合处理券后价、弹窗、图片价、复杂促销说明等局部问题。

## 4. 可复用项目能力

### 4.1 DouyinCSAgent

可复用点：

- Chrome CDP 启动和附着方式。
- Playwright 页面控制。
- `page.on("response", ...)` 网络响应监听。
- DashScope/OpenAI-compatible 文本模型客户端。

重点文件：

- `D:\Workspace\gui-agent\DouyinCSAgent\start_chrome.bat`
- `D:\Workspace\gui-agent\DouyinCSAgent\douyin_watch.py`
- `D:\Workspace\gui-agent\DouyinCSAgent\src\qwen_text.py`

### 4.2 WXWorkAgent

可复用点：

- 常驻 OCR HTTP 服务。
- RapidOCR/PaddleOCR 双后端。
- 截图降采样、OCR boxes 坐标返回。
- 本地截图和 OCR 结果结构化。

重点文件：

- `D:\Workspace\gui-agent\WXWorkAgent\wxwork_ocr_service.py`
- `D:\Workspace\gui-agent\WXWorkAgent\src\ocr_client.py`
- `D:\Workspace\gui-agent\WXWorkAgent\src\capture.py`
- `D:\Workspace\gui-agent\WXWorkAgent\src\ocr_run.py`

建议 ECAgent 先直接通过 `WXWORK_OCR_SERVICE_URL=http://127.0.0.1:8765` 调用现有 OCR 服务。等流程稳定后，再把 OCR 服务抽成 `gui-agent/shared/ocr_service`。

## 5. 推荐架构

```text
ECAgent/
  config.yaml
  docs/
    TECH_PLAN.md
  src/
    browser.py
    runner.py
    platforms/
      taobao.py
      tmall.py
      jd.py
      douyin.py
    extractors/
      dom.py
      network.py
      ocr_fallback.py
      vlm_verify.py
    domain/
      catalog.py
      price.py
      scoring.py
    storage.py
    exporter.py
```

模块职责：

- `browser.py`：启动/附着 Chrome CDP，查找已有平台 Tab，处理登录态检测。
- `platforms/*.py`：各平台搜索、进店、排序、翻页、详情页适配。
- `extractors/dom.py`：从 DOM 抽商品卡片、标题、价格、店铺、销量、链接。
- `extractors/network.py`：监听 XHR/JSON，从接口响应抽商品和价格数据。
- `extractors/ocr_fallback.py`：调用 WXWorkAgent OCR 服务，校验页面可见价格和促销文案。
- `extractors/vlm_verify.py`：只处理低置信度截图，例如系列归属、图片价、复杂券后价。
- `domain/catalog.py`：品牌、系列关键词、排除词、店铺白名单。
- `domain/price.py`：规格片数、到手价、片单计算。
- `domain/scoring.py`：候选商品置信度和代表商品排序。
- `storage.py`：保存 JSONL/SQLite 中间结果，方便断点续跑。
- `exporter.py`：输出 CSV/XLSX 和截图目录。

## 6. 采集流程设计

### 6.1 平台级流程

1. 启动或附着 Chrome。
2. 打开平台首页。
3. 检测登录态；未登录则暂停等待人工登录。
4. 输入关键词或店铺名。
5. 进入官方旗舰店、京东自营或抖音旗舰店。
6. 在店内按系列词搜索。
7. 切换销量排序。
8. 抽取候选商品列表。
9. 根据规则和 LLM 判断系列匹配度。
10. 进入候选商品详情页。
11. 抽取标题、SKU、到手价、促销、销量、店铺、主图。
12. 局部截图价格区域，OCR 校验。
13. 低置信度样本调用 VLM。
14. 计算片单。
15. 保存记录和截图。
16. 输出表格。

### 6.2 单商品记录字段

建议字段：

- `platform`
- `brand`
- `series`
- `shop_name`
- `shop_type`
- `product_title`
- `product_url`
- `sku_name`
- `spec_text`
- `piece_count`
- `price_raw`
- `final_price`
- `unit_price`
- `sales_text`
- `rank_position`
- `image_url`
- `screenshot_path`
- `source`
- `confidence`
- `notes`
- `captured_at`

`source` 用于记录数据来源：

- `dom`
- `network`
- `ocr`
- `vlm`
- `manual_review`

## 7. 淘宝/天猫下一步实现细节

### 7.1 搜索输入

正式代码不要通过 PowerShell 直接传中文。应从配置文件读取：

```yaml
targets:
  - platform: taobao
    brand: 杜蕾斯
    series: "001"
```

Playwright 输入时优先使用 DOM setter + input/change 事件：

```python
await input_locator.evaluate(
    """
    (el, kw) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(el, kw);
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: kw }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    """,
    keyword,
)
await page.keyboard.press("Enter")
```

### 7.2 商品卡片抽取

下一步先在淘宝搜索结果页做商品卡片抽取，目标字段：

- 商品标题
- 价格
- 券后价
- 付款人数
- 店铺名
- 发货地
- 商品链接

抽取方式按优先级：

1. 监听搜索页 XHR 响应，找结构化商品数据。
2. DOM 选择器抽取可见卡片。
3. OCR 读取页面可见文本作为校验。

### 7.3 登录态检测

搜索前检查页面是否包含登录入口或用户昵称区域。

如果未登录：

```text
请在当前 Chrome 窗口完成淘宝登录，登录后按回车继续。
```

不要尝试自动处理扫码、短信验证码或风控验证。

## 8. 低成本策略

1. 同一关键词先抓搜索结果页前 1-2 页，不盲目深翻。
2. 每个品牌/系列先保留 Top N 候选，例如销量排序前 10。
3. 商品详情页只进入高置信度候选。
4. OCR 只截价格区域和 SKU 区域，不跑全屏。
5. VLM 只处理低置信度样本。
6. 每个平台保存中间 JSONL，失败可断点续跑。

## 9. 后续开发计划

### 阶段 1：淘宝搜索结果页 PoC

- 新建 ECAgent Python 项目骨架。
- 实现 `browser.py`，支持附着 `http://127.0.0.1:9222`。
- 实现淘宝搜索关键词。
- 抽取搜索结果页商品卡片。
- 保存 JSONL 和页面截图。

验收标准：

- 输入 `杜蕾斯 001` 后能稳定进入搜索结果页。
- 能抽取至少 10 条商品候选。
- 每条候选包含标题、价格、付款人数、店铺、链接。

### 阶段 2：详情页价格和片单

- 打开候选商品详情页。
- 抽取 SKU 和到手价。
- 调 OCR 服务校验可见价格。
- 解析规格片数。
- 计算片单。

验收标准：

- 输出 `final_price`、`piece_count`、`unit_price`。
- 保存详情页截图。
- 标记低置信度记录。

### 阶段 3：官方店铺和系列筛选

- 建立品牌店铺白名单。
- 支持店内搜索。
- 支持销量排序。
- 加入系列匹配和排除词。

验收标准：

- 能优先采集官旗/自营/旗舰店。
- 对“超薄”“玻尿酸”“情趣”等模糊系列能筛掉明显错误商品。

### 阶段 4：多平台扩展

- 京东适配器。
- 抖音适配器。
- 天猫独立适配器或淘宝/天猫统一适配器。

### 阶段 5：表格和复核

- 输出 CSV/XLSX。
- 输出截图目录。
- 输出低置信度复核清单。
- 可选：做一个简单 Web 页面查看采集结果。

## 10. 当前结论

淘宝自动搜索链路已经打通。下一步最应该做的是淘宝搜索结果页商品卡片抽取，而不是继续讨论完整 Agent。只要搜索结果页能稳定抽出商品标题、价格、付款人数、店铺和链接，后面的店铺筛选、详情页价格校验、OCR/VLM 兜底都可以逐步接上。

## 11. 2026-05-03 淘宝 MVP 实现进度

已新增淘宝 MVP 脚本：

- `eca_taobao.py`
- `src/browser.py`
- `src/taobao.py`
- `src/price.py`
- `src/exporter.py`
- `src/ocr_client.py`
- `config.example.yaml`
- `README.md`

已验证命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3.11 eca_taobao.py --max-candidates 6 --detail-limit 2 --out-dir exports\taobao_latest
```

验证结果：

- 能附着 `http://127.0.0.1:9222` 的 Chrome。
- 能搜索 `杜蕾斯 001`。
- 能从淘宝搜索结果页抽取 6 条候选。
- 能进入前 2 条详情页。
- 能保存搜索结果截图和详情页截图。
- 能输出 `taobao_results.json` 和 `taobao_results.csv`。
- 价格解析已修正，避免把 `001` 或 `1万+人收货` 误识别为价格。

当前输出目录：

- `exports/taobao_latest/search_results.png`
- `exports/taobao_latest/screenshots/`
- `exports/taobao_latest/taobao_results.json`
- `exports/taobao_latest/taobao_results.csv`

剩余问题：

- 淘宝搜索结果页 DOM 是动态结构，当前卡片抽取以 DOM 文本启发式为主，标题和店铺名偶尔仍会混在一起。
- 还没有监听 XHR 接口，后续应优先从接口 JSON 中抽商品字段。
- 详情页 SKU 和片数目前靠标题/正文正则解析，复杂规格需要进一步增强。
- OCR 服务接口已预留，但默认未启用。下一步接 `WXWorkAgent` 的 OCR 服务校验可见价格。
