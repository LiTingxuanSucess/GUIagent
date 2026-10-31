# PRAgent

GUI Agent for Google News scraping using **OmniParser + Qwen-VL API + Playwright**.

## Architecture

- **OmniParser** (optional): Local screen parsing, reduces API calls
- **Qwen-VL API**: Decision making (low cost)
- **Playwright**: Browser automation

## Setup

- Python 3.11+

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Set API key:
- Windows: `set DASHSCOPE_API_KEY=your_key`
- Linux/Mac: `export DASHSCOPE_API_KEY=your_key`
- Or set `api_key` in config.yaml (not recommended for commit)

## Usage

```bash
python main.py --keyword "XREAL" --pages 30 --output xreal_news.csv
```

Or on Windows: `run.bat --keyword XREAL --pages 30 --output xreal_news.csv`

Options:
- `--keyword`: Search keyword (default: XREAL)
- `--pages`: Number of pages to scrape (default: 30)
- `--output`: Output file path (CSV or JSON)
- `--headless`: Run browser in headless mode

## OmniParser (optional)

When OmniParser server is running, set `omniparser.enabled: true` in config.yaml.
