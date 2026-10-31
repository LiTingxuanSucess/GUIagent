import hashlib
import io
import hmac
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import geo_ask
import geo_callback_client


class ClientSigningTest(unittest.TestCase):
    def test_cancelled_job_stops_polling_without_waiting_for_callback(self):
        self.assertEqual(geo_ask.classify_wait('cancelled'),'finished')
        job={'http_status':200,'status':'cancelled','callback_status':'skipped'}
        with patch.object(geo_ask,'call',return_value=job) as call,redirect_stdout(io.StringIO()):
            self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','saved_job'),3)
            call.assert_called_once()
        class Event:
            def wait(self,seconds):return False
        class Box:event=Event()
        poll=unittest.mock.Mock(return_value=job)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(geo_callback_client.wait_for_callback(Box(),poll,job_id='saved_job'))
        poll.assert_called_once()

    def test_permanent_job_read_errors_stop_but_503_keeps_polling_same_job(self):
        class Event:
            def wait(self,seconds):return False
        class Box:event=Event()
        for status in (401,403,404):
            with self.subTest(http_status=status):
                output=io.StringIO()
                with patch.object(geo_ask,'call',return_value={'http_status':status}) as call,redirect_stdout(output):
                    self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','saved_job'),5)
                call.assert_called_once()
                self.assertEqual(call.call_args.args[1:3],('GET','/api/v1/jobs/saved_job'))
                self.assertIn('job_id=saved_job',output.getvalue())
                self.assertIn('no resubmission',output.getvalue())
                output=io.StringIO();poll=unittest.mock.Mock(return_value={'http_status':status})
                with redirect_stdout(output):
                    self.assertEqual(geo_callback_client.wait_for_callback(Box(),poll,job_id='saved_job'),{'http_status':status})
                poll.assert_called_once()
                self.assertIn('job_id=saved_job',output.getvalue())
        with patch.object(geo_ask.time,'sleep'),patch.object(geo_ask,'call',side_effect=[{'http_status':503},{'http_status':200,'status':'succeeded'}]) as call,redirect_stdout(io.StringIO()):
            self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','saved_job'),0)
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(args.args[2]=='/api/v1/jobs/saved_job' for args in call.call_args_list))
        poll=unittest.mock.Mock(side_effect=[{'http_status':503},{'status':'succeeded','callback_status':'ok'}])
        with redirect_stdout(io.StringIO()):geo_callback_client.wait_for_callback(Box(),poll,job_id='saved_job')
        self.assertEqual(poll.call_count,2)

    def test_default_poll_waits_through_long_queue_and_moving_estimates(self):
        clock=[0.0]
        states=[{'status':'queued','deadline_policy':'web_first'},
                {'status':'running','deadline_policy':'web_first','deadline_at':'2026-10-16T00:00:00+08:00'},
                {'status':'running','deadline_policy':'web_first','deadline_at':'2026-10-17T00:00:00+08:00'},
                {'status':'succeeded','job_id':'job_fixture','results':[]}]
        with patch.object(geo_ask.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(geo_ask.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+400)), \
             patch.object(geo_ask,'call',side_effect=states) as call,redirect_stdout(io.StringIO()):
            self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','job_fixture'),0)
        self.assertGreater(clock[0],600)
        self.assertEqual(call.call_count,4)
        self.assertTrue(all(args[0][1]=='GET' for args in call.call_args_list))

    def test_explicit_poll_limit_returns_unfinished_and_never_resubmits(self):
        clock=[0.0];output=io.StringIO()
        with patch.object(geo_ask.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(geo_ask.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)), \
             patch.object(geo_ask,'call',return_value={'status':'queued','deadline_policy':'web_first'}) as call,redirect_stdout(output):
            self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','job_fixture',10),4)
        self.assertEqual(clock[0],10)
        self.assertEqual(call.call_count,2)
        self.assertIn('unfinished job_id=job_fixture status=queued',output.getvalue())
        self.assertIn('client_wait_limit_only',output.getvalue())

    def test_poll_network_error_retries_same_read_without_losing_job(self):
        with patch.object(geo_ask.time,'sleep'),patch.object(geo_ask,'call',side_effect=[OSError('offline'),{'status':'succeeded'}]) as call,redirect_stdout(io.StringIO()):
            self.assertEqual(geo_ask.poll_job('http://fixture','ak','sk','job_fixture'),0)
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(args[0][2]=='/api/v1/jobs/job_fixture' for args in call.call_args_list))

    def test_callback_wait_has_no_hidden_600_second_limit_but_respects_explicit_cap(self):
        class Event:
            def __init__(self,clock,step):self.clock=clock;self.step=step
            def wait(self,seconds):self.clock[0]+=self.step*seconds;return False
        class Box:pass
        clock=[0.0];box=Box();box.event=Event(clock,100)
        with patch.object(geo_ask.time,'monotonic',side_effect=lambda:clock[0]),redirect_stdout(io.StringIO()):
            with patch.object(geo_callback_client,'api') as unused:
                poll=unittest.mock.Mock(side_effect=[OSError('temporary read failure'),{'status':'queued'},{'status':'running','deadline_policy':'web_first'}, {'status':'succeeded','callback_status':'failed'}])
                geo_callback_client.wait_for_callback(box,poll)
                self.assertEqual(poll.call_count,4)
                unused.assert_not_called()
        self.assertGreater(clock[0],600)
        clock[0]=0;box.event=Event(clock,1)
        with patch.object(geo_ask.time,'monotonic',side_effect=lambda:clock[0]),redirect_stdout(io.StringIO()):
            poll=unittest.mock.Mock(return_value={'status':'queued','deadline_policy':'web_first'})
            geo_callback_client.wait_for_callback(box,poll,10)
        self.assertEqual(clock[0],10)

    def test_both_clients_can_query_jobs_with_filters(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                canonical = '\n'.join(['GET', path, self.headers['X-Geo-Timestamp'],
                                       self.headers['X-Geo-Nonce'], hashlib.sha256(b'').hexdigest()])
                expected = hmac.new(b'fixture-secret', canonical.encode(), hashlib.sha256).hexdigest()
                self.send_response(200 if hmac.compare_digest(expected, self.headers['X-Geo-Signature']) else 401)
                self.end_headers()
                self.wfile.write(b'{"jobs":[],"total":0}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for call in (geo_ask.call, geo_callback_client.api):
                with self.subTest(client=call.__module__):
                    response = call('http://127.0.0.1:' + str(server.server_port), 'GET',
                                    '/api/v1/jobs?request_id=customer-1&limit=1', 'fixture-ak', 'fixture-secret')
                    self.assertEqual(response['http_status'], 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_callback_query_and_escaped_path_match_server_signing(self):
        body = json.dumps({'job_id': 'fixture', 'status': 'succeeded'}).encode()
        canonical = '\n'.join(['POST', '/callback/中文', '123', 'fixture-nonce', hashlib.sha256(body).hexdigest()])
        signature = hmac.new(b'fixture-secret', canonical.encode(), hashlib.sha256).hexdigest()
        headers = {'X-Geo-AK': 'fixture-ak', 'X-Geo-Timestamp': '123',
                   'X-Geo-Nonce': 'fixture-nonce', 'X-Geo-Signature': signature}
        with tempfile.TemporaryDirectory() as directory:
            box = geo_callback_client.CallbackBox('fixture-secret', Path(directory) / 'callback.json')
            status, _ = box.handle('/callback/%E4%B8%AD%E6%96%87?token=fixture', headers, body)
            self.assertEqual(status, 200)
            self.assertTrue(box.received['sig_ok'])


if __name__ == '__main__':
    unittest.main()
