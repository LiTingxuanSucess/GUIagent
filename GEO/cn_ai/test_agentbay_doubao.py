from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from datetime import datetime

from cn_ai.agentbay_doubao import (
    _recent_request_ids,
    _yuanbao_phone_filled,
    doubao_text_logged_in,
    yuanbao_entry_failure,
    yuanbao_page_kind,
    _latest_sms_code,
    is_agentbay_disconnect,
    latest_doubao_sms_code,
    use_agentbay_for_doubao,
)
from cn_ai.volcengine_doubao import (
    VolcengineDoubaoLease,
    browser_cdp_url,
    cookies_for_site,
    doubao_browser_backend,
)


def _load_run_scrape():
    path = Path(__file__).resolve().parents[1] / "server" / "scripts" / "run_scrape.py"
    spec = importlib.util.spec_from_file_location("geo_run_scrape", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentBayDoubaoTest(unittest.TestCase):
    def test_agentbay_is_the_doubao_fallback_after_local_chrome(self):
        self.assertTrue(use_agentbay_for_doubao("doubao", "local"))
        self.assertTrue(use_agentbay_for_doubao("doubao", ""))
        self.assertTrue(use_agentbay_for_doubao("doubao", "agentbay"))
        self.assertTrue(use_agentbay_for_doubao("doubao", "cloud"))
        self.assertFalse(use_agentbay_for_doubao("kimi", "agentbay"))
        self.assertFalse(use_agentbay_for_doubao("doubao", "local-only"))
        self.assertFalse(use_agentbay_for_doubao("doubao", "off"))

    def test_latest_sms_code_ignores_stale_and_other_senders(self):
        messages = [
            {
                "requestid": "new",
                "receiver": "13800138000",
                "result": "【豆包】验证码111111，用于手机验证码登录。",
                "code": "111112",
            },
            {
                "requestid": "kimi",
                "receiver": "13800138000",
                "result": "【月之暗面】验证码111113，用于Kimi登录。",
                "code": "111114",
            },
            {
                "requestid": "old",
                "receiver": "13800138000",
                "result": "【豆包】验证码111115，用于手机验证码登录。",
                "code": "111116",
            },
        ]
        self.assertEqual(
            latest_doubao_sms_code(
                messages,
                "13800138000",
                {"old"},
            ),
            "111112",
        )

    def test_yuanbao_sms_ignores_doubao_codes(self):
        messages = [
            {
                "requestid": "doubao",
                "receiver": "13800138000",
                "result": "【豆包】验证码111112，用于手机验证码登录。",
                "code": "111117",
            },
            {
                "requestid": "yuanbao",
                "receiver": "13800138000",
                "result": "【腾讯元宝】您正在使用手机登录，验证码为：111118，5分钟内有效！",
                "code": "111118",
            },
        ]
        self.assertEqual(
            _latest_sms_code(messages, "13800138000", "元宝"),
            "111118",
        )

    def test_yuanbao_phone_fill_accepts_separated_digits(self):
        self.assertTrue(_yuanbao_phone_filled("+86 13800138000", "13800138000"))
        self.assertFalse(_yuanbao_phone_filled("请输入手机号", "13800138000"))

    def test_doubao_homepage_composer_is_not_logged_in(self):
        self.assertFalse(doubao_text_logged_in("登录\n发消息\n新对话\n用户协议"))
        self.assertTrue(doubao_text_logged_in("用户ab12\n发消息或按住空格说话"))

    def test_yuanbao_home_hero_is_not_logged_in(self):
        self.assertEqual(yuanbao_page_kind("Hi, 我是元宝\n未登录\n请登录后输入内容", True), "logged_out")
        self.assertEqual(yuanbao_page_kind("Hi, 我是元宝"), "unknown")
        self.assertEqual(yuanbao_page_kind("发消息\n新对话"), "logged_in")
        self.assertEqual(yuanbao_page_kind("用户3f87\n今天能帮你做什么？"), "logged_in")
        self.assertEqual(yuanbao_page_kind("选择最符合描述的图片\n黄色玻璃花瓶"), "captcha")

    def test_blank_yuanbao_page_is_not_an_auth_failure(self):
        self.assertEqual(
            yuanbao_entry_failure("", False),
            ("platform_busy", "元宝页面没有就绪，请稍后重试"),
        )
        self.assertIsNone(yuanbao_entry_failure("未登录\n请登录后输入内容", True))
        self.assertEqual(
            yuanbao_entry_failure("页面已打开但没有登录按钮也没有账号标识内容", False)[0],
            "platform_auth_unavailable",
        )

    def test_recent_yuanbao_code_is_reused_during_countdown(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ids = _recent_request_ids(
            [{
                "requestid": "fresh",
                "receiver": "13800138000",
                "result": "【腾讯元宝】验证码",
                "time": now,
            }],
            "13800138000",
            "元宝",
        )
        self.assertEqual(ids, {"fresh"})

    def test_sms_code_can_be_parsed_from_message_text(self):
        messages = [
            {
                "requestid": "new",
                "receiver": "13800138000",
                "result": "【豆包】验证码 111119，5分钟内有效。",
            }
        ]
        self.assertEqual(
            latest_doubao_sms_code(messages, "13800138000"),
            "111119",
        )

    def test_local_success_does_not_start_agentbay(self):
        run_scrape = _load_run_scrape()
        calls = []

        def local_call():
            calls.append("local")
            return {"ok": True, "answer": "6"}

        def agentbay_call():
            calls.append("agentbay")
            return {"ok": True, "answer": "cloud"}

        result = run_scrape.run_doubao_with_fallback(local_call, agentbay_call, True)
        self.assertEqual(calls, ["local"])
        self.assertEqual(result["browser_backend"], "local")
        self.assertEqual(result["answer"], "6")

    def test_local_auth_failure_falls_back_to_agentbay(self):
        run_scrape = _load_run_scrape()
        calls = []

        def local_call():
            calls.append("local")
            return {
                "ok": False,
                "error_code": "platform_auth_unavailable",
                "error": "平台登录已失效，请重新登录",
            }

        def agentbay_call():
            calls.append("agentbay")
            return {"ok": True, "answer": "6"}

        result = run_scrape.run_doubao_with_fallback(local_call, agentbay_call, True)
        self.assertEqual(calls, ["local", "agentbay"])
        self.assertEqual(result["browser_backend"], "agentbay")
        self.assertIn("平台登录已失效", result["local_error"])

    def test_disabled_fallback_preserves_the_local_failure(self):
        run_scrape = _load_run_scrape()

        def agentbay_call():
            raise AssertionError("AgentBay must stay unused")

        result = run_scrape.run_doubao_with_fallback(
            lambda: {"ok": False, "error": "logged_out"},
            agentbay_call,
            False,
        )
        self.assertFalse(result["ok"])
        self.assertNotIn("browser_backend", result)

    def test_volcengine_is_a_direct_backend(self):
        self.assertEqual(doubao_browser_backend("volcengine"), "volcengine")
        self.assertEqual(doubao_browser_backend("huoshan"), "volcengine")
        self.assertEqual(doubao_browser_backend("local"), "agentbay")
        self.assertEqual(doubao_browser_backend("local-only"), "local")
        self.assertFalse(use_agentbay_for_doubao("doubao", "volcengine"))

    def test_other_sites_use_volcengine_and_only_their_cookies(self):
        run_scrape = _load_run_scrape()
        for site in ("deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"):
            self.assertEqual(run_scrape.browser_route(site, "volcengine"), "volcengine")
        for site in ("deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"):
            self.assertEqual(run_scrape.browser_route(site, "agentbay"), "agentbay")
        self.assertEqual(run_scrape.browser_route("yuanbao", "local"), "local")
        import tempfile

        from cn_ai.volcengine_doubao import profile_site_cookies

        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / ".session-store.json"
            store.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "hy_token",
                                "value": "kept",
                                "domain": ".tencent.com",
                                "path": "/",
                                "expires": 4_000_000_000,
                                "http_only": True,
                                "secure": True,
                                "same_site": "None",
                            },
                            {
                                "name": "old",
                                "value": "drop",
                                "domain": ".tencent.com",
                                "path": "/",
                                "expires": 1,
                            },
                            {
                                "name": "other",
                                "value": "drop",
                                "domain": ".example.com",
                                "path": "/",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            selected = profile_site_cookies(Path(directory), "yuanbao")
            store = Path(directory) / ".session-store.json"
            saved = json.loads(store.read_text(encoding="utf-8"))
            saved["local_storage"] = {
                "https://www.kimi.com": [["access_token", "kept"], ["other", "no"]],
                "https://chat.deepseek.com": [["ds", "no"]],
            }
            saved["cookies"].append(
                {
                    "name": "HWWAFSESID",
                    "value": "waf",
                    "domain": "chat.deepseek.com",
                    "path": "/",
                }
            )
            saved["cookies"].append(
                {
                    "name": "ds_session_id",
                    "value": "session",
                    "domain": "chat.deepseek.com",
                    "path": "/",
                }
            )
            store.write_text(json.dumps(saved), encoding="utf-8")
            from cn_ai.volcengine_doubao import profile_site_storage

            storage = profile_site_storage(Path(directory), "kimi")
            deepseek = run_scrape._seed_cookies(
                "deepseek", profile_site_cookies(Path(directory), "deepseek")
            )
            leveldb = Path(directory) / "Default" / "Local Storage" / "leveldb"
            leveldb.mkdir(parents=True)
            origin = "https://chat.deepseek.com"
            payload = b'{"value":"abc","__version":"1"}'
            seq = (10 << 8) | 1
            record = (
                origin.encode()
                + b"\x00\x01userToken"
                + seq.to_bytes(8, "little")
                + b"\x01"
                + payload
            )
            (leveldb / "111120.log").write_bytes(record + b"\x87\xff")
            deepseek_storage = profile_site_storage(Path(directory), "deepseek")
        self.assertEqual([item["name"] for item in selected], ["hy_token"])
        self.assertEqual(list(storage), ["https://www.kimi.com"])
        self.assertEqual(storage["https://www.kimi.com"]["access_token"], "kept")
        self.assertEqual([item["name"] for item in deepseek], ["ds_session_id"])
        self.assertEqual(
            deepseek_storage["https://chat.deepseek.com"]["userToken"],
            payload.decode(),
        )
        self.assertTrue(selected[0]["httpOnly"])
        self.assertEqual(selected[0]["sameSite"], "None")
        cookies = cookies_for_site(
            [
                {"name": "ds", "value": "1", "domain": ".deepseek.com", "path": "/"},
                {"name": "k", "value": "1", "domain": ".kimi.com", "path": "/"},
                {"name": "q", "value": "1", "domain": "www.qianwen.com", "path": "/"},
                {"name": "empty", "value": "1", "domain": ".example.com", "path": "/"},
            ],
            "kimi",
        )
        self.assertEqual([item["name"] for item in cookies], ["k"])
        doubao_cookies = cookies_for_site(
            [
                {"name": "sessionid", "value": "1", "domain": ".doubao.com", "path": "/"},
                {"name": "k", "value": "1", "domain": ".kimi.com", "path": "/"},
            ],
            "doubao",
        )
        self.assertEqual([item["name"] for item in doubao_cookies], ["sessionid"])
        repaired = cookies_for_site(
            [
                {
                    "name": "sessionid",
                    "value": "1",
                    "domain": ".doubao.com",
                    "path": "/",
                    "sameSite": "None",
                    "secure": False,
                }
            ],
            "doubao",
        )
        self.assertEqual(repaired[0]["sameSite"], "Lax")
        missing = cookies_for_site(
            [
                {
                    "name": "sessionid",
                    "value": "1",
                    "domain": ".doubao.com",
                    "path": "/",
                    "secure": False,
                }
            ],
            "doubao",
        )
        self.assertEqual(missing[0]["sameSite"], "Lax")

    def test_volcengine_job_does_not_start_local_or_agentbay(self):
        run_scrape = _load_run_scrape()
        calls = []

        def volcengine_call():
            calls.append("volcengine")
            return {"ok": True, "answer": "6"}

        result = run_scrape.run_doubao(
            lambda: calls.append("local"),
            lambda: calls.append("agentbay"),
            volcengine_call,
            "volcengine",
        )
        self.assertEqual(calls, ["volcengine"])
        self.assertEqual(result["browser_backend"], "volcengine")

    def test_browser_cdp_keeps_the_sandbox_authorization(self):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"data":{"cdp_url":"ws://browser.example/cdp"}}'

        cdp = browser_cdp_url(
            "https://sandbox.example/path?faasInstanceName=box&Authorization=token",
            opener=lambda request, timeout=15: _Response(),
        )
        self.assertTrue(cdp.startswith("ws://browser.example/cdp?"))
        self.assertIn("Authorization=token", cdp)

    def test_browser_cdp_waits_until_chromium_replaces_the_placeholder(self):
        bodies = iter(
            [
                b'{"data":{"cdp_url":"ws://browser.example/cdp/devtools/browser/default"}}',
                b'{"data":{"cdp_url":"ws://browser.example/cdp/devtools/browser/real-id"}}',
            ]
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return next(bodies)

        cdp = browser_cdp_url(
            "https://sandbox.example/?Authorization=token",
            opener=lambda request, timeout=15: _Response(),
        )
        self.assertIn("/real-id", cdp)
        self.assertNotIn("/default", cdp)

    def test_lease_deletes_the_sandbox_when_the_job_ends(self):
        import os
        import tempfile

        from cn_ai import volcengine_doubao

        deleted = []

        def fake_create(opener):
            return {
                "session_id": "s-1",
                "endpoint": "https://sandbox.example/?Authorization=token",
                "tool_id": "t-1",
            }

        def fake_cdp(endpoint, opener):
            return "ws://browser.example/cdp"

        def fake_delete(tool_id, session_id, opener):
            deleted.append((tool_id, session_id))

        with tempfile.TemporaryDirectory() as directory:
            os.environ["GEO_VOLCENGINE_LOCK"] = str(
                Path(directory) / "volc.lock"
            )
            original = (
                volcengine_doubao.create_session,
                volcengine_doubao.browser_cdp_url,
                volcengine_doubao.delete_session,
            )
            volcengine_doubao.create_session = fake_create
            volcengine_doubao.browser_cdp_url = fake_cdp
            volcengine_doubao.delete_session = fake_delete
            try:
                with VolcengineDoubaoLease() as lease:
                    self.assertEqual(lease.cdp_url, "ws://browser.example/cdp")
            finally:
                (
                    volcengine_doubao.create_session,
                    volcengine_doubao.browser_cdp_url,
                    volcengine_doubao.delete_session,
                ) = original
                os.environ.pop("GEO_VOLCENGINE_LOCK", None)
        self.assertEqual(deleted, [("t-1", "s-1")])

    def test_kept_lease_leaves_the_sandbox_for_the_next_question(self):
        import os
        import tempfile

        from cn_ai import volcengine_doubao

        deleted = []

        def fake_create(opener):
            return {
                "session_id": "s-keep",
                "endpoint": "https://sandbox.example/?Authorization=token",
                "tool_id": "t-keep",
            }

        def fake_cdp(endpoint, opener):
            return "ws://browser.example/cdp"

        def fake_delete(tool_id, session_id, opener):
            deleted.append((tool_id, session_id))

        with tempfile.TemporaryDirectory() as directory:
            os.environ["GEO_VOLCENGINE_LOCK"] = str(Path(directory) / "volc.lock")
            original = (
                volcengine_doubao.create_session,
                volcengine_doubao.browser_cdp_url,
                volcengine_doubao.delete_session,
            )
            volcengine_doubao.create_session = fake_create
            volcengine_doubao.browser_cdp_url = fake_cdp
            volcengine_doubao.delete_session = fake_delete
            try:
                with VolcengineDoubaoLease() as lease:
                    lease.keep = True
            finally:
                (
                    volcengine_doubao.create_session,
                    volcengine_doubao.browser_cdp_url,
                    volcengine_doubao.delete_session,
                ) = original
                os.environ.pop("GEO_VOLCENGINE_LOCK", None)
        self.assertEqual(deleted, [])

    def test_warm_session_expires_before_it_can_be_reused(self):
        import os
        import tempfile

        from cn_ai.volcengine_doubao import load_warm_session, save_warm_session

        with tempfile.TemporaryDirectory() as directory:
            os.environ["GEO_VOLCENGINE_WARM"] = str(Path(directory) / "warm.json")
            try:
                save_warm_session("s", "t", "ws://browser.example/cdp", 20)
                fresh = load_warm_session()
                self.assertEqual(fresh["session_id"], "s")
                fresh["created_at"] = 1
                Path(os.environ["GEO_VOLCENGINE_WARM"]).write_text(
                    json.dumps(fresh), encoding="utf-8"
                )
                self.assertIsNone(load_warm_session())
            finally:
                os.environ.pop("GEO_VOLCENGINE_WARM", None)

    def test_expired_warm_session_is_forgotten_when_delete_is_rejected(self):
        import os
        import tempfile
        from unittest.mock import patch

        from cn_ai import volcengine_doubao as cloud

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warm.json"
            os.environ["GEO_VOLCENGINE_WARM"] = str(path)
            try:
                cloud.save_warm_session("s", "t", "ws://browser.example/cdp", 20)
                record = json.loads(path.read_text(encoding="utf-8"))
                record["created_at"] = 1
                path.write_text(json.dumps(record), encoding="utf-8")
                with patch.object(
                    cloud,
                    "_api",
                    side_effect=cloud.VolcengineDoubaoError(
                        "火山引擎沙箱DeleteSession失败: Could not find operation DeleteSession for version 2025-10-30."
                    ),
                ):
                    cloud.release_expired_warm_session()
                self.assertFalse(path.exists())
                self.assertEqual(
                    cloud.VolcengineDoubaoError("sandbox").error_code,
                    "site_request_failed",
                )
            finally:
                os.environ.pop("GEO_VOLCENGINE_WARM", None)

    def test_unknown_operation_is_retried_on_the_published_host(self):
        import io
        from email.message import EmailMessage
        from unittest.mock import patch
        from urllib.error import HTTPError

        from cn_ai.volcengine_doubao import _api

        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {"Result": {"SessionId": "s-ok", "Endpoint": "https://sandbox.example"}}
                ).encode()

        def opener(request, timeout=40):
            calls.append(request.full_url)
            if "open.volcengineapi.com" in request.full_url:
                body = json.dumps(
                    {
                        "ResponseMetadata": {
                            "Error": {
                                "Message": "Could not find operation DeleteSession for version 2025-10-30."
                            }
                        }
                    }
                ).encode()
                raise HTTPError(
                    request.full_url,
                    400,
                    "bad",
                    EmailMessage(),
                    io.BytesIO(body),
                )
            return _Response()

        with patch.dict(
            "os.environ",
            {
                "VOLCENGINE_ACCESS_KEY": "ak",
                "VOLCENGINE_SECRET_KEY": "sk",
                "GEO_VOLCENGINE_API_HOST": "",
            },
        ):
            result = _api("DeleteSession", {"ToolId": "t", "SessionId": "s"}, opener=opener)
        self.assertEqual(result["SessionId"], "s-ok")
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("open.volcengineapi.com", calls[0])

    def test_cloud_leases_actually_overlap_and_release_slots(self):
        import os
        import tempfile
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from cn_ai import volcengine_doubao as cloud

        barrier = threading.Barrier(3)
        deleted = []
        def work():
            with cloud.VolcengineDoubaoLease():
                barrier.wait(timeout=2)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GEO_VOLCENGINE_LOCK": str(Path(directory) / "cloud.lock"),
            "GEO_VOLCENGINE_CONCURRENCY": "3", "GEO_VOLCENGINE_WAIT_SEC": "1",
        }), patch.object(cloud, "create_session", return_value={
            "session_id": "test", "tool_id": "tool", "endpoint": "https://test.invalid",
        }), patch.object(cloud, "browser_cdp_url", return_value="ws://test.invalid"), patch.object(
            cloud, "delete_session", side_effect=lambda *args: deleted.append(args)
        ):
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(work) for _ in range(3)]
                for future in futures:
                    future.result(timeout=4)
            with cloud.VolcengineDoubaoLease():
                pass
        self.assertEqual(len(deleted), 4)

    def test_cloud_slot_limit_times_out_without_creating_another_session(self):
        import os
        import tempfile
        from unittest.mock import patch
        from cn_ai import volcengine_doubao as cloud
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GEO_VOLCENGINE_LOCK": str(Path(directory) / "cloud.lock"),
            "GEO_VOLCENGINE_CONCURRENCY": "1", "GEO_VOLCENGINE_WAIT_SEC": "0",
        }), patch.object(cloud, "create_session", return_value={
            "session_id": "test", "tool_id": "tool", "endpoint": "https://test.invalid",
        }) as create, patch.object(cloud, "browser_cdp_url", return_value="ws://test.invalid"), patch.object(cloud, "delete_session"):
            with cloud.VolcengineDoubaoLease():
                with self.assertRaises(cloud.VolcengineDoubaoError):
                    with cloud.VolcengineDoubaoLease():
                        pass
            self.assertEqual(create.call_count, 1)

    def test_storage_export_reuses_existing_tab_without_closing_it(self):
        from unittest.mock import MagicMock
        from cn_ai.volcengine_doubao import seed_origin_storage
        pw, browser, cloud_browser = MagicMock(), MagicMock(), MagicMock()
        page = MagicMock()
        page.url = "https://www.kimi.com/chat/test"
        page.evaluate.return_value = {"access_token": "test-only"}
        browser.contexts = [MagicMock()]
        browser.contexts[0].pages = [page]
        cloud_browser.contexts = [MagicMock()]
        pw.chromium.connect_over_cdp.return_value = browser
        self.assertEqual(seed_origin_storage(pw, "http://127.0.0.1:1000", cloud_browser, "https://www.kimi.com/"), 1)
        browser.contexts[0].new_page.assert_not_called()
        page.goto.assert_not_called()
        page.close.assert_not_called()
        cloud_browser.contexts[0].add_init_script.assert_called_once()

    def test_disconnect_errors_are_retryable_with_a_fresh_session(self):
        self.assertTrue(
            is_agentbay_disconnect(
                RuntimeError("Target page, context or browser has been closed")
            )
        )
        self.assertTrue(
            is_agentbay_disconnect(
                RuntimeError("Protocol error (Target.createTarget): Failed to open a new tab")
            )
        )
        self.assertTrue(is_agentbay_disconnect(RuntimeError("Session not found")))
        self.assertFalse(is_agentbay_disconnect(RuntimeError("验证码错误")))


if __name__ == "__main__":
    unittest.main()
