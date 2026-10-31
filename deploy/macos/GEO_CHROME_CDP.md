# GEO 在 Mac 上使用独立 Chrome CDP

macOS 默认使用 `mac-cdp` 模式，部署环境也显式设置
`GEO_CHROME_MODE=mac-cdp`。通过 LaunchServices 打开已安装的 Google Chrome，
随后连接该浏览器的本机 CDP。每个号池账号仍使用自己的资料目录：
`GEO/.chrome-cn-ai-profiles/<account_id>/`，旧默认账号使用
`GEO/.chrome-cn-ai-profile/`。

浏览器有可见窗口，人工登录与 GEO 操作共用同一个账号资料。Chrome 的账号资料
与日常个人 Chrome 资料分开，这是新版本 Chrome 对远程调试的要求，见
[Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)。

## 会话与生命周期

- 首先验证 `DevToolsActivePort` 对应的本机浏览器，存活时直接复用。
- 没有可用浏览器时，通过 `/usr/bin/open -na` 启动 Chrome，CDP 只绑定本机。
- 启动保留系统默认渲染、安全和扩展设置，不使用旧启动器的禁用 GPU、关闭沙箱、
  忽略证书、模拟钥匙串或禁用站点隔离参数。
- 保留资料锁、原会话文件和已有平台/登录标签页。连接存活的 Chrome 时不导入旧快照；
  新启动时仍可补充资料中缺失的已保存登录信息。
- GEO 停止、重启或回收空闲连接时，先断开 CDP，再清理服务自己的上下文，
  保留 Chrome 进程、业务标签页和人工操作现场。不会主动冻结独立 Chrome 的页面。
- 下次需要账号时重新连接，通过实际页面判断登录状态；Cookie 存在不等于登录有效。
- 人工关闭 Chrome 后，下次使用会尝试重新打开该账号的浏览器。

CDP 只是一种连接方式，不保证平台不会要求短信、安全验证或限制账号。
保持账号隔离、正常使用频率和人工处理验证仍然必要。

## 配置与回退

Mac 部署的启动设置在
`~/Library/LaunchAgents/cn.revai.guiagent.geo.plist` 的 `EnvironmentVariables` 中。
设置 `GEO_CHROME_MODE=exec` 可以使用旧的进程启动方式；省略或设置 `auto`
在 macOS 使用 `mac-cdp`，其他系统保持 `exec`。修改 LaunchAgent 后需重新加载服务。
先等正在执行的任务结束；切换旧模式前应手动退出相应的独立 Chrome，避免资料冲突。

Windows/Linux 原有部署方式保持不变。

## 验证

```bash
cd /Users/chuzu/Services/GUIAgent/GEO/server
go test ./...
go vet ./...
GEO_TEST_MAC_CDP=1 go test -v -run TestMacCDPStopAndReconnectPreservesPage -timeout 75s ./internal/sessions
```

真实浏览器测试只使用临时账号资料和本机测试页，验证连接断开后进程仍可访问、
再次连接复用同一标签页、Cookie 与本地存储保留，不向业务平台发送问题或验证码。

2026-10-22 验证：上述浏览器测试及其 `-race` 运行通过，会话、配置、HTTP 接口
测试和 `go vet ./...` 通过。本机服务已经启用新模式；实际重启 GEO 后，Chrome
进程与 DeepSeek 标签页保持不变，重新检查复用原页面，公网后台仍可用。

`go test ./...` 尚有原有 Linux 进程检测测试在 Mac 上失败：
`TestFakeCursorLaunchPersistsActualInvocationFingerprint`、
`TestReadPPIDMatchesParent`、`TestDescendantsIncludeChild`。
这些失败在未修改的 `HEAD` 上同样复现，涉及 `/proc`，本次未改变相关功能。
