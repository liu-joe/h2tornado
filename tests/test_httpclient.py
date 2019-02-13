import json

from tests import AsyncHTTP2TestCase


class TestHTTP2Client(AsyncHTTP2TestCase):
    def test_get(self):
        response = self.fetch("/")
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, '')

    def test_post(self):
        response = self.fetch("/", method="POST")
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, '')

    def test_get_large_body(self):
        large_body = "asdf" * 100000
        response = self.fetch("/", body=large_body)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, large_body)

    def test_post_large_body(self):
        large_body = "asdf" * 100000
        response = self.fetch("/", method="POST", body=large_body)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, large_body)

    def test_sending_headers(self):
        extra_headers = {'Asdf': 'asdf', 'Fdsa': 'fdsa'}
        response = self.fetch("/headers", headers=extra_headers)
        self.assertEqual(response.code, 200)
        echoed_headers = json.loads(response.body)
        for key, value in extra_headers.iteritems():
            self.assertEqual(echoed_headers[key], value)

    def test_handling_stream_rst(self):
        response = self.fetch("/rst")
        self.assertEquals(response.code, 500)
        self.assertEquals(response.reason, "Stream reset")
        # Subsequent requests should work
        self.test_get()

    def test_handling_connection_rst(self):
        response = self.fetch("/connrst")
        self.assertEquals(response.code, 500)
        self.assertEquals(
            "Encountered error REFUSED_STREAM" in response.reason, True)
        # Subsequent requests should work
        self.test_get()

    def test_handling_protocol_error(self):
        response = self.fetch("/frametoolarge")
        self.assertEquals(response.code, 500)
        self.assertEquals("Received overlong frame" in response.reason, True)
        # Subsequent requests should work
        self.test_get()
