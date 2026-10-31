"""Small, non-secret evidence about a webpage's search configuration."""
import hashlib
import json
from urllib.parse import urlsplit
from cn_ai.search_policy import outbound_prompt


SEARCH_FIELDS = {
    'search_enabled', 'enable_search', 'enablesearch', 'searchenabled',
    'web_search', 'websearch', 'web_search_enabled', 'enable_web_search',
    'search_mode', 'searchmode', 'force_search', 'forced_search',
    'openinternetsearch', 'internetflag', 'search_triggered',
}
SEARCH_MODES = {'auto', 'on', 'off', 'force', 'forced', 'enabled', 'disabled', 'always', 'required', 'none'}
JSON_WRAPPERS = {'prompt', 'query', 'content', 'text', 'message', 'data', 'body', 'request', 'payload'}
HOSTS = {
    'deepseek': ('deepseek.com',), 'doubao': ('doubao.com',),
    'kimi': ('kimi.com', 'moonshot.cn'), 'qianwen': ('qianwen.com', 'aliyun.com'),
    'yuanbao': ('yuanbao.tencent.com',), 'yiyan': ('chat.baidu.com', 'yiyan.baidu.com', 'wenxin.baidu.com'),
}


def request_search_evidence(site_id, url, payload, prompt):
    """Inspect the body in memory; retain no body, text, token, or query string."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if not any(host == h or host.endswith('.' + h) for h in HOSTS.get(site_id, ())):
        return None
    if not any(k in parsed.path.lower() for k in ('completion', '/chat', 'task_mode', 'conversation')):
        return None
    flags = {}
    original_found = False
    submitted_found = False
    submitted = outbound_prompt(site_id, prompt)
    budget = [1000]

    def walk(value, depth=0):
        nonlocal original_found, submitted_found
        budget[0] -= 1
        if depth > 12 or budget[0] < 0:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:100]:
                lower = str(key).lower()
                if lower in SEARCH_FIELDS:
                    if isinstance(child, bool) or (isinstance(child, str) and child.lower() in SEARCH_MODES):
                        flags[lower] = child
                    elif lower in ('internetflag', 'openinternetsearch') and type(child) is int and child in (0, 1):
                        flags[lower] = child
                if lower in {'prompt', 'query', 'content', 'text'} and isinstance(child, str):
                    if child == prompt:
                        original_found = True
                    if child == submitted:
                        submitted_found = True
                if lower in JSON_WRAPPERS and isinstance(child, str) and len(child) <= 2000000:
                    if child.lstrip().startswith(('{', '[')):
                        try:
                            walk(json.loads(child), depth + 1)
                        except (ValueError, TypeError, RecursionError):
                            pass
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                walk(child, depth + 1)

    walk(payload)
    if not flags and not original_found and not submitted_found:
        return None
    result = {'endpoint': parsed.path, 'search_flags': flags,
            'original_prompt_observed': original_found,
            'expected_prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}
    if submitted != prompt:
        result.update(submitted_prompt_observed=submitted_found,
                      expected_submitted_prompt_sha256=hashlib.sha256(submitted.encode('utf-8')).hexdigest())
    return result


def web_search_evidence(site_id, diagnostic, network_citations, request_evidence):
    settings = (diagnostic.meta.get('web_search_settings') or {}) if diagnostic else {}
    legacy = (diagnostic.meta.get('deepseek_settings') or {}) if diagnostic else {}
    setting = settings.get('status', settings.get('state', legacy.get('search', 'not_observed')))
    # Only protocol-provided sources prove a search occurred here. Arbitrary
    # URLs in the answer and the UI's enabled/auto state do not establish it.
    sources = list(dict.fromkeys(network_citations))
    result = {'web_search_setting': setting,
              'web_search_setting_confirmed': settings.get('confirmed', setting == 'on'),
              'web_search_status': 'used' if sources else 'not_confirmed',
              'web_search_provider': site_id, 'web_search_mode': 'native',
              'search_result_count': len(sources)}
    if settings.get('control'):
        result['web_search_control'] = settings['control']
    if request_evidence:
        result['web_search_request_evidence'] = request_evidence[-10:]
    if not sources:
        result['web_search_error_code'] = 'search_not_confirmed'
    return result
