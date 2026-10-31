# 企业微信朋友圈自动发布 —— 踩坑记录

脚本：`wxwork_pengyouquan.py`  
目标：CDP 连接企业微信管理后台，全自动完成「新建内容→填文案→上传图片→选可见客户→通知成员发表」全流程。

---

## 坑 1：SPA hash 导航不清除弹窗蒙层

**现象**：每次运行结束后，页面 JS 状态里残留 `.js_dialog_mask` 蒙层，下次 `page.goto()` 同 URL 时 SPA 不刷新 DOM，蒙层仍在，导致「新建内容」点击被拦截（`intercepts pointer events`）。

**解决**：`goto()` 之后用 JS 强制移除残留元素：
```python
await page.evaluate("""() => {
    document.querySelectorAll('.js_dialog_mask, .ww_dialog').forEach(e => e.remove());
}""")
```

> `.js_dialog_mask` 在页面里可能有多个实例，Playwright strict mode 下 `locator.is_visible()` 会报错，必须加 `.first`。

---

## 坑 2：`input[name=uploadImageFile]` 被视频 input 遮挡

**现象**：`uploadImageFile` 和 `uploadVideoFile` 两个 `input[type=file]` 叠放在同一位置，`uploadVideoFile` z-index 更高，导致对 `uploadImageFile` 的 `click()` 始终被 `uploadVideoFile` 拦截。

**解决**：放弃触发 OS 文件选择框，直接 `set_input_files` 绕过点击：
```python
await page.locator("input[name='uploadImageFile']").first.set_input_files(path)
```

> `expect_file_chooser` 也不适用：WeCom 这套 UI 的上传控件不走标准 file chooser 事件，file chooser 等待会超时。  
> `force=True` 对 **不可见** 元素仍然失败（Playwright 仍检查可见性）；对被遮挡元素同理。  
> `dispatch_event("click")` 能跳过可见性检查，但因为触发的是 JS click 事件而非真实指针事件，有时无法激活后续 UI 流程。

---

## 坑 3：「添加图片/视频/网页」按钮当表单有图片时 CSS 隐藏

**现象**：表单中已存在上传图片后（上次运行遗留），该 `<span>` 按钮被 CSS 设为 `display:none`，`wait_for(state="visible")` 和 `force=True` 均失败。

**解决**：直接跳过该弹窗交互，对 `input[name=uploadImageFile]` 调用 `set_input_files`，不依赖 UI 弹窗路径。

---

## 坑 4：「确定」按钮不是 `<button>`，是 `<a>`

**现象**：对话框「选择可见的客户」中，「确定」渲染为 `<a>` 标签而非 `<button>`，`get_by_role("button", name="确定")` 超时。

**解决**：改用文本定位器：
```python
await page.locator("text=确定").first.click(timeout=5_000)
```

---

## 坑 5：必须先点「选择客户」才能提交

**现象**：直接点「通知成员发表」无反应或弹窗不出现（表单顶部「可见的客户：选择客户」未确认时，提交被静默拦截）。

**正确流程**：
1. 点「选择客户」链接
2. 等待「选择可见的客户」对话框出现（`input[type='radio']` 出现后再操作）
3. 点选「公开」单选
4. 点「确定」关闭对话框
5. 再点「通知成员发表」

---

## 坑 6：最终确认按钮是「发送通知」而非「确定」

**现象**：点「通知成员发表」后弹出「提示」对话框（「成员将收到服务通知，确认后内容将发表到客户的朋友圈」），按钮文字是「发送通知」，不是「确定」。

**解决**：
```python
send_btn = page.get_by_role("button", name="发送通知").first
if not await send_btn.count():
    send_btn = page.locator("text=发送通知").first
await send_btn.click(timeout=5_000)
```

成功标志：页面顶部出现绿色 Toast「通知已发送」。

---

## 关键经验总结

| 场景 | 推荐做法 |
|------|---------|
| SPA 页面残留 DOM 状态 | `page.evaluate` 强制移除，比 `goto` / `reload` 更可靠 |
| 隐藏的 `input[type=file]` | 直接 `set_input_files`，不走 UI 点击路径 |
| 叠放的 input 遮挡 | 用 `name` 属性精确定位，不用索引（`nth(cnt-1)` 可能选错） |
| 按钮角色未知 | 先用 `text=` 定位，比 `get_by_role` 容错性更好 |
| 弹窗 strict mode 报错 | 始终加 `.first` 避免多元素匹配 |
| OOM / 偶发弹窗未出现 | 重跑即可；核心流程本身是幂等的 |
