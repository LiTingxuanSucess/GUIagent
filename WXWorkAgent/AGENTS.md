# WXWorkAgent Agent Instructions

## Encoding

All source, docs, config templates, and web files in this repo are UTF-8. Chinese text is expected and must not be converted through a Windows ANSI code page.

Mandatory workflow for future agents:

- Before assuming a file is corrupted, verify with Python:
  `py -3.11 -c "from pathlib import Path; print(repr(Path('file').read_text(encoding='utf-8-sig')[:500]))"`
- PowerShell `Get-Content` can display valid UTF-8 as mojibake. Set UTF-8 output before using it for Chinese text:
  `[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $OutputEncoding=[System.Text.UTF8Encoding]::new($false)`
- Use `apply_patch` for edits. Avoid shell redirection or command-generated text unless the command explicitly writes `encoding='utf-8'`.
- Run `py -3.11 tools\check_text_encoding.py` after editing Python, YAML, Markdown, JSON, Vue, CSS, HTML, or TypeScript files.
- Some OCR fallback strings may intentionally include historical OCR/mojibake variants for matching. Do not delete those unless replacing them with a tested structured matcher.

## GUI Automation

Keep loop/watch scripts separate from personal-WeChat test sender scripts. Loop tests should default to dry-run unless the user explicitly asks to send enterprise replies.
