# ECAgent Web Console

本地价格追踪运营台，读取 `exports/*/taobao_results.json` 并展示淘宝采集结果、详情截图和需求缺口。

## 启动后端

```powershell
cd D:\Workspace\gui-agent\ECAgent
py -3.11 -m pip install -r requirements.txt
py -3.11 -m uvicorn web.backend.main:app --host 127.0.0.1 --port 8010 --reload
```

## 启动前端

```powershell
cd D:\Workspace\gui-agent\ECAgent\web\frontend
npm install
npm run dev
```

默认前端端口是 `5174`，后端 API 是 `http://127.0.0.1:8010`。

## 当前数据源

- `exports/taobao_latest/taobao_results.json`
- `exports/taobao_latest/search_results.png`
- `exports/taobao_latest/screenshots/*.png`

## 页面能力

- 查看采集批次。
- 查看商品列表。
- 查看到手价、片数、片单、销量、店铺、截图。
- 查看每条商品复核标记。
- 对照需求查看当前能力覆盖和缺口。
