import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import restart_services as restart


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data / "jobs").mkdir()
        self.barrier = self.data / ".deployment-drain.json"

    def job(self, job_id, status):
        (self.data / "jobs" / (job_id + ".json")).write_text(
            json.dumps({"job_id": job_id, "status": status}), encoding="utf-8")

    def test_terminal_history_and_subdirectories_are_ignored(self):
        self.job("done", "succeeded")
        nested = self.data / "jobs/cooldowns"
        nested.mkdir()
        (nested / "site.json").write_text("{}", encoding="utf-8")
        with restart.drain(self.data, 0, 10):
            self.assertEqual(json.loads(self.barrier.read_text(encoding="utf-8"))["allow"], [])
        self.assertFalse(self.barrier.exists())

    def test_running_task_prevents_restart_and_releases_barrier(self):
        self.job("busy", "running")
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            with restart.drain(self.data, 0, 10):
                self.fail("must not restart")
        self.assertFalse(self.barrier.exists())

    def test_queued_existing_work_must_finish(self):
        self.job("waiting", "queued")
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            with restart.drain(self.data, 0, 10):
                self.fail("must not restart")

    def test_existing_work_finishes_and_new_queued_work_stays_held(self):
        self.job("busy", "running")

        def finish(_):
            state = json.loads(self.barrier.read_text(encoding="utf-8"))
            self.assertEqual(state["allow"], ["job_busy"])
            self.job("busy", "succeeded")
            self.job("new", "queued")

        with patch.object(restart.time, "sleep", side_effect=finish):
            with restart.drain(self.data, 10, 10):
                self.assertEqual(restart.pending_jobs(self.data), {"new"})
        self.assertFalse(self.barrier.exists())

    def test_existing_barrier_is_never_overwritten(self):
        self.barrier.write_text('{"expires_at":0,"owner":"another"}', encoding="utf-8")
        original = self.barrier.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "已有部署屏障"):
            with restart.drain(self.data, 0, 10):
                self.fail("must not restart")
        self.assertEqual(self.barrier.read_bytes(), original)

    def test_restart_error_releases_owned_barrier(self):
        with self.assertRaisesRegex(RuntimeError, "restart error"):
            with restart.drain(self.data, 0, 10):
                raise RuntimeError("restart error")
        self.assertFalse(self.barrier.exists())

    def test_corrupt_job_aborts_and_releases_barrier(self):
        (self.data / "jobs/bad.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            with restart.drain(self.data, 0, 10):
                self.fail("must not restart")
        self.assertFalse(self.barrier.exists())

    def test_wrong_service_directory_never_restarts(self):
        with patch.object(restart.os, "geteuid", return_value=0), \
                patch.object(restart.sys, "argv", ["restart"]), \
                patch.object(restart, "systemctl", return_value="/wrong/environment") as systemctl:
            with self.assertRaisesRegex(RuntimeError, "工作目录"):
                restart.main()
        self.assertTrue(all(call.args[0] == "show" for call in systemctl.call_args_list))

    def test_monitor_must_refresh_after_restart(self):
        root = self.data
        (root / "data").mkdir()
        (root / "data/monitor-dashboard.json").write_text(
            '{"service_healthy":true}', encoding="utf-8")
        args = argparse.Namespace(health_timeout=1, http_url="http://local", https_url="https://local")
        with patch.object(restart, "systemctl", return_value="active"), \
                patch.object(restart, "probe"), \
                patch.object(restart.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(restart.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "启动检查超时"):
                restart.wait_ready(root, ("server", "monitor"), args, restart.time.time() + 10)


if __name__ == "__main__":
    unittest.main()
