"""
WXWorkAgent 统一的 OCR / Paddle 运行环境（各脚本共用）。

用法（每个入口脚本尽量靠前、且在 import win32gui / mss 截图之前）::

    from src.ocr_runtime import init_ocr_runtime
    init_ocr_runtime()

职责划分
--------
* ``init_ocr_runtime()``：调用 ``bootstrap_env.apply()`` — DPI、Paddle 前置环境变量
  （关 oneDNN、限制 BLAS 线程）、PaddleX 模型源与 filelock 补丁。**必须在**
  ``import paddle`` 之前执行。
* ``prepare_paddle_inference()``：首次创建 ``PaddleOCR`` 前执行；由 ``ocr_run._get_ocr``
  内部调用即可，一般业务代码无需直接调用。
"""
from __future__ import annotations

import os

_paddle_inference_prepared = False


def init_ocr_runtime() -> None:
    """
    进程级初始化：截图 DPI、Paddle CPU 稳定性环境变量、PaddleX/HF/filelock。

    可多次调用，等价于一次 ``bootstrap_env.apply()``。
    """
    from src.bootstrap_env import apply

    apply()


def prepare_paddle_inference() -> None:
    """
    在 ``import paddle`` 之后、构造 ``PaddleOCR`` 之前调用一次。
    关闭 oneDNN、固定 CPU，降低 Windows 上 ``predictor.run()`` 异常概率。
    """
    global _paddle_inference_prepared
    if _paddle_inference_prepared:
        return
    import paddle

    try:
        paddle.set_flags({"FLAGS_use_mkldnn": False})
    except Exception:
        pass
    device = os.environ.get("WXWORK_OCR_DEVICE", "cpu")
    try:
        paddle.set_device(device)
    except Exception:
        if device != "cpu":
            paddle.set_device("cpu")
    _paddle_inference_prepared = True
