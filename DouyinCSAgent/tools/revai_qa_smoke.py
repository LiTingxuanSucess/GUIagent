"""Smoke test the RevAI Carzone respond API without touching Douyin."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.revai_qa import RevaiQaClient


def main() -> int:
    parser = argparse.ArgumentParser(description="RevAI respond API smoke test")
    parser.add_argument("--base-url", default="https://car.revai.cn")
    parser.add_argument("--endpoint", default="/api/project/carzone/compat/chat/respond")
    parser.add_argument("--session-id", default="douyin-smoke")
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    client = RevaiQaClient(
        base_url=args.base_url,
        endpoint=args.endpoint,
        timeout=args.timeout,
        session_prefix="smoke",
    )
    print(client.complete("", args.message, session_id=args.session_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
