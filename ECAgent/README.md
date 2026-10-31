# ECAgent

电商产品价格追踪 Agent。当前先做通淘宝搜索结果页到商品详情页的 MVP。

## 运行前准备

安装依赖：

```powershell
cd D:\Workspace\gui-agent\ECAgent
py -3.11 -m pip install -r requirements.txt
```

启动带 CDP 的 Chrome。这个方式复用本机默认 Chrome 用户数据目录，和 `DouyinCSAgent` 的思路一致：

```powershell
& "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
  --no-first-run `
  --no-default-browser-check `
  "https://www.taobao.com"
```

验证 CDP：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9222/json/version
```

## 淘宝 MVP

复制配置：

```powershell
copy config.example.yaml config.yaml
```

运行默认目标 `杜蕾斯 001`：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3.11 eca_taobao.py --max-candidates 12 --detail-limit 5
```

指定关键词：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3.11 eca_taobao.py --keyword "冈本 003" --max-candidates 12 --detail-limit 5
```

输出：

- `exports/taobao_*/search_results.png`
- `exports/taobao_*/screenshots/*.png`
- `exports/taobao_*/taobao_results.json`
- `exports/taobao_*/taobao_results.csv`

## 当前能力

- 附着本机 Chrome CDP。
- 在淘宝搜索框写入中文关键词。
- 触发搜索并尝试切销量排序。
- 抽取搜索结果候选商品。
- 进入前 N 个详情页。
- 保存搜索页和详情页截图。
- 解析标题、价格、付款/收货人数、店铺、片数、片单。
- 输出 JSON 和 CSV。

## 下一步

- 增强淘宝商品卡片 DOM 解析，进一步分离标题和店铺名。
- 监听搜索页 XHR，优先从结构化接口拿商品字段。
- 接入 WXWorkAgent OCR 服务校验详情页可见价格。
- 增加官方旗舰店/自营白名单和店内搜索。
- 扩展京东、抖音适配器。
