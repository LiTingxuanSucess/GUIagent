"""
一次性预下载 PaddleOCR / PaddleX 所需的全部官方模型（仅首次需几分钟）。

请在首次使用或重装 paddlex 后执行：
  py -3.11 prefetch_ocr.py

卡住且已关闭所有其它 Python 时，可清理锁后重试：
  py -3.11 prefetch_ocr.py --clear-paddlex-locks

监听脚本 wxwork_watch 启动前也应先跑完本脚本，避免「发消息了但还在下模型」。

HuggingFace 拉取卡住时，先用百度 BOS 直链（推荐）:
  py -3.11 download_paddlex_bos.py
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="预下载 WXWorkAgent PaddleOCR 模型")
    parser.add_argument(
        "--clear-paddlex-locks",
        action="store_true",
        help="删除 ~/.paddlex/locks 下 .lock（请先结束所有 python.exe 再执行）",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.bootstrap_env import clear_paddlex_lock_files
    from src.ocr_runtime import init_ocr_runtime

    if args.clear_paddlex_locks:
        n, lock_root = clear_paddlex_lock_files()
        print(f"已清理 {n} 个锁文件 under {lock_root}")

    init_ocr_runtime()

    from src.ocr_warmup import warmup_ocr_pipeline

    print("=" * 60)
    print("WXWorkAgent：预拉取 OCR 模型（请只开一个终端，勿同时跑 watch / reply）")
    print("默认已使用 HF 镜像；改用官方: set HF_ENDPOINT=https://huggingface.co")
    print("=" * 60)
    t0 = time.perf_counter()
    text = warmup_ocr_pipeline()
    dt = time.perf_counter() - t0
    print(f"完成，耗时 {dt:.1f}s，样例识别长度 {len(text)}。现在可运行 wxwork_watch。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
