"""Page tool for Cursor CLI web recovery: one CDP, one existing target, no navigation.

python3 -m cn_ai.recovery_cdp --cdp URL --target ID --site SITE state
python3 -m cn_ai.recovery_cdp ... shot /tmp/geo-recovery/page.png [--frame captcha]
python3 -m cn_ai.recovery_cdp ... eval 'JS expression' [--frame captcha]
python3 -m cn_ai.recovery_cdp ... click X Y [--frame captcha]
python3 -m cn_ai.recovery_cdp ... drag X1 Y1 X2 Y2 [--frame captcha]
python3 -m cn_ai.recovery_cdp ... drags 'X1,Y1,X2,Y2;X1,Y1,X2,Y2' [--frame captcha]
python3 -m cn_ai.recovery_cdp ... ready --journal PATH
Coordinates are page CSS pixels, or pixels inside the iframe when --frame is
given (the same numbers getBoundingClientRect returns in `eval --frame`).
Only this target's page socket is opened, so a busy browser with many tabs
does not slow the connection down.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import random
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import websocket

from cn_ai.web_recovery import HOSTS, evaluate_ready, fresh_protocol_auth, snapshot

FRAME_BOXES = r"""() => [...document.querySelectorAll('iframe')].map(e => {
  const r = e.getBoundingClientRect();
  return {url: (e.src || '').slice(0, 160), box: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}};
}).filter(f => f.box.width > 2 && f.box.height > 2)"""


def list_targets(base):
    # A loaded host (load average 90+) can stall the DevTools HTTP endpoint for
    # several seconds; one slow read must not end a recovery step.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(base + "/json/list", timeout=25) as response:
                return json.load(response)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1)


def is_wenxin_login(url, site):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (site == "yiyan" and parsed.scheme == "https"
            and host == "wappass.baidu.com" and parsed.path == "/passport/login")


def on_site(url, site):
    # Wenxin's existing target can redirect to Baidu's mobile login page.
    # This is a recovery-tool exception only; answer/journal URLs stay strict.
    if is_wenxin_login(url, site):
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in HOSTS.get(site, ()))


class Page:
    def __init__(self, cdp, target, site, wait=20):
        base = cdp.rstrip("/")
        deadline = time.time() + wait
        while True:
            targets = list_targets(base)
            info = next((t for t in targets if t.get("id") == target and t.get("type") == "page"), None)
            if not info or not info.get("webSocketDebuggerUrl"):
                raise LookupError("target_missing")
            # A tab the server just opened is blank until its first load.
            url = info.get("url", "")
            loading = url in ("", "about:blank") or url.startswith("chrome-error:")
            if not loading or on_site(url, site) or time.time() >= deadline:
                break
            time.sleep(1)
        self.url = info.get("url", "")
        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self.next_id = 0
        self.children = {}

    def send(self, method, params=None, session=None, timeout=20):
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        # Event traffic keeps every recv() short, so the socket timeout alone
        # never ends a call whose reply does not come.
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"{method} no reply in {timeout}s")
            reply = json.loads(self.ws.recv())
            if reply.get("method") == "Target.attachedToTarget":
                child = reply["params"]
                self.children[child["sessionId"]] = child["targetInfo"].get("url", "")
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"].get("message", "cdp_error"))
                return reply.get("result", {})

    def evaluate(self, expression, *arg, session=None, context=None, read_only=False):
        source = f"({expression})({json.dumps(arg[0])})" if arg else expression
        params = {"expression": source, "returnByValue": True, "awaitPromise": True}
        if read_only:
            # V8 enforces this for model-supplied inspection expressions. An
            # eval must never manufacture the evidence later used by ready.
            params.update(throwOnSideEffect=True, awaitPromise=False)
        if context:
            params["contextId"] = context
        result = self.send("Runtime.evaluate", params, session)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "evaluate_failed"))
        return result.get("result", {}).get("value")

    def frame_context(self, needle):
        """Return (session, context id) for the first frame whose URL contains needle."""
        # Same-site iframes share the page process and get an isolated world.
        frames = [self.send("Page.getFrameTree")["frameTree"]]
        while frames:
            node = frames.pop()
            if needle in node["frame"].get("url", "") and node["frame"].get("parentId"):
                world = self.send("Page.createIsolatedWorld", {"frameId": node["frame"]["id"], "worldName": "geo-recovery"})
                return None, world["executionContextId"]
            frames.extend(node.get("childFrames", []))
        # Cross-site captcha iframes run out of process and are only reachable
        # as auto-attached child sessions.
        self.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        deadline = time.time() + 2
        while time.time() < deadline:
            for session, url in self.children.items():
                if needle in url:
                    return session, None
            self.send("Runtime.evaluate", {"expression": "1"})
        return None, None

    def mouse(self, kind, x, y, buttons=0):
        self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": buttons, "clickCount": 1})


def drag(page, x1, y1, x2, y2):
    # Slider captchas (Aliyun nc rejected a 0.6s straight line) score the
    # trajectory: hold, accelerate then decelerate over about a second with
    # small wobble, overshoot a few pixels and settle back on the end point.
    page.mouse("mouseMoved", x1 - random.uniform(3, 8), y1 + random.uniform(-3, 3))
    time.sleep(random.uniform(0.08, 0.16))
    page.mouse("mouseMoved", x1, y1)
    page.mouse("mousePressed", x1, y1, buttons=1)
    time.sleep(random.uniform(0.12, 0.25))
    overshoot = random.uniform(3, 7) if x2 >= x1 else -random.uniform(3, 7)
    steps = random.randint(38, 52)
    for step in range(1, steps + 1):
        progress = step / steps
        eased = progress * progress * (3 - 2 * progress)
        x = x1 + (x2 + overshoot - x1) * eased
        y = y1 + (y2 - y1) * eased + random.uniform(-1.5, 1.5)
        page.mouse("mouseMoved", x, y, buttons=1)
        time.sleep(random.uniform(0.012, 0.03) if 0.2 < progress < 0.8 else random.uniform(0.025, 0.05))
    for step in range(1, 4):
        page.mouse("mouseMoved", x2 + overshoot * (1 - step / 3), y2 + random.uniform(-1, 1), buttons=1)
        time.sleep(random.uniform(0.04, 0.08))
    time.sleep(random.uniform(0.1, 0.2))
    page.mouse("mouseReleased", x2, y2)


def frame_text(page, needle):
    """The captcha's own text after an action, e.g. the drop counter."""
    if not needle:
        return {}
    try:
        session, context = page.frame_context(needle)
        if session is None and context is None:
            return {"frame_text": "(frame gone)"}
        return {"frame_text": page.evaluate("(document.body && document.body.innerText || '').slice(0, 300)", session=session, context=context)}
    except Exception as error:
        return {"frame_text": f"(unreadable: {type(error).__name__})"}


