# APAgent SKU、价格采集与下单路径说明

## 目标

SKU 价格采集不只是为了展示价格，还要沉淀后续自动化下单需要的“选择路径”。每条价格记录需要回答三个问题：

- 这个 SKU 是哪些选项组合出来的。
- 价格是从哪个页面、哪个 URL、哪个配置状态读取的。
- 后续自动下单时，应该按什么路径选择同一个商品配置。

## 数据表

数据库位置：

```powershell
C:\workspace\GUIAgent\APAgent\data\apagent.db
```

相关表：

- `sku_price_runs`：一次采集批次，记录平台、来源、开始/结束时间、总数、成功数、失败数。
- `sku_price_records`：每个 SKU 配置的一条价格记录，记录机型、容量、颜色、连接方式、URL、价格、采集时间和错误信息。
- `sku_price_records.raw_json`：扩展信息字段，用来记录读取价格时的页面信息、选项路径和后续下单建议。
- `skus`：可下单 SKU。下单任务使用这里的 `part_number`、`option_config`、`model_path`、`storage`、`color`、`carrier` 等字段。

## 采集脚本

脚本位置：

```powershell
C:\workspace\GUIAgent\APAgent\scripts\capture_apple_sku_prices.py
C:\workspace\GUIAgent\APAgent\scripts\capture_apple_catalog_sku_prices.py
```

### 单产品矩阵（已有）

`capture_apple_sku_prices.py` 负责 iPhone 17 Pro 系列等已建模产品：

```powershell
cd C:\workspace\GUIAgent\APAgent
py -3.11 scripts\capture_apple_sku_prices.py --concurrency 3
```

### 全目录采集（推荐）

`capture_apple_catalog_sku_prices.py` 读取 `APPLE_PRODUCT_CATALOG.json`，按 `kind` 自动分工：

- `fixed_product`：直接读取 `/shop/product/{part}/a` 固定商品页价格。
- `configurable`：进入配置页递归枚举可见选项组合，写入完整 SKU 矩阵。

```powershell
cd C:\workspace\GUIAgent
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --concurrency 4
```

常用参数：

```powershell
# 只采固定商品页
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --kind fixed_product --concurrency 4

# 只采可配置主产品矩阵
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --kind configurable --concurrency 2 --max-combos-per-product 5000

# 按 family 或 path 过滤
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --family iPad --path-contains ipad-air

# 仅导出 docs 索引
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --export-only

# 只补采历史覆盖率不足的可配置产品（例如已有 ok 数 < 50 的入口）
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --kind configurable --concurrency 1 --max-combos-per-product 5000 --recapture-if-ok-below 50

# 重试某次 fixed 批次失败项
py -3.11 APAgent\scripts\capture_apple_catalog_sku_prices.py --kind fixed_product --retry-failed-from-run 9 --concurrency 1
```

并发建议：固定商品页 `4`；可配置矩阵 `1-2`（每个产品会打开大量页面）。

## Apple 产品入口目录发现

在读取每个 SKU 的配置和价格之前，先跑产品入口目录发现脚本：

```powershell
cd C:\workspace\GUIAgent
py -3.11 APAgent\scripts\discover_apple_product_catalog.py --prefix APPLE_PRODUCT_CATALOG
```

输出文件：

- `C:\workspace\GUIAgent\docs\APPLE_PRODUCT_CATALOG.csv`
- `C:\workspace\GUIAgent\docs\APPLE_PRODUCT_CATALOG.json`
- `C:\workspace\GUIAgent\docs\APPLE_PRODUCT_CATALOG.md`

当前发现结果：

- 总入口：`424`
- 可配置主产品入口：`38`
- 固定商品页：`386`

目录字段：

- `name`：页面展示商品名。
- `family`：粗分类，例如 `iPhone`、`iPad`、`Mac`、`Apple Watch`、`Audio`。
- `kind`：`configurable` 或 `fixed_product`。
- `part_number`：固定商品页可从 `/shop/product/{part}/a` 解析出来。
- `selection_strategy`：后续采集策略。
- `url`：采集入口。
- `source`：从哪个 Apple 页面发现。

两类入口的处理方式不同：

