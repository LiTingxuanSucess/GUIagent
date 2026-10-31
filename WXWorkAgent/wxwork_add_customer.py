"""
企业微信 PC：按手机号添加外部客户（自动化 GUI）。

默认用 PaddleOCR 识别「通讯录」「新的客户」「+ 添加」等文字框并点击中心；
未识别时回退到 add_customer 里的比例。可用 `use_ocr: false` 或 `--no-ocr` 纯比例。

用法:
  py -3.11 wxwork_add_customer.py --phone 13800138000
  py -3.11 wxwork_add_customer.py --phone 13800138000 --skip-nav
  py -3.11 wxwork_add_customer.py --phone 13800138000 --no-final-send
  py -3.11 wxwork_add_customer.py --phone 13800138000 --dry-run
  py -3.11 wxwork_add_customer.py --phone 13800138000 --no-final-send --no-launch --foreground-wait-sec 90
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src.add_customer import AddCustomerOcrOptions, AddCustomerRatios, add_customer_by_phone
from src.window import ensure_wework_hwnd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _load_cfg(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="WXWork: add external customer by phone")
    ap.add_argument("--phone", required=True, help="Mobile number (digits)")
    ap.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.yaml")
    ap.add_argument(
        "--skip-nav",
        action="store_true",
        help="Skip 通讯录 + 新的客户；假定当前已在「新的客户」主列表页",
    )
    ap.add_argument(
        "--no-final-send",
        action="store_true",
        help="点完搜索结果「添加」后停止，不点「发送添加邀请」里的「发送」",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印坐标，不点击")
    ap.add_argument(
        "--no-launch",
        action="store_true",
        help="找不到窗口时不执行 WXWork.exe（默认会尝试启动以从托盘唤醒进程）",
    )
    ap.add_argument(
        "--no-ocr",
        action="store_true",
        help="禁用 OCR，全部使用比例坐标（调试用）",
    )
    ap.add_argument(
        "--allow-ratio-fallback",
        action="store_true",
        help="OCR 模式下允许识别失败时回退比例坐标；默认关闭以避免异常窗口状态误点",
    )
    ap.add_argument(
        "--foreground-wait-sec",
        type=float,
        default=12.0,
        help="启动后等待人工切到企业微信前台的秒数；只检测，不主动唤起窗口",
    )
    ap.add_argument(
        "--diag-dir",
        type=Path,
        default=Path("logs") / "add_customer",
        help="OCR 未命中时保存失败截图和 OCR 框数据的目录（默认 logs/add_customer）",
    )
    ap.add_argument(
        "--invite-message",
        default=None,
        help="自定义邀请语（留空则使用企微默认文案）；填写后脚本会先点「清空」再输入该文案",
    )
    ap.add_argument(
        "--platform-url",
        default=None,
        help="Agent Console 后端 URL（如 http://127.0.0.1:8000），用于步骤事件上报",
    )
    ap.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="平台线索 ID（lead_id），与 --platform-url 配合使用",
    )
    args = ap.parse_args()

    cfg = _load_cfg(args.config)
    ac = dict(cfg.get("add_customer") or {})
    if args.no_ocr:
        ac["use_ocr"] = False
    ratios = AddCustomerRatios.from_dict(ac)
    ocr_opts = AddCustomerOcrOptions.from_dict(ac)

    hwnd = ensure_wework_hwnd(launch_if_missing=not args.no_launch)
    if not hwnd:
        print(
            "未找到企业微信主窗口 (WeWorkWindow)。"
            "请确认已登录；若在托盘仅图标，去掉 --no-launch 让脚本尝试启动 WXWork.exe 唤醒。",
            flush=True,
        )
        return 2

    diag_dir = args.diag_dir.resolve()
    print(f">>> 添加客户: phone={args.phone!r}  diag_dir={diag_dir}", flush=True)
    try:
        add_customer_by_phone(
            hwnd,
            args.phone,
            ratios,
            ocr_opts,
            skip_nav=args.skip_nav,
            no_final_send=args.no_final_send,
            dry_run=args.dry_run,
            allow_ratio_fallback=args.allow_ratio_fallback or args.no_ocr,
            require_foreground=True,
            foreground_wait_sec=args.foreground_wait_sec,
            diag_dir=diag_dir,
            invite_message=args.invite_message,
            task_id=args.task_id,
            platform_url=args.platform_url,
        )
    except Exception as e:
        print(f">>> 失败: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
