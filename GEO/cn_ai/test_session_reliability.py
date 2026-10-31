import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cn_ai import agentbay_doubao as cloud
from cn_ai.site_flows import PlatformFlowError, detect_logged_out, detect_platform_failure


class SessionReliabilityTest(unittest.TestCase):
    def test_successful_warm_session_reuses_browser_without_create_or_init(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            cloud._write_state(path, {"session_id":"warm", "context_id":"saved-login", "context_name":"geo-test", "login_seeded":"true", "warm_until":str(time.time()+60)})
            session = MagicMock()
            session.session_id = "warm"
            session.keep_alive.return_value = SimpleNamespace(success=True)
            session.browser.get_endpoint_url.return_value = "ws://test.invalid"
            client = MagicMock()
            client.get.return_value = SimpleNamespace(session=session)
            lease = cloud.AgentBayDoubaoLease(state_path=path)
            with patch.dict(os.environ, {"AGENTBAY_API_KEY":"test-key", "GEO_AGENTBAY_WARM_SEC":"90"}), patch(
                "agentbay.AgentBay", return_value=client
            ), patch.object(lease, "_acquire_slot"), patch.object(lease, "_acquire_startup_slot") as startup, patch.object(lease, "_start_browser") as init:
                with lease:
                    self.assertTrue(lease.reused)
                    self.assertFalse(lease.needs_login_seed)
                    lease.keep_session = True
                startup.assert_not_called()
                init.assert_not_called()
            client.create.assert_not_called()
            client.delete.assert_not_called()
            self.assertIsNone(lease._lock)
            state = cloud._read_state(path)
            self.assertEqual(state["session_id"], "warm")
            self.assertGreater(float(state["warm_until"]), time.time())

    def test_expired_warm_session_is_deleted_before_fresh_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            cloud._write_state(path, {"session_id":"expired", "context_id":"saved-login", "context_name":"geo-test", "warm_until":"1"})
            old, fresh = MagicMock(), MagicMock()
            old.session_id = "expired"
            fresh.session_id = "fresh"
            fresh.keep_alive.return_value = SimpleNamespace(success=True)
            fresh.browser.get_endpoint_url.return_value = "ws://test.invalid"
            client = MagicMock()
            client.get.return_value = SimpleNamespace(session=old)
            client.delete.return_value = SimpleNamespace(success=True)
            client.create.return_value = SimpleNamespace(session=fresh)
            events=[]
            client.delete.side_effect=lambda *args,**kwargs: events.append("delete") or SimpleNamespace(success=True)
            client.create.side_effect=lambda *args,**kwargs: events.append("create") or SimpleNamespace(session=fresh)
            lease = cloud.AgentBayDoubaoLease(state_path=path)
            with patch.dict(os.environ, {"AGENTBAY_API_KEY":"test-key"}), patch("agentbay.AgentBay",return_value=client), patch.object(
                lease,"_acquire_slot"
            ), patch.object(lease,"_acquire_startup_slot"), patch.object(lease,"_start_browser",return_value=""):
                with lease:
                    self.assertFalse(lease.reused)
            self.assertEqual(events[:2], ["delete","create"])
            self.assertEqual(client.create.call_args.args[0].browser_context.context_id,"saved-login")

    def test_warm_success_is_deleted_when_later_flow_raises(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{"GEO_AGENTBAY_WARM_SEC":"90"}):
            lease=cloud.AgentBayDoubaoLease(state_path=Path(directory)/"state.json")
            lease.session=SimpleNamespace(session_id="warm")
            lease.client=MagicMock()
            lease.client.delete.return_value=SimpleNamespace(success=True)
            lease.context_id="saved"
            lease.endpoint="ws://test.invalid"
            lease.keep_session=True
            lease.__exit__(RuntimeError,RuntimeError("flow failed"),None)
            lease.client.delete.assert_called_once()
            self.assertNotIn("session_id",cloud._read_state(lease.path))

    def test_browser_restart_preserves_context_and_sets_idle_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = cloud.AgentBayDoubaoLease(state_path=Path(directory) / "state.json")
            client = MagicMock()
            client.context.get.return_value = SimpleNamespace(context=SimpleNamespace(id="saved-login"))
            sessions = [MagicMock(), MagicMock()]
            for index, session in enumerate(sessions):
                session.session_id = f"session-{index}"
                session.keep_alive.return_value = SimpleNamespace(success=True)
                session.browser.get_endpoint_url.return_value = "ws://test.invalid"
            client.create.side_effect = [SimpleNamespace(session=s) for s in sessions]
            client.delete.return_value = SimpleNamespace(success=True)
            with patch.dict(os.environ, {"AGENTBAY_API_KEY": "test-key", "GEO_AGENTBAY_IDLE_RELEASE_SEC": "180"}), patch(
                "agentbay.AgentBay", return_value=client
            ), patch.object(lease, "_acquire_slot"), patch.object(
                lease, "_start_browser", side_effect=["not ready", "not ready", ""]
            ), patch.object(cloud.time, "sleep"):
                with lease:
                    self.assertEqual(lease.context_id, "saved-login")
                    self.assertEqual(lease.endpoint, "ws://test.invalid")
            client.context.get.assert_called_once()
            self.assertEqual(client.create.call_count, 2)
            for call in client.create.call_args_list:
                self.assertEqual(call.args[0].browser_context.context_id, "saved-login")
                self.assertEqual(call.args[0].idle_release_timeout, 180)

    def test_profile_selection_prefers_logged_in_storage_only_account(self):
        from cn_ai.test_agentbay_doubao import _load_run_scrape
        from cn_ai import volcengine_doubao
        run = _load_run_scrape()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "accounts.json").write_text(json.dumps({"accounts": [
                {"id": "stale", "phone": "preferred", "enabled": True, "sites": {"kimi": {"status": "not_logged_in"}}},
                {"id": "live", "phone": "other", "enabled": True, "sites": {"kimi": {"status": "logged_in"}}},
                {"id": "disabled", "enabled": False, "sites": {"kimi": {"status": "logged_in"}}},
            ]}), encoding="utf-8")
            with patch.dict(os.environ, {"GEO_DATA_DIR": directory, "GEO_AI_PROFILE_ROOT": directory}), patch.object(
                volcengine_doubao, "profile_site_cookies", side_effect=lambda p, s: [{"name":"stale"}] if p.name == "stale" else []
            ), patch.object(volcengine_doubao, "profile_site_storage", side_effect=lambda p, s: {"https://www.kimi.com":{"access_token":"live"}} if p.name == "live" else {}):
                profile, cookies = run._profile_for_site("preferred", "kimi")
            self.assertEqual(profile.name, "live")
            self.assertEqual(cookies, [])

    def test_failed_connect_releases_created_session_and_all_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = cloud.AgentBayDoubaoLease(state_path=Path(directory) / "state.json")
            client = MagicMock()
            client.delete.return_value = SimpleNamespace(success=True)

            def fail_connect():
                lease.client = client
                lease.session = SimpleNamespace(session_id="created")
                lease.context_id = "persistent"
                lease.context_name = "geo-test"
                raise RuntimeError("browser startup failed")

            with patch.object(lease, "_acquire_slot"), patch.object(lease, "_connect", side_effect=fail_connect):
                with self.assertRaisesRegex(RuntimeError, "startup"):
                    lease.__enter__()
            client.delete.assert_called_once_with(SimpleNamespace(session_id="created"), sync_context=True)
            self.assertIsNone(lease._lock)
            self.assertIsNone(lease._slot)
            state = cloud._read_state(lease.path)
            self.assertEqual(state["context_id"], "persistent")
            self.assertNotIn("session_id", state)

    def test_state_write_failure_still_releases_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = cloud.AgentBayDoubaoLease(state_path=Path(directory) / "state.json")
            with patch.object(lease, "_acquire_slot"), patch.object(lease, "_connect"):
                lease.__enter__()
            lease.context_id = "persistent"
            with patch.object(cloud, "_write_state", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    lease.__exit__(None, None, None)
            self.assertIsNone(lease._lock)

    def test_profile_lock_has_a_deadline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GEO_AGENTBAY_WAIT_SEC": "0"}):
            path = Path(directory) / "state.json"
            first = cloud.AgentBayDoubaoLease(state_path=path)
            second = cloud.AgentBayDoubaoLease(state_path=path)
            with patch.object(first, "_acquire_slot"), patch.object(first, "_connect"):
                with first:
                    with patch.object(second, "_connect") as connect:
                        with self.assertRaises(cloud.AgentBayDoubaoError):
                            second.__enter__()
                        connect.assert_not_called()
                    self.assertIsNone(second._lock)

    def test_failed_delete_keeps_session_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = cloud.AgentBayDoubaoLease(state_path=Path(directory) / "state.json")
            lease.client = MagicMock()
            lease.client.delete.return_value = SimpleNamespace(success=False)
            lease.session = SimpleNamespace(session_id="still-alive")
            lease.context_id = "persistent"
            lease.needs_login_seed = False
            lease.__exit__(None, None, None)
            state = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(state["session_id"], "still-alive")
            self.assertEqual(state["login_seeded"], "true")
            self.assertEqual(lease.path.stat().st_mode & 0o777, 0o600)

    def test_risk_stops_immediately_without_solver_wait(self):
        page = MagicMock()
        page.evaluate.return_value = {"text": "请完成安全验证", "challenge_visible": True, "challenge_urls": ["https://verify.snssdk.com/captcha"]}
        with patch.object(cloud.time, "sleep") as sleep:
            with self.assertRaises(PlatformFlowError):
                detect_platform_failure(page, "doubao")
            sleep.assert_not_called()
        page.evaluate.assert_called_once()

    def test_deepseek_sign_in_is_auth_failure(self):
        page = SimpleNamespace(url="https://chat.deepseek.com/sign_in")
        with self.assertRaises(PlatformFlowError) as error:
            detect_logged_out(page, "deepseek")
        self.assertEqual(error.exception.error_code, "platform_auth_unavailable")


if __name__ == "__main__":
    unittest.main()
