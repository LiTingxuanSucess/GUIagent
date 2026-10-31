import argparse
from pathlib import Path
import tempfile
import unittest

import openpyxl
import sample_excel as sampling


class SamplingRoundLimitTest(unittest.TestCase):
    def test_finishing_first_round_never_schedules_second_and_preserves_history(self):
        for policy in ('global', 'per_site'):
            with self.subTest(policy=policy):
                state = {'batch_id': 'fixture', 'sites': ['doubao', 'qianwen'],
                         'max_round': 1, 'round_policy': policy, 'samples': [
                    {'site': 'doubao', 'round': 1, 'status': 'complete'},
                    {'site': 'qianwen', 'round': 1, 'status': 'pending'},
                    {'site': 'doubao', 'round': 2, 'status': 'pending'},
                    {'site': 'qianwen', 'round': 2, 'status': 'waiting_manual', 'job_id': 'retained'}]}
                self.assertEqual([s['site'] for s in sampling.pending_in_current_round(state)], ['qianwen'])
                state['samples'][1]['status'] = 'complete'
                self.assertEqual(sampling.pending_in_current_round(state), [])
                self.assertIsNone(sampling.current_round(state))
                progress = sampling.summary(state)
                self.assertEqual((progress['status'], progress['total']), ('completed', 2))
                self.assertEqual(progress['counts'], {'complete': 2})
                self.assertEqual(set(progress['by_round']), {'1'})
                self.assertEqual(state['samples'][3]['job_id'], 'retained')
                self.assertEqual(len(state['samples']), 4)

    def test_limit_survives_resume_without_changing_batch_or_request_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            source = out / 'source.xlsx'
            book = openpyxl.Workbook()
            book.active.title = '300题'
            book.active.append(['Q300', '类别', '问题', '属性', 2])
            book.save(source)
            args = argparse.Namespace(excel=source, repeats=2, count=1, batch_id='fixture',
                                      sites=['doubao'], max_round=1)
            first = sampling.initialize(args, out)
            sampling.save_json(out / 'manifest.json', first)
            args.max_round = None
            resumed = sampling.initialize(args, out)
            self.assertEqual(resumed['max_round'], 1)
            self.assertEqual([s['request_id'] for s in resumed['samples']],
                             [s['request_id'] for s in first['samples']])
            self.assertEqual(len(resumed['samples']), 2)
            self.assertEqual([s['round'] for s in sampling.pending_in_current_round(resumed)], [1])


if __name__ == '__main__':
    unittest.main()
