import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

from cn_ai.share_links import _open_fresh_context, create_public_share
from cn_ai.site_flows import _kimi_toggle_state


class RecoverySpeedBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_kimi_delayed_share_copy_returns_only_observed_public_url(self):
        context = self.browser.new_context()
        page = context.new_page()
        url = "https://www.kimi.com/share/fixture123456"
        requests = []

        def route(request):
            if request.request.url.endswith("CreateChatShare"):
                requests.append(request.request.url)
                request.fulfill(content_type="application/json", body=json.dumps({"url": url}))
            else:
                request.fulfill(content_type="text/html; charset=utf-8", body='''<div class="segment-assistant-actions">
                  <button class="icon-button" onclick="setTimeout(()=>{document.getElementById('copy').hidden=false},100)">
                  <svg name="Share_a"></svg></button></div>
                  <button id="copy" hidden onclick="fetch('/api/CreateChatShare').then(r=>r.json()).then(r=>navigator.clipboard.writeText(r.url))">复制链接</button>''')

        context.route("https://www.kimi.com/**", route)
        try:
            page.goto("https://www.kimi.com/chat/fixture")
            result = create_public_share(page, "kimi")
            self.assertEqual(result["url"], url)
            self.assertEqual(len(requests), 1)
        finally:
            context.close()

    def test_public_verification_loads_answer_scripts_without_image_downloads_or_cookies(self):
        hits, cookies = [], []
        answer = "这是这次问题对应的完整公开回答，可以核对分享内容。"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                cookies.append(self.headers.get("Cookie"))
                if self.path == "/answer.js":
                    content = "document.getElementById('answer').textContent=" + json.dumps(answer)
                    kind = "application/javascript"
                else:
                    content = '<div id="answer"></div><img src="/heavy.png"><script src="/answer.js"></script>'
                    kind = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            text, _ = _open_fresh_context(self.browser, f"http://127.0.0.1:{server.server_port}/share", ["完整公开回答"], timeout_sec=3)
            self.assertIn(answer, text)
            self.assertIn("/answer.js", hits)
            self.assertNotIn("/heavy.png", hits)
            self.assertFalse(any(cookies))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_kimi_toggle_retains_explicit_and_unknown_states(self):
        context = self.browser.new_context()
        page = context.new_page()
        try:
            page.set_content('''<button id="checked" aria-checked="true"></button>
              <button id="pressed" aria-pressed="false"></button>
              <input id="input" type="checkbox" checked>
              <div id="row" class="toolkit-item toolkit-item--selected" role="menuitem"></div>
              <button id="unknown"></button>''')
            for name, expected in [("checked", True), ("pressed", False), ("input", True), ("row", True), ("unknown", None)]:
                self.assertIs(_kimi_toggle_state(page.locator("#" + name)), expected)
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
