# Apple iPhone 17 Pro Max 下单选项记录

记录时间：2026-05-24

## 目标商品

- 商品：iPhone 17 Pro Max
- Apple 购买页：https://www.apple.com/shop/buy-iphone/iphone-17-pro
- 示例直达配置：`https://www.apple.com/shop/buy-iphone/iphone-17-pro/6.9-inch-display-256gb-silver-unlocked`

## URL 设置方式

Apple 当前购买页可以通过 URL path 直接设置基础 SKU：

```text
https://www.apple.com/shop/buy-iphone/iphone-17-pro/{display}-{storage}-{color}-{carrier}
```

字段取值：

- `display`
  - `6.9-inch-display` = iPhone 17 Pro Max
  - `6.3-inch-display` = iPhone 17 Pro
- `storage`
  - `256gb`
  - `512gb`
  - `1tb`
  - `2tb`
- `color`
  - `silver`
  - `deep-blue`
  - `cosmic-orange`
- `carrier`
  - `unlocked` = Connect on your own later
  - `att`
  - `boost-mobile`
  - `t-mobile`
  - `verizon`

示例：

```text
6.9-inch-display-2tb-cosmic-orange-unlocked
6.9-inch-display-512gb-deep-blue-t-mobile
6.9-inch-display-256gb-silver-att
```

## iPhone 17 Pro Max 基础选项

### 颜色

- Silver
- Deep Blue
- Cosmic Orange

### 容量和裸机价

以下为选择 `Connect on your own later` 时 Apple 页面展示的价格：

- 256 GB：$1,199.00
- 512 GB：$1,399.00
- 1 TB：$1,599.00
- 2 TB：$1,999.00

### 运营商连接方式

- AT&T
- Boost Mobile
- T-Mobile
- Verizon
- Connect on your own later

说明：

- 选择运营商可能涉及连接折扣、分期、运营商促销或激活流程。
- 选择 `Connect on your own later` 是最少个人信息的路径，设备到手后自行激活。
- 美国和波多黎各销售的 iPhone 17 Pro 系列无实体 SIM 卡槽，只支持 eSIM。

## 后续下单流程选项

这些选项通常不完全体现在基础 SKU URL 中，需要在页面继续点选：

- Trade In：是否以旧换新
- Payment：一次性付款、Apple Card Monthly Installments、Apple iPhone Payments、iPhone Upgrade Program 等
- AppleCare：不购买、AppleCare+、AppleCare+ with Theft and Loss、AppleCare One
- Fulfillment：配送或门店自取
- Accessories：配件推荐，可跳过

## 本次页面读取结果

已用 CloakBrowser 读取官方 Apple Store 配置页：

- 页面标题：`Buy iPhone 17 Pro Max 256GB Silver - Apple`
- 当前 SKU：`iPhone 17 Pro Max 256GB Silver`
- 当前价格：`$1,199.00`
- 当前付款：`One-time payment`
- 发货提示：`Ships: 1 business day`
- 配送：`Free Shipping`，页面提供 `Get delivery dates`
- 自取：页面提供 `Check availability`
- 购物按钮：页面存在 `Add to Bag`

页面明确展示的配置项：

- Model
  - iPhone 17 Pro，6.3-inch display，From $1099
  - iPhone 17 Pro Max，6.9-inch display，From $1199
- Finish
  - Silver
  - Cosmic Orange
  - Deep Blue
- Storage
  - 256GB，From $1199 / $49.95/mo. for 24 mo.
  - 512GB，From $1399 / $58.29/mo. for 24 mo.
  - 1TB，From $1599 / $66.62/mo. for 24 mo.
  - 2TB，From $1999 / $83.29/mo. for 24 mo.
- Apple Trade In
  - Add a trade-in
  - No trade-in
  - 页面提示可获得 `$35-$685` 折抵，实际估值需要你填写旧设备信息
- Payment options
  - Buy：`$1,199.00`
  - Finance：`$49.95/mo. for 24 mo.`
  - Carrier deal finance：From `$10.25/mo. over 36 mo.` with select carrier deal
  - Apple iPhone Upgrade Program：`$61.16/mo. for 24 mo.`
- Connectivity
  - AT&T
  - Boost Mobile
  - T-Mobile
  - Verizon
  - Connect to any carrier later
- AppleCare coverage
  - AppleCare+ with Theft and Loss：`$13.99/mo.` or `$139.99/yr.`
  - AppleCare One：`$19.99/mo.`
  - No AppleCare coverage

本次自动化只读取并尝试触发 `Add to Bag`。页面没有进入最终 checkout；未填写任何个人信息、支付信息或运营商信息。

## 建议默认配置

为了减少需要你输入的敏感信息，建议先用：

```text
https://www.apple.com/shop/buy-iphone/iphone-17-pro/6.9-inch-display-256gb-silver-unlocked
```

配置含义：

- Model：iPhone 17 Pro Max
- Display：6.9 inch
- Storage：256 GB
- Color：Silver
- Carrier：Connect on your own later / unlocked
- Price：$1,199.00 before tax

## 必须你本人填写或确认的内容

我不能替你填写或提交这些信息：

- Apple Account 登录、双重验证
- 收货人姓名、电话、邮箱
- 配送地址或门店自取人信息
- 支付卡、Apple Pay、账单地址
- 运营商账号、手机号、PIN、SSN/信用审核相关信息
- Trade-in 设备序列号、IMEI、设备状态确认
- 最终 Place Order / Buy / Submit 按钮

## 自动化注意事项

- 可以用 URL path 直接定位基础 SKU。
- 在 Apple 页面上，后续选项建议用按钮文本/aria-label 选择，不要依赖 DOM 顺序。
- 到购物车和 checkout 后停止自动化，等待人工输入个人、支付和最终确认信息。
