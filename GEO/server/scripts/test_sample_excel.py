import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl
import sample_excel as sampling


class SamplingTest(unittest.TestCase):
    def test_legacy_submission_policy_has_a_stable_new_id_and_preserves_original(self):
        sample = {'site': 'qianwen', 'prompt': '原题？', 'submitted_prompt': '旧的通用后缀',
                  'request_id': 'old-request', 'attempts': [{'job_id': 'original-job'}]}
        payload = sampling.prepare_submission(sample)
        self.assertEqual(payload, {'site': 'qianwen', 'prompt': '原题？', 'request_id': 'old-request-search-v3'})
        self.assertEqual(sampling.prepare_submission(sample), payload)
        self.assertEqual(sample['submitted_prompt'], '原题？')
        self.assertEqual(sample['attempts'], [{'job_id': 'original-job'}])

    def test_share_verifier_upgrade_retries_once_without_resubmitting_question(self):
        sample = {'site': 'doubao', 'status': 'share_pending', 'job_id': 'original-job',
                  'request_id': 'original-request', 'result': {'answer': '原答案'},
                  'share_repairs': [{'ok': False}] * 3, 'share_repair_exhausted': True,
                  'share_repair_version': 2, 'share_repair_error': 'share_verify_timeout'}
        with patch.object(sampling, 'observed_candidate', return_value='https://www.doubao.com/thread/12345678'):
            sampling.retain_share_only_samples({'samples': [sample]})
            self.assertFalse(sample['share_repair_exhausted'])
            self.assertEqual(sample['share_repair_budget_start'], 3)
            sample['share_repair_exhausted'] = True
            sampling.retain_share_only_samples({'samples': [sample]})
        self.assertTrue(sample['share_repair_exhausted'])
        self.assertEqual(sample['job_id'], 'original-job')
        self.assertEqual(sample['request_id'], 'original-request')
        self.assertEqual(len(sample['share_repairs']), 3)

    def test_independent_platform_rounds_do_not_wait_for_blocked_platform(self):
        state = {'round_policy': 'per_site', 'sites': ['kimi', 'doubao'], 'samples': [
            {'site': 'kimi', 'round': 1, 'status': 'complete'},
            {'site': 'doubao', 'round': 1, 'status': 'waiting_manual'},
            {'site': 'kimi', 'round': 2, 'status': 'pending'},
            {'site': 'doubao', 'round': 2, 'status': 'pending'},
            {'site': 'kimi', 'round': 3, 'status': 'pending'}]}
        self.assertEqual(sampling.site_rounds(state), {'kimi': 2, 'doubao': 1})
        self.assertEqual([(s['site'], s['round']) for s in sampling.pending_in_current_round(state)], [('kimi', 2)])

    def test_clarification_never_counts_as_qualified_and_advance_is_explicit(self):
        state = {'batch_id': 'fixture', 'round_policy': 'per_site', 'sites': ['kimi'], 'samples': [
            {'site': 'kimi', 'round': 1, 'status': 'needs_clarification'},
            {'site': 'kimi', 'round': 2, 'status': 'pending'}]}
        self.assertEqual(sampling.pending_in_current_round(state), [])
        state['allow_clarification_advance'] = True
        self.assertEqual(sampling.site_rounds(state), {'kimi': 2})
        self.assertEqual(sampling.summary(state)['counts'].get('complete', 0), 0)
        self.assertEqual(sampling.summary(state)['processed'], 1)

    def test_repeated_clarification_stops_resending_original_question(self):
        sample = {'site': 'kimi', 'job_id': 'one', 'request_id': 'original'}
        result = {'site_id': 'kimi', 'ok': True, 'answer_origin': 'web',
                  'answer': '不过你还没说具体是哪两款车，我现在无法有效搜索。请告诉我两款车型。',
                  'share_status': 'verified', 'url': 'https://www.kimi.com/share/12345678'}
        job = {'status': 'succeeded', 'results': [result]}
        sampling.finish_sample(sample, job, max_attempts=10, retry_delay=60)
        self.assertEqual(sample['status'], 'retry_wait')
        sample['job_id'] = 'two'
        sampling.finish_sample(sample, job, max_attempts=10, retry_delay=60)
        self.assertEqual(sample['status'], 'needs_clarification')
        self.assertEqual(len(sample['attempts']), 2)
        self.assertEqual(sample['quality_issues'], ['missing_search_sources'])

    def test_round_barrier_blocks_later_rounds_even_when_a_site_is_waiting(self):
        state={'samples':[
            {'question_id':'Q201','round':1,'site':'kimi','status':'complete'},
            {'question_id':'Q300','round':1,'site':'doubao','status':'waiting_manual'},
            {'question_id':'Q300','round':1,'site':'yuanbao','status':'pending'},
            {'question_id':'Q201','round':2,'site':'kimi','status':'pending'},
            {'question_id':'Q201','round':3,'site':'kimi','status':'pending'},
        ]}
        self.assertEqual(sampling.current_round(state),1)
        self.assertEqual([(s['question_id'],s['round']) for s in sampling.pending_in_current_round(state)],[('Q300',1)])
        state['samples'][2]['status']='complete'
        self.assertEqual(sampling.pending_in_current_round(state),[])
        state['samples'][1]['status']='incomplete'
        self.assertEqual(sampling.pending_in_current_round(state),[])
        state['samples'][1]['status']='complete'
        self.assertEqual(sampling.current_round(state),2)
        self.assertEqual([s['round'] for s in sampling.pending_in_current_round(state)],[2])
        state['samples'][3]['status']='complete'
        self.assertEqual([s['round'] for s in sampling.pending_in_current_round(state)],[3])
        state['samples'][4]['status']='complete'
        self.assertIsNone(sampling.current_round(state))
        self.assertEqual(sampling.pending_in_current_round(state),[])

    def test_last_100_five_rounds_and_resume_preserve_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "题库.xlsx"
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.title = "300题"
            sheet.append(["题号", "类别", "消费者独立问题", "属性", "重复次数"])
            for index in range(1, 301):
                sheet.append([f"Q{index:03}", "原类别", f"原问题 {index}？", "原属性", 5])
            book.save(source)
            args = argparse.Namespace(excel=source, count=100, repeats=5, sites=["kimi"], batch_id="fixture")
            state = sampling.initialize(args, root)
            self.assertEqual(len(state["samples"]), 500)
            first, last = state["samples"][0], state["samples"][-1]
            self.assertEqual((first["question_id"], first["round"], first["excel_row"], first["prompt"]),
                             ("Q201", 1, 202, "原问题 201？"))
            self.assertEqual((last["question_id"], last["round"]), ("Q300", 5))
            first.update(status="complete", job_id="existing")
            sampling.save_json(root / "manifest.json", state)
            self.assertEqual(sampling.initialize(args, root), state)
            args.sites.append("deepseek")
            extended = sampling.initialize(args, root)
            self.assertEqual(len(extended["samples"]), 1000)
            self.assertEqual(extended["samples"][0]["job_id"], "existing")
            args.require_search = True
            with self.assertRaisesRegex(ValueError, "联网搜索要求不同"):
                sampling.initialize(args, root)
            fresh = root / 'formal'
            fresh.mkdir()
            args.sites = list(sampling.SITES)
            formal = sampling.initialize(args, fresh)
            self.assertEqual(len(formal['samples']), 3000)
            self.assertEqual(len({s['request_id'] for s in formal['samples']}), 3000)
            for sample in formal['samples']:
                self.assertEqual(sample['submitted_prompt'], sampling.outbound_prompt(sample['site'], sample['prompt']))
                self.assertEqual(sample['api_prompt'], sample['prompt'])
            self.assertEqual(formal['samples'][0]['prompt'], '原问题 201？')

    def test_incomplete_terminal_attempt_is_audited_before_a_fresh_retry(self):
        sample = {'site': 'kimi', 'request_id': 'batch-Q201-r1-kimi', 'job_id': 'first'}
        job = {'status': 'succeeded', 'results': [{'site_id': 'kimi', 'ok': True, 'answer': '无来源的回答'}]}
        sampling.finish_sample(sample, job, max_attempts=2, retry_delay=60)
        self.assertEqual(sample['status'], 'retry_wait')
        self.assertEqual(sample['request_id'], 'batch-Q201-r1-kimi-a2')
        self.assertNotIn('job_id', sample)
        self.assertEqual(sample['attempts'][0]['job_id'], 'first')
        self.assertIn('missing_search_sources', sample['attempts'][0]['quality_issues'])
        sample['job_id'] = 'second'
        sampling.finish_sample(sample, job, max_attempts=2, retry_delay=60)
        self.assertEqual(sample['status'], 'incomplete')
        self.assertEqual(len(sample['attempts']), 2)

    def test_quality_requires_actual_sources_and_verified_public_share(self):
        good = {"ok": True, "answer": "真实正文", "answer_origin": "web", "web_search_status": "used",
                "citation_details": [{"url": "https://example.org/source"}], "share_status": "verified",
                "url": "https://www.kimi.com/share/12345678"}
        self.assertEqual(sampling.quality(good, "kimi"), [])
        for field, value, expected in [
            ("answer", "", "missing_answer"),
            ("web_search_status", "not_confirmed", "missing_search_sources"),
            ("citation_details", [], "missing_search_sources"),
            ("url", "https://www.kimi.com/chat/12345678", "missing_verified_share"),
            ("share_status", "generation_failed", "missing_verified_share"),
            ("answer_origin", "official_api", "not_web_answer"),
        ]:
            with self.subTest(field=field):
                self.assertIn(expected, sampling.quality({**good, field: value}, "kimi"))

    def test_share_only_failure_never_submits_a_new_question(self):
        result = {'site_id': 'doubao', 'ok': True, 'answer': '已有回答', 'answer_origin': 'web',
                  'web_search_status': 'used', 'citations': ['https://example.org/source']}
        sample = {'site': 'doubao', 'request_id': 'original', 'job_id': 'original-job'}
        sampling.finish_sample(sample, {'status': 'succeeded', 'results': [result]}, max_attempts=10, retry_delay=60)
        self.assertEqual(sample['status'], 'share_pending')
        self.assertEqual(sample['job_id'], 'original-job')
        self.assertEqual(sample['request_id'], 'original')
        self.assertEqual(sampling.pending_in_current_round({'samples': [sample]}), [])
        proof = {'binding': sampling.share_binding(result, 'doubao'), 'ok': True,
                 'url': 'https://www.doubao.com/thread/12345678'}
        sampling.finish_share_repair(sample, proof, 'proof.json')
        self.assertEqual(sample['status'], 'complete')
        self.assertEqual(sample['result']['answer'], result['answer'])
        self.assertEqual(len(sample['attempts']), 1)

    def test_unrelated_share_proof_never_completes_sample(self):
        sample = {'site': 'doubao', 'status': 'share_pending', 'result': {'answer': '原答案'}}
        sampling.finish_share_repair(sample, {'binding': 'wrong', 'ok': True, 'url': 'https://www.doubao.com/thread/12345678'}, 'proof.json')
        self.assertEqual(sample['status'], 'share_pending')
        self.assertEqual(sample['share_repair_error'], 'share_proof_mismatch')

    def test_old_exhausted_share_retries_are_migrated_with_audit_intact(self):
        attempt = {'job_id': 'old-job', 'request_id': 'old-request'}
        sample = {'site': 'doubao', 'status': 'incomplete', 'quality_issues': ['missing_verified_share'],
                  'result': {'answer': '原答案'}, 'attempts': [attempt]}
        active = dict(sample, status='waiting_manual')
        sampling.retain_share_only_samples({'samples': [sample, active]})
        self.assertEqual((sample['status'], sample['request_id'], sample['job_id']), ('share_pending', 'old-request', 'old-job'))
        self.assertEqual(sample['attempts'], [attempt])
        self.assertEqual(active['status'], 'waiting_manual')

    def test_export_keeps_formula_looking_answers_literal(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            state = {"batch_id": "fixture", "sites": ["kimi"], "samples": [
                {"question_id": "Q201", "round": 1, "site": "kimi", "prompt": "问题？", "category": "类别",
                 "status": "incomplete", "result": {"answer": "=1+1", "citation_details": [{"title": "来源", "url": "https://example.org"}]}}
            ]}
            sampling.export(out, state)
            book = openpyxl.load_workbook(out / "采样结果.xlsx")
            self.assertEqual(book["采样结果"]["G2"].value, "=1+1")
            self.assertEqual(book["采样结果"]["G2"].data_type, "s")
            self.assertEqual(book["搜索来源"]["F2"].value, "https://example.org")
            book.close()
            self.assertEqual(json.loads((out / "progress.json").read_text(encoding="utf-8"))["counts"], {"incomplete": 1})


if __name__ == "__main__":
    unittest.main()
