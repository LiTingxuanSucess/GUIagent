# 淘宝价格追踪 MVP 开发记录

更新时间：2026-05-03

## 1. 背景

客户需求来自 `docs/产品价格追踪需求文档.pptx`，已提取到 `docs/REQ.md`。核心流程是：

1. 进入平台首页。
2. 按店铺搜索。
3. 选择品牌官方旗舰店、京东自营、抖音旗舰等目标店铺。
4. 进入品牌店铺后搜索系列名。
5. 按销量排行锁定重点系列代表产品。
6. 进入商品详情链接。
7. 记录主图产品、到手价格、截图。
8. 计算片单。
9. 最终以表格形式输出。

本阶段先以淘宝/天猫为例，把搜索、详情页抓取、数据导出和 Web 表格展示打通。

## 2. 已完成内容

### 2.1 浏览器接管

复用 `DouyinCSAgent` 的 Chrome CDP 方案，通过用户本机 Chrome 登录态执行采集。

启动方式：

```powershell
& "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
  --no-first-run `
  --no-default-browser-check `
  "https://www.taobao.com"
```

验证：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9222/json/version
```

### 2.2 淘宝采集脚本

新增入口：

- `eca_taobao.py`

新增模块：

- `src/browser.py`
- `src/taobao.py`
- `src/price.py`
- `src/exporter.py`
- `src/ocr_client.py`

当前采集命令：

```powershell
cd D:\Workspace\gui-agent\ECAgent
$env:PYTHONIOENCODING='utf-8'
py -3.11 eca_taobao.py --max-candidates 6 --detail-limit 6 --out-dir exports\taobao_latest
```

当前输出：

- `exports/taobao_latest/taobao_results.json`
- `exports/taobao_latest/taobao_results.csv`
- `exports/taobao_latest/search_results.png`
- `exports/taobao_latest/screenshots/*.png`

### 2.3 已抓取字段

当前 JSON/CSV 已包含：

- `platform`
- `brand`
- `series`
- `keyword`
- `rank_position`
- `rank_source`
- `title`
- `product_url`
- `shop_name`
- `shop_type`
- `official_store_flag`
- `price_raw`
- `final_price`
- `price_source`
- `ocr_price`
- `sales_text`
- `location`
- `main_image_url`
- `spec_text`
- `selected_sku`
- `sku_options`
- `piece_count`
- `unit_price`
- `series_match_reason`
- `detail_title`
- `screenshot_path`
- `confidence`
- `notes`
- `captured_at`

### 2.4 Web 平台

参考 `WXWorkAgent/web` 架构，新增：

- `web/backend/main.py`
- `web/frontend/src/App.vue`
- `web/frontend/src/style.css`
- `web/frontend/package.json`
- `web/frontend/vite.config.js`
- `web/README.md`

启动后端：

```powershell
cd D:\Workspace\gui-agent\ECAgent
py -3.11 -m uvicorn web.backend.main:app --host 127.0.0.1 --port 8010
```

启动前端：

```powershell
cd D:\Workspace\gui-agent\ECAgent\web\frontend
npm run dev
```

访问：

```text
http://127.0.0.1:5174
```

当前 Web 页面按客户视角展示：

- 按采集日期/批次管理。
- 表格形式展示抓取结果。
- 点击行展开详情。
- 展示商品链接、主图产品/SKU、规格信息、价格来源、排序来源、确认项、主图、详情截图。
- 支持 CSV 导出。

## 3. 当前验证结果

目标关键词：

```text
杜蕾斯 001
```

当前抓取 6 条候选，6 条均进入详情页。

字段覆盖情况：

```text
候选商品 6/6
商品链接 6/6
店铺信息 6/6
到手价 6/6
详情截图 6/6
主图 6/6
SKU/规格 6/6
片数 6/6
片单 6/6
```

样例：

```text
#1 durex杜蕾斯官方旗舰店
价格 74.8
SKU ①【⭐64%用户选择⭐】黑金三合一12只✖2
片数 24
片单 3.1167

#2 正琪大药房旗舰店
价格 51.9
SKU 超薄体验【20只】玻尿酸爆水6只+大胆爱吧6只+活力4只+凸点4只
片数 20
片单 2.595
标记：店铺待确认

#6 durex杜蕾斯官方旗舰店
价格 39.44
SKU 【杜杜全家福】凸点螺纹3+Q弹大颗粒2+持久3+001 2+超薄3+超薄玻尿酸3
片数 16
片单 2.465
标记：系列待核验
```

## 4. 遇到的问题和解决方式

### 4.1 普通 Chrome 无法直接被 Playwright 接管

问题：

用户一开始打开的是普通 Chrome，没有 remote debugging 端口，Playwright 无法附着。

解决：

按 `DouyinCSAgent/start_chrome.bat` 的方式，用默认 Chrome 用户数据目录启动 CDP Chrome：

```powershell
--remote-debugging-port=9222
--user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data"
```

这样既能复用登录态，又能通过 `connect_over_cdp` 接管。

### 4.2 PowerShell 中文编码导致关键词变成 `???`

问题：

PowerShell here-string 中的中文传给 Python 后变成 `???`，第一次搜索变成了 `??? 001`。

解决：

- 脚本内统一 `sys.stdout.reconfigure(encoding="utf-8")`。
- 运行时设置：

```powershell
$env:PYTHONIOENCODING='utf-8'
```

- 配置文件中尽量使用 UTF-8 文件读取，不在 PowerShell 命令中直接拼中文。
- 必要时用 Unicode 转义写默认值。

### 4.3 淘宝搜索页 DOM 不稳定

问题：

搜索结果页 DOM 动态生成，商品标题、店铺、价格、销量会混在同一个卡片文本中。早期出现过：

- 店铺名被商品标题污染。
- 价格把 `001` 误识别成 `1.0`。
- `1万+人收货` 中的 `1` 被误当成价格。

解决：

- 价格优先从 `¥/￥` 后面的金额抽取。
- 避免把 `001/003` 当成价格。
- 屏蔽 `万人付款/人收货` 这类销量数字。
- 店铺名优先从详情页标题区和店铺区二次覆盖。

### 4.4 片数解析不稳定

问题：

SKU 文案里有多种写法：

- `12只✖2`
- `【20只】`
- `共17只`
- `3+2+3+001 2+3+3`

早期会把 `12只✖2` 算成 12，或把 `【20只】...6+6+4+4` 算成 16。

解决：

在 `src/price.py` 中增加优先级：

1. `共 N 只`
2. `N只 × M`
3. `【N只】`
4. 加法组合
5. 普通 `N只`

当前样例已修正：

```text
12只✖2 => 24
【20只】 => 20
3+2+3+001 2+3+3 => 16
```

### 4.5 主图误抓成顶部横幅图

问题：

详情页图片很多，早期 `main_image_url` 会抓到顶部横幅，例如 `3456x864` 的活动图。

解决：

主图筛选增加约束：

- 只接受较接近正方形/竖图的图片。
- 排除 `-tps-`、`/tfs/` 等偏营销物料。
- 优先 `uploaded/imgextra` 的商品图。

### 4.6 官方店标记误判

问题：

`正琪大药房旗舰店` 包含“旗舰店”，但不是杜蕾斯官方店。

解决：

`official_store_flag` 不再只看“旗舰店”，而是要求：

- 自营直接通过。
- 旗舰店需包含品牌名，或包含官方/品牌旗舰相关文本。

当前 #2 会被标记为 `店铺待确认`。

### 4.7 前端一开始偏成内部研发面板

问题：

最初 Web 页面展示了“需求对照/还缺什么”等研发视角内容，不适合作为面向客户的系统。

解决：

重写前端为客户视角表格：

- 去掉需求对照。
- 去掉无意义统计卡片。
- 用日期/批次管理。
- 主视图改为高密度表格。
- 点击行展开商品详情、SKU、截图。

## 5. 当前仍存在的不足

### 5.1 销量排序未强确认

当前 `rank_source` 为：

```text
sales_sort_clicked_unconfirmed
```

含义是脚本已尝试点击“销量”，但还没有从 URL 参数或接口响应确认排序一定生效。

后续需要：

- 监听搜索页 XHR。
- 找到排序字段。
- 保存明确的 `rank_source = sales_api_confirmed` 或类似状态。

### 5.2 还不是严格的“按店铺搜索 -> 店内搜索”

当前流程仍然是关键词搜索结果页抽取，再识别店铺和官方标记。

客户需求要求：

```text
按店铺搜索 -> 选择品牌官方旗舰店 -> 店内搜索系列
```

后续需要：

- 配置品牌店铺白名单。
- 先搜店铺。
- 进入目标店铺。
- 店内搜索系列词。
- 再按销量抽代表产品。

### 5.3 系列筛选还只是规则标记

当前能标出疑似错系列，例如：

- 搜 `001` 出现 `AiR/003`
- 搜 `001` 出现情趣混合装

但还没有自动剔除或重排。

后续需要：

- 系列关键词表。
- 排除词表。
- 对模糊系列如“超薄/玻尿酸/情趣”增加判定规则。
- 低置信度样本可调用 LLM 做归类。

### 5.4 OCR 校验还未默认启用

已预留 `src/ocr_client.py`，可以调用 `WXWorkAgent` 的 OCR 服务，但当前默认未跑。

后续需要：

- 对详情页价格区截图。
- 调 OCR 校验可见价。
- 记录 `ocr_price`。
- 比较 `final_price` 与 `ocr_price`，不一致则标记复核。

### 5.5 数据存储还是 JSON 文件

当前以 `exports/*/taobao_results.json` 作为数据源，适合 MVP。

后续如果要长期追踪价格趋势，应升级为：

- SQLite 或 Postgres。
- `tasks`
- `runs`
- `products`
- `price_snapshots`
- `screenshots`

## 6. 后续改进方向

优先级建议：

### P0：搜索页 XHR 结构化抽取

目标：

- 稳定拿标题、店铺、价格、销量、主图、商品 ID。
- 确认销量排序是否生效。

原因：

DOM 文本解析可用但脆弱，接口 JSON 会更稳定。

### P1：店铺白名单和店内搜索

目标：

- 对每个品牌配置官方店铺。
- 进入官方店后店内搜索系列。
- 避免第三方药房、分销店混入。

### P2：SKU/规格解析增强

目标：

- 更准确识别当前选中 SKU。
- 对组合装、买赠、多盒装做规则化。
- 输出更可信的 `piece_count` 和 `unit_price`。

### P3：OCR 价格校验

目标：

- 复用 `WXWorkAgent` OCR 服务。
- 对价格区域局部截图做 OCR。
- 补 `ocr_price`、`price_source`、`price_confidence`。

### P4：系列智能筛选

目标：

- 自动剔除错系列。
- 对“超薄/玻尿酸/情趣”等模糊系列做更细筛选。
- 必要时引入 LLM/VLM 做低置信度样本判断。

### P5：趋势和多平台

目标：

- 按日期追踪价格变化。
- 接入京东、抖音。
- 支持跨平台同品牌同系列对比。

## 7. 运行入口汇总

启动 Chrome：

```powershell
& "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
  --no-first-run `
  --no-default-browser-check `
  "https://www.taobao.com"
```

运行采集：

```powershell
cd D:\Workspace\gui-agent\ECAgent
$env:PYTHONIOENCODING='utf-8'
py -3.11 eca_taobao.py --max-candidates 6 --detail-limit 6 --out-dir exports\taobao_latest
```

启动后端：

```powershell
cd D:\Workspace\gui-agent\ECAgent
py -3.11 -m uvicorn web.backend.main:app --host 127.0.0.1 --port 8010
```

启动前端：

```powershell
cd D:\Workspace\gui-agent\ECAgent\web\frontend
npm run dev
```

访问：

```text
http://127.0.0.1:5174
```
