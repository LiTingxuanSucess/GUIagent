# GEO Results Viewer

只读展示 `GEO/exports/cn-ai-*.json` 批量采集结果的本地 FastAPI 页面。

## 本机运行

```bash
cd GEO/viewer
python3 -m pip install -r requirements.txt
./start_viewer.sh
```

默认监听 `127.0.0.1:18765`。运行 `./stop_viewer.sh` 停止服务。Viewer 读取 GEO 根目录下的导出 JSON，不负责发问或登录 AI 平台。

公网部署需要由部署方自行配置反向代理、域名、TLS 和访问控制；仓库不绑定个人部署地址或隧道服务。
