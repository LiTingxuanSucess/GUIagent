import json
from pathlib import Path
import tempfile
import unittest

from sampling_alert import deliver


class RecoveryAlertTest(unittest.TestCase):
    def test_failure_is_durable_and_retry_delivers_once(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.json'
            config.write_text('{}',encoding='utf-8')
            event={'job_id':'job123','site':'doubao','stage':'发现验证','code':'platform_risk_control'}
            def failed(*_): raise RuntimeError('failed')
            with self.assertRaises(RuntimeError): deliver(config,event,sender=failed,now=1000)
            path=Path(folder)/'recovery-alerts.json'
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['pending']),1)
            sent=[]
            deliver(config,sender=lambda c,t:sent.append(t),now=1100)
            deliver(config,event,sender=lambda c,t:sent.append(t),now=1101)
            self.assertEqual(len(sent),1)
            self.assertIn('job123',sent[0]);self.assertIn('豆包',sent[0])
            self.assertFalse(json.loads(path.read_text(encoding='utf-8'))['pending'])
