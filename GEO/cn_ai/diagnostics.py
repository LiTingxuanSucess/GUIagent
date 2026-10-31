"""Bounded, private browser evidence. Recording never changes scraper outcomes."""
from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from cn_ai import budget

_current = contextvars.ContextVar("geo_diagnostics", default=None)
SAFE_QUERY = {"action", "subtype", "verify_scene", "lang"}
SECRET = re.compile(r"token|password|passwd|secret|api.?key|authorization|cookie|session.?id|access.?key", re.I)


def current():
    return _current.get()


def number(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def safe_url(value):
    try:
        u = urlsplit(str(value))
        if u.scheme not in ("http", "https", "ws", "wss"):
            return "" if u.scheme in ("data", "javascript") else str(value)[:500]
        host = u.netloc.rsplit("@", 1)[-1]
        query = [(k, v if k in SAFE_QUERY and re.fullmatch(r"[\w.-]{0,64}", v) else "REDACTED")
                 for k, v in parse_qsl(u.query, keep_blank_values=True)[:40]]
        return urlunsplit((u.scheme, host, u.path, urlencode(query), ""))[:2000]
    except Exception:
        return "[invalid URL]"


def redact(value):
    value = str(value)
    value = re.sub(r"(?:https?|wss?)://[^\s<>\"']+", lambda m: safe_url(m[0]), value)
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value, flags=re.I)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT REDACTED]", value)
    value = re.sub(r'''(?i)((?:token|password|passwd|secret|api[_-]?key|authorization|cookie)\s*["']?\s*[:=]\s*)["']?[^\s,;<>"'}]+''', r"\1[REDACTED]", value)
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE REDACTED]", value)


def json_shape(value, depth=0):
    if depth > 4:
        return "truncated"
    if isinstance(value, dict):
        return {str(k)[:100]: "redacted" if SECRET.search(str(k)) else json_shape(v, depth+1)
                for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "item": json_shape(value[0], depth+1) if value else None}
    return "null" if value is None else type(value).__name__


DOM_SCRIPT = r"""() => {
  const source = document.documentElement;
  if (!source) return {html:'', text:'', storage_keys:[]};
  const root = source.cloneNode(true);
  root.querySelectorAll('script, input[type=hidden]').forEach(el => el.remove());
  const csp=document.createElement('meta');
  csp.httpEquiv='Content-Security-Policy';csp.content="default-src 'none'; style-src 'unsafe-inline'";
  (root.querySelector('head')||root).prepend(csp);
  root.querySelectorAll('input, textarea, [contenteditable=true]').forEach(el => {
    el.removeAttribute('value'); el.textContent='[INPUT REDACTED]';
  });
  const deadline=performance.now()+50;
  const all=[root, ...root.querySelectorAll('*')];
  let truncated=false;
  for (let i=0; i<all.length; i++) {
    if (i>15000 || performance.now()>deadline) {truncated=true;break;}
    for (const attr of [...all[i].attributes]) {
      if (/^on|^srcdoc$|token|secret|password|authorization|cookie|session.?id|api.?key/i.test(attr.name))
        all[i].removeAttribute(attr.name);
      else if (['href','src','action'].includes(attr.name)) {
        try {
          const u=new URL(attr.value,document.baseURI);
          u.username='';u.password='';u.hash='';
          for(const key of [...u.searchParams.keys()])
            if (!['action','subtype','verify_scene','lang'].includes(key)) u.searchParams.set(key,'REDACTED');
          all[i].setAttribute(attr.name,u.protocol==='data:'?'[DATA OMITTED]':u.href);
        } catch (_) {all[i].removeAttribute(attr.name);}
      }
    }
  }
  // Do not save partially scrubbed DOM; oversized pages still have metadata.
  let keys=[];try {keys=Object.keys(localStorage).slice(0,100);} catch(_) {}
  return {html:truncated?'':root.outerHTML.slice(0,524288),
    text:truncated?'':(root.textContent||'').slice(0,65536),
    storage_keys:keys, truncated, ready_state:document.readyState, title:document.title,
    viewport:{width:innerWidth,height:innerHeight,device_pixel_ratio:devicePixelRatio},
    browser:{user_agent:navigator.userAgent,language:navigator.language,platform:navigator.platform},
    scripts:[...document.scripts].filter(s=>s.src).slice(0,100).map(s=>s.src)};
}"""


