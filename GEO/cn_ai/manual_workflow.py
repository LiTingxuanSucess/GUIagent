"""Local Chrome workflow: pause for a person and resume the same question."""
from __future__ import annotations
import time
from urllib.parse import urlsplit

from cn_ai.protocol_completion import normalized
from cn_ai.web_recovery import RecoveryJournal, display_prompt_matches, fail, page_url, probe, snapshot


def restore_original_turn(page, site, data):
    """Explicitly restore the recorded conversation; never adopt a newer turn."""
    from cn_ai.web_recovery import HOSTS
    turn = data.get('manual_turn') or {}
    url = turn.get('url', '')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (data.get('send_state') != 'sent' or parsed.scheme != 'https'
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment
            or host not in HOSTS.get(site, ()) and not any(
                host.endswith('.' + h) for h in HOSTS.get(site, ()))
            or parsed.path.rstrip('/') in ('', '/chat', '/sign_in', '/login')
            or '/local_' in parsed.path):
        fail('manual_question_mismatch', '没有可核对的原对话地址，请人工找回原问题')
    if page_url(page) != url:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
    deadline = time.monotonic() + 8
    while True:
        current = snapshot(page, site)
        if current.get('logged_out'):
            fail('platform_auth_unavailable', '请先登录原账号，再恢复原对话')
        if current.get('challenge_visible'):
            fail('platform_risk_control', '请在原对话完成安全验证')
        if validate_manual_turn(current, data, page_url(page)):
            return current
        if time.monotonic() >= deadline:
            fail('manual_question_mismatch', '恢复原地址后仍无法核对原问题轮次，未重新发送')
        page.wait_for_timeout(200)


def busy_rejection(page, site):
    if site not in ('kimi', 'doubao'):
        return False
    return page.evaluate("""site => {
      const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&s.visibility!=='hidden'&&s.display!=='none'};
      return [...document.querySelectorAll('div,p,span')].some(e=>{
        if(!visible(e))return false;
        const t=(e.innerText||'').trim();
        if(site==='kimi')return !e.closest('.segment,[data-message-role]')&&t.length<150&&
          (t.includes('和Kimi聊天的人太多了')||t.includes('订阅会员可进入优先队列'));
        return t.length<180&&t.includes('算力紧张')&&(t.includes('办公能力得晚点')||t.includes('高峰期优先使用'));
      });
    }""", site) is True


