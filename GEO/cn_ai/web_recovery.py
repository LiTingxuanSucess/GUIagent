"""Private, identity-bound browser journals used to resume without resending."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cn_ai.protocol_completion import normalized
from cn_ai.search_policy import prompt_variants


HOSTS = {
    "deepseek": ("chat.deepseek.com",), "doubao": ("doubao.com",),
    "yiyan": ("wenxin.baidu.com", "yiyan.baidu.com", "chat.baidu.com"),
    "yuanbao": ("yuanbao.tencent.com",), "qianwen": ("qianwen.com", "tongyi.aliyun.com"),
    "kimi": ("kimi.com", "kimi.moonshot.cn", "moonshot.cn"),
}


def fail(code, message):
    from cn_ai.site_flows import PlatformFlowError
    raise PlatformFlowError(code, message, False)


def display_prompt_matches(actual, expected, site):
    actual = normalized(actual)
    expected_variants = [normalized(value) for value in prompt_variants(site, expected)]
    if site == 'doubao':
        # Doubao can insert typographic spaces at Chinese/ASCII boundaries.
        # Keep spaces inside Latin words/numbers significant.
        def compact(value):
            return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])\s+(?=[\u3400-\u9fff])', '', value)
        actual = compact(actual)
        expected_variants = [compact(value) for value in expected_variants]
    return actual in expected_variants


def page_url(page):
    try:
        parts = urlsplit(page.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return ""


def is_site_page(page, site):
    try:
        host = (urlsplit(page.url).hostname or "").lower()
        return any(host == suffix or host.endswith("."+suffix) for suffix in HOSTS.get(site, ()))
    except Exception:
        return False


def target_id(page):
    session = page.context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        session.detach()


def snapshot(page, site):
    """Read current visible state only; historic network URLs are not blockers."""
    return page.evaluate(r"""site => {
      const visible = e => {
        if (!e) return false;
        const r=e.getBoundingClientRect(); if(r.width<=2 || r.height<=2) return false;
        for(let p=e;p;p=p.parentElement) {
          const s=getComputedStyle(p);
          if(s.display==='none' || s.visibility==='hidden' || Number(s.opacity||1)===0) return false;
        }
        return true;
      };
      const userSelectors={
        deepseek:'[data-message-role="user"], [data-role="user"], .ds-message--user, .ds-user-message, .fbb737a4',
        doubao:'[data-message-role="user"] [data-testid="message_text_content"], [data-role="user"], [data-testid="message_text_content"]:has([datatype="query-source-paragraph-start"])',
        yiyan:'.conversation-flow-question-container .cs-question-pure-text',
        yuanbao:'[data-conv-speaker="human"]',
        qianwen:'[data-message-role="user"], [data-role="user"], .user-message, .question-text, .question-text-card',
        kimi:'.segment.segment-user, [data-message-role="user"], [data-role="user"]'
      };
      const users=[...document.querySelectorAll(userSelectors[site]||'[data-message-role="user"]')]
        .filter(e=>visible(e)&&!e.closest('aside,nav,textarea,[contenteditable=true]'));
      const user=users.pop();
      const userText=site==='kimi'&&user ? (user.querySelector('.user-content__text,.user-content')||user) : user;
      const editors=[...document.querySelectorAll('textarea,[contenteditable=true],[role=textbox]')].filter(visible);
      const editor=editors.find(e=>!e.closest('[role=dialog],[aria-modal=true],[class*=captcha],[id*=captcha]'));
      const challengeFrames=[...document.querySelectorAll('iframe')].filter(e=>visible(e)&&
        /captcha|verifycenter|tcaptcha|\/punish|geetest|hcaptcha|recaptcha/i.test(e.src||''));
      const dialogs=[...document.querySelectorAll('[class*=captcha],[id*=captcha],[class*=verify],[id*=verify],[class*=geetest]')]
        .filter(e=>visible(e)&&!e.closest('[data-message-role],[data-role=user],[data-role=assistant],.segment,[data-conv-speaker],.answer-container'));
      const shapeHint=[...document.querySelectorAll('div,p,span,h1,h2,button')].some(e=>visible(e)
        &&!e.closest('[data-message-role],[data-role=user],[data-role=assistant],.segment,[data-conv-speaker],.answer-container')
        &&(e.innerText||'').trim().length>0&&(e.innerText||'').trim().length<40
        &&/点击图中|请依次点击|^依次点击|向右滑动|向右拖动/.test((e.innerText||'').trim()));
      const challenge=challengeFrames.length>0 || shapeHint || dialogs.some(e=>/请完成安全验证|拖动滑块|滑动滑块|点选|点击.*验证|安全验证/.test(e.innerText||''));
      const login=[...document.querySelectorAll('button,a,[role=button]')].filter(e=>visible(e)&&
        !e.closest('[data-message-role],[data-role=user],[data-role=assistant],.segment,[data-conv-speaker],.answer-container'))
        .some(e=>/^(登录|登录\s*\/\s*注册|立即登录|登录后使用)$/.test((e.innerText||'').trim()));
      const placeholder=editor ? (editor.getAttribute('placeholder')||editor.getAttribute('data-placeholder')||'') : '';
      let account=[...document.querySelectorAll('header [class*=user-avatar],header [class*=user-info],aside [class*=user-info],.hyc-user-info,.user-info-container,.chat-aside-user-info,[data-testid=user-menu],[data-testid=user-info],[data-testid=user-avatar],[aria-label="账户"],[aria-label="个人中心"]')].some(visible);
      if (site==='kimi') {
        account=[...document.querySelectorAll('button.user-info-container,.user-info-container .user-profile-trigger,.user-area .user-profile-trigger')]
          .some(e=>visible(e)&&!!(e.innerText||'').trim()&&!/登录|注册|我的\s*Kimi/.test(e.innerText||'')&&
            !e.closest('[data-message-role],[data-role=user],[data-role=assistant],.segment'));
      }
      // Doubao's current sidebar footer renders the signed-in username in a
      // plain button. Limit this evidence to that observed account control;
      // usernames quoted in a conversation or a guest composer are not auth.
      if (site==='doubao' && !account) {
        account=[...document.querySelectorAll('[class*="data-sidebar-footer-account-menu"] button')]
          .some(e=>visible(e)&&!e.closest('[data-message-role],[data-role=user],[data-role=assistant],.segment,[data-conv-speaker],.answer-container')&&
            /^用户[A-Za-z0-9]{4,}$/.test((e.innerText||'').trim()));
      }
      if (site==='yuanbao' && !account) {
        account=[...document.querySelectorAll('.yb-nav__user .nick-info-container')]
          .some(e=>visible(e)&&!e.closest('[data-conv-speaker],.answer-container')&&
            /^(用户[A-Za-z0-9]{4,}|1[3-9]\d\*{4}\d{4})$/.test((e.innerText||'').trim()));
      }
      // Qianwen's new sidebar uses a plain button in its footer, with none
      // of the previous user/avatar selectors. Match that real account
      // control, excluding navigation, conversation text and the guest CTA.
      if (site==='qianwen' && !account) {
        account=[...document.querySelectorAll('.bg-pc-sidebar.text-primary.px-6.pt-3 > button')]
          .some(e=>visible(e)&&!!(e.innerText||'').trim()&&!/登录|注册/.test(e.innerText||''));
      }
      const otp=[...document.querySelectorAll('input[autocomplete="one-time-code"],input[placeholder*="验证码"],input[aria-label*="验证码"]')]
        .some(e=>visible(e)&&!e.closest('[class*=captcha],[id*=captcha]'));
      const loginText=(document.body&&document.body.innerText)||'';
      const smsWaiting=otp&&(/短信.{0,12}(已发送|已发出)|验证码已发送|已发送.{0,12}验证码/.test(loginText)||
        /\d+\s*(秒|s)\s*(后|重新)|重新发送\s*[（(]?\s*\d+|\d+\s*(秒|s).*重发/.test(loginText));
      return {last_user:(userText&&(userText.innerText||userText.textContent)||'').trim(),user_count:users.length+(user?1:0),
        user_turn_id:user ? (user.getAttribute('data-message-id')||user.getAttribute('data-virtual-list-item-key')||'') : '',
        editor_present:!!editor,editor_ready:!!editor&&!editor.disabled&&!editor.readOnly,
        logged_out:login||/请登录|登录后|请先登录/.test(placeholder)||/\/sign_in(?:\/|$)/.test(location.pathname),
        account_marker:account,challenge_visible:challenge,waiting_sms:smsWaiting};
    }""", site) or {}


def fresh_protocol_auth(page, site):
    # DeepSeek's profile control uses unstable generated classes. Its current
    # user endpoint has an explicit authenticated business result; keep all
    # credentials and personal fields inside the page and return one boolean.
    if site not in ("deepseek", "yiyan"):
        return False
    from cn_ai import budget
    timeout = budget.timeout_ms(2000)
    if timeout <= 0:
        return False
    try:
        if site == "yiyan":
            # The current Wenxin landing page has no account avatar in its
            # collapsed navigation. Re-fetch its server-rendered login flag;
            # do not infer authentication from the public editor or cookies.
            return page.evaluate("""async timeout => {
              const r=await fetch(location.href,{credentials:'include',cache:'no-store',signal:AbortSignal.timeout(timeout)});
              if(!r.ok)return false;
              const doc=new DOMParser().parseFromString(await r.text(),'text/html');
              for(const script of doc.querySelectorAll('script[type="application/json"]')) {
                try {const data=JSON.parse(script.textContent||'{}'),u=data.userInfo||{};
                  if((u.isUserLogin===1||u.isUserLogin===true)&&typeof u.name==='string'&&u.name.trim())return true;
                } catch (_) {}
              }
              return false;
            }""", timeout) is True
        return page.evaluate("""async timeout => {
          let token='';
          try {token=JSON.parse(localStorage.getItem('userToken')||'{}').value||'';} catch (_) {}
          const headers=token?{Authorization:'Bearer '+token}:{};
          const r=await fetch('/api/v0/users/current',{credentials:'include',cache:'no-store',headers,signal:AbortSignal.timeout(timeout)});
          const value=await r.json(), data=value.data||{}, user=data.biz_data||{};
          return r.ok&&value.code===0&&data.biz_code===0&&typeof user.id==='string'&&user.id.length>0;
        }""", timeout) is True
    except Exception:
        return False


class RecoveryJournal:
    def __init__(self, path, site, prompt, question_id, account_id, *, create=False):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            fail("recovery_unconfirmed", "恢复记录路径无效")
        identity = {"site_id": site, "prompt": prompt, "question_id": question_id,
                    "account_id": account_id, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        if not account_id or not question_id or site not in HOSTS:
            fail("recovery_unconfirmed", "恢复记录缺少账号或任务身份")
        try:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8_000_000:
                fail("recovery_unconfirmed", "恢复记录格式无效")
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not create:
                fail("recovery_unconfirmed", "当前任务没有恢复记录")
            self.data = {"version": 1, **identity, "send_state": "before_send", "phase": "before_send",
                         "target_id": "", "page_url": "", "prior_logged_in": False}
            self.save()
        except (ValueError, OSError):
            fail("recovery_unconfirmed", "恢复记录无法读取")
        if not isinstance(self.data, dict) or any(self.data.get(k) != v for k, v in identity.items()):
            fail("recovery_unconfirmed", "恢复记录与当前账号或问题不匹配")
        if self.data.get("send_state") not in ("before_send", "sending", "sent"):
            fail("recovery_unconfirmed", "恢复记录发送状态无效")
        if self.data["send_state"] == "sending" and "before_user_count" not in self.data:
            fail("recovery_unconfirmed", "发送中记录缺少原轮次证据，未重新提交")

    @classmethod
    def from_env(cls, site, prompt, question_id, *, create=False, path=None):
        selected = path or os.getenv("GEO_WEB_RECOVERY_FILE", "")
        if not selected:
            return None
        return cls(selected, site, prompt, question_id, os.getenv("GEO_WEB_RECOVERY_ACCOUNT_ID", ""), create=create)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data["updated_at"] = time.time()
        temporary = self.path.with_name(self.path.name+f".{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def bind(self, page):
        identity = target_id(page)
        previous = self.data.get("target_id")
        if previous and previous != identity and self.data["send_state"] != "before_send":
            fail("recovery_unconfirmed", "已发送的问题不能切换页面")
        self.data.update(target_id=identity, page_url=page_url(page))
        if os.getenv("GEO_WEB_RECOVERY_PRIOR_LOGGED_IN") == "1":
            self.data["prior_logged_in"] = True
        self.save()

    def state(self, state, page=None):
        if self.data["send_state"] == "sent" and state != "sent":
            fail("recovery_unconfirmed", "已发送的问题不能再次提交")
        self.data.update(send_state=state, phase=state)
        if page is not None:
            self.data["page_url"] = page_url(page)
            if state == "sending":
                current = snapshot(page, self.data["site_id"])
                self.data.update(before_user_count=current.get("user_count", 0),
                                 before_user_turn_id=current.get("user_turn_id", ""),
                                 before_page_url=page_url(page))
        self.save()

    def matches_sent_turn(self, page, current):
        return prompt_matched_from_snapshot(current, self.data)

    def checkpoint(self, value):
        self.data.update(send_state="sent", phase="answer_completed", checkpoint=value)
        self.save()

    def failure(self, error, page=None):
        self.data.update(last_error_code=getattr(error, "error_code", "browser_disconnected"),
                         phase="verification" if getattr(error, "error_code", "") == "platform_risk_control" else "interrupted")
        if page is not None:
            self.data["page_url"] = page_url(page)
        self.save()

    def find(self, browser):
        wanted = self.data.get("target_id")
        if not wanted:
            fail("recovery_unconfirmed", "恢复记录没有原页面身份")
        for context in browser.contexts:
            for page in context.pages:
                if is_site_page(page, self.data["site_id"]) and target_id(page) == wanted:
                    return page
        fail("recovery_page_missing", "原提问页面已不存在，未重新发送")


def prepare(browser, site, prompt, question_id, *, path=None, bootstrap=False):
    journal = RecoveryJournal.from_env(site, prompt, question_id, create=True, path=path)
    if journal is None:
        return None
    missing_before_send = False
    if journal.data.get("target_id"):
        # Only a durable, identity-matching before_send record can safely use
        # another existing page after its original target has disappeared.
        try:
            journal.find(browser)
            return journal
        except Exception as error:
            if (getattr(error, "error_code", "") != "recovery_page_missing"
                    or journal.data["send_state"] != "before_send"):
                raise
            missing_before_send = True
    pages = [page for context in browser.contexts for page in context.pages if is_site_page(page, site)]
    if len(pages) == 1:
        journal.bind(pages[0])
    elif bootstrap or missing_before_send:
        fail("recovery_unconfirmed", "无法唯一定位该账号的平台页面")
    return journal


def prompt_matched_from_snapshot(current, journal_data):
    """Whether the visible user turn still matches the journaled question."""
    if not display_prompt_matches(current.get("last_user", ""), journal_data.get("prompt", ""), journal_data.get('site_id')):
        return False
    if journal_data.get("send_state") == "sent":
        return True
    if journal_data.get("send_state") != "sending":
        return False
    return (current.get("user_count", 0) > journal_data.get("before_user_count", 0)
            or bool(current.get("user_turn_id") and current["user_turn_id"] != journal_data.get("before_user_turn_id")))


def evaluate_ready(current, journal_data, *, authenticated=None, require_fresh_auth=None):
    """Shared ready gate for the server probe and the CLI `ready` tool.

    After a CLI attempt the DOM account marker is often virtualized. When the
    journal already recorded a logged-in shell and the page still shows an
    interactive editor with no challenge/login wall, treat that as authenticated
    enough for resume instead of failing every candidate on a missing avatar.
    """
    if require_fresh_auth is None:
        require_fresh_auth = os.getenv("GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH") == "1"
    if authenticated is None:
        authenticated = bool(current.get("account_marker"))
    send_state = journal_data.get("send_state") or "before_send"
    matched = prompt_matched_from_snapshot(current, journal_data)
    result = {
        "ok": False, "ready": False, "reason": "recovery_unconfirmed", "error_code": "recovery_unconfirmed",
        "authenticated": bool(authenticated), "prompt_matched": matched,
        "phase": journal_data.get("phase") or send_state, "send_state": send_state,
        **{k: bool(current.get(k)) for k in
           ("challenge_visible", "editor_present", "editor_ready", "logged_out", "account_marker", "waiting_sms")},
    }
    prior = bool(journal_data.get("prior_logged_in"))
    # DeepSeek still has a dedicated authenticated business probe; other sites
    # often virtualize the avatar after captcha, so the journaled login plus an
    # interactive editor is enough under RequireFreshAuth.
    soft_auth = (prior and current.get("editor_present") and not current.get("logged_out")
                 and journal_data.get("site_id") != "deepseek")
    if current.get("challenge_visible"):
        result.update(reason="captcha_visible", error_code="platform_risk_control", phase="verification")
    elif current.get("waiting_sms"):
        result.update(reason="sms_required", error_code="sms_required", phase="waiting_sms")
    elif current.get("logged_out"):
        result.update(reason="logged_out", error_code="platform_auth_unavailable")
    elif not (current.get("editor_present") or authenticated):
        result.update(reason="editor_unavailable", error_code="recovery_unconfirmed")
    elif not (authenticated or prior):
        result.update(reason="login_unconfirmed", error_code="recovery_unconfirmed")
    elif require_fresh_auth and not authenticated and not soft_auth:
        result.update(reason="login_unconfirmed", error_code="recovery_unconfirmed")
    elif send_state == "before_send" and not current.get("editor_ready"):
        result.update(reason="editor_unavailable", error_code="recovery_unconfirmed")
    elif send_state != "before_send" and not matched:
        result.update(reason="question_unconfirmed", error_code="recovery_unconfirmed")
    else:
        result.update(ok=True, ready=True, reason="ready", error_code="")
    return result


def probe(browser, journal):
    result = {"ok": False, "ready": False, "recovery_action": "probe", "context_valid": False,
              "phase": journal.data["phase"], "send_state": journal.data["send_state"],
              "target_id": journal.data.get("target_id", ""), "reason": "recovery_unconfirmed"}
    try:
        page = journal.find(browser)
        current = snapshot(page, journal.data["site_id"])
        if (not current.get('logged_out') and not current.get('challenge_visible')
                and journal.data.get("manual_workflow") and journal.data.get("manual_turn")):
            from cn_ai.manual_workflow import validate_manual_turn
            if not validate_manual_turn(current, journal.data, page_url(page)):
                fail("manual_question_mismatch", "当前页面不是人工确认的原问题轮次")
        authenticated = bool(current.get("account_marker"))
        if ((os.getenv("GEO_WEB_RECOVERY_REQUIRE_FRESH_AUTH") == "1" or journal.data["site_id"] in ("deepseek", "yiyan")) and not authenticated
                and not current.get("challenge_visible") and not current.get("waiting_sms")
                and not current.get("logged_out")):
            authenticated = fresh_protocol_auth(page, journal.data["site_id"])
        matched = journal.matches_sent_turn(page, current)
        # The known complete protocol can prove identity when a site's user
        # DOM is virtualized. It never promotes a partial or unmatched turn.
        if (not matched and journal.data["send_state"] == "sent" and not current.get("challenge_visible")
                and not current.get("waiting_sms") and not current.get("logged_out")):
            from cn_ai.scraper import _fetch_deepseek_history, _fetch_yuanbao_detail, _fetch_yiyan_history
            fetch = {"deepseek": _fetch_deepseek_history, "yuanbao": _fetch_yuanbao_detail, "yiyan": _fetch_yiyan_history}.get(journal.data["site_id"])
            extra = fetch(page, journal.data["prompt"]) if fetch else None
            matched = bool(extra and extra.get("prompt_matched"))
        # Keep evaluate_ready's prompt_matched in sync when protocol history wins.
        current = dict(current)
        if matched and not prompt_matched_from_snapshot(current, journal.data):
            current["last_user"] = journal.data["prompt"]
            if journal.data["send_state"] == "sending":
                current["user_count"] = journal.data.get("before_user_count", 0) + 1
        decided = evaluate_ready(current, journal.data, authenticated=authenticated)
        result.update(context_valid=True, target_id=journal.data.get("target_id", ""), **decided)
        result["prompt_matched"] = matched
        if decided.get("ready"):
            if authenticated and not journal.data.get("prior_logged_in"):
                journal.data["prior_logged_in"] = True
                journal.save()
            if matched and journal.data["send_state"] == "sending":
                journal.state("sent", page)
            result.update(ok=True, ready=True, reason="ready", phase=journal.data["phase"],
                          send_state=journal.data["send_state"], error_code="")
        return result
    except Exception as error:
        result.update(reason=getattr(error, "error_code", "recovery_unconfirmed"),
                      error_code=getattr(error, "error_code", "recovery_unconfirmed"))
        return result
