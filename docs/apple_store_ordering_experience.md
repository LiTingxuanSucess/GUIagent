# Apple Store ?????????

?????2026-05-24

## ??

- ?? Apple Store iPhone 17 Pro Max ????
- ?? SKU ???
- ?? Add to Bag ????????
- ?? checkout ?????????????????????

## ??????

- ???? Cloak Chromium?
- CDP?`http://127.0.0.1:9223`
- Profile?`C:\workspace\GUIAgent\.cloak-profiles\apple-visible-cdp`

## SKU URL ??

- ??????`https://www.apple.com/shop/buy-iphone/iphone-17-pro`
- Pro Max ?????`/6.9-inch-display-256gb-silver-unlocked`
- URL ???`/{display}-{storage}-{color}-{carrier}`
- `unlocked` ?? `Connect to any carrier later`???????????? URL ? 404????? `t-mobile` / `boost-mobile` ???

## Add to Bag ????

### ????

- ??????? `Add to Bag` ?????????
- ?? fallback GET ?????????? `product=MFXG4LL/A&purchaseOption=fullPrice&cppart=UNLOCKED/US&step=select&acpart=none&bfil=2&add-to-cart=add-to-cart`?
- Apple ?? `541`??????? 404?

### ?? 1?AppleCare ??

- ??? `Add to Bag` ??? AppleCare ????? `disabled=true`?
- ???? AppleCare???????? `No AppleCare coverage`?
- ???????????`acpart=none`?

### ?? 2??????? HTML form

- ?? form ? action ? `#`?method ? GET?
- ????/?? form ?? fallback?????????? API?
- ?? fetch `/shop/bag/add?product=MFXG4LL/A` ????`CSRF_ERROR`?

### ????

- ?????????? Apple Store ???`window.acStore.addItem(partNumber)`?
- ?????? localStorage ?? `ac-store-cache`??? `/us/shop/bag/add?product=MFXG4LL%2FA&apikey=...&atbtoken=...`?
- ???????`window.acStore.addItem("MFXG4LL/A")`?
- ??? Bag ?? `iPhone 17 Pro Max 256GB Silver`?

### ??

- ????????? 3 ??Bag ???? 3 ????????????? Quantity ?? 1?
- ?? Bag ???1 ??Subtotal `$1,199.00`?

## Checkout ??

- Checkout ?????button index 0
- ??????????
- ?????????????????????? Place Order?

## ????

- `docs/apple_iphone17_pro_max_order_options.md`?SKU ???????
- `docs/apple_checkout_fill_guide.md`?checkout ?????
- `docs/apple_add_to_bag_success_probe.json`?Add to Bag ????????
- `docs/apple_checkout_after_bag_success.json`?checkout ?????

## 2026-05-24 Continue Order Notes

- Read updated `C:\workspace\apple_info.txt` locally. Sensitive values were not written into this document.
- Reached Shipping page: `https://secure8.store.apple.com/shop/checkout?_s=Shipping-init`.
- Filled shipping/contact fields:
  - Recipient name.
  - Street address.
  - ZIP `97089`.
  - Email.
  - Phone, using the last 10 digits from the phone-like line in the info file.
- ZIP lookup initially showed a mismatched city/state option. Resolution:
  - Switched city/state selector to `Other`.
  - Manually filled `Damascus` and `OR`.
  - Continued successfully to Payment.
- Reached Payment page: `https://secure8.store.apple.com/shop/checkout?_s=Billing-init`.
- Selected `Credit or Debit Card`.
- Filled card number, expiration, and CVV from the local info file.
- Apple validation stopped the flow with: `Please enter a valid card number.`
- Stopped on Payment page. No order was submitted and no `Place Order` action was clicked.

## Current Blocker

Apple rejects the card number in `apple_info.txt` as invalid. Next step is to correct the card details in the file or use Apple Pay / PayPal / another card in the visible browser window.

## 2026-05-24 APAgent Verification Notes

Scope:

- Built APAgent web console for SKU profiles, buyer profiles, and order tasks.
- Task progress uses HTTP polling. No WebSocket.
- Task and progress should be one page: task table on the left, selected task progress/logs on the right.
- SKU options must be manually editable by operators. Do not hard-code every option as a closed select.

