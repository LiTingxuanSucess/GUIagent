# Legacy Scripts

This folder keeps historical probes and experiments that are not part of the active message-reply or add-customer workflows.

Active scripts intentionally remain at the repository root or under `tools/`:

- Message reply / QA:
  - `wxwork_agent_loop.py`
  - `wxwork_reply.py`
  - `wxwork_watch.py`
  - `wxwork_export_recent.py` (dependency of `wxwork_agent_loop.py`)
  - `tools/current_dialogue_qa_test.py`
  - `tools/wechat_send_test.py`
  - `tools/revai_qa_smoke.py`
- Add customer:
  - `wxwork_add_customer.py`
  - `tools/test_add_customer_on_shots.py`
- OCR support:
  - `wxwork_ocr_service.py`
  - `run_ocr_service.bat`
  - `prefetch_ocr.py`
  - `download_paddlex_bos.py`

Moved here:

- `wxwork_hover_time_probe.py`: historical hover timestamp probe.
- `wxwork_scroll_probe.py`: historical scroll behavior probe.
