import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import repair_sample_share as repair


class ShareRepairTest(unittest.TestCase):
    def test_candidate_requires_original_private_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / '.scrape_cache/cn_ai/doubao/original'
            run.mkdir(parents=True)
            url = 'https://www.doubao.com/thread/12345678'
            (run / 'share_response.json').write_text(json.dumps({'share': {'url': '', 'rejected_url': url}}), encoding='utf-8')
            with patch.object(repair, 'GEO', root):
                self.assertEqual(repair.observed_candidate({'run_dir': str(run)}, 'doubao'), url)
                self.assertEqual(repair.observed_candidate({'run_dir': '.scrape_cache/cn_ai/doubao/original'}, 'doubao'), url)
                self.assertEqual(repair.observed_candidate({'run_dir': str(root)}, 'doubao'), '')
                self.assertEqual(repair.observed_candidate({'run_dir': str(run)}, 'kimi'), '')

    def test_missing_evidence_never_opens_any_browser(self):
        with patch.object(repair, 'observed_candidate', return_value=''), \
                patch('playwright.sync_api.sync_playwright') as browser:
            self.assertEqual(repair.repair({'answer': '原答案'}, 'doubao')['error_code'], 'share_candidate_missing')
        browser.assert_not_called()

    def test_proof_binding_changes_with_question_and_answer(self):
        result = {'question_id': 'job-original', 'prompt': '原问题', 'answer': '原答案'}
        bound = repair.binding(result, 'doubao')
        for changed in [dict(result, question_id='other'), dict(result, answer='其他答案'), dict(result, prompt='其他问题')]:
            self.assertNotEqual(bound, repair.binding(changed, 'doubao'))

    def test_success_is_checkpointed_before_browser_cleanup_can_fail(self):
        browser = Mock()
        browser.close.side_effect = RuntimeError('browser cleanup failed')
        checkpoints = []
        with patch.object(repair, 'observed_candidate', return_value='https://www.doubao.com/thread/12345678'), \
                patch.object(repair, 'verify_public_share', return_value={'ok': True}), \
                patch.object(repair, 'share_evidence', return_value={}), \
                patch('playwright.sync_api.sync_playwright') as playwright:
            playwright.return_value.__enter__.return_value.chromium.launch.return_value = browser
            with self.assertRaises(RuntimeError):
                repair.repair({'answer': '原答案'}, 'doubao', lambda value: checkpoints.append(dict(value)))
        self.assertTrue(checkpoints[-1]['ok'])
        self.assertTrue(checkpoints[-1]['finished'])

    def test_existing_chrome_is_never_closed_by_public_verifier(self):
        browser = Mock()
        with patch.object(repair, 'observed_candidate', return_value='https://www.doubao.com/thread/12345678'), \
                patch.object(repair, 'public_browser', return_value=(browser, False)), \
                patch.object(repair, 'verify_public_share', return_value={'ok': True}) as verify, \
                patch.object(repair, 'share_evidence', return_value={}), \
                patch('playwright.sync_api.sync_playwright'):
            proof = repair.repair({'answer': '原答案'}, 'doubao')
        self.assertTrue(proof['ok'])
        verify.assert_called_once()
        browser.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
