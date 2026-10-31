#!/usr/bin/env python3
"""Verify an already-created share anonymously, without accessing account tabs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

GEO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GEO))
from cn_ai.share_links import is_public_share_url, share_answer_hint, verify_public_share
from cn_ai import budget


def binding(result, site):
    value = [site, result.get('question_id'), result.get('prompt'), result.get('answer')]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode('utf-8')).hexdigest()


def share_evidence(result, site):
    internal = 'yiyan' if site == 'wenxinyiyan' else site
    root = (GEO / '.scrape_cache/cn_ai' / internal).resolve()
    run = Path(result.get('run_dir') or '/nonexistent')
    if not run.is_absolute():
        run = GEO / run
    run = run.resolve()
    if not run.is_relative_to(root):
        return {}
    try:
        evidence = json.loads((run / 'share_response.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return evidence.get('share') or {}


def observed_candidate(result, site):
    internal = 'yiyan' if site == 'wenxinyiyan' else site
    share = share_evidence(result, site)
    url = share.get('url') or share.get('rejected_url', '')
    return url if is_public_share_url(internal, url) else ''


def public_browser(pw, result):
    """Reuse Chrome's transport; verification still creates a cookie-free context."""
    account = str(result.get('account_id') or '')
    if re.fullmatch(r'acct_[a-zA-Z0-9]+', account):
        try:
            active = GEO / '.chrome-cn-ai-profiles' / account / 'DevToolsActivePort'
            port = int(active.read_text(encoding='utf-8').splitlines()[0])
            if 1 <= port <= 65535:
                return pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=3000), False
        except Exception:
            pass
    return pw.chromium.launch(channel='chrome', headless=True, timeout=12000), True


def repair(result, site, checkpoint=lambda value: None):
    proof = {'binding': binding(result, site), 'ok': False, 'error_code': 'share_candidate_missing'}
    url = observed_candidate(result, site)
    if not url:
        proof.update(finished=True, phase='candidate_missing')
        return proof
    internal = 'yiyan' if site == 'wenxinyiyan' else site
    from playwright.sync_api import sync_playwright
    proof.update(phase='browser_start', finished=False)
    checkpoint(proof)
    with sync_playwright() as pw:
        browser, owned = public_browser(pw, result)
        try:
            proof['phase'] = 'public_verification'
            checkpoint(proof)
            expected_answer = share_answer_hint(internal, share_evidence(result, site), result['answer'])
            verified = budget.call(lambda: verify_public_share(browser, internal, url, expected_answer),
                                   32, reserve=0, code='share_verify_timeout')
            proof.update(verification=verified, error_code=verified.get('error', 'share_verify_failed'))
            if verified.get('ok') is True:
                final = verified.get('final_url') or url
                proof.update(ok=True, url=final if is_public_share_url(internal, final) else url, error_code='')
            # Browser teardown must never erase a completed verification.
            proof.update(finished=True, phase='verified' if proof['ok'] else 'verification_failed')
            checkpoint(proof)
        finally:
            if owned:
                browser.close()
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result-file', type=Path, required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    job = json.loads(args.result_file.read_text(encoding='utf-8'))
    result = next(r for r in job['results'] if r.get('site_id') == args.site)
    def checkpoint(proof):
        proof = dict(proof, updated_at=time.time())
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(args.output)
    try:
        proof = repair(result, args.site, checkpoint)
    except (Exception, budget.StageDeadline) as error:
        # A successful proof already persisted before teardown remains valid.
        try:
            saved = json.loads(args.output.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        proof = saved if saved.get('finished') and saved.get('binding') == binding(result, args.site) else {
            'binding': binding(result, args.site), 'ok': False, 'finished': True,
            'error_code': getattr(error, 'error_code', type(error).__name__), 'phase': saved.get('phase', 'startup')}
    checkpoint(proof)


if __name__ == '__main__':
    main()