- `fixed_product`：一般已经是单个可购买 SKU，优先按 `part_number + product page` 直接读取价格、库存和加购路径。
- `configurable`：只是产品族入口，必须继续展开页面里的尺寸、颜色、容量、连接方式、AppleCare、配件等配置矩阵，再写入 `sku_price_records`。

注意：这个目录是基于当前 Apple 美国官网可见入口抓取出来的，不保证包含隐藏商品、地区限定商品、临时下架商品或 Apple 接口里未展示的内部 SKU。后续每次正式采集前建议重新跑一遍目录发现。

## iPhone 17 Pro 遍历规则

当前矩阵：

- iPhone 17 Pro：3 个容量 × 3 个颜色 × 5 个连接方式 = 45 条。
- iPhone 17 Pro Max：4 个容量 × 3 个颜色 × 5 个连接方式 = 60 条。
- 合计 105 条。

URL 拼接规则：

```text
https://www.apple.com/shop/buy-iphone/iphone-17-pro/{model_path}-{storage}-{color}-{carrier}
```

示例：

```text
https://www.apple.com/shop/buy-iphone/iphone-17-pro/6.9-inch-display-256gb-silver-unlocked
```

字段含义：

- `model_path`：`6.3-inch-display` 或 `6.9-inch-display`。
- `storage`：`256gb`、`512gb`、`1tb`、`2tb`。
- `color`：`silver`、`cosmic-orange`、`deep-blue`。
- `carrier`：`unlocked`、`att`、`boost-mobile`、`t-mobile`、`verizon`。

## 读取价格时必须记录什么

每条 `sku_price_records` 除了价格本身，还要在 `raw_json` 里记录这些内容：

```json
{
  "title": "Buy iPhone 17 Pro - Apple",
  "url": "实际打开后的页面 URL",
  "price_text": "$1,199.00",
  "selection_strategy": "url_path",
  "product_url_pattern": "https://www.apple.com/shop/buy-iphone/iphone-17-pro/{model_path}-{storage_path}-{color_path}-{carrier_path}",
  "selection_path": [],
  "order_task_defaults": {}
}
```

字段说明：

- `title`：页面标题，方便排查是否打开了正确商品。
- `url`：最终读取价格的页面 URL。
- `price_text`：页面读取到的价格文本。
- `selection_strategy`：选择策略。iPhone 当前为 `url_path`，表示配置可以通过 URL 路径表达。
- `product_url_pattern`：后续下单可复用的 URL 模板。
- `selection_path`：人工页面路径和自动化选择路径。
- `order_task_defaults`：创建下单任务时可默认使用的配置。

## iPhone 下单选择路径

iPhone 17 Pro 系列当前推荐优先走 URL 路径，不逐个点击商品页选项。

推荐路径：

1. 用 `product_url_pattern` 拼出完整商品 URL。
2. 打开商品页。
3. 等待商品页出现 `Add to Bag` 或 AppleCare 区块。
4. 选择 `No AppleCare coverage`。
5. 通过 `part_number` 或页面 `Add to Bag` 加入购物袋。
6. 打开购物袋，将数量归一化为 `1`。
7. 进入 checkout。
8. 填写配送、收货和支付信息。
9. 停在最终提交前。

`selection_path` 建议记录为：

```json
[
  {
    "step": "model",
    "label": "iPhone 17 Pro Max",
    "value": "6.9-inch-display",
    "url_segment": "6.9-inch-display",
    "page_section": "Model. Which is best for you?"
  },
  {
    "step": "finish",
    "label": "Silver",
    "value": "silver",
    "url_segment": "silver",
    "page_section": "Finish. Pick your favorite."
  },
  {
    "step": "storage",
    "label": "256GB",
    "value": "256gb",
    "url_segment": "256gb",
    "page_section": "Storage. How much space do you need?"
  },
  {
    "step": "payment_option",
    "label": "Buy",
    "value": "fullPrice",
    "page_section": "Payment options. Select the one that works for you."
  },
  {
    "step": "carrier",
    "label": "Connect to any carrier later",
    "value": "unlocked",
    "url_segment": "unlocked",
    "page_section": "Connectivity. Choose a carrier."
  },
  {
    "step": "applecare",
    "label": "No AppleCare coverage",
    "value": "none",
    "page_section": "AppleCare coverage. Peace of mind in every plan."
  }
]
```