UI lessons:

- Customer-facing UI must not show internal runtime terms such as `CDP`, `Cloak`, or browser profile paths.
- Classic admin CRUD layout is expected:
  - table list page
  - `Create` button in page header
  - modal form for create/edit
  - row actions for edit/delete/run
- For SKU option maintenance, use editable inputs with suggestions plus an `option_config` JSON field. This allows operators to revise model/storage/color/carrier mappings after Apple changes the page.

Backend lessons:

- Add real CRUD endpoints, not only create/list:
  - `PUT /api/skus/{id}`
  - `DELETE /api/skus/{id}`
  - `PUT /api/identities/{id}`
  - `DELETE /api/identities/{id}`
  - `PUT /api/tasks/{id}`
  - `DELETE /api/tasks/{id}`
- Identity update should not expose or require existing card/CVV values. If `card_number` or `cvv` is empty during update, keep the old value.
- SQLite migration added `skus.option_config TEXT NOT NULL DEFAULT ''`. Existing rows should be backfilled with the default option JSON.
- Importing phone numbers from `apple_info.txt` must handle `+86` style numbers by taking the last 10 US-style digits when needed.
- ZIP parsing should prefer the ZIP after a state code, or the last ZIP-like token. Otherwise a 5-digit street number can be mistaken for ZIP.

Apple checkout automation lessons:

- `window.acStore.addItem(partNumber)` remains the reliable Add to Bag path. Direct form submit or direct bag API fetch can fail with CSRF/404 style errors.
- AppleCare still needs explicit handling. Select `No AppleCare coverage` when no AppleCare is desired.
- Bag quantity can accumulate across retries. Always normalize quantity to 1 after opening the bag.
- `Continue with other payment options` can appear as a link or text-like control, and may not be immediately visible after ZIP/delivery calculation.
  - Wait for text visibility first.
  - Try role `button`, role `link`, then text fallback.
- Delivery ZIP input may exist in the DOM but be hidden when the ZIP is already applied.
  - Check `is_visible()` before `fill()`.
  - If hidden, skip ZIP fill and continue.
- Shipping form may load slower than 15 seconds.
  - Wait explicitly for `#checkout.shipping.addressSelector.newAddress.address.firstName`.
  - Use a longer timeout, currently 45 seconds.
- The `Continue with other payment options` click can navigate from:
  - `https://secure8.store.apple.com/shop/apw/checkout?_s=Fulfillment-init`
  - to `https://secure8.store.apple.com/shop/checkout?_s=Shipping-init`
- Current validated path reached:
  - connect browser
  - open product
  - select AppleCare
  - add to bag
  - open bag
  - checkout
  - fulfillment/delivery
  - shipping/contact
  - payment
  - continue to review
- Final observed blocker: Apple rejected the provided card number as invalid. The automation stopped before final order submission.

Artifacts from this APAgent verification:

- `APAgent/web/frontend/apagent-task-flow-verified.png`
- `APAgent/web/frontend/apple-checkout-stage.txt`
- `APAgent/web/frontend/apple-checkout-stage-2.txt`
- `APAgent/web/frontend/apple-shipping-controls-2.json`

## 2026-05-24 Gift Card Checkout Notes

Goal:

- Test guest checkout with an Apple gift card from `APAgent/gift_card.txt`.
- Do not print gift card number/PIN into chat or logs.
- Try to reach the final review step without placing the order.

Observed flow:

- Apple checkout entered `Sign In - Secure Checkout`.
- `Continue as Guest` is available via:
  - button id: `signIn.guestLogin.guestLogin`
  - `data-autom`: `guest-checkout-btn`
- Guest checkout proceeds to fulfillment and shipping normally.
- Guest payment page reached:
  - `https://secure8.store.apple.com/shop/checkout?_s=Billing-init`

Payment page observations:

- Guest payment page shows:
  - `In order to use your Apple Account Balance, Sign In with your Apple Account.`
  - `Credit or Debit Card`
  - `Apple Pay`
- Selecting `Credit or Debit Card` expands only card fields:
  - card number
  - expiration
  - CVV
  - split payment with second card
  - billing address
