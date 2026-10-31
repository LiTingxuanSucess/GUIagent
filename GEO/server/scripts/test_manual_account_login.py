import importlib.util
import json
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import requests

spec = importlib.util.spec_from_file_location('manual_account_login', Path(__file__).with_name('manual_account_login.py'))
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class ManualSnapshotTest(unittest.TestCase):
    def test_new_login_replaces_target_and_preserves_other_platforms(self):
        old = {'cookies': [
            {'domain': '.doubao.com', 'path': '/', 'name': 'sessionid', 'value': 'old'},
            {'domain': '.doubao.com', 'path': '/', 'name': 'removed', 'value': 'stale'},
            {'domain': '.yuanbao.tencent.com', 'path': '/', 'name': 'hy_token', 'value': 'keep'},
        ], 'local_storage': {'https://www.doubao.com': [['old', 'stale']], 'https://yuanbao.tencent.com': [['token', 'keep']]}}
        current = {'cookies': [{'domain': '.doubao.com', 'path': '/', 'name': 'sessionid', 'value': 'new', 'expires': -1, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}],
                   'origins': [{'origin': 'https://www.doubao.com', 'localStorage': [{'name': 'user', 'value': 'new'}]}]}
        got = manual.merge_snapshot(old, current, ['doubao.com'])
        self.assertEqual({c['name']: c['value'] for c in got['cookies']}, {'sessionid': 'new', 'hy_token': 'keep'})
        cookie = next(c for c in got['cookies'] if c['name'] == 'sessionid')
        self.assertTrue(cookie['http_only'])
        self.assertTrue(cookie['session'])
        self.assertEqual(cookie['same_site'], 'Lax')
        self.assertEqual(got['local_storage']['https://www.doubao.com'], [['user', 'new']])
        self.assertEqual(got['local_storage']['https://yuanbao.tencent.com'], [['token', 'keep']])

    def test_similar_domain_does_not_replace_another_identity(self):
        old = {'cookies': [{'domain': '.otherdoubao.com', 'path': '/', 'name': 'private', 'value': 'keep'}]}
        got = manual.merge_snapshot(old, {'cookies': [], 'origins': []}, ['doubao.com'])
        self.assertEqual(got['cookies'], old['cookies'])


class ManualAdminSessionTest(unittest.TestCase):
    @contextmanager
    def admin_server(self, *, reject_login=False, operation_status=200):
        state = {'generation': 'before-restart', 'writes': 0, 'requests': []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, cookie=None):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if cookie:
                    self.send_header('Set-Cookie', 'admin=' + cookie + '; Path=/')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode('utf-8'))

            def do_POST(self):
                state['requests'].append(('POST', self.path))
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if reject_login or body.get('password') != state['generation']:
                    self.reply(401, {'detail': 'Admin login rejected'})
                    return
                self.reply(200, {'ok': True}, cookie=state['generation'])

            def do_GET(self):
                state['requests'].append(('GET', self.path))
                self.reply(200, {'accounts': [{'id': 'fixture-account', 'enabled': True}]})

            def do_PATCH(self):
                state['requests'].append(('PATCH', self.path))
                self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Cookie') != 'admin=' + state['generation']:
                    self.reply(401, {'detail': 'Admin session expired'})
                    return
                if operation_status != 200:
                    self.reply(operation_status, {'detail': 'Account operation rejected'})
                    return
                state['writes'] += 1
                self.reply(200, {'enabled': False})

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = requests.Session()
        session.trust_env = False
        try:
            yield state, session, 'http://127.0.0.1:' + str(server.server_port)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_waiting_operator_refreshes_expired_session_and_writes_once(self):
        with self.admin_server() as (state, session, base):
            manual.admin_call(session, base, 'POST', '/api/admin/login',
                              {'user': 'admin', 'password': state['generation']})
            manual.admin_call(session, base, 'GET', '/api/admin/accounts')
            # Simulate a service restart while the human waits at the chooser.
            state['generation'] = 'after-restart'
            with patch.object(manual, 'service_environment', return_value={
                'GEO_ADMIN_USER': 'admin', 'GEO_ADMIN_PASSWORD': state['generation'],
            }) as refreshed:
                got = manual.admin_call(session, base, 'PATCH', '/api/admin/accounts/fixture-account', {'enabled': False})
            self.assertEqual(got, {'enabled': False})
            self.assertEqual(state['writes'], 1)
            refreshed.assert_called_once_with()
            self.assertEqual(state['requests'], [
                ('POST', '/api/admin/login'), ('GET', '/api/admin/accounts'),
                ('PATCH', '/api/admin/accounts/fixture-account'),
                ('POST', '/api/admin/login'), ('PATCH', '/api/admin/accounts/fixture-account'),
            ])

    def test_failed_reauthentication_stops_without_repeating_the_write_or_login(self):
        with self.admin_server(reject_login=True) as (state, session, base):
            with patch.object(manual, 'service_environment', return_value={'GEO_ADMIN_PASSWORD': 'fixture'}):
                with self.assertRaisesRegex(RuntimeError, 'Admin login rejected'):
                    manual.admin_call(session, base, 'PATCH', '/api/admin/accounts/fixture-account', {'enabled': False})
            self.assertEqual(state['writes'], 0)
            self.assertEqual(state['requests'], [
                ('PATCH', '/api/admin/accounts/fixture-account'), ('POST', '/api/admin/login'),
            ])

    def test_other_operation_errors_are_never_retried(self):
        for status in (403, 409, 500):
            with self.subTest(status=status), self.admin_server(operation_status=status) as (state, session, base):
                session.cookies.set('admin', state['generation'])
                with patch.object(manual, 'service_environment') as refreshed:
                    with self.assertRaisesRegex(RuntimeError, 'Account operation rejected'):
                        manual.admin_call(session, base, 'PATCH', '/api/admin/accounts/fixture-account', {'enabled': False})
                refreshed.assert_not_called()
                self.assertEqual(state['writes'], 0)
                self.assertEqual(state['requests'], [('PATCH', '/api/admin/accounts/fixture-account')])

    def test_second_unauthorized_response_is_not_retried_again(self):
        with self.admin_server(operation_status=401) as (state, session, base):
            with patch.object(manual, 'service_environment', return_value={
                'GEO_ADMIN_PASSWORD': state['generation'],
            }) as refreshed:
                with self.assertRaisesRegex(RuntimeError, 'Account operation rejected'):
                    manual.admin_call(session, base, 'PATCH', '/api/admin/accounts/fixture-account', {'enabled': False})
            refreshed.assert_called_once_with()
            self.assertEqual(state['writes'], 0)
            self.assertEqual(state['requests'], [
                ('PATCH', '/api/admin/accounts/fixture-account'), ('POST', '/api/admin/login'),
                ('PATCH', '/api/admin/accounts/fixture-account'),
            ])

    def test_connection_failure_does_not_repeat_an_uncertain_write(self):
        with requests.Session() as session:
            with patch.object(session, 'request', side_effect=requests.ConnectionError('connection lost')) as request:
                with patch.object(manual, 'service_environment') as refreshed:
                    with self.assertRaises(requests.ConnectionError):
                        manual.admin_call(session, 'http://127.0.0.1:8080', 'PATCH', '/api/admin/accounts/fixture-account', {'enabled': False})
            refreshed.assert_not_called()
            request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