注意：页面展示顺序可能是 Model、Finish、Storage、Trade In、Payment options、Connectivity、AppleCare。URL 路径里当前顺序是 `model-storage-color-carrier`，不要把页面展示顺序误当成 URL 拼接顺序。

## HomePod mini 选择路径

HomePod mini 不走 iPhone 的容量、运营商路径。它更适合使用固定商品 URL。

已验证 SKU：

- 商品页：`https://www.apple.com/shop/buy-homepod/homepod-mini/white`
- Part Number：`MY5H2LL/A`
- 价格：`$99.00`
- 下单 SKU 名称：`HomePod mini White`

`option_config` 示例：

```json
{
  "product_family": "HomePod",
  "product_url": "https://www.apple.com/shop/buy-homepod/homepod-mini/white",
  "colors": ["white", "midnight", "orange", "yellow", "blue"]
}
```

HomePod mini 下单路径：

1. 打开 `product_url`。
2. 等待页面出现 `Add to Bag`、`HomePod mini` 或价格。
3. 使用 `part_number` 加购，失败时点击页面 `Add to Bag`。
4. 打开购物袋，将数量归一化为 `1`。
5. 进入 checkout。
6. 填写配送和收货信息。
7. 按任务支付方式选择信用卡或礼品卡。
8. 停在最终提交前；如果礼品卡余额不足，任务直接失败。

## iPad Air 选择路径验证

本次用 iPad Air 做了一个样例验证，确认文档里的 `selection_path` 模型可以覆盖非 iPhone 产品。

样例配置：

- 商品页：`https://www.apple.com/shop/buy-ipad/ipad-air`
- 型号：`iPad Air 11-inch`
- 尺寸：`11inch`
- 颜色：`space_gray`
- 容量：`128gb`
- 连接方式：`wifi`
- 价格：`$599.00`
- 价格记录批次：`sku_price_runs.id = 3`
- 价格记录路径：`ipad-air-11inch-128gb-space_gray-wifi`

验证结论：

- iPad Air 页面可以读取到完整选项集合。
- iPad Air 的 URL 不像 iPhone 那样稳定保留完整配置路径。尝试打开 `https://www.apple.com/shop/buy-ipad/ipad-air/11-inch-128gb-space-gray-wifi` 会回到基础商品页。
- 因此 iPad Air 的 `selection_strategy` 应记录为 `page_click`，自动化下单时按页面控件点击，而不是只依赖 URL 拼接。
- 当前样例已写入 `sku_price_records.raw_json`，里面包含 `selection_path`、`observed_options` 和 `order_task_defaults`。
- 由于暂未确认 iPad Air 的 `part_number`，本次只写价格记录，不写入 `skus` 下单表；等拿到 `part_number` 后再升级成可执行下单 SKU。

iPad Air 样例 `selection_path`：

```json
[
  {
    "step": "model",
    "label": "11-inch model",
    "value": "11inch",
    "selector": "input[name=\"dimensionScreensize\"][value=\"11inch\"]",
    "data_autom": "dimensionScreensize11inch",
    "page_section": "Model. Choose your size."
  },
  {
    "step": "finish",
    "label": "Space Gray",
    "value": "space_gray",
    "selector": "input[name=\"dimensionColor\"][value=\"space_gray\"]",
    "data_autom": "dimensionColorspace_gray",
    "page_section": "Finish. Pick your favorite."
  },
  {
    "step": "storage",
    "label": "128GB",
    "value": "128gb",
    "selector": "input[name=\"dimensionCapacity\"][value=\"128gb\"]",
    "data_autom": "dimensionCapacity128gb",
    "page_section": "Storage. Choose how much space you’ll need."
  },
  {
    "step": "connection",
    "label": "Wi-Fi",
    "value": "wifi",
    "selector": "input[name=\"dimensionConnection\"][value=\"wifi\"]",
    "data_autom": "dimensionConnectionwifi",
    "page_section": "Connectivity. Choose how you’ll stay connected."
  },
  {
    "step": "engraving",
    "label": "No engraving",
    "value": "noEngraving",
    "selector": "input[name=\"engraving-option-select-\"][value=\"noEngraving\"]",
    "data_autom": "noEngraving-",
    "page_section": "Personalize it. Add engraving."
  },
  {
    "step": "trade_in",
    "label": "No trade-in",
    "value": "noTradeIn",
    "selector": "#noTradeIn",
    "data_autom": "choose-noTradeIn",
    "page_section": "Apple Trade In"
  },
  {
    "step": "payment_option",
    "label": "Buy",
    "value": "fullprice",
    "selector": "input[name=\"purchase_option_group\"][value=\"fullprice\"]",
    "data_autom": "purchaseGroupOptionfullprice",
    "page_section": "Payment options"
  },
  {
    "step": "applecare",
    "label": "No AppleCare coverage",
    "value": "none",
    "selector": "input[name=\"applecare-options\"][data-autom=\"noapplecare\"]",
    "data_autom": "noapplecare",
    "page_section": "AppleCare coverage"
  }
]
```

