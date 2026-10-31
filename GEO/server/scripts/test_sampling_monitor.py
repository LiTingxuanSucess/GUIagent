import json
from pathlib import Path
import tempfile
import unittest

import sampling_monitor as monitor


class SamplingMonitorTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = {"batch_dir": str(self.root), "jobs_dir": str(self.root)}
        self.state = self.root / "notification.json"
        self.manifest = {"batch_id": "batch", "sites": ["kimi", "doubao"], "samples": [
            {"site": "kimi", "question_id": "Q201", "round": 1, "status": "complete"},
            {"site": "doubao", "question_id": "Q202", "round": 3,
             "status": "waiting_manual", "job_id": "job123"}]}
        self.write("manifest.json", self.manifest)
        self.write("progress.json", {"updated_at": "2026-10-22T14:00:00+08:00"})
        self.write("job123.json", {"status": "waiting_manual", "started_at": "2026-10-22T13:00:00+08:00",
                   "manual_states": {"doubao": {"error_code": "platform_risk_control", "send_state": "sending",
                                                   "reason": "请完成安全验证"}}, "ak": "PRIVATE_KEY"})
        self.now = monitor.stamp("2026-10-22T14:30:00+08:00")

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_precise_blocker_stale_heartbeat_and_no_private_data(self):
        text, state = monitor.report(self.config, {"batch_id": "batch", "complete": 1,
                                                   "last_progress_at": self.now-1200}, self.now)
        for expected in ("1/2", "Q202 第3遍", "job123", "platform_risk_control", "阶段 sending",
                         "没有新增完成", "心跳", "账号浏览器"):
            self.assertIn(expected, text)
        self.assertNotIn("PRIVATE_KEY", text)
        self.assertEqual(state["complete"], 1)

    def test_delivery_failure_does_not_advance_baseline_or_suppress_retry(self):
        def fail(*_):
            raise RuntimeError("delivery failed")
        with self.assertRaises(RuntimeError):
            monitor.tick(self.config, self.state, sender=fail, now=self.now)
        self.assertFalse(self.state.exists())
        sent = []
        self.assertTrue(monitor.tick(self.config, self.state, sender=lambda c,t: sent.append(t), now=self.now))
        self.assertFalse(monitor.tick(self.config, self.state, sender=lambda c,t: sent.append(t), now=self.now+5))
        self.assertEqual(len(sent), 1)
        self.assertTrue(monitor.tick(self.config, self.state, sender=lambda c,t: sent.append(t), now=self.now+600))

    def test_completion_is_sent_once_with_increment(self):
        self.write("notification.json", {"batch_id": "batch", "complete": 1, "last_sent_at": self.now-600})
        self.manifest["samples"][1]["status"] = "complete"
        self.write("manifest.json", self.manifest)
        sent = []
        self.assertTrue(monitor.tick(self.config, self.state, sender=lambda c,t: sent.append(t), now=self.now))
        self.assertIn("新增 1 次", sent[0])
        self.assertIn("全部完成", sent[0])
        self.assertFalse(monitor.tick(self.config, self.state, sender=lambda c,t: sent.append(t), now=self.now+600))

    def test_exception_completion_keeps_qualified_count_truthful_and_stops_notifications(self):
        self.manifest['allow_clarification_advance'] = True
        self.manifest['samples'][1]['status'] = 'needs_clarification'
        self.write('manifest.json', self.manifest)
        text, state = monitor.report(self.config, {}, self.now)
        self.assertTrue(state['finished'])
        self.assertEqual(state['complete'], 1)
        self.assertIn('合格 1 条', text)
        self.assertIn('不计合格数', text)


if __name__ == "__main__":
    unittest.main()
