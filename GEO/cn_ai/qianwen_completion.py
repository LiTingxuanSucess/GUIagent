"""Read a completed Qianwen answer and its native source panel on the same tab."""
import json
import re
import time
from urllib.parse import urlsplit

from cn_ai import budget
from cn_ai.web_recovery import display_prompt_matches, snapshot


def completed_answer(page, prompt):
    observed = snapshot(page, 'qianwen')
    if (observed.get('user_count') != 1 or observed.get('logged_out')
            or observed.get('challenge_visible')
            or not display_prompt_matches(observed.get('last_user'), prompt, 'qianwen')):
        return None
    answer = page.locator('.answer-common-card .qk-markdown-complete').filter(visible=True).last
    if not answer.count() or page.get_by_role('button', name=re.compile(r'^停止(生成|回答)$')).filter(visible=True).count():
        return None
    text = answer.inner_text(timeout=1500).strip()
    if not text:
        return None
    details = []
    source_button = page.locator('[class*="search-content-"]').filter(has_text=re.compile(r'^\d+篇来源$')).filter(visible=True).last
    if source_button.count():
        source_button.click(timeout=2000)
        cards = page.locator('[id^="deep-think-source-card-"][data-c="refer_panel"][data-click-extra]')
        try:
            cards.first.wait_for(state='visible', timeout=2000)
            rows = cards.evaluate_all('(els)=>els.map(e=>e.getAttribute("data-click-extra"))')
            req_id = None
            for raw in rows:
                row = json.loads(raw)
                if row.get('display_scene') != 'answer_rag' or not row.get('req_id'):
                    return None
                if req_id is not None and req_id != row['req_id']:
                    return None
                req_id = row['req_id']
                url = row.get('ref_url') or row.get('url') or ''
                parsed = urlsplit(url)
                if parsed.scheme in ('http', 'https') and parsed.hostname and row.get('source_type') != 'video':
                    details.append({'url': url, 'title': row.get('title', ''), 'site_name': parsed.hostname})
        finally:
            # Close this panel by its observed close control, without touching
            # the editor, model, search mode, or the question's original tab.
            close = page.locator('[class*="deep-think-source-"] [class*="right-item-"] [class*="item-img-"]').filter(visible=True)
            if close.count() == 1:
                close.click(timeout=1500)
    current = snapshot(page, 'qianwen')
    if current != observed or answer.inner_text(timeout=1500).strip() != text:
        return None
    return {'answer': text, 'citations': list(dict.fromkeys(d['url'] for d in details)),
            'citation_details': details, 'complete': True, 'prompt_matched': True,
            'native_answer_verified': True,
            'source': 'dom:qianwen-completed-native-sources' if details else 'dom:qianwen-completed'}


def wait_completed(page, prompt):
    from cn_ai.site_flows import detect_platform_failure
    deadline = time.monotonic() + budget.wait_window(105, reserve=15)
    while True:
        detect_platform_failure(page, 'qianwen')
        result = completed_answer(page, prompt)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            from cn_ai.site_flows import PlatformFlowError
            raise PlatformFlowError('generation_timeout', '千问本题的完整回答尚未就绪，保留原页面等待续采', False)
        page.wait_for_timeout(700)
