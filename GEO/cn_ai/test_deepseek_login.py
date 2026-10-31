import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from cn_ai import volcengine_doubao
from cn_ai.site_flows import PlatformFlowError
from cn_ai.deepseek_session import DeepSeekAuthObserver, cache_path, remember_rejection, rejected
from cn_ai.test_agentbay_doubao import _load_run_scrape


ORIGIN = "https://chat.deepseek.com"


class DeepSeekLoginTest(unittest.TestCase):
    def test_explicit_invalid_token_is_cached_and_new_token_is_not_blocked(self):
        raw=json.dumps({"value":"private-old-token"})
        fresh=json.dumps({"value":"private-new-token"})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{"GEO_AGENTBAY_STATE_DIR":folder}):
            observer=DeepSeekAuthObserver({ORIGIN:{"userToken":raw}})
            response=SimpleNamespace(url=ORIGIN+"/api/v0/users/current",status=200,json=lambda:{"code":40003,"msg":"invalid token"})
            request=SimpleNamespace(url=response.url,headers={"authorization":"Bearer private-old-token"},response=lambda:response)
            observer.observe(request)
            self.assertTrue(rejected(raw))
            self.assertFalse(rejected(fresh))
            self.assertNotIn("private-old-token",cache_path().read_text(encoding="utf-8"))
            self.assertEqual(cache_path().stat().st_mode & 0o777,0o600)
            with patch("cn_ai.deepseek_session.time.time",return_value=__import__('time').time()+1801):
                self.assertFalse(rejected(raw))

    def test_another_token_or_transient_error_does_not_blacklist_source(self):
        raw=json.dumps({"value":"selected-token"})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{"GEO_AGENTBAY_STATE_DIR":folder}):
            observer=DeepSeekAuthObserver({ORIGIN:{"userToken":raw}})
            response=SimpleNamespace(url=ORIGIN+"/api/v0/users/current",status=200,json=lambda:{"code":40003})
            request=SimpleNamespace(url=response.url,headers={"authorization":"Bearer other-token"},response=lambda:response)
            observer.observe(request)
            self.assertFalse(rejected(raw))
            request.headers["authorization"]="Bearer selected-token"
            response.json=lambda:{"code":500}
            observer.observe(request)
            self.assertFalse(rejected(raw))

    def test_known_rejected_login_stops_before_cloud_session_creation(self):
        run=_load_run_scrape()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{"GEO_AGENTBAY_STATE_DIR":folder}), patch.object(run,"_profile_for_site",return_value=(None,[])), patch("cn_ai.agentbay_doubao.AgentBayDoubaoLease") as cloud, patch("cn_ai.volcengine_doubao.doubao_browser_backend",return_value="agentbay"):
            args=SimpleNamespace(site="deepseek",phone="",cdp_url="http://127.0.0.1:9",prompt="问题",question_id="test")
            with self.assertRaises(PlatformFlowError):
                run._run(args)
            cloud.assert_not_called()

    def test_profile_with_cookie_hint_cannot_displace_real_token(self):
        run = _load_run_scrape()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/"accounts.json").write_text(json.dumps({"accounts":[
                {"id":"cookie-only", "phone":"preferred", "enabled":True,"sites":{"deepseek":{"status":"logged_in"}}},
                {"id":"real-token", "phone":"other", "enabled":True,"sites":{"deepseek":{"status":"logged_in"}}},
                {"id":"disabled", "enabled":False,"sites":{"deepseek":{"status":"logged_in"}}},
            ]}),encoding="utf-8")
            token = {ORIGIN:{"userToken":json.dumps({"value":"valid","__version":"1"})}}
            with patch.dict(os.environ,{"GEO_DATA_DIR":folder,"GEO_AI_PROFILE_ROOT":folder}), patch.object(
                volcengine_doubao,"profile_site_cookies",return_value=[{"name":"ds_session_id"}]
            ), patch.object(volcengine_doubao,"profile_site_storage",side_effect=lambda p,s: token if p.name=="real-token" else {ORIGIN:{"userToken":'{"value":null}'}}):
                profile,_ = run._profile_for_site("preferred","deepseek")
            self.assertEqual(profile.name,"real-token")

    def test_no_token_is_not_a_usable_deepseek_login(self):
        run = _load_run_scrape()
        for raw in ('', 'null', 'invalid', '{}', '{"value":null}', '{"value":""}', '{"value":false}'):
            self.assertEqual(run._deepseek_token({ORIGIN:{"userToken":raw}}),"")

    def test_device_storage_is_not_copied_into_another_browser(self):
        run = _load_run_scrape()
        items = {ORIGIN:{"smidV2":"local-device",".thumbcache_123":"local-device","userToken":'{"value":"valid"}',"searchEnabled":"true"}}
        self.assertEqual(set(run._seed_storage("deepseek",items)[ORIGIN]),{"userToken","searchEnabled"})
        self.assertEqual(run._seed_storage("kimi",items),items)

    def test_login_wall_fails_before_waiting_for_composer(self):
        run = _load_run_scrape()
        page = SimpleNamespace(url=ORIGIN+"/sign_in")
        with self.assertRaises(PlatformFlowError) as error:
            run._ensure_deepseek_login(SimpleNamespace(contexts=[SimpleNamespace(pages=[page])]))
        self.assertEqual(error.exception.error_code,"platform_auth_unavailable")

    def test_real_browser_repairs_null_auth_and_does_not_replay_over_refreshed_token(self):
        run = _load_run_scrape()
        source = json.dumps({"value":"saved-valid","__version":"1"})
        fresh = json.dumps({"value":"refreshed-by-website","__version":"1"})
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.route(ORIGIN+"/**",lambda route: route.fulfill(content_type="text/html",body='''<html><body><script>
              if(localStorage.getItem('userToken')===null) localStorage.setItem('userToken','{"value":null,"__version":"1"}');
              localStorage.setItem('searchEnabled',localStorage.getItem('searchEnabled')||'live-preference');
              const token=JSON.parse(localStorage.getItem('userToken')).value;
              if(!token && location.pathname!='/sign_in')location.replace('/sign_in');
              if(token)document.write('<textarea placeholder="给 DeepSeek 发送消息"></textarea>');
            </script></body></html>'''))
            page.goto(ORIGIN+"/sign_in")
            self.assertIsNone(json.loads(page.evaluate("localStorage.getItem('userToken')"))["value"])
            storage={ORIGIN:{"userToken":source,"searchEnabled":"stale-preference"}}
            with patch.object(volcengine_doubao,"profile_site_cookies",return_value=[]), patch.object(volcengine_doubao,"profile_site_storage",return_value=storage):
                self.assertTrue(run._apply_saved_login(context,Path("unused"),"deepseek",ORIGIN+"/"))
            self.assertEqual(page.evaluate("localStorage.getItem('userToken')"),source)
            self.assertEqual(page.evaluate("localStorage.getItem('searchEnabled')"),"live-preference")
            run._ensure_deepseek_login(browser)
            page.evaluate("token=>localStorage.setItem('userToken',token)",fresh)
            page.reload()
            self.assertEqual(page.evaluate("localStorage.getItem('userToken')"),fresh)
            # The existing init script must also repair null persisted values
            # before the website executes on the next document.
            page.evaluate("localStorage.setItem('userToken','{\"value\":null}')")
            page.reload()
            self.assertEqual(page.evaluate("localStorage.getItem('userToken')"),source)
            browser.close()

    def test_real_browser_replaces_nonempty_expired_auth_once(self):
        run = _load_run_scrape()
        source = json.dumps({"value":"saved-valid","__version":"1"})
        fresh = json.dumps({"value":"new-cloud-token","__version":"1"})
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            context=browser.new_context();page=context.new_page()
            page.route(ORIGIN+"/**",lambda route:route.fulfill(content_type="text/html",body='<html><body><textarea></textarea></body></html>'))
            page.goto(ORIGIN+"/sign_in")
            page.evaluate("localStorage.setItem('userToken','{\"value\":\"expired\"}')")
            with patch.object(volcengine_doubao,"profile_site_cookies",return_value=[]), patch.object(volcengine_doubao,"profile_site_storage",return_value={ORIGIN:{"userToken":source}}):
                run._apply_saved_login(context,Path("unused"),"deepseek",ORIGIN+"/")
            self.assertEqual(page.evaluate("localStorage.getItem('userToken')"),source)
            page.evaluate("token=>localStorage.setItem('userToken',token)",fresh)
            page.reload()
            self.assertEqual(page.evaluate("localStorage.getItem('userToken')"),fresh)
            browser.close()


if __name__ == "__main__":
    unittest.main()
