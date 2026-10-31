# Mac 本机部署

部署模块：WXWorkAgent 统一 Agent Console。入口：<http://127.0.0.1:8000>。

实际运行目录：`/Users/chuzu/Services/GUIAgent`。桌面的 `GUIAgent` 是指向该目录的链接，
避免 macOS 对后台进程读取桌面目录的权限限制。

前端使用 Vue 构建产物，和 FastAPI 后端在同一端口提供服务。服务只监听本机地址。
launchd 在当前用户登录后启动服务，进程退出后自动重启。

## 日常操作

- 启动并打开页面：双击 `start.command`。
- 停止当前服务：双击 `stop.command`。
- 查看状态：在仓库根目录运行 `.venv/bin/python deploy/macos/manage.py status`。
- 日志目录：仓库根目录下的 `logs/macos-console/`。
- SQLite 数据库：`WXWorkAgent/data/agent_platform.db`。

停止操作仅影响当前登录会话；下次登录仍自动启动。取消自动启动需先停止服务，再删除
`~/Library/LaunchAgents/cn.revai.guiagent.console.plist`。

## 首次安装或重新构建

```bash
cd /Users/chuzu/Services/GUIAgent
/opt/homebrew/bin/uv venv --python /opt/homebrew/bin/python3.11 .venv
/opt/homebrew/bin/uv pip install --python .venv/bin/python -r deploy/macos/requirements.txt
cd WXWorkAgent/web/frontend
PATH=/opt/homebrew/opt/node@22/bin:$PATH npm ci --no-audit --no-fund
PATH=/opt/homebrew/opt/node@22/bin:$PATH npm run build
cd ../../..
.venv/bin/python deploy/macos/manage.py start
```

已安装的 Python 依赖版本记录在 `requirements.installed.txt`，前端依赖由
`WXWorkAgent/web/frontend/package-lock.json` 锁定。

## 功能边界

管理后台提供客户线索、抖音会话、企微归档和朋友圈任务界面。初次部署没有业务数据，
需从实际 Agent 上报或导入归档。部署不会自动启动客服回复或发送朋友圈任务。

企微客户端加客户、聊天截图和桌面 OCR 脚本使用 Windows API，需要 Windows 执行端。
网页自动化还需单独启动 CDP 浏览器并登录相应业务账号。本次部署未配置这些业务登录态。

GitLab 凭据仅用于首次拉取，没有写入仓库或服务配置。

## 本次验证

前端生产构建通过；首页、健康检查和主要查询 API 返回 HTTP 200。
使用本机 Chrome 验证四个菜单均能完成加载并显示空数据状态，无页面脚本异常。
服务进程终止后，launchd 自动创建新进程，健康检查恢复 HTTP 200。
