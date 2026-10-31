"""Collect an explicitly supplied public transcript without submitting a question."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from cn_ai.network_parser import parse_qianwen_chat_sse
from cn_ai.protocol_completion import normalized
from cn_ai.share_links import _answer_snippets, _snippets_visible, is_public_share_url
from cn_ai.web_recovery import fail


def parse_transcript(payload, prompt):
    if payload.get('code', 0) != 0 or payload.get('success') is False or (payload.get('data') or {}).get('expired'):
        fail('recovery_unconfirmed', '分享不可用或已失效')
    records = ((payload.get('data') or {}).get('session') or {}).get('record_list') or []
    # A multi-turn share is ambiguous for an independent-question sample.
    if len(records) != 1:
        fail('manual_question_mismatch', '分享必须只包含本题的一轮问答')
    record = records[0]
    requests = [m.get('content', '') for m in record.get('request_messages') or []
                if m.get('mime_type') in ('text/plain', 'text/markdown')]
    if len(requests) != 1 or normalized(requests[0]) != normalized(prompt):
        fail('manual_question_mismatch', '分享中的问题与本任务不一致')
    messages = record.get('response_messages') or []
    if any(m.get('status') in ('processing', 'generating') for m in messages):
        fail('generation_timeout', '分享中的回答尚未结束')
    answer, sources = parse_qianwen_chat_sse('data: ' + json.dumps({'data': {'messages': messages}}, ensure_ascii=False))
    if not answer.strip() or not sources:
        fail('recovery_unconfirmed', '分享缺少完整正文或实际搜索来源')
    return answer, sources


def collect(browser, site, url, prompt, question_id):
    if site != 'qianwen' or not is_public_share_url(site, url):
        fail('recovery_unconfirmed', '仅支持已核对的千问公开分享地址')
    context = browser.new_context()
    page = context.new_page()
    payloads = []
    share_id = urlsplit(url).path.rsplit('/', 1)[-1]
    def response(response):
        parsed = urlsplit(response.url)
        if parsed.hostname != 'chat2-api.qianwen.com' or parsed.path != '/api/v1/share/info':
            return
        try:
            request = json.loads(response.request.post_data or '{}')
            if request.get('share_id') == share_id:
                payloads.append(response.json())
        except Exception:
            pass
    page.on('response', response)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if payloads:
                answer, sources = parse_transcript(payloads[-1], prompt)
                text = page.locator('body').inner_text(timeout=2000)
                if _snippets_visible(_answer_snippets(answer), text):
                    run = Path(__file__).resolve().parents[1] / '.scrape_cache/cn_ai/qianwen' / datetime.now().strftime('%Y%m%d_%H%M%S_shared')
                    run.mkdir(parents=True, exist_ok=True, mode=0o700)
                    (run / 'public_share.json').write_text(json.dumps(payloads[-1], ensure_ascii=False, indent=2), encoding='utf-8')
                    return {'ok': True, 'site_id': site, 'question_id': question_id, 'prompt': prompt,
                            'answer': answer, 'answer_origin': 'web', 'source': 'web',
                            'citations': sources, 'citation_details': [{'url': u} for u in sources],
                            'web_search_status': 'used', 'web_search_provider': site,
                            'search_result_count': len(sources), 'citations_status': 'available',
                            'url': url, 'share_status': 'verified', 'run_dir': str(run),
                            'recovery_source': 'user_provided_public_share', 'submission_mode': 'manual',
                            'timestamp': datetime.now().astimezone().isoformat()}
            page.wait_for_timeout(250)
        fail('recovery_unconfirmed', '未能匿名核验分享中的本题正文')
    finally:
        context.close()