def refresh_busy(browser, journal, page, site, prompt, question_id):
    """Only an explicit platform rejection permits a fresh independent send."""
    before = snapshot(page, site)
    rejected = busy_rejection(page, site)
    if before.get('user_count') and not display_prompt_matches(before.get('last_user'), prompt, site):
        fail('manual_question_mismatch', '原问题发生变化，未刷新或重发')
    page.reload(wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(800)
    current = snapshot(page, site)
    if current.get('user_count') and not display_prompt_matches(current.get('last_user'), prompt, site):
        fail('manual_question_mismatch', '刷新后无法核对原问题，未重发')
    if current.get('challenge_visible'):
        fail('platform_risk_control', '刷新后出现验证，保留原问题交给辅助处理')
    # A completed/accepted question always resumes its own answer. Never reset
    # a checkpoint or a sent journal merely because a busy notice is visible.
    if journal.data.get('checkpoint') or journal.data['send_state'] == 'sent' or (current.get('user_count') and not busy_rejection(page, site)):
        return run(browser, site, prompt, question_id, 'manual-continue')
    if not rejected:
        fail('manual_send_unconfirmed', '没有明确的平台拒绝证据，刷新后未重复发送')
    history = journal.data.setdefault('busy_retry_history', [])
    history.append({'at': time.time(), 'send_state': journal.data['send_state'],
                    'url': page_url(page), 'user_count': current.get('user_count', 0),
                    'reason': 'explicit_platform_busy_rejection'})
    journal.data['busy_retry_history'] = history[-20:]
    journal.save()
    # A rejected reply can remain in the conversation after reload. Start an
    # independent retry in the same bound tab, retaining the rejected turn above.
    home = 'https://www.kimi.com/' if site == 'kimi' else 'https://www.doubao.com/chat/'
    page.goto(home, wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(500)
    if site == 'kimi':
        from cn_ai.site_flows import dismiss_stale_kimi_busy_dialog
        dismiss_stale_kimi_busy_dialog(page)
    for key in ('manual_turn', 'manual_baseline', 'submission_observed'):
        journal.data.pop(key, None)
    journal.data.update(send_state='before_send', phase='busy_refreshed', page_url=page_url(page))
    journal.save()
    return run(browser, site, prompt, question_id, 'manual-auto')


def new_manual_turn(current, data, current_url):
    if not display_prompt_matches(current.get("last_user", ""), data.get("prompt", ""), data.get('site_id')):
        return False
    baseline = data.get("manual_baseline")
    if not isinstance(baseline, dict):
        return False
    return (current.get("user_count", 0) > baseline.get("user_count", 0)
            or bool(current.get("user_turn_id") and current["user_turn_id"] != baseline.get("user_turn_id")))


def remember_turn(journal, page, current):
    journal.data.pop('manual_new_tab', None)
    journal.data.update(manual_turn={"user_turn_id": current.get("user_turn_id", ""),
                                    "user_count": current.get("user_count", 0), "url": page_url(page)})
    journal.state("sent", page)


def confirm_current_turn(journal, page, current, *, allow_unsent_new_tab=False, latest_answer_proof=None):
    """An explicit operator action can collect a manually submitted replacement."""
    url = page_url(page)
    allowed = (journal.data.get('send_state') in ('sent', 'sending')
               or allow_unsent_new_tab and journal.data.get('manual_new_tab')
               and journal.data.get('send_state') == 'before_send')
    latest_verified = (journal.data['site_id'] == 'yiyan' and isinstance(latest_answer_proof, dict)
                       and latest_answer_proof.get('turn') and latest_answer_proof.get('answer'))
    if (not allowed or journal.data.get('checkpoint')
            or (current.get('user_count') != 1 and not latest_verified)
            or not display_prompt_matches(current.get('last_user'), journal.data['prompt'], journal.data['site_id'])
            or current.get('logged_out') or current.get('challenge_visible')
            or not current.get('editor_present') or '/local_' in url
            or urlsplit(url).path.rstrip('/') in ('', '/chat', '/sign_in')):
        fail('manual_question_mismatch', '当前页面必须只有本题的一次明确提问；未变更原记录或发送问题')
    journal.data.setdefault('manual_turn_history', []).append({
        'previous_turn': journal.data.get('manual_turn'), 'previous_url': journal.data.get('page_url'),
        'confirmed_url': url, 'observed_prompt': current['last_user'],
        'reason': 'operator_confirmed_current_turn', 'at': time.time()})
    journal.data['submission_mode'] = 'manual'
    remember_turn(journal, page, current)


def validate_manual_turn(current, data, current_url):
    turn = data.get("manual_turn")
    if not isinstance(turn, dict):
        return True
    if not display_prompt_matches(current.get("last_user", ""), data.get("prompt", ""), data.get('site_id')):
        return False
    if turn.get("url") and current_url != turn["url"]:
        return False
    if turn.get("user_turn_id"):
        return current.get("user_turn_id") == turn["user_turn_id"]
    return current.get("user_count", 0) == turn.get("user_count", 0)


def run(browser, site, prompt, question_id, action):
    from cn_ai.scraper import scrape_one
    journal = RecoveryJournal.from_env(site, prompt, question_id)
    if journal is None:
        fail("recovery_unconfirmed", "当前任务缺少发送记录")
    page = journal.find(browser)
    if action == 'manual-restore':
        restore_original_turn(page, site, journal.data)
        action = 'manual-continue'
    if action == 'manual-refresh':
        return refresh_busy(browser, journal, page, site, prompt, question_id)
    if journal.data["send_state"] == "before_send" and action in ("manual-auto", "manual-continue"):
        from cn_ai.site_flows import detect_platform_failure
        detect_platform_failure(page, site)
    if journal.data["send_state"] == "before_send" and action in ("manual-auto", "manual-continue"):
        from cn_ai.share_links import dismiss_share_ui
        dismiss_share_ui(page, site)
    current = snapshot(page, site)
    if current.get('logged_out') and journal.data['send_state'] != 'before_send':
        fail('platform_auth_unavailable', '原对话登录已失效，请先恢复登录')
    if action != 'manual-pause' and current.get('challenge_visible'):
        fail('platform_risk_control', '原页面存在安全验证，保留发送状态并恢复验证')
    journal.data["manual_workflow"] = True
    if current.get("account_marker") and not current.get("logged_out"):
        journal.data["prior_logged_in"] = True

    # A new tab is created only by an explicit operator action. Rebase an
    # unsent turn only after observing an empty, interactive page; never reset
    # sent/sending state or adopt an existing conversation as a blank composer.
    if (journal.data.get('manual_new_tab') and journal.data['send_state'] == 'before_send'
            and action in ('manual-auto', 'manual-continue')
            and current.get('user_count', 0) == 0 and current.get('editor_ready')
            and not current.get('logged_out') and not current.get('challenge_visible')):
        journal.data['manual_baseline'] = {'user_count': 0, 'user_turn_id': '', 'url': page_url(page)}
        journal.data.pop('manual_new_tab', None)

    if journal.data["send_state"] == "before_send" and (action in ("manual-auto", "manual-pause")):
        journal.data["manual_baseline"] = {"user_count": current.get("user_count", 0),
                                           "user_turn_id": current.get("user_turn_id", ""), "url": page_url(page)}
    journal.save()
    if action == "manual-pause":
        if journal.data["send_state"] == "sent" and not journal.data.get("manual_turn") and journal.matches_sent_turn(page, current):
            remember_turn(journal, page, current)
        return {"ok": True, "send_state": journal.data["send_state"], "target_id": journal.data["target_id"], "url": page_url(page)}

    state = journal.data["send_state"]
    if (action in ('manual-submitted', 'manual-continue') and state == 'before_send'
            and journal.data.get('manual_new_tab') and current.get('user_count', 0) > 0):
        # The same operator's Continue button also confirms an already answered
        # exact question in their replacement tab. Do not require another button
        # or pretend the populated page is a blank composer.
        proof = None
        if site == 'yiyan' and current.get('user_count', 0) > 1:
            from cn_ai.scraper import _yiyan_standard_answer_candidate
            proof = _yiyan_standard_answer_candidate(page, prompt)
        confirm_current_turn(journal, page, current, allow_unsent_new_tab=True, latest_answer_proof=proof)
        state = journal.data['send_state']
    if action == 'manual-reconcile':
        if (state in ('sent', 'sending') and current.get('user_count') == 1
                and display_prompt_matches(current.get('last_user'), prompt, site)
                and current.get('editor_present') and not journal.data.get('checkpoint')
                and (state == 'sending' or not validate_manual_turn(current, journal.data, page_url(page)))):
            confirm_current_turn(journal, page, current)
            journal.data['manual_turn_history'][-1]['reason'] = 'automatic_verified_current_turn'
            journal.save()
            state = journal.data['send_state']
        action = 'manual-continue'
    if action == 'manual-confirm-current':
        confirm_current_turn(journal, page, current)
        state = journal.data['send_state']
        action = 'manual-continue'
    if action in ("manual-submitted", "manual-continue"):
        if state == "sent":
            if not validate_manual_turn(current, journal.data, page_url(page)):
                fail("manual_question_mismatch", "当前对话已变化，请回到本任务的原问题")
        elif state == "sending" and journal.matches_sent_turn(page, current):
            remember_turn(journal, page, current)
        elif new_manual_turn(current, journal.data, page_url(page)):
            journal.data["submission_mode"] = "manual"
            remember_turn(journal, page, current)
        elif action == "manual-submitted" or state == "sending":
            fail("manual_send_unconfirmed", "未确认本任务的新问题已经提交，请核对原问题；代码未再次发送")
        elif state == "before_send":
            baseline = journal.data.get("manual_baseline") or {}
            if (current.get("user_count", 0) != baseline.get("user_count", 0)
                    or current.get("user_turn_id", "") != baseline.get("user_turn_id", "")
                    or page_url(page) != baseline.get("url")):
                fail("manual_question_mismatch", "当前对话已变化，无法确认安全发送位置")

    checked = probe(browser, journal)
    if not checked.get("ready"):
        fail(checked.get("error_code") or "recovery_unconfirmed", "原页面尚未就绪，请完成人工处理")
    result = scrape_one(browser, site, prompt, question_id=question_id,
                        recovery_action="" if action == "manual-auto" else "resume")
    if isinstance(result, dict):
        result["submission_mode"] = journal.data.get("submission_mode", "automatic")
        result["human_assisted"] = action in ("manual-submitted", "manual-continue")
        result["browser_backend"] = "local"
    return result
