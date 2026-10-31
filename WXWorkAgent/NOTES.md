# WXWorkAgent 开发笔记

企业微信 PC 客户端（员工账号）自动监听 + Qwen 回复的实现方案、踩坑记录与当前状态。

---

## 一、目标与约束

| 项目 | 说明 |
|---|---|
| 目标 | 员工账号（非 Bot API）自动读取新消息、Qwen 生成回复、自动发送 |
| 平台 | Windows 10/11，企业微信 5.x PC 客户端 |
| 成本约束 | 不用全屏 VL 大模型；截小图 + 本地 OCR + 纯文本 LLM |
| OCR | PaddleOCR PP-OCRv4 mobile，本地 CPU 推理 |
| LLM | DashScope Qwen（`qwen-turbo`），纯文本 API |

---

## 二、整体架构

```
企业微信窗口
    │
    │  mss 截图（物理像素，DPI-aware）
    ▼
pixel hash 快速判断有无变化
    │ 变化
    ▼
PaddleOCR  →  OCR 文字
    │
difflib 模糊 diff  →  新增内容
    │ 有新消息
    ▼
DashScope Qwen  →  回复文本
    │
PostMessage / WM_CHAR  →  企业微信输入框
```

### 文件结构

```
WXWorkAgent/
├── wxwork_watch.py          # 单联系人监控主脚本
├── wxwork_multi_watch.py    # 多会话监控（提醒 + 按需回复）
├── wxwork_reply.py          # 单次 OCR + 回复（调试用）
├── wxwork_add_customer.py   # 按手机号添加外部客户（GUI 流程）
├── prefetch_ocr.py          # 一次性预下载 & 预热 OCR 模型
├── download_paddlex_bos.py  # 从百度 BOS 直接下载模型（国内网络）
├── config.yaml              # 所有参数（不进 git，含 API key）
├── config.example.yaml      # 参数模板
├── requirements.txt
└── src/
    ├── ocr_runtime.py       # 统一入口：init_ocr_runtime()（各脚本 + ocr_run 共用）
    ├── bootstrap_env.py     # DPI、Paddle CPU 环境变量、BOS 源、filelock 超时（由 ocr_runtime 调用）
    ├── capture.py           # mss 截图、RegionRatios、pixel hash
    ├── ocr_run.py           # PaddleOCR 封装，单例，移动模型
    ├── ocr_warmup.py        # 启动时预热，打印详细进度
    ├── diff_util.py         # SequenceMatcher 模糊行 diff
    ├── actions.py           # 键鼠输入（全部 PostMessage，无 SendInput）
    ├── add_customer.py      # 「添加客户」分步点击 + 输入（与截图箭头一致）
    ├── contact_list.py      # 左侧会话列表 OCR + 解析 + diff
    ├── window.py            # find_wework_hwnd / activate / ensure_visible
    ├── qwen_text.py         # DashScope Qwen 文本客户端
    └── dashscope_key.py     # API key 多源解析
```

---

## 三、踩过的坑与解决方案

### 坑 1：模型首次下载卡死 / 超慢

**现象**：脚本启动后长时间无输出，终端卡在 HuggingFace 下载。国内网络访问 HuggingFace 极慢或超时。

**根因**：PaddleX 默认从 HuggingFace 下载模型权重；PaddleOCR 3.x 在多进程场景下会竞争 `filelock`，死锁导致永久挂起。

**解决**：
- `src/bootstrap_env.py` 最早设置 `PADDLE_PDX_MODEL_SOURCE=bos`（百度对象存储，国内快），`HF_ENDPOINT=https://hf-mirror.com` 作为备用。
- 同文件 monkey-patch `filelock.FileLock.__init__`，强制 7200s 超时，防止永久死锁。
- 新增 `download_paddlex_bos.py` —— 直接 HTTP 下载 BOS tarball，完全绕过 PaddleX 下载逻辑。
- 新增 `prefetch_ocr.py` —— 一次性预下载并预热，之后每次启动直接加载本地缓存。

```bash
# 首次使用必跑（只需一次）
py -3.11 prefetch_ocr.py
```

---

### 坑 1b：`RuntimeError: Unknown exception`（`predictor.run()` / oneDNN）

**现象**：`paddlex` 文本检测 `static_infer` 里 `self.predictor.run()` 抛出 `RuntimeError: Unknown exception`，终端里常先出现 `onednn_context.cc` 或 NumPy `Signature ... does not match`。

