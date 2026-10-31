"""
进程级环境补丁；业务入口请优先调用 ``src.ocr_runtime.init_ocr_runtime()``（内部即 ``apply()``）。

必须在首次 ``import paddle`` / paddlex / huggingface_hub 之前执行。

作用：
- Windows：Per-Monitor DPI、CPU 推理稳定性（oneDNN/OMP 等，见 ``_apply_paddle_cpu_runtime_guards``）
- 默认从百度 BOS 拉官方推理包（大 tar 直链），避免 HuggingFace 多文件慢传卡住
- 默认 HF 镜像（仅当仍走 HF 时生效；可用环境变量覆盖）
- PaddleX 内部 filelock 默认无限等待 → 改为有界等待，避免多开 Python 时死等
"""
from __future__ import annotations

import os
from pathlib import Path

_DONE = False


def clear_paddlex_lock_files() -> tuple[int, Path]:
    """删除 ~/.paddlex/locks 下 .lock（请先结束所有 python，避免与活进程冲突）。"""
    default = Path.home() / ".paddlex"
    cache = Path(os.environ.get("PADDLE_PDX_CACHE_HOME", str(default)))
    lock_root = cache / "locks"
    n = 0
    if lock_root.is_dir():
        for p in lock_root.rglob("*.lock"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n, lock_root


def _set_process_dpi_aware() -> None:
    """
    声明进程为 Per-Monitor DPI Aware。
    必须在首次调用任何 Win32 坐标 API 前执行，否则 GetClientRect / ClientToScreen
    在高 DPI（125%/150%/...）缩放下返回逻辑坐标，与 mss 的物理像素坐标不一致，
    导致截图区域偏移甚至完全截到桌面。
    """
    import ctypes

    try:
        # Windows 10 1703+：Per-Monitor Aware v2，最精确
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_ssize_t(-4))
        return
    except Exception:
        pass
    try:
        # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        # Windows Vista+（fallback）
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _apply_paddle_cpu_runtime_guards() -> None:
    """
    降低 Windows CPU 上 paddlex static_infer predictor.run() 报
    RuntimeError: Unknown exception 的概率。

    常见根因：oneDNN 与部分 CPU/驱动组合不兼容；OpenMP/MKL 多线程与 Paddle
    内部线程冲突；NumPy ABI 异常（见 requirements 对 numpy 的约束）。

    必须在 **import paddle / paddleocr** 之前设置环境变量；部分标志还需
    paddle.set_flags（见 ocr_run._get_ocr）。
    """
    # 默认关闭 MKLDNN/oneDNN（仍可走 OpenBLAS 等）。若需 CPU 极限性能可设
    # WXWORK_DISABLE_ONEDNN=0
    if os.environ.get("WXWORK_DISABLE_ONEDNN", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        os.environ["FLAGS_use_mkldnn"] = "0"

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def _add_nvidia_dll_dirs() -> None:
    """Make CUDA runtime wheels installed in the current Python visible."""
    import site

    for root in site.getsitepackages():
        base = Path(root) / "nvidia"
        if not base.is_dir():
            continue
        for bin_dir in base.glob("*/bin"):
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def apply() -> None:
    global _DONE
    if _DONE:
        return

    _apply_paddle_cpu_runtime_guards()
    _add_nvidia_dll_dirs()
    _set_process_dpi_aware()

    # PaddleX 模型源：默认 bos（与 paddlex 内置一致：paddle3.0.0/{Model}_infer.tar）
    # 若要强制 HuggingFace：set PADDLE_PDX_MODEL_SOURCE=huggingface
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
    # 国内默认镜像；要用官方 Hub：启动前 set HF_ENDPOINT=https://huggingface.co
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")

    import filelock

    _orig_init = filelock.FileLock.__init__
    wait = float(os.environ.get("WXWORK_PADDLEX_LOCK_WAIT_SEC", "7200"))

    def _patched_init(self, lock_file, timeout=-1, *args, **kwargs):
        if timeout is None or float(timeout) < 0:
            timeout = wait
        _orig_init(self, lock_file, timeout, *args, **kwargs)

    filelock.FileLock.__init__ = _patched_init  # type: ignore[method-assign]

    _DONE = True
