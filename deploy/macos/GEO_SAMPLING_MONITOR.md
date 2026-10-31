# Excel 采样任务飞书通知

当前批次 `auto-20261022-six-websearch` 由 macOS LaunchAgent
`cn.revai.guiagent.geo-sampling-monitor` 每 600 秒执行一次独立检查。
安装后立即发送首条；即使采样控制器退出，监控仍会发送停滞或心跳失效消息。

脚本：`GEO/server/scripts/sampling_monitor.py`。
私有配置：`GEO/server/data/sampling-monitor/config.json`（权限 0600）。
通知状态及日志在同一私有目录；Webhook 不放入版本库或日志。
使用现有 `request_monitor.send_feishu`，同时检查 HTTP 状态和飞书业务错误码。
飞书消息格式参见[官方自定义机器人文档](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

通知内容包括：

- 完成数、较上次成功通知新增数、六平台分别完成数。
- 执行、排队、待人工、重试与缺项数量。
- 具体题号、轮次、平台和任务 ID，以及任务持续时长。
- 已记录的错误原因、发送阶段、人工处理入口及账号占用影响。
- 采样心跳超过两分钟未更新、连续没有新增完成等异常。

状态只在飞书确认接收后保存。发送失败不更新完成数基线，下次仍会尝试通知。
全部完成时发送一次最终报告，之后定时检查不再重复发完成消息。
监控只读取任务和进度，不自动点击网页或重复发送问题。

采样服务另有即时恢复通知：发现验证码时先发通知并调用 Cursor CLI，
辅助结束后通知实际结果。私有配置同上，`sampling_alert.py` 保存失败通知并重试；
十分钟汇报也会列出 CLI 的实际错误（包括账单或登录问题）。
当前六路采样启用平台隔离，待人工平台不会阻塞其他平台。

查看运行情况：

```bash
launchctl print gui/$(id -u)/cn.revai.guiagent.geo-sampling-monitor
cat GEO/server/data/sampling-monitor/monitor.log
cat GEO/server/data/sampling-monitor/monitor-error.log
```

手动执行一条报告（十分钟内默认去重，`--force` 可强制发送）：

```bash
.venv/bin/python GEO/server/scripts/sampling_monitor.py \
  --config GEO/server/data/sampling-monitor/config.json \
  --state GEO/server/data/sampling-monitor/state.json
```

停止通知：

```bash
launchctl bootout gui/$(id -u)/cn.revai.guiagent.geo-sampling-monitor
```

重新启用：

```bash
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/cn.revai.guiagent.geo-sampling-monitor.plist
```

Mac 需保持开机且当前用户已登录；休眠期间的定时执行由系统恢复后继续。
采样控制器本身已用 `caffeinate` 防止空闲休眠。
