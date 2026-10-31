import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from playwright.sync_api import TimeoutError as PwTimeout

from cn_ai import budget
from cn_ai.diagnostics import DiagnosticRun
from cn_ai.site_flows import PlatformFlowError, _wait_kimi_composer, send_prompt


class KimiSendReadinessTest(unittest.TestCase):
    def test_ambiguous_click_never_falls_back_to_enter_even_without_journal(self):
        for callback in (None, Mock()):
            page, editor, send = Mock(), Mock(), Mock()
            editor.evaluate.return_value = '完整的问题'
            page.locator.return_value.first = send
            send.count.return_value = 1
            send.is_visible.return_value = True
            send.click.side_effect = PwTimeout('click acknowledgement lost')
            with self.subTest(journal=callback is not None), \
                 patch('cn_ai.site_flows._wait_kimi_composer', return_value=editor), \
                 patch('cn_ai.site_flows.find_input', return_value=editor), \
                 patch.dict('cn_ai.site_flows.SITE_PREP', {'kimi': lambda _page: 'skip'}), \
                 patch('cn_ai.site_flows.dismiss_kimi_notice', return_value=False), \
                 patch('cn_ai.site_flows.detect_logged_out'), \
                 patch('cn_ai.site_flows.detect_platform_failure'), \
                 patch('cn_ai.site_flows._dismiss_overlays'), \
                 patch('cn_ai.site_flows._kimi_submission_state', return_value={'length': 0, 'generating': False}), \
                 patch('cn_ai.site_flows.time.sleep'):
                with self.assertRaises(PlatformFlowError) as caught:
                    send_prompt(page, '完整的问题', 'kimi', new_chat=False, on_send_state=callback)
            self.assertEqual(caught.exception.error_code, 'recovery_unconfirmed')
            self.assertEqual(send.click.call_count, 1)
            self.assertNotIn('Enter', [call.args[0] for call in page.keyboard.press.call_args_list])

    def test_delayed_editor_waits_before_marking_any_submission(self):
        editor = object()
        with patch("cn_ai.site_flows.find_input", side_effect=[None, None, editor]), \
             patch("cn_ai.web_recovery.snapshot", return_value={}), \
             patch("cn_ai.site_flows.detect_platform_failure"), \
             patch("cn_ai.site_flows.budget.sleep") as sleep:
            self.assertIs(_wait_kimi_composer(object()), editor)
        self.assertEqual(sleep.call_count, 2)

    def test_missing_editor_is_pre_send_retryable_failure(self):
        with patch("cn_ai.site_flows.find_input", return_value=None), \
             patch("cn_ai.web_recovery.snapshot", return_value={}), \
             patch("cn_ai.site_flows.detect_platform_failure"), \
             patch("cn_ai.site_flows.budget.remaining", return_value=0):
            with self.assertRaises(PlatformFlowError) as raised:
                _wait_kimi_composer(object())
        self.assertEqual(raised.exception.error_code, "platform_busy")
        self.assertTrue(raised.exception.retryable)

    def test_login_required_shell_does_not_wait_or_submit(self):
        with patch("cn_ai.site_flows.find_input", return_value=None), \
             patch("cn_ai.web_recovery.snapshot", return_value={"logged_out": True}), \
             patch("cn_ai.site_flows.budget.sleep") as sleep:
            with self.assertRaises(PlatformFlowError) as raised:
                _wait_kimi_composer(object())
        self.assertEqual(raised.exception.error_code, "platform_auth_unavailable")
        sleep.assert_not_called()

    def test_composer_wait_cannot_outlast_the_available_send_window(self):
        with patch("cn_ai.site_flows.find_input", return_value=None), \
             patch("cn_ai.web_recovery.snapshot", return_value={}), \
             patch("cn_ai.site_flows.detect_platform_failure"), \
             patch("cn_ai.site_flows.time.monotonic", side_effect=[100, 101.1]), \
             patch("cn_ai.site_flows.budget.remaining", return_value=1), \
             patch("cn_ai.site_flows.budget.sleep") as sleep:
            with self.assertRaises(PlatformFlowError) as raised:
                _wait_kimi_composer(object())
        self.assertEqual(raised.exception.error_code, "platform_busy")
        sleep.assert_not_called()

    def test_unconfirmed_input_never_enters_sending_state(self):
        for value in ("文字只输入了一半", RuntimeError("renderer unavailable")):
            editor = Mock()
            if isinstance(value, Exception):
                editor.evaluate.side_effect = value
            else:
                editor.evaluate.return_value = value
            page, send_state = Mock(), Mock()
            with self.subTest(value=type(value).__name__), \
                 patch("cn_ai.site_flows.find_input", return_value=editor), \
                 patch.dict("cn_ai.site_flows.SITE_PREP", {"kimi": lambda _page: "skip"}), \
                 patch("cn_ai.site_flows.dismiss_kimi_notice", return_value=False), \
                 patch("cn_ai.site_flows.detect_logged_out"), \
                 patch("cn_ai.site_flows.detect_platform_failure"), \
                 patch("cn_ai.site_flows._dismiss_overlays"), \
                 patch("cn_ai.site_flows.time.sleep"):
                with self.assertRaises(PlatformFlowError):
                    send_prompt(page, "完整的问题", "kimi", new_chat=False,
                                on_send_state=send_state)
            send_state.assert_not_called()
            self.assertNotIn("Enter", [call.args[0] for call in page.keyboard.press.call_args_list])

    def test_browser_progress_keeps_input_phase_and_never_saves_prompt(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "GEO_DIAGNOSTIC_DIR": folder, "GEO_DIAGNOSTICS_ENABLED": "1",
        }):
            run = DiagnosticRun("kimi", "fixture", "private fixture prompt")
            run.phase("01_send")
            run.browser_progress("composer_focused")
            manifest = json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "01_send")
            self.assertEqual(manifest["send_step"], "composer_focused")
            self.assertTrue(manifest["browser_available"])
            self.assertTrue(manifest["checkpoint_at"])
            self.assertNotIn("private fixture prompt", json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