class DiagnosticRun:
    def __init__(self, site, question_id, prompt):
        self.enabled = os.getenv("GEO_DIAGNOSTICS_ENABLED", "1") != "0"
        supplied = os.getenv("GEO_DIAGNOSTIC_RUN_DIR", "")
        data = os.getenv("GEO_DATA_DIR", str(Path(__file__).resolve().parents[1] / "server/data"))
        self.root = Path(os.getenv("GEO_DIAGNOSTIC_DIR", "") or Path(data) / "diagnostics")
        self.path = Path(supplied) if supplied else self.root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
        self.started = time.monotonic()
        self.last_snapshot = 0.0
        self.deadline_ms = number("GEO_DIAGNOSTIC_DEADLINE_MS", 0, 0, 10**14)
        self.meta = {"schema_version": 1, "diagnostic_id": self.path.name, "site_id": site,
                     "question_id": question_id, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                     "prompt_chars": len(prompt), "started_at": self.now(), "status": "running",
                     "phase": "process_start", "stages": [], "capture_errors": [],
                     "limitations": ["Network capture starts after CDP attachment; earlier traffic is unavailable.",
                                      "DOM and text are bounded and redacted; screenshots mask inputs.",
                                      "Network bodies are omitted; selected error fields and JSON shapes are retained.",
                                      "A last checkpoint may precede a hard timeout; inspect its timestamp."]}
        self.browser = None
        self.pages = []
        self.preferred_page = None
        self.network = deque(maxlen=150)
        self.console = deque(maxlen=50)
        self.pending = {}
        self.capturing = False
        self.failed = False
        self.last_failure_error = None
        self.keep_evidence = False

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def write(self, name, value):
        if not self.enabled:
            return
        try:
            self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.path.chmod(0o700)
            target = self.path / name
            temp = target.with_suffix(target.suffix + ".tmp")
            payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            temp.write_text(payload, encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(target)
        except Exception:
            pass

    def phase(self, name, **facts):
        if not self.enabled:
            return
        now = time.monotonic()
        if self.meta["stages"]:
            self.meta["stages"][-1]["elapsed_ms"] = round((now-self.meta["stages"][-1].pop("_clock", now))*1000)
        self.meta["phase"] = name
        self.meta["stages"].append({"phase": name, "at": self.now(), "_clock": now})
        self.meta.update({k: redact(v)[:500] if isinstance(v, str) else v for k, v in facts.items()})
        self.persist()

    def browser_progress(self, step):
        """Record a completed browser action without collecting another DOM.

        Callers invoke this only after the browser has answered. A timer or a
        still-pending CDP operation must never make a stalled page look alive.
        """
        if not self.enabled:
            return
        self.meta.update(browser_available=True, checkpoint_at=self.now(),
                         send_step=str(step)[:80])
        self.persist()

    def persist(self):
        now = time.monotonic()
        stages = []
        for stage in self.meta["stages"][-60:]:
            public = {k:v for k,v in stage.items() if k != "_clock"}
            if "_clock" in stage:
                public["elapsed_ms"] = round((now-stage["_clock"])*1000)
            stages.append(public)
        meta = {**self.meta, "elapsed_ms": round((time.monotonic()-self.started)*1000),
                "stages": stages}
        self.write("manifest.json", meta)
        self.write("network.json", [{k:v for k,v in record.items() if k != "_clock"} for record in self.network])
        self.write("console.json", list(self.console))

    def bind_browser(self, browser):
        if not self.enabled:
            return
        if self.browser is not browser:
            self.pages = []
            self.preferred_page = None
        self.browser = browser
        try:
            for context in browser.contexts:
                for page in context.pages:
                    self.bind_page(page)
                context.on("page", self.bind_page)
        except Exception:
            pass

    def bind_page(self, page):
        self.pages = [p for p in self.pages if not p.is_closed()]
        if not self.enabled or page in self.pages or len(self.pages) >= 3:
            return
        self.pages.append(page)
        def requested(req):
            try:
                if req.resource_type in ("image", "font", "media", "stylesheet"):
                    return
                record = {"url": safe_url(req.url), "method": req.method, "type": req.resource_type,
                          "started_at": self.now(), "_clock": time.monotonic(), "status": None}
                # Object identity separates requests to the same streaming URL.
                self.pending[id(req)] = record
                self.network.append(record)
                if len(self.pending) > 150:
                    self.pending.pop(next(iter(self.pending)))
            except Exception:
                pass
        def responded(res):
            try:
                rec = self.pending.get(id(res.request))
                if rec is not None:
                    rec.update(status=res.status, mime=res.headers.get("content-type", ""),
                               response_headers={k:v for k,v in res.headers.items() if k.lower() in ("content-type","retry-after","x-request-id","x-trace-id")})
            except Exception:
                pass
        def finished(req):
            try:
                rec = self.pending.pop(id(req), None)
                if rec is not None:
                    rec["elapsed_ms"] = round((time.monotonic()-rec.pop("_clock"))*1000)
                    if req.failure:
                        rec["failure"] = redact(req.failure)[:500]
            except Exception:
                pass
        page.on("request", requested)
        page.on("response", responded)
        page.on("requestfinished", finished)
        page.on("requestfailed", finished)
        def console_message(msg):
            try:
                if msg.type in ("warning", "error"):
                    self.console.append({"at": self.now(), "type": msg.type, "message": redact(msg.text)[:1000]})
            except Exception:
                pass
        def page_error(exc):
            try:
                self.console.append({"at": self.now(), "type": "pageerror", "message": redact(exc)[:1000]})
            except Exception:
                pass
        page.on("console", console_message)
        page.on("pageerror", page_error)

    def body(self, url, body, mime, status):
        try:
            value = json.loads(body[:32768])
            facts = {"json_shape": json_shape(value)}
            if isinstance(value, dict):
                facts["error_fields"] = {k:redact(v)[:500] for k,v in value.items()
                                         if k.lower() in ("code", "error_code", "message", "msg") and isinstance(v,(str,int))}
            for record in reversed(self.network):
                if record["url"] == safe_url(url):
                    record.update(facts)
                    break
        except Exception:
            pass

    def checkpoint(self, page=None, *, force=False, screenshot=False):
        try:
            return budget.call(lambda: self._checkpoint(page, force=force, screenshot=screenshot), 1.5,
                               reserve=.5, code="diagnostic_timeout")
        except budget.StageDeadline:
            self.capturing = False
            self.meta["capture_errors"].append("diagnostic_timeout")
            self.persist()

    def _checkpoint(self, page=None, *, force=False, screenshot=False):
        if not self.enabled or self.capturing:
            return
        near_deadline = self.deadline_ms and 0 < self.deadline_ms-time.time()*1000 < 5000
        if not force and time.monotonic()-self.last_snapshot < (2 if near_deadline else 20):
            return
        self.capturing = True
        self.last_snapshot = time.monotonic()
        try:
            if page is not None:
                self.bind_page(page)
                self.preferred_page = page
            selected = page or (self.preferred_page if self.preferred_page is not None and not self.preferred_page.is_closed() else None)
            selected = selected or next((p for p in self.pages if not p.is_closed()), None)
            self.meta["checkpoint_at"] = self.now()
            self.meta["browser_available"] = selected is not None
            if selected is not None:
                self.meta["url"] = safe_url(selected.url)
                frames = []
                all_frames = selected.frames
                priority = [f for f in all_frames[1:] if re.search(r"captcha|verify|login|sign.in", f.url, re.I)]
                chosen = list(dict.fromkeys(all_frames[:1] + priority + all_frames[1:]))[:4]
                self.meta["frame_inventory"] = [safe_url(f.url) for f in all_frames[:20]]
                for index, frame in enumerate(chosen):
                    entry = {"index": index, "url": safe_url(frame.url)}
                    try:
                        snapshot = frame.locator("html").evaluate(DOM_SCRIPT, timeout=700)
                        entry.update({k:redact(v) if isinstance(v,str) else v for k,v in snapshot.items() if k not in ("html", "text", "scripts")})
                        entry["scripts"] = [safe_url(u) for u in snapshot.get("scripts", [])]
                        self.write(f"frame-{index}.html", redact(snapshot["html"]))
                        self.write(f"frame-{index}.txt", redact(snapshot["text"]))
                    except Exception as exc:
                        entry["capture_error"] = type(exc).__name__
                    frames.append(entry)
                self.meta["frames"] = frames
                if screenshot or near_deadline:
                    try:
                        masks = [f.locator("input, textarea, [contenteditable=true], [class*='avatar'], [class*='user-info']") for f in all_frames[:20]]
                        target = self.path / "viewport.png"
                        selected.screenshot(path=str(target), timeout=1000, mask=masks, animations="disabled")
                        target.chmod(0o600)
                        if target.stat().st_size > 2*1024*1024:
                            target.unlink()
                            self.meta["capture_errors"].append("screenshot_size_limit")
                    except Exception as exc:
                        self.meta["capture_errors"].append("screenshot:"+type(exc).__name__)
            self.persist()
            self.export_har()
        except Exception as exc:
            self.meta["capture_errors"].append(type(exc).__name__)
        finally:
            self.capturing = False

    def public_share_checkpoint(self, page, matched):
        """Keep anonymous-page evidence separate from the signed-in answer."""
        if not self.enabled:
            return
        self.meta["public_share_probe"] = {"at":self.now(), "url":safe_url(page.url), "answer_matched":bool(matched)}
        if not matched:
            try:
                snapshot=page.locator("html").evaluate(DOM_SCRIPT,timeout=700)
                self.write("public-share.html",redact(snapshot.get("html", "")))
                self.write("public-share.txt",redact(snapshot.get("text", "")))
                self.meta["public_share_probe"]["title"]=redact(snapshot.get("title", ""))
                self.meta["public_share_probe"]["truncated"]=snapshot.get("truncated",False)
                target=self.path/"public-share.png"
                page.screenshot(path=str(target),timeout=1000,mask=[page.locator("input,textarea,[contenteditable=true]")],animations="disabled")
                target.chmod(0o600)
                if target.stat().st_size>2*1024*1024:target.unlink()
            except Exception as exc:
                self.meta["public_share_probe"]["capture_error"]=type(exc).__name__
        self.persist()

    def fail(self, error):
        if not self.enabled or error is self.last_failure_error:
            return
        self.last_failure_error = error
        self.failed = True
        self.meta.update(status="failed", failed_at=self.now(), failed_phase=self.meta["phase"], error=redact(error)[:2500],
                         error_type=type(error).__name__, error_code=getattr(error,"error_code",self.meta.get("error_code", "")))
        failures = self.meta.setdefault("failures", [])
        failures.append({k:self.meta[k] for k in ("failed_at", "failed_phase", "error", "error_type", "error_code")})
        self.meta["failures"] = failures[-5:]
        if isinstance(error, BaseException):
            self.meta["stack"] = redact("".join(traceback.format_exception(type(error), error, error.__traceback__)))[:8000]
        self.meta["upstream_request_ids"] = re.findall(r"(?i)(?:request.?id)[\s:'\"]+([A-Za-z0-9-]{8,64})", str(error))[:8]
        self.checkpoint(force=True, screenshot=True)
        self.persist()
        self.export_har()

    def export_har(self):
        entries=[]
        for rec in self.network:
            elapsed=rec.get("elapsed_ms", 0)
            entries.append({"startedDateTime": rec["started_at"], "time": elapsed,
                "request":{"method":rec["method"],"url":rec["url"],"httpVersion":"", "headers":[],"cookies":[],"queryString":[],"headersSize":-1,"bodySize":-1},
                "response":{"status":rec.get("status") or 0,"statusText":"","httpVersion":"", "headers":[{"name":k,"value":redact(v)} for k,v in rec.get("response_headers",{}).items()],"cookies":[],"content":{"size":-1,"mimeType":rec.get("mime") or "application/octet-stream"},"redirectURL":"","headersSize":-1,"bodySize":-1},
                "cache":{},"timings":{"send":-1,"wait":-1,"receive":-1},
                "_geo":{"resource_type":rec["type"],"incomplete": "elapsed_ms" not in rec,
                        "failure":rec.get("failure"),"json_shape":rec.get("json_shape"),"error_fields":rec.get("error_fields"),
                        "omitted":["request_headers","cookies","request_body","response_body","http_version","phase_timings"]}})
        self.write("network.har", {"log":{"version":"1.2","creator":{"name":"GEO diagnostics","version":"1"},"entries":entries}})

    @contextmanager
    def guard(self):
        try:
            yield
        except Exception as exc:
            self.fail(exc)
            raise

    def retain_quality_issue(self, issues, page):
        if not self.enabled:
            return
        self.keep_evidence = True
        self.meta["quality_issues"] = list(issues)
        self.checkpoint(page, force=True, screenshot=True)
        self.persist()

    def __enter__(self):
        if self.enabled:
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
                self.root.chmod(0o700)
                self.write(".active", str(os.getpid()))
                self.persist()
            except Exception:
                self.enabled = False
            if self.enabled:
                try:
                    prune(self.root, self.path)
                except Exception:
                    pass
        self.token = _current.set(self)
        return self

    def should_sample(self):
        percent = number("GEO_DIAGNOSTIC_SUCCESS_PERCENT", 5, 0, 100)
        return self.enabled and int(hashlib.sha256(self.path.name.encode()).hexdigest()[:8],16) % 100 < percent

    def __exit__(self, kind, exc, tb):
        try:
            if exc is not None:
                self.fail(exc)
            if self.failed and self.meta.get("outcome") == "succeeded":
                self.meta["status"] = "partial" if self.keep_evidence else "recovered"
            if self.enabled and not self.failed:
                if self.keep_evidence:
                    self.meta["status"] = "partial"
                    self.export_har()
                elif self.should_sample():
                    self.meta["status"] = "sampled_success"
                    self.export_har()
                else:
                    shutil.rmtree(self.path, ignore_errors=True)
            if self.path.exists():
                (self.path/".active").unlink(missing_ok=True)
                self.persist()
        except Exception:
            pass
        finally:
            _current.reset(self.token)


def prune(root, active):
    """Serialize retention across six processes; never remove a live run."""
    lock = root / ".retention.lock"
    with lock.open("a+", encoding="utf-8") as handle:
        lock.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        days=number("GEO_DIAGNOSTIC_KEEP_DAYS",3,1,30)
        limit=number("GEO_DIAGNOSTIC_MAX_MB",256,16,2048)*1024*1024
        max_runs=number("GEO_DIAGNOSTIC_MAX_RUNS",300,10,2000)
        candidates=[];total=0
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not any((directory/n).exists() for n in ("manifest.json", "process.json")):
                continue
            size=sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
            total+=size
            if directory == active or ((directory/".active").exists() and time.time()-directory.stat().st_mtime < 600):
                continue
            candidates.append((directory.stat().st_mtime,directory,size))
        remaining=len(candidates)
        for stamp,directory,size in sorted(candidates):
            if time.time()-stamp > days*86400 or total > limit or remaining > max_runs:
                shutil.rmtree(directory, ignore_errors=True)
                total-=size
                remaining-=1