iPad Air 当前观察到的选项集合：

```json
{
  "sizes": ["11inch", "13inch"],
  "colors": ["space_gray", "blue", "purple", "starlight"],
  "storages": ["128gb", "256gb", "512gb", "1tb"],
  "connections": ["wifi", "wificell"]
}
```

## 价格读取流程

脚本当前流程：

1. 生成 SKU 矩阵。
2. 每个配置打开一次 Apple 商品页。
3. 等待页面主体加载。
4. 从页面正文提取 `Buy` 或 `One-time payment` 附近的价格。
5. 将价格文本转成分值，例如 `$1,199.00` -> `119900`。
6. 记录页面标题、实际 URL、价格文本、选择路径和下单默认值到 `raw_json`。
7. 写入 `sku_price_records`。
8. 如果某些配置读取失败，用同机型同容量的规范配置补齐价格，并在 `raw_json.selection_strategy` 标记为 `canonical_price_fill`。

价格读取成功记录要包含：

- `platform`
- `product_family`
- `model`
- `model_path`
- `display`
- `storage`
- `color`
- `carrier`
- `path`
- `url`
- `price_text`
- `price_cents`
- `captured_at`
- `raw_json.selection_path`

失败记录要包含：

- 原始目标 URL。
- 失败原因。
- 采集时间。
- 不要写入敏感信息。

## 已验证价格摘要

最近一次完整 iPhone 批次采集结果：

- iPhone 17 Pro 256GB：`$1,099.00`
- iPhone 17 Pro 512GB：`$1,299.00`
- iPhone 17 Pro 1TB：`$1,499.00`
- iPhone 17 Pro Max 256GB：`$1,199.00`
- iPhone 17 Pro Max 512GB：`$1,399.00`
- iPhone 17 Pro Max 1TB：`$1,599.00`
- iPhone 17 Pro Max 2TB：`$1,999.00`

## 新增商品时怎么做

1. 先跑 `discover_apple_product_catalog.py`，拿到当前 Apple 产品入口目录。
2. 按 `kind` 分工：`fixed_product` 直接读取固定商品页，`configurable` 继续做配置矩阵。
3. 确认商品 URL 结构，是 iPhone 这种可拼接路径，还是 iPad/Mac/Watch 这种页面点击路径。
4. 枚举所有可选项，形成矩阵字段。
5. 记录每个选项的页面文案、业务值、URL 片段或 selector、页面区块。
6. 如果能通过 URL 表达配置，优先用 URL 遍历。
7. 如果 URL 不能稳定表达配置，再用页面点击方式读取配置和价格。
8. 价格采集时同步写入 `selection_path`，不要等到做下单自动化时再补。
9. 每次采集都新建 `sku_price_runs`，不要覆盖历史价格。
10. 每条配置写入 `sku_price_records`，价格用文本和分值同时保存。

## 后台查看

启动前后端后，在 APAgent 后台打开“SKU 价格”页面即可查看最近一次价格记录。

当前导出的完整列表：

- Markdown 摘要：`C:\workspace\GUIAgent\docs\APAGENT_CURRENT_SKU_LIST.md`
- CSV 明细：`C:\workspace\GUIAgent\docs\APAGENT_ALL_SKU_PRICE_RECORDS.csv`
