# WXWork Agent Console

企业微信抓取结果的本地可视化控制台：FastAPI 提供导出数据接口，Vue 前端按企业微信风格展示会话列表、消息气泡和原始截图链接，并预留“抖音后台”入口。

## 启动后端

```powershell
cd d:\Workspace\gui-agent\WXWorkAgent
py -3.11 -m pip install -r requirements.txt
py -3.11 -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8000
```

## 启动前端

```powershell
cd d:\Workspace\gui-agent\WXWorkAgent\web\frontend
npm install
npm run dev
```

打开 Vite 输出的地址，默认会请求 `http://127.0.0.1:8000`。如果后端端口不同，可设置 `VITE_API_BASE_URL`。

## 数据来源

后端会读取 `exports/*/recent_chats.json`，默认展示最近修改的一批。截图通过 `/exports/...` 静态路径提供。
