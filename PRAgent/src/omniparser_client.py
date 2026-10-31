"""
OmniParser HTTP client for screen element parsing.
Optional: when omniparserserver is running, use this for structured elements.
Falls back gracefully when server is unavailable.
"""
from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

import requests


class OmniParserClient:
    """Client for OmniParser HTTP API (omniparserserver)."""

    def __init__(self, url: str = "http://localhost:8000", timeout: int = 60):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def probe(self) -> bool:
        """Check if OmniParser server is available."""
        try:
            r = requests.get(f"{self.url}/probe", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def parse(self, screenshot: bytes | BytesIO) -> str | list[dict[str, Any]]:
        """
        Send screenshot to OmniParser, return structured elements.
        Returns empty list/string on failure (caller can fall back to DOM).
        """
        try:
            if isinstance(screenshot, BytesIO):
                screenshot = screenshot.getvalue()
            img_b64 = base64.b64encode(screenshot).decode("utf-8")

            # Try common API patterns used by OmniParser server
            for endpoint, payload in [
                ("/parse", {"image": img_b64}),
                ("/", {"image": img_b64}),
                ("/parse", {"screenshot": img_b64}),
            ]:
                try:
                    r = requests.post(
                        f"{self.url}{endpoint}",
                        json=payload,
                        timeout=self.timeout,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            return data
                        if isinstance(data, dict):
                            elem = data.get("elements") or data.get("parsed") or data.get("content")
                            if elem is not None:
                                return elem if isinstance(elem, str) else str(elem)
                        return str(data)
                except Exception:
                    continue

            return []
        except Exception:
            return []