def page_state(page, site):
    state = {"url": page.evaluate("location.href"), "title": page.evaluate("document.title")}
    state.update(snapshot(page, site))
    if is_wenxin_login(state["url"], site):
        # Passport's split OTP inputs do not use the chat-page selectors.
        # Its login route always remains a login wall, never auth evidence.
        state.update(logged_out=True, account_marker=False, editor_ready=False)
    # DeepSeek's account CSS changes often. A fresh successful current-user
    # response provides identity evidence while credentials stay in the page.
    if (site == "deepseek" and not state.get("account_marker")
            and not any(state.get(key) for key in ("challenge_visible", "logged_out", "waiting_sms"))
            and fresh_protocol_auth(page, site)):
        state["account_marker"] = True
        state["auth_evidence"] = "fresh_user_api"
    state["frames"] = page.evaluate(FRAME_BOXES, None)
    state["text"] = page.evaluate("(document.body && document.body.innerText || '').slice(0, 1500)")
    return state


def journal_ready(page, site, target, journal_path):
    """Same ready gate the server will use after recovery-candidate."""
    path = Path(journal_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return {"ok": False, "ready": False, "error": "bad_journal", "reason": "recovery_unconfirmed",
                "error_code": "recovery_unconfirmed"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "ready": False, "error": "bad_journal", "reason": "recovery_unconfirmed",
                "error_code": "recovery_unconfirmed"}
    if not isinstance(data, dict) or data.get("site_id") != site:
        return {"ok": False, "ready": False, "error": "journal_site_mismatch", "reason": "recovery_unconfirmed",
                "error_code": "recovery_unconfirmed"}
    wanted = data.get("target_id") or ""
    if wanted and wanted != target:
        return {"ok": False, "ready": False, "reason": "target_changed", "error_code": "recovery_unconfirmed",
                "target_match": False, "send_state": data.get("send_state"),
                "challenge_visible": False, "logged_out": False}
    current = snapshot(page, site)
    authenticated = bool(current.get("account_marker"))
    if not authenticated and not any(current.get(k) for k in ("challenge_visible", "waiting_sms", "logged_out")):
        authenticated = fresh_protocol_auth(page, site)
    decided = evaluate_ready(current, data, authenticated=authenticated, require_fresh_auth=True)
    decided.update(ok=True, target_match=True, target_id=wanted or target,
                   send_state=data.get("send_state"), prior_logged_in=bool(data.get("prior_logged_in")))
    return decided


def run(args):
    page = Page(args.cdp, args.target, args.site)
    if not on_site(page.url, args.site):
        return {"ok": False, "error": "target_not_site_page", "url": page.url[:120]}
    if args.command == "state":
        return {"ok": True, **page_state(page, args.site)}
    if args.command == "ready":
        if not args.journal:
            return {"ok": False, "ready": False, "error": "bad_arguments", "usage": "ready --journal PATH"}
        return journal_ready(page, args.site, args.target, args.journal)
    if args.command in ("shot", "eval") and len(args.args) != 1:
        return {"ok": False, "error": "bad_arguments", "usage": "shot PATH | eval 'JS'"}
    if args.command == "shot":
        box = None
        if args.frame:
            box = next((f["box"] for f in page.evaluate(FRAME_BOXES, None) if args.frame in f["url"]), None)
            if box is None:
                return {"ok": False, "error": "frame_missing"}
        data = base64.b64decode(page.send("Page.captureScreenshot", {"format": "png"})["data"])
        if box:
            from PIL import Image
            image = Image.open(io.BytesIO(data))
            scale = image.width / max(1, page.evaluate("innerWidth"))
            image.crop(tuple(round(v * scale) for v in (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]))).save(args.args[0])
        else:
            with open(args.args[0], "wb") as out:
                out.write(data)
        return {"ok": True, "path": args.args[0], **({"box": box} if box else {})}
    if args.command == "eval":
        session = context = None
        if args.frame:
            session, context = page.frame_context(args.frame)
            if session is None and context is None:
                return {"ok": False, "error": "frame_missing"}
        return {"ok": True, "value": page.evaluate(args.args[0], session=session, context=context, read_only=True)}
    if args.command == "phone":
        if len(args.args) != 1 or not re.fullmatch(r"1[0-9]{10}", args.args[0]):
            return {"ok": False, "error": "bad_arguments", "usage": "phone BOUND_ACCOUNT_PHONE"}
        # Keep the one allowed form edit separate from read-only eval. This
        # never edits account markers, verification state, or SMS code inputs.
        filled = page.evaluate(r"""phone => {
          const inputs=[...document.querySelectorAll('input[type=tel],input[placeholder*=手机],input[aria-label*=手机]')];
          const e=inputs.find(e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2&&!e.disabled&&!e.readOnly&&!/验证码/.test(e.placeholder||'');});
          if(!e)return false;
          const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          set.call(e,phone);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
          return true;
        }""", args.args[0])
        return {"ok": filled is True, **({} if filled else {"error": "phone_input_missing"})}
    offset = (0, 0)
    if args.frame:
        box = next((f["box"] for f in page.evaluate(FRAME_BOXES, None) if args.frame in f["url"]), None)
        if box is None:
            return {"ok": False, "error": "frame_missing"}
        offset = (box["x"], box["y"])
    if args.command == "drags":
        moves = [[float(v) for v in item.split(",")] for item in " ".join(args.args).replace(";", " ").split()]
        if not moves or any(len(move) != 4 for move in moves):
            return {"ok": False, "error": "bad_arguments", "usage": "drags 'X1,Y1,X2,Y2;X1,Y1,X2,Y2'"}
        for x1, y1, x2, y2 in moves:
            drag(page, x1 + offset[0], y1 + offset[1], x2 + offset[0], y2 + offset[1])
            time.sleep(random.uniform(0.5, 0.9))
        return {"ok": True, "drags": len(moves), **frame_text(page, args.frame), **page_state(page, args.site)}
    numbers = [float(value) for value in args.args]
    numbers = [value + offset[index % 2] for index, value in enumerate(numbers)]
    if args.command == "click" and len(numbers) == 2:
        page.mouse("mouseMoved", *numbers)
        page.mouse("mousePressed", *numbers, buttons=1)
        page.mouse("mouseReleased", *numbers)
    elif args.command == "drag" and len(numbers) == 4:
        drag(page, *numbers)
    else:
        return {"ok": False, "error": "bad_arguments"}
    time.sleep(0.6)
    return {"ok": True, **frame_text(page, args.frame), **page_state(page, args.site)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--frame", default="")
    parser.add_argument("--journal", default="")
    parser.add_argument("command", choices=("state", "shot", "eval", "phone", "click", "drag", "drags", "ready"))
    parser.add_argument("args", nargs="*")
    args = parser.parse_args(argv)

    def expire(signum, frame):
        raise TimeoutError("tool_deadline")

    # The Cursor CLI moves a shell command that runs past about 30s to the
    # background, and the model then spends minutes polling it instead of
    # reading the page again.
    previous_handler = signal.signal(signal.SIGALRM, expire)
    previous_alarm = signal.alarm(28)
    began = time.monotonic()
    try:
        result = run(args)
    except LookupError as error:
        result = {"ok": False, "error": str(error)}
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_alarm:
            signal.alarm(max(1, previous_alarm - int(time.monotonic() - began)))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
