import os
import unittest
from unittest.mock import patch

from server.scripts.run_scrape import configure_loopback_proxy_bypass


class LoopbackProxyTest(unittest.TestCase):
    def test_cloud_api_still_uses_configured_proxy(self):
        with patch.dict(os.environ, {'HTTP_PROXY':'http://proxy.invalid:8080','HTTPS_PROXY':'http://proxy.invalid:8080'}, clear=True):
            configure_loopback_proxy_bypass()
            self.assertEqual(os.environ['HTTP_PROXY'],'http://proxy.invalid:8080')
            self.assertEqual(os.environ['HTTPS_PROXY'],'http://proxy.invalid:8080')
            self.assertEqual(set(os.environ['NO_PROXY'].split(',')), {'localhost','127.0.0.1','::1'})
            self.assertNotIn('*',os.environ['NO_PROXY'])

    def test_existing_bypass_hosts_are_preserved_in_both_cases(self):
        with patch.dict(os.environ, {'NO_PROXY':'existing.example','no_proxy':'another.example'}, clear=True):
            configure_loopback_proxy_bypass()
            self.assertIn('existing.example',os.environ['NO_PROXY'].split(','))
            self.assertIn('another.example',os.environ['NO_PROXY'].split(','))
            self.assertEqual(os.environ['NO_PROXY'],os.environ['no_proxy'])

    def test_repeated_configuration_does_not_duplicate_hosts(self):
        with patch.dict(os.environ, {'NO_PROXY':'localhost,127.0.0.1'}, clear=True):
            configure_loopback_proxy_bypass()
            initial=os.environ['NO_PROXY']
            configure_loopback_proxy_bypass()
            self.assertEqual(initial,os.environ['NO_PROXY'])


if __name__=='__main__':
    unittest.main()
