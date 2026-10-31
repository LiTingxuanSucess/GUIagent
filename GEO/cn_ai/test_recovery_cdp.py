import io
import json
import signal
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from cn_ai import recovery_cdp


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.replies = []

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        expression = message["params"].get("expression", "")
        value = {"location.href": "https://www.kimi.com/chat/1", "document.title": "Kimi"}.get(expression, {})
        if expression.startswith("(" + recovery_cdp.FRAME_BOXES):
            value = [{"url": "https://www.kimi.com/captcha", "box": {"x": 100, "y": 50, "width": 300, "height": 200}}]
        self.replies.append(json.dumps({"id": message["id"], "result": {"result": {"value": value}}}))

    def recv(self):
        return self.replies.pop(0)


def targets(url):
    body = json.dumps([{"id": "T1", "type": "page", "url": url, "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/T1"}])
    return io.BytesIO(body.encode())


class RecoveryCDPTest(unittest.TestCase):
    def test_wenxin_bound_mobile_login_is_not_an_answer_page(self):
        from cn_ai.web_recovery import is_site_page
        url = 'https://wappass.baidu.com/passport/login?return_url=wenxin'
        self.assertTrue(recovery_cdp.on_site(url, 'yiyan'))
        page = Mock(url=url)
        self.assertFalse(is_site_page(page, 'yiyan'))
        for rejected in [
            'http://wappass.baidu.com/passport/login',
            'https://wappass.baidu.com.evil.example/passport/login',
            'https://evil.example/passport/login',
            'https://wappass.baidu.com/other',
        ]:
            with self.subTest(url=rejected):
                self.assertFalse(recovery_cdp.on_site(rejected, 'yiyan'))
        self.assertFalse(recovery_cdp.on_site(url, 'deepseek'))
        self.assertFalse(recovery_cdp.on_site(url, 'kimi'))

    def test_wenxin_passport_state_stays_logged_out_with_split_otp_inputs(self):
        url = 'https://wappass.baidu.com/passport/login?return_url=wenxin'
        page = Mock()
        page.evaluate.side_effect = lambda expression, *args: url if expression == 'location.href' else ''
        ambiguous = {'account_marker': True, 'editor_ready': True, 'logged_out': False,
                     'waiting_sms': False, 'challenge_visible': False}
        with patch.object(recovery_cdp, 'snapshot', return_value=ambiguous):
            current = recovery_cdp.page_state(page, 'yiyan')
        self.assertTrue(current['logged_out'])
        self.assertFalse(current['account_marker'])
        self.assertFalse(current['editor_ready'])

    def test_deepseek_state_uses_fresh_identity_without_overriding_login_walls(self):
        page = Mock()
        page.evaluate.return_value = ''
        current = {'account_marker':False,'challenge_visible':False,'logged_out':False,'waiting_sms':False,'editor_ready':True}
        with patch.object(recovery_cdp,'snapshot',return_value=current),patch.object(recovery_cdp,'fresh_protocol_auth',return_value=True) as auth:
            state=recovery_cdp.page_state(page,'deepseek')
            self.assertTrue(state['account_marker'])
            self.assertEqual(state['auth_evidence'],'fresh_user_api')
            auth.assert_called_once_with(page,'deepseek')
        for block in ('challenge_visible','logged_out','waiting_sms'):
            with patch.object(recovery_cdp,'snapshot',return_value={**current,block:True}),patch.object(recovery_cdp,'fresh_protocol_auth') as auth:
                self.assertFalse(recovery_cdp.page_state(page,'deepseek')['account_marker'])
                auth.assert_not_called()
        with patch.object(recovery_cdp,'snapshot',return_value=current),patch.object(recovery_cdp,'fresh_protocol_auth',return_value=False):
            self.assertFalse(recovery_cdp.page_state(page,'deepseek')['account_marker'])

    def call(self, url, *argv):
        socket = FakeSocket()
        out = io.StringIO()
        with patch.object(recovery_cdp.urllib.request, "urlopen", return_value=targets(url)), \
             patch.object(recovery_cdp.websocket, "create_connection", return_value=socket) as connect, \
             redirect_stdout(out):
            code = recovery_cdp.main(["--cdp", "http://127.0.0.1:1", "--site", "kimi", *argv])
        return code, json.loads(out.getvalue()), socket, connect

    def test_state_wraps_snapshot_function_with_site_argument(self):
        code, result, socket, connect = self.call("https://www.kimi.com/chat/1", "--target", "T1", "state")
        self.assertEqual((code, result["ok"], result["url"]), (0, True, "https://www.kimi.com/chat/1"))
        self.assertEqual(connect.call_args.args[0], "ws://127.0.0.1:1/devtools/page/T1")
        self.assertTrue(any(m["params"]["expression"].endswith('("kimi")') for m in socket.sent))

    def test_frame_coordinates_are_offset_to_the_page(self):
        with patch.object(recovery_cdp.time, "sleep"):
            code, _, socket, _ = self.call("https://www.kimi.com/", "--target", "T1", "--frame", "captcha", "click", "10", "20")
        presses = [m["params"] for m in socket.sent if m["method"] == "Input.dispatchMouseEvent" and m["params"]["type"] == "mousePressed"]
        self.assertEqual((code, presses[0]["x"], presses[0]["y"]), (0, 110, 70))

    def test_model_eval_is_read_only_at_the_browser_protocol(self):
        code, _, socket, _ = self.call('https://www.kimi.com/', '--target', 'T1', 'eval', 'document.title')
        self.assertEqual(code, 0)
        params = socket.sent[-1]['params']
        self.assertTrue(params['throwOnSideEffect'])
        self.assertFalse(params['awaitPromise'])

    def test_tool_deadline_does_not_leak_into_the_calling_process(self):
        before = signal.getsignal(signal.SIGALRM)
        self.call('https://www.kimi.com/', '--target', 'T1', 'state')
        self.assertIs(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_only_the_named_site_target_is_accepted(self):
        self.assertEqual(self.call("https://www.kimi.com/", "--target", "T2", "state")[1], {"ok": False, "error": "target_missing"})
        code, result, socket, _ = self.call("https://evil.example/", "--target", "T1", "click", "1", "2")
        self.assertEqual((code, result["error"]), (1, "target_not_site_page"))
        self.assertEqual(socket.sent, [])

    def test_ready_uses_journal_and_shared_gate(self):
        import tempfile
        from pathlib import Path
        journal = {
            "site_id": "kimi", "prompt": "hello", "send_state": "before_send",
            "phase": "before_send", "target_id": "T1", "prior_logged_in": True,
            "question_id": "job_x", "account_id": "acct_x",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acct.json"
            path.write_text(json.dumps(journal), encoding="utf-8")
            socket = FakeSocket()
            # Snapshot fields the ready gate needs.
            def send(self_sock, raw):
                message = json.loads(raw)
                self_sock.sent.append(message)
                expression = message["params"].get("expression", "")
                value = {"location.href": "https://www.kimi.com/", "document.title": "Kimi"}.get(expression)
                if expression.startswith("(site =>") or "account_marker" in expression:
                    value = {"editor_present": True, "editor_ready": True, "logged_out": False,
                             "account_marker": False, "challenge_visible": False, "waiting_sms": False,
                             "last_user": "", "user_count": 0, "user_turn_id": ""}
                self_sock.replies.append(json.dumps({"id": message["id"], "result": {"result": {"value": value}}}))
            socket.send = send.__get__(socket, FakeSocket)
            out = io.StringIO()
            with patch.object(recovery_cdp.urllib.request, "urlopen", return_value=targets("https://www.kimi.com/")), \
                 patch.object(recovery_cdp.websocket, "create_connection", return_value=socket), \
                 redirect_stdout(out):
                code = recovery_cdp.main(["--cdp", "http://127.0.0.1:1", "--site", "kimi", "--target", "T1",
                                          "--journal", str(path), "ready"])
            result = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(result["ready"], result)
            self.assertEqual(result["reason"], "ready")


if __name__ == "__main__":
    unittest.main()
