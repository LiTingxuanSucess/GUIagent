"""Remember explicit frontend authentication rejections without saving tokens."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


def token_value(raw):
    try:
        value = json.loads(raw).get("value")
        return value if isinstance(value, str) and value.strip() else ""
    except (TypeError, ValueError, AttributeError):
        return ""


def fingerprint(raw):
    value = token_value(raw)
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def cache_path():
    root = Path(os.getenv("GEO_AGENTBAY_STATE_DIR") or Path(__file__).resolve().parents[1]/".scrape_cache")
    return root/"deepseek-invalid-tokens.json"


def read_cache():
    try:
        value = json.loads(cache_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def rejected(raw):
    try:
        entry = read_cache().get(fingerprint(raw), {})
        return float(entry.get("expires_at", 0)) > time.time()
    except (TypeError, ValueError, AttributeError):
        return False


def remember_rejection(raw):
    key = fingerprint(raw)
    if not key:
        return
    temporary = None
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            entries = {k:v for k,v in read_cache().items() if isinstance(v,dict) and float(v.get("expires_at",0)) > time.time()}
            entries[key] = {"code":40003,"observed_at":time.time(),"expires_at":time.time()+1800}
            entries = dict(sorted(entries.items(),key=lambda item:item[1].get("observed_at",0))[-8:])
            temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(entries,handle,ensure_ascii=False)
            temporary.replace(path)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class DeepSeekAuthObserver:
    def __init__(self, storage, diagnostic=None):
        self.raw = (storage.get("https://chat.deepseek.com") or {}).get("userToken", "")
        self.value = token_value(self.raw)
        self.diagnostic = diagnostic

    def bind(self, context):
        context.on("requestfinished", self.observe)

    def observe(self, request):
        try:
            if not self.value or not request.url.startswith("https://chat.deepseek.com/api/v0/users/current"):
                return
            if request.headers.get("authorization") != "Bearer "+self.value:
                return  # Never blame this source token for another token's failure.
            response = request.response()
            if response is None:
                return
            body = response.json()
            if not isinstance(body,dict) or body.get("code") != 40003:
                return
            remember_rejection(self.raw)
            if self.diagnostic:
                self.diagnostic.meta["auth_rejection"] = {"code":40003,"reason":"invalid_token","credential_fingerprint":fingerprint(self.raw)[:16]}
                self.diagnostic.body(response.url,json.dumps(body,ensure_ascii=False),"application/json",response.status)
                self.diagnostic.persist()
        except Exception:
            pass
