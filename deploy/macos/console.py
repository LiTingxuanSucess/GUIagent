"""Serve the existing console API and compiled frontend on one local port."""

import sys
from pathlib import Path

from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "WXWorkAgent"))

from web.backend.main import app  # noqa: E402

app.mount(
    "/",
    StaticFiles(directory=ROOT / "WXWorkAgent/web/frontend/dist", html=True),
    name="console",
)
