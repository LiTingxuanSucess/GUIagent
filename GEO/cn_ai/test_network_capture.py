import unittest

from cn_ai.scraper import NetworkCapture


class FakeRequest:
    url = "https://example.com/api/chat/stream"
    resource_type = "fetch"

    def __init__(self, response):
        self._response = response

    def response(self):
        return self._response


class FakeResponse:
    url = FakeRequest.url
    status = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self):
        self.body_calls = 0
        self.request = FakeRequest(self)

    def body(self):
        self.body_calls += 1
        return b'{"answer":"finished"}'


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class NetworkCaptureTests(unittest.TestCase):
    def test_flush_reads_finished_responses_once_outside_callbacks(self):
        page = FakePage()
        response = FakeResponse()
        capture = NetworkCapture()
        capture.attach(page)

        self.assertNotIn("response", page.handlers)
        capture.flush()
        self.assertEqual(response.body_calls, 0)

        page.handlers["requestfinished"](response.request)
        self.assertEqual(response.body_calls, 0)
        self.assertEqual(len(capture.items), 0)

        capture.flush()
        self.assertEqual(response.body_calls, 1)
        self.assertEqual(len(capture.items), 1)
        capture.flush()
        self.assertEqual(response.body_calls, 1)

    def test_yiyan_same_endpoint_keeps_request_bound_streams_separate(self):
        capture = NetworkCapture("yiyan", "current question")
        page = FakePage()
        capture.attach(page)
        for prompt in ["old question", "current question"]:
            response = FakeResponse()
            response.url = "https://chat.baidu.com/aichat/api/conversation"
            response.request.url = response.url
            response.request.method = "POST"
            response.request.post_data_json = {"query": prompt}
            page.handlers["request"](response.request)
            capture._ingest(response)
        self.assertEqual(len(capture.items), 2)
        self.assertFalse(capture.items[0]["request_prompt_matched"])
        self.assertTrue(capture.items[1]["request_prompt_matched"])

    def test_yiyan_old_response_is_rejected_even_when_its_question_is_repeated(self):
        capture = NetworkCapture("yiyan", "repeated question")
        response = FakeResponse()
        response.url = "https://chat.baidu.com/aichat/api/conversation"
        response.request.post_data_json = {"query": "repeated question"}
        # Its request predates this capture, so no matching request event ran.
        capture._ingest(response)
        self.assertFalse(capture.items[0]["request_prompt_matched"])

    def test_real_browser_request_and_response_keep_the_same_binding(self):
        from playwright.sync_api import sync_playwright
        from cn_ai.test_yiyan_citations import stream, PROMPT
        from cn_ai.yiyan_citations import current_yiyan_sources
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            def route(r):
                is_stream = r.request.url.endswith('/aichat/api/conversation')
                r.fulfill(content_type='text/event-stream' if is_stream else 'text/html',
                          body=stream()['body'] if is_stream else '<html>offline fixture</html>')
            page.route('https://chat.baidu.com/**', route)
            page.goto('https://chat.baidu.com/fixture')
            capture = NetworkCapture('yiyan', PROMPT)
            capture.attach(page)
            with page.expect_request_finished(lambda request: request.url.endswith('/aichat/api/conversation')):
                page.evaluate('async query=>await(await fetch("/aichat/api/conversation",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query})})).text()', PROMPT)
            capture.flush()
            self.assertEqual(len(current_yiyan_sources(capture.items, PROMPT, page.url)[0]), 1)
            self.assertTrue(capture.items[0]['request_prompt_matched'])
            browser.close()


if __name__ == "__main__":
    unittest.main()
