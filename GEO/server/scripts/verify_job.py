#!/usr/bin/env python3
"""Read a signed GEO result and check answers, references, and optional saved data."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import geo_ask


def valid_url(value):
    try:
        url = urlsplit(value)
        return url.scheme in ('http', 'https') and bool(url.hostname) and not url.username
    except (TypeError, ValueError):
        return False


def urls(values):
    return {item.get('url') if isinstance(item, dict) else item for item in values or []
            if valid_url(item.get('url') if isinstance(item, dict) else item)}


def canonical_site(site):
    return 'weixinyiyan' if site in ('yiyan', 'wenxinyiyan') else site


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--base', default=geo_ask.DEFAULT_BASE)
    parser.add_argument('--keys', type=Path, default=geo_ask.ROOT / '.aksk.json')
    parser.add_argument('--require-citations', action='store_true')
    parser.add_argument('--saved-dir', type=Path, help='Optional server-side raw jobs directory')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.job_id):
        parser.error('invalid job_id')
    ak, sk = geo_ask.load_aksk(args.keys)
    job = geo_ask.call(args.base, 'GET', '/api/v1/jobs/' + args.job_id, ak, sk)
    if job.get('http_status') != 200:
        print(json.dumps({'passed': False, 'http_status': job.get('http_status')}))
        return 1
    if job.get('status') in ('queued', 'running'):
        print(json.dumps({'passed': False, 'unfinished': True, 'status': job['status']}))
        return 2
    saved = {}
    if args.saved_dir:
        record = json.loads((args.saved_dir / (args.job_id + '.json')).read_text(encoding='utf-8'))
        saved = {canonical_site(r['site_id']): r for r in record.get('results', [])}
    rows = []
    results = job.get('results') or []
    for result in results:
        site = canonical_site(result.get('site_id'))
        refs = result.get('citations') or []
        details = result.get('citation_details') or []
        reference_urls = urls(refs)
        valid = (isinstance(refs, list) and all(isinstance(r, dict) and valid_url(r.get('url')) for r in refs)
                 and len(reference_urls) == len(refs) and refs == details)
        answer_present = bool((result.get('answer') or '').strip())
        matched = True
        if args.saved_dir:
            raw = saved.get(site, {})
            expected = urls(raw.get('citations')) | urls(raw.get('citation_details'))
            matched = site in saved and reference_urls == expected
        passed = (result.get('ok') is True and answer_present and valid and matched
                  and (bool(refs) or not args.require_citations))
        rows.append({'site': site, 'passed': passed, 'answer_chars': len(result.get('answer') or ''),
                     'citations': len(refs), 'references_valid_and_compatible': valid,
                     'saved_urls_match': matched if args.saved_dir else None,
                     'answer_origin': result.get('answer_origin'), 'share_status': result.get('share_status'),
                     'error_code': result.get('error_code')})
    requested = [canonical_site(s) for s in job.get('sites', [])]
    returned = [r['site'] for r in rows]
    complete = (sorted(requested) == sorted(returned) and len(returned) == len(set(returned))
                and job.get('completed_sites') == len(rows))
    passed = bool(rows) and job.get('status') == 'succeeded' and complete and all(r['passed'] for r in rows)
    print(json.dumps({'job_id': args.job_id, 'passed': passed, 'status': job.get('status'),
                      'complete_sites': complete, 'total_ms': job.get('total_ms'), 'results': rows},
                     ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
