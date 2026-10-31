"""
不经过 HuggingFace，用百度 BOS HTTP 直链下载 PaddleX 官方推理包到本地缓存。

与 paddlex 内建逻辑一致：
  https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/{Model}_infer.tar

适用：HF / hf-mirror 多文件 Fetch 卡住、进度条不动时。

用法:
  cd WXWorkAgent
  py -3.11 download_paddlex_bos.py
  py -3.11 download_paddlex_bos.py --redownload
  py -3.11 download_paddlex_bos.py --models PP-OCRv5_server_det PP-OCRv5_server_rec
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# PaddleOCR(lang=ch) PP-OCRv5 常用 + 文档类依赖（与日志中 UVDoc / textline_ori 一致）
DEFAULT_MODELS = [
    "PP-OCRv5_server_det",
    "PP-OCRv5_server_rec",
    "UVDoc",
    "PP-LCNet_x1_0_textline_ori",
]

BOS_VERSION = "paddle3.0.0"
BOS_BASE = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="BOS 直链下载 PaddleX official_models")
    parser.add_argument(
        "--models",
        nargs="*",
        default=DEFAULT_MODELS,
        help="模型目录名（与 .paddlex/official_models 下文件夹名一致）",
    )
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="先删除本次要下的模型目录再下载（修复半拉子 HF 缓存）",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="覆盖 PADDLE_PDX_CACHE_HOME（默认 ~/.paddlex）",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from src.ocr_runtime import init_ocr_runtime

    init_ocr_runtime()

    import shutil

    from paddlex.utils.download import download_and_extract

    if args.cache_dir is not None:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.cache_dir)

    official = Path(os.environ.get("PADDLE_PDX_CACHE_HOME", Path.home() / ".paddlex")) / "official_models"
    official.mkdir(parents=True, exist_ok=True)

    for name in args.models:
        dst = official / name
        if args.redownload and dst.exists():
            print(f"移除旧目录: {dst}", flush=True)
            shutil.rmtree(dst, ignore_errors=True)

        url = f"{BOS_BASE}/{BOS_VERSION}/{name}_infer.tar"
        print(f"下载并解压: {url} -> {dst}", flush=True)
        download_and_extract(url, str(official), name, print_progress=True, overwrite=False)
        print(f"完成: {dst}", flush=True)

    print("全部完成。可运行: py -3.11 prefetch_ocr.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