**根因（常见）**：
- Windows CPU 上 **oneDNN/MKLDNN** 与部分环境不兼容；
- **NumPy wheel 与 Paddle 不匹配**（尤其 NumPy 2.x 或损坏安装）；
- 在 **非主线程** 里跑同全局 `PaddleOCR`（异步 `to_thread` 等），见 [PaddleOCR#16164](https://github.com/PaddlePaddle/PaddleOCR/issues/16164)。

**项目内已做**：
- `bootstrap_env.apply()` 在 import paddle 前设置 `FLAGS_use_mkldnn=0`、`OMP_NUM_THREADS=1` 等；
- `ocr_run._get_ocr()` 内 `paddle.set_flags({"FLAGS_use_mkldnn": False})`、`paddle.set_device("cpu")`；
- `requirements.txt` 钉 `numpy>=1.26.4,<2.0`。

**仍失败时请本机执行**：
```bat
py -3.11 -m pip install "numpy>=1.26.4,<2.0" --force-reinstall
py -3.11 prefetch_ocr.py
```
若必须启用 oneDNN（一般不必）：启动前 `set WXWORK_DISABLE_ONEDNN=0`。

---

### 坑 2：PaddlePaddle 3.3.x `NotImplementedError`

**现象**：OCR 推理阶段抛出 `NotImplementedError` 在 oneDNN PIR 转换路径。

**根因**：PaddlePaddle 3.3.x 在 CPU + oneDNN 模式下 PIR IR 转换有 bug（issue #77340）。

**解决**：`requirements.txt` 钉死 `paddlepaddle==3.2.2`。

---

### 坑 3：截图坐标偏移（DPI 缩放）

**现象**：OCR 内容与屏幕实际内容对不上，截到的是错误区域（例如只有时间戳）。

**根因**：Windows 显示缩放（125%/150%）下，`win32gui.GetWindowRect` 默认返回逻辑像素坐标，而 `mss` 需要物理像素坐标。两者有 1.25×/1.5× 的偏差。

**解决**：`src/bootstrap_env.py` 在进程启动时调用 `SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)`，使 Win32 API 全程返回物理像素。

```python
ctypes.windll.user32.SetProcessDpiAwarenessContext(
    ctypes.c_void_p(-4)   # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
)
```

---

### 坑 4：pixel hash 不稳定（PNG 压缩随机性）

**现象**：同一屏幕内容，相邻两次截图 hash 不同，导致误触 OCR。

**根因**：最初对 PNG 文件字节做 SHA256；`mss.tools.to_png` 内部压缩参数有随机性（实际后来验证是确定的，但为保险起见）。

**解决**：改为对 `mss` 原始 RGB 像素矩阵做 SHA256，完全确定性。

```python
def pixel_hash(shot) -> str:
    raw = bytes(shot.rgb)   # 原始 RGB，无压缩
    return hashlib.sha256(raw).hexdigest()
```

---

### 坑 5：企业微信检测"远程操控行为"

**现象**：脚本发送消息后，企业微信弹出"检测到远程操控行为"警告。

**根因**：企业微信挂了低级键鼠钩子，检测 `LLMHF_INJECTED` 标志——所有经过 `SendInput` API 模拟的输入（包括 `pyautogui` 的 `click/hotkey/press`）都会带此标志。当远控软件（ToDesk、TeamViewer 等）同时运行时，"有远控软件 + 注入输入"触发报警。

**解决**：`src/actions.py` 全部改用 `PostMessage` / `SendMessage` 直发 WeWork 子窗口，完全绕过全局输入队列，不产生 `LLMHF_INJECTED`。

| 旧方式（触发检测） | 新方式（安全） |
|---|---|
| `pyautogui.click(x, y)` → `SendInput(MOUSEINPUT)` | `PostMessage(hwnd, WM_LBUTTONDOWN, ...)` |
| `pyautogui.hotkey("ctrl","v")` → `SendInput(KEYBDINPUT)` | `WM_PASTE` / `WM_CHAR` 逐字 |
| `pyautogui.press("enter")` → `SendInput(KEYBDINPUT)` | `PostMessage(hwnd, WM_KEYDOWN, VK_RETURN, ...)` |

---

### 坑 6：PostMessage Ctrl+V 泄漏裸字符 "v"

**现象**：脚本运行后，输入框出现裸 `v`（或 `fv`），没有粘贴效果。

**根因**：`PostMessage(WM_KEYDOWN, VK_CONTROL)` + `PostMessage(WM_KEYDOWN, VK_V)` 不会更新 `GetKeyState()`。CEF（企业微信用 Chromium 内核）内部检查 Ctrl 状态时得到"未按下"，把 V 当普通字符处理。

**解决**：
- 粘贴改为 `WM_PASTE`（剪贴板消息，不依赖键盘状态），再改为 `WM_CHAR` 逐字输入（CEF 直接接受 Unicode 码点，无需修饰键）。
- Enter 改为 `WM_KEYDOWN(VK_RETURN)` + `WM_KEYUP`（物理键事件，触发"发送"而非换行）。

---

### 坑 7：`WM_PASTE` 对 CEF 无效

**现象**：`SendMessage(cef_hwnd, WM_PASTE, 0, 0)` 调用成功无报错，但输入框里没有内容。

**根因**：CEF 渲染器（`Chrome_RenderWidgetHostHWND`）不处理 `WM_PASTE` 这个 Windows 标准剪贴板消息；CEF 有自己的粘贴路径。

**解决**：改用 `WM_CHAR` 对文本逐字发送。`WM_CHAR` 携带 Unicode 码点，CEF 输入处理器直接接收，等价于用户打字，完全可靠。

```python
for ch in text:
    win32api.PostMessage(cef_hwnd, win32con.WM_CHAR, ord(ch), 0)
    time.sleep(0.01)
```

---

### 坑 8：截图裁掉左侧气泡 / 右侧气泡

**现象**：OCR 文字残缺，左侧消息只剩最右边一两个字，右侧（己方）消息完全看不到。

**根因**：`left_ratio` 设置过高（0.33），截图起点在聊天区左边缘右边约 6%，左对齐气泡被截掉左半；`width_ratio` 不够（0.62+0.33=0.95），右对齐气泡起点在 0.95 附近被截掉。

**解决**：调整截图比例：

```yaml
capture:
  left_ratio:  0.27   # 原 0.33，向左扩 6%
  width_ratio: 0.72   # 原 0.62，右边缘扩到 0.99
```

---

### 坑 9：OCR 结果抖动导致误触/漏触

**现象**：同一内容相邻两次 OCR 结果有 1-2 字差异，diff 误判为新消息；或同一内容的微小 OCR 差异阻止"与上轮相同"判断。

**解决**：
1. `src/diff_util.py`：行级比较改用 `difflib.SequenceMatcher`（阈值 0.82），允许少量 OCR 噪声。
2. `_ocr_stable()`：要求连续 N 次 OCR 结果相同才采纳，过滤渲染中间状态。

---

### 坑 10：切换 App 后截图变成其他窗口内容

**现象**：用户切到其他 App，mss 截取的是屏幕坐标处的内容（可能是另一个窗口），OCR 后误触 Qwen 回复。

**根因**：mss 按屏幕物理坐标截图，不按窗口内容截图。企业微信被遮挡后，截到的是覆盖它的窗口。

**解决**：`wxwork_watch.py` 在 hash 变化后，先检查企业微信是否是前台进程（`win32process.GetWindowThreadProcessId`），不是则跳过 OCR 并保留旧 hash，等用户切回来再处理。

```python
def _wework_is_foreground(hwnd: int) -> bool:
    fg = win32gui.GetForegroundWindow()
    _, fg_pid = win32process.GetWindowThreadProcessId(fg)
    _, wx_pid = win32process.GetWindowThreadProcessId(hwnd)
    return fg_pid == wx_pid
```

---

### 坑 11：`_find_cef_hwnd` 找错子窗口

**现象**：键鼠消息发到了错误的 CEF 子窗口（比如顶栏、搜索框），输入框没有反应。

**根因**：企业微信包含多个 `Chrome_RenderWidgetHostHWND`，盲取"最后一个"不一定是消息输入框所在的那个。

**解决**：改为按屏幕坐标找子窗口——枚举所有 CEF 子窗口，找其窗口 Rect 包含目标点（输入框坐标）的那个。

```python
def _find_cef_hwnd_at(parent, x_screen, y_screen):
    for child in enum_cef_children(parent):
        l, t, r, b = win32gui.GetWindowRect(child)
        if l <= x_screen <= r and t <= y_screen <= b:
            return child
    return parent
```

---

## 四、当前状态

### 已完成 ✅

| 功能 | 脚本 |
|---|---|
| 单联系人监控 + Qwen 自动回复 | `wxwork_watch.py` |
| 多会话监控 + 分级提醒/回复 | `wxwork_multi_watch.py` |
| 单次 OCR + 回复（调试） | `wxwork_reply.py` |
| 模型预下载（BOS / HF Mirror） | `prefetch_ocr.py` + `download_paddlex_bos.py` |
| 截图 DPI 矫正 | `src/bootstrap_env.py` |
| 无 SendInput 键鼠输入 | `src/actions.py` |
| 切 App 后暂停 OCR | `wxwork_watch.py` |
| 模糊行 diff | `src/diff_util.py` |

### 已知局限 / 待完善

| 问题 | 说明 |
|---|---|
| 多会话列表解析依赖 OCR 质量 | 联系人名字 OCR 偶尔有误，`auto_reply_contacts` 模糊匹配可能误触 |
| WM_CHAR 逐字输入较慢 | 长消息（>200字）需要 2-3 秒打完；可考虑剪贴板+CEF原生粘贴路径 |
| mss 按屏幕坐标截图 | 企业微信被其他窗口完全遮挡时截图内容错误，当前用"是否前台"做保护，但非完美 |
| Ctrl+F 搜索联系人 | 用 `WM_CHAR(0x06)` 模拟，部分布局下可能失效；失效时可降级到 pyautogui |
| 无持久化 | 重启脚本后 OCR 基线重建，历史记录不保留 |
| 未处理图片/文件消息 | OCR 只读文字气泡，图片内容忽略 |

---

## 五、快速上手

```bash
# 1. 安装依赖（仅首次）
py -3.11 -m pip install -r requirements.txt

# 2. 预下载 OCR 模型（仅首次，需几分钟）
py -3.11 prefetch_ocr.py

# 3. 复制配置并填写 API Key
copy config.example.yaml config.yaml
# 编辑 config.yaml，填入 qwen.api_key

# 4a. 单联系人监控
set DASHSCOPE_API_KEY=sk-xxx
py -3.11 wxwork_watch.py --contact "联系人名"

# 4b. 多会话监控
py -3.11 wxwork_multi_watch.py

# 4c. 按手机号添加外部客户（先校准 config.yaml 的 add_customer 比例）
py -3.11 wxwork_add_customer.py --phone 13800138000

# 调试：保存截图看截取区域是否正确
py -3.11 wxwork_watch.py --contact "联系人名" --debug-shot shots/debug.png
```

### 关键配置项

```yaml
capture:
  left_ratio:  0.27   # 聊天区左边界（窗口宽度比例）
  width_ratio: 0.72   # 截图宽度（调大可覆盖右侧气泡）

multi_watch:
  alert_only: false               # true=只提醒，false=也自动回复
  auto_reply_contacts: ["张三"]   # 空列表=对所有人回复
```

---

## 六、技术选型说明

| 选项 | 选择 | 原因 |
|---|---|---|
| 截图方案 | mss | 比 PIL/pyautogui 快 3-5×，支持物理像素 |
| OCR | PaddleOCR PP-OCRv4 mobile | 本地 CPU，中文准确率高，mobile 版推理 0.5-1.2s |
| 变化检测 | pixel hash (SHA256 of raw RGB) | 稳定，无压缩随机性 |
| 键鼠输入 | PostMessage + WM_CHAR | 不触发 LLMHF_INJECTED，绕过企业微信远控检测 |
| LLM | DashScope Qwen | 中文效果好，成本低，纯文本 API 无需多模态 |
| 配置 | YAML | 人类可读，易调整比例参数 |

---

## 七、按手机号添加外部客户（与 `shots/add_customer` 箭头一致）

**流程**（PC 企业微信）：

1. 左侧竖条点 **通讯录**
2. 中间列点 **新的客户**
3. 右侧主区顶部点 **+ 添加**
4. 弹窗 **添加客户** 内点 **手机号/邮箱** 输入框 → `WM_CHAR` 输入号码
5. 等待搜索 → 点结果行右侧 **添加**
6. 弹窗 **发送添加邀请** → 点蓝色 **发送**

**脚本**：`wxwork_add_customer.py`。默认 **全窗截图 + PaddleOCR** 识别「通讯录」「新的客户」「+ 添加」「手机/邮箱」「添加」「发送」等文字框并点击中心；未命中时 **回退** 到 `add_customer` 里的比例坐标。可调 `ocr_region_*`（归一化区域）与 `ocr_kw_*`（关键字）减少误点；`use_ocr: false` 或 `--no-ocr` 可纯比例模式。

**参数**：

- `--skip-nav`：已在「新的客户」列表页时跳过前两步
- `--no-final-send`：点完结果「添加」后停住，便于人工改附言再发
- `--dry-run`：只打印换算后的屏幕坐标，不点击

**自测截图**：`py -3.11 tools/test_add_customer_on_shots.py`（真实 Paddle）；若推理环境报错可用 ` --mock` 只验证关键字+区域筛选逻辑。

**局限**：对方须允许通过手机号被搜到；可能有风控；无法绕过对方在微信侧确认。

**托盘 / 无主窗口**：`find_wework_hwnd` 会枚举 **不可见** 的 `WeWorkWindow`（仅托盘时常仍有 HWND）；若进程在但枚举不到，`wxwork_add_customer.py` 默认会 **启动一次 `WXWork.exe`**，利用「二次启动唤起已有实例」把界面拉出来。不需要也不模拟点击托盘图标（易碎）。加 `--no-launch` 可禁止启动 exe。
