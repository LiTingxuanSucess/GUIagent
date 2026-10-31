"""Cancellation remains terminal across deployment audits."""
import json
from pathlib import Path
import tempfile
import unittest

import verify_delivery_handover as audit


class CancelledHandoverTests(unittest.TestCase):
    def test_cancelled_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / 'jobs').mkdir()
            path = data / 'jobs/one.json'
            job = {'job_id': 'one', 'status': 'cancelled', 'prompt': 'fixture'}
            path.write_text(json.dumps(job), encoding='utf-8')
            manifest = audit.snapshot(data)
            self.assertEqual(audit.verify(data, manifest)['verified_jobs'], 1)
            job['status'] = 'queued'
            path.write_text(json.dumps(job), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'terminal history'):
                audit.verify(data, manifest)

    def test_cancellation_may_clean_up_a_pending_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / 'jobs').mkdir()
            journal = data / 'web-recovery/one/doubao/account.json'
            journal.parent.mkdir(parents=True)
            journal.write_text('{}', encoding='utf-8')
            path = data / 'jobs/one.json'
            job = {'job_id': 'one', 'status': 'running', 'prompt': 'fixture'}
            path.write_text(json.dumps(job), encoding='utf-8')
            manifest = audit.snapshot(data)
            job['status'] = 'cancelled'
            path.write_text(json.dumps(job), encoding='utf-8')
            journal.unlink()
            self.assertEqual(audit.verify(data, manifest)['original_pending_jobs'], 1)


if __name__ == '__main__':
    unittest.main()
