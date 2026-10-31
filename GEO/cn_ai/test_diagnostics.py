import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from cn_ai.diagnostics import DiagnosticRun, current, prune, redact, safe_url
from cn_ai.scraper import NetworkCapture
from cn_ai.agentbay_doubao import AgentBayDoubaoLease


class DiagnosticTest(unittest.TestCase):
    def test_startup_failure_keeps_stage_without_claiming_a_dom(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GEO_DIAGNOSTIC_DIR": folder}):
            run = DiagnosticRun("doubao", "job-test", "私有问题")
            with self.assertRaises(RuntimeError):
                with run, run.guard():
                    run.phase("browser_initialize")
                    raise RuntimeError("startup failed token=credential-secret request id: TRACE-12345678")
            manifest = json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "browser_initialize")
            self.assertFalse(manifest["browser_available"])
            self.assertEqual(manifest["upstream_request_ids"], ["TRACE-12345678"])
            self.assertNotIn("credential-secret", json.dumps(manifest))
            self.assertNotIn("私有问题", json.dumps(manifest, ensure_ascii=False))
            self.assertIsNone(current())
            self.assertEqual(run.path.stat().st_mode & 0o777, 0o700)

    def test_capture_failure_does_not_replace_the_original_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            blocked = Path(folder) / "not-a-directory"
            blocked.write_text("occupied", encoding="utf-8")
            with patch.dict(os.environ, {"GEO_DIAGNOSTIC_DIR": str(blocked)}):
                run = DiagnosticRun("kimi", "job-test", "问题")
                with self.assertRaisesRegex(RuntimeError, "original failure"):
                    with run, run.guard():
                        raise RuntimeError("original failure")
                self.assertFalse(run.enabled)

    def test_retention_removes_old_owned_runs_and_preserves_live_and_unrelated_dirs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "old"
            old.mkdir()
            (old / "manifest.json").write_text("{}", encoding="utf-8")
            os.utime(old, (time.time()-4*86400, time.time()-4*86400))
            active = root / "live"
            active.mkdir()
            (active / "manifest.json").write_text("{}", encoding="utf-8")
            (active / ".active").write_text("1", encoding="utf-8")
            unrelated = root / "unrelated"
            unrelated.mkdir()
            prune(root, active)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(unrelated.exists())

    def test_urls_keep_challenge_type_but_remove_credentials(self):
        url = safe_url("https://user:password@example.com/captcha?token=secret&subtype=semantic_reasoning#access_token=secret")
        self.assertNotIn("secret", url)
        self.assertNotIn("password", url)
        self.assertIn("subtype=semantic_reasoning", url)
        self.assertNotIn("credential", redact("Authorization: Bearer credential"))

    def test_success_sampling_and_recovered_failure_have_distinct_outcomes(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEO_DIAGNOSTIC_DIR": folder, "GEO_DIAGNOSTIC_SUCCESS_PERCENT": "0",
        }):
            with DiagnosticRun("kimi", "job-ok", "问题") as normal:
                normal.meta["outcome"] = "succeeded"
            self.assertFalse(normal.path.exists())
            with DiagnosticRun("kimi", "job-retry", "问题") as recovered:
                recovered.fail(RuntimeError("first connection failed"))
                recovered.meta["outcome"] = "succeeded"
            manifest = json.loads((recovered.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "recovered")
            self.assertEqual(manifest["error"], "first connection failed")
            with patch.dict(os.environ, {"GEO_DIAGNOSTIC_SUCCESS_PERCENT": "100"}):
                with DiagnosticRun("kimi", "job-sample", "问题") as sampled:
                    sampled.meta["outcome"] = "succeeded"
                manifest = json.loads((sampled.path / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "sampled_success")
                self.assertTrue((sampled.path / "network.har").exists())

    def test_lease_records_failure_before_deleting_cloud_session(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GEO_DIAGNOSTIC_DIR": folder}):
            order = []
            lease = AgentBayDoubaoLease(state_path=Path(folder)/"state.json")
            lease.session = SimpleNamespace(session_id="test-session")
            lease.client = SimpleNamespace(delete=lambda *args, **kwargs: order.append("delete") or SimpleNamespace(success=True))
            with DiagnosticRun("doubao", "job-cleanup", "问题") as run:
                with patch.object(run, "checkpoint", side_effect=lambda **kwargs: order.append("capture")):
                    error = RuntimeError("login failed")
                    lease.__exit__(RuntimeError, error, None)
            self.assertEqual(order, ["capture", "delete"])
            self.assertTrue((run.path/"manifest.json").exists())

    def test_retries_keep_failure_history_without_duplicate_exception_snapshots(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GEO_DIAGNOSTIC_DIR": folder}):
            with DiagnosticRun("doubao", "job-retries", "问题") as run:
                first = RuntimeError("initialization failed")
                run.phase("browser_initialize")
                run.fail(first)
                run.fail(first)
                run.phase("login_doubao")
                run.fail(RuntimeError("login failed"))
            manifest = json.loads((run.path/"manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["failures"]), 2)
            self.assertEqual(manifest["failures"][0]["failed_phase"], "browser_initialize")
            self.assertEqual(manifest["failed_phase"], "login_doubao")

    def test_real_browser_snapshot_is_redacted_and_does_not_mutate_the_page(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEO_DIAGNOSTIC_DIR": folder, "GEO_DIAGNOSTIC_SUCCESS_PERCENT": "0",
        }), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            def route(request):
                url = request.request.url
                if "/api/" in url:
                    request.fulfill(content_type="application/json", body=json.dumps({"code":429,"message":"Too many requests","token":"response-secret"}))
                elif "/verify" in url:
                    request.fulfill(content_type="text/html", body='<html><body>安全验证<input value="iframe-secret"></body></html>')
                else:
                    request.fulfill(content_type="text/html", body='''<html><head><title>登录检查</title><script>const token="bootstrap-secret";</script></head><body>
                      <input id="password" type="password" value="input-secret"><input type="hidden" value="hidden-secret">
                      <textarea>textarea-secret</textarea><div contenteditable="true">composer-secret</div>
                      <iframe src="http://diagnostics.test/verify?token=iframe-url-secret&subtype=semantic_reasoning"></iframe>
                      <iframe srcdoc='<input value="srcdoc-secret">'></iframe>
                    </body></html>''')
            page.route("http://diagnostics.test/**", route)
            run = DiagnosticRun("doubao", "job-browser", "问题")
            with self.assertRaises(RuntimeError):
                with run, run.guard():
                    run.bind_browser(browser)
                    capture = NetworkCapture()
                    capture.attach(page)
                    page.goto("http://diagnostics.test/login?token=url-secret")
                    page.evaluate("localStorage.setItem('access_token','storage-secret')")
                    page.evaluate("console.error('token=console-secret')")
                    page.evaluate("fetch('/api/error?token=request-secret',{headers:{Authorization:'Bearer header-secret'}}).then(r=>r.json())")
                    page.wait_for_timeout(100)
                    run.phase("login_doubao")
                    run.checkpoint(page, force=True)
                    raise RuntimeError("token=exception-secret")
            self.assertEqual(page.locator("#password").input_value(), "input-secret")
            self.assertTrue((run.path / "viewport.png").exists())
            manifest = json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["frames"]), 3)
            self.assertEqual(manifest["frames"][0]["storage_keys"], ["access_token"])
            evidence = "\n".join(f.read_text(encoding="utf-8") for f in run.path.iterdir() if f.suffix != ".png")
            for secret in ["input-secret","hidden-secret","bootstrap-secret","textarea-secret","composer-secret","storage-secret","console-secret","header-secret","response-secret","request-secret","url-secret","iframe-secret","iframe-url-secret","exception-secret","srcdoc-secret"]:
                self.assertNotIn(secret, evidence)
            har = json.loads((run.path / "network.har").read_text(encoding="utf-8"))
            entries = har["log"]["entries"]
            self.assertTrue(any(e["response"]["status"] == 200 and "/api/error" in e["request"]["url"] for e in entries))
            self.assertIn("Content-Security-Policy", (run.path / "frame-0.html").read_text(encoding="utf-8"))
            browser.close()


if __name__ == "__main__":
    unittest.main()
