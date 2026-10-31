# WXWorkAgent OCR Service Startup

Last updated: 2026-05-05 +08:00

## What Happened

Some screenshot/OCR test scripts may fail with:

```text
ModuleNotFoundError: No module named 'paddle'
```

This means the script was run with the wrong Python interpreter. The normal system Python selected by `py -3.11` does not have Paddle installed. Real WeWork OCR should not import Paddle directly from every business script. The project uses a resident local OCR HTTP service on `127.0.0.1:8765`.

## Correct Startup Command

From the repo root:

```powershell
Set-Location D:\Workspace\gui-agent\WXWorkAgent
D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe wxwork_ocr_service.py --port 8765 --backend paddle --device gpu:0 --max-side 960
```

Shortcut:

```powershell
Set-Location D:\Workspace\gui-agent\WXWorkAgent
.\run_ocr_service.bat
```

`run_ocr_service.bat` first tries:

```text
D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe
```

If that GPU OCR environment is missing, it falls back to:

```powershell
py -3.11 wxwork_ocr_service.py --backend rapidocr --max-side 480
```

The fallback is useful for basic smoke tests, but the preferred production/local QA path is Paddle in `.venv-wxocr-gpu`.

## How Business Scripts Use OCR

Business scripts call:

```text
src/ocr_client.py
```

That client sends HTTP requests to:

```text
http://127.0.0.1:8765
```

Do not make every script import `paddle` directly. Keep Paddle loaded once in `wxwork_ocr_service.py`; it avoids repeated model initialization, PaddleX lock contention, and inconsistent Python environments.

## Verify the Service

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Warmup check:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/warmup -ContentType 'application/json' -Body '{}'
```

Expected shape:

```json
{
  "ok": true,
  "text": "...",
  "elapsed_sec": 1.23
}
```

Python client check:

```powershell
py -3.11 -c "from src.ocr_client import warmup_ocr_service; print(warmup_ocr_service())"
```

If this succeeds, normal scripts such as `wxwork_export_recent.py`, `wxwork_agent_loop.py`, `tools/wechat_send_test.py`, and `tools/current_dialogue_qa_test.py` can use OCR through the resident service even though `py -3.11` itself has no `paddle`.

## Process Check

Check whether OCR service is already running:

```powershell
Get-Process | Where-Object { $_.ProcessName -in @('python','py') } |
  Select-Object Id,ProcessName,MainWindowTitle,Path
```

More specific command line check:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'wxwork_ocr_service.py' } |
  Select-Object ProcessId,Name,CommandLine
```

## Common Problems

### Wrong Python

Symptom:

```text
ModuleNotFoundError: No module named 'paddle'
```

Cause:

```text
py -3.11
```

does not contain Paddle. Use:

```text
D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe
```

for the OCR service.

### Port Already in Use

Symptom:

```text
OSError: [WinError 10048] only one usage of each socket address is normally permitted
```

Cause: another OCR service is already listening on `8765`.

Action: reuse the existing service, or stop the old `wxwork_ocr_service.py` process before starting a new one.

### PaddleX Lock or First-Run Download Hangs

Only one process should initialize PaddleOCR models at a time. If first-run model download or lock files get stuck, close other OCR/Python processes first, then run:

```powershell
py -3.11 prefetch_ocr.py --clear-paddlex-locks
```

If the normal PaddleX download path is slow, use the BOS downloader:

```powershell
py -3.11 download_paddlex_bos.py
```

Then restart the OCR service with the GPU Python command above.

### RapidOCR Fallback Is Not the Same as Paddle

`rapidocr` fallback can keep simple tests moving, but coordinate boxes and Chinese recognition quality may differ. For WeWork full QA and screenshots that depend on reliable text boxes, prefer:

```powershell
D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe wxwork_ocr_service.py --backend paddle --device gpu:0 --max-side 960
```

## Recommended Workflow

1. Start OCR service once in a separate terminal.
2. Run `Invoke-RestMethod http://127.0.0.1:8765/health`.
3. Run `/warmup`.
4. Keep that terminal open.
5. Run WeWork/WeChat automation scripts from normal repo terminal.

## Key Files

```text
wxwork_ocr_service.py       resident OCR HTTP service
run_ocr_service.bat         preferred local startup wrapper
src/ocr_client.py           HTTP client used by business scripts
src/ocr_runtime.py          Paddle runtime guards
src/ocr_run.py              PaddleOCR wrapper
prefetch_ocr.py             model prefetch / lock cleanup
download_paddlex_bos.py     direct BOS model downloader
```
