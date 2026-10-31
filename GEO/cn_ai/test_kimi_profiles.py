import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cn_ai import volcengine_doubao
from cn_ai.test_agentbay_doubao import _load_run_scrape


class KimiProfileTest(unittest.TestCase):
    def test_analytics_profile_cannot_displace_real_login(self):
        run=_load_run_scrape()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'accounts.json').write_text(json.dumps({'accounts':[
                {'id':'analytics','phone':'preferred','enabled':True,'sites':{'kimi':{'status':'logged_in'}}},
                {'id':'actual','phone':'other','enabled':True,'sites':{'kimi':{'status':'logged_in'}}},
            ]}),encoding='utf-8')
            def storage(profile,site):
                return {'https://www.kimi.com':{'access_token':'fixture-access'}} if profile.name=='actual' else {'https://www.kimi.com':{'theme':'dark'}}
            with patch.dict(os.environ,{'GEO_DATA_DIR':folder,'GEO_AI_PROFILE_ROOT':folder}),patch.object(volcengine_doubao,'profile_site_cookies',return_value=[{'name':'HMACCOUNT','value':'analytics'}]),patch.object(volcengine_doubao,'profile_site_storage',side_effect=storage):
                profile,_=run._profile_for_site('preferred','kimi')
            self.assertEqual(profile.name,'actual')

    def test_empty_and_placeholder_tokens_are_not_login_material(self):
        run=_load_run_scrape()
        for value in ('','null','undefined','false','{}','{"value":null}','""'):
            self.assertFalse(run._kimi_login_material({'https://www.kimi.com':{'access_token':value}}),value)
        self.assertTrue(run._kimi_login_material({'https://www.kimi.com':{'refresh_token':'fixture-refresh'}}))
        self.assertTrue(run._kimi_login_material({'https://www.kimi.com':{'access_token':'{"value":"fixture-access"}'}}))

    def test_disabled_account_is_not_used_even_if_it_has_auth(self):
        run=_load_run_scrape()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'accounts.json').write_text(json.dumps({'accounts':[{'id':'disabled','phone':'preferred','enabled':False}]}),encoding='utf-8')
            with patch.dict(os.environ,{'GEO_DATA_DIR':folder,'GEO_AI_PROFILE_ROOT':folder}),patch.object(volcengine_doubao,'profile_site_storage') as storage:
                self.assertEqual(run._profile_for_site('preferred','kimi'),(None,[]))
            storage.assert_not_called()


if __name__=='__main__':unittest.main()