- No visible or hidden guest checkout fields were found for raw Apple gift card number/PIN.
- Searching the body text and controls for `gift`, `redeem`, `pin`, and `apply` did not reveal a gift-card entry form.

Important conclusion:

- In this checkout flow, Apple gift card payment appears to be handled through `Apple Account Balance`, not through a guest raw gift-card form.
- Clicking `Apple Account Balance` / sign-in balance path navigates back to the Apple sign-in / guest choice page.
- Therefore `gift_card.txt` cannot be applied directly in guest checkout in the currently observed Apple flow.

Operational implication:

- If the business wants to pay with Apple gift cards, the card likely needs to be redeemed into an Apple Account first, then checkout must use that signed-in Apple Account Balance.
- Guest checkout can continue with credit/debit card or Apple Pay, but not direct gift-card code entry.

Artifacts:

- `APAgent/gift_guest_checkout_page.png`
- `APAgent/gift_payment_page.png`
- `APAgent/gift_after_credit_select.png`
- `APAgent/gift_balance_signin_page.png`
- `APAgent/gift_payment_controls.json`
- `APAgent/gift_after_credit_select_controls.json`
## HomePod mini 礼品卡支付验证

本次用 HomePod mini White 验证了低金额商品的礼品卡支付入口。

- 商品页：`https://www.apple.com/shop/buy-homepod/homepod-mini/white`
- Part Number：`MY5H2LL/A`
- 商品价格：`$99.00`
- 礼品卡入口文案：`Apply an Apple Gift Card:`

关键控件：

- 展开礼品卡输入：`checkout.billing.billingOptions.selectedBillingOptions.giftCard.giftCardInput.resetFields`
- 礼品卡输入框：`checkout.billing.billingOptions.selectedBillingOptions.giftCard.giftCardInput.giftCard`
- 应用礼品卡按钮：`checkout.billing.billingOptions.selectedBillingOptions.giftCard.giftCardInput.applyGiftCard`

验证结果：

- 第一张礼品卡可以成功应用。
- 页面显示礼品卡余额 `$8.00`。
- 因订单剩余 `$91.00`，页面提示需要另一种支付方式补齐。
- 当前 APAgent 只支持“信用卡”或“礼品卡”二选一，不支持组合支付，所以检测到剩余余额后任务直接标记失败。

实现经验：

- 高金额 iPhone 订单的 guest checkout 支付页不一定直接展示礼品卡输入。
- 低金额 HomePod mini 订单可以稳定出现礼品卡入口，适合作为礼品卡流程回归样例。
- 礼品卡号不要写入日志、接口响应或前端展示，只从本地受控文件读取。
- 自动化流程仍停在最终提交前，不点击最终提交订单按钮。

## 单任务耗时优化记录

本次把固定等待改成了事件等待，并给每个阶段补充耗时日志。HomePod mini + 礼品卡余额不足路径的最新验证结果如下：

- 连接浏览器：约 `1.2s`
- 打开商品页：约 `1.7s`
- 加入购物袋：约 `0.4s`
- 购物袋归一化数量：约 `2.6s`
- 进入 checkout：约 `4.6s`
- 配送/ZIP：约 `4.7s`
- 收货联系人表单：约 `12.4s`
- 礼品卡应用并判断余额不足：约 `9s`
- 总耗时：约 `37s`

优化结论：

- 固定 sleep 去掉后，商品页、加购、购物袋和 checkout 前半段明显变快。
- 当前主要瓶颈是收货表单联动和支付校验，不是 Python 执行速度。
- 礼品卡输入区必须先 `scroll_into_view_if_needed`，再点击展开，否则按钮可见但页面不一定展开输入框。
- 购物袋数量控件在 Apple 页面里可能是 `select`，不是 `input`；归一化数量时需要同时兼容两种控件。
- 连续测试会让购物袋累计同款商品，执行任务时必须把数量归一化为 `1`，否则订单金额会不断变大。
- Playwright 异常可能带出输入值，任务日志和错误字段必须统一做卡号/礼品卡号脱敏。

下一步可优化点：

- 收货地址如果能复用账号地址簿，可省掉 8-12 秒。
- checkout 可预热到配送页，再开始任务计时。
- 礼品卡流程如果只用于余额检测，可以在支付页做更短的状态判断，不必继续 review。
