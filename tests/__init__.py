import os
import ssl
import json


from h2tornado.client import AsyncHTTP2Client

from tornado.gen import coroutine, sleep
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application, RequestHandler

from tornado_http2.server import Server
from tornado_http2.constants import ErrorCode, FrameType, Setting
from tornado_http2.frames import Frame


class EchoHandler(RequestHandler):
    def initialize(self, *args, **kwargs):
        self.sleep_for = int(
            self.request.headers.get("Sleep-Before-Return", "0"))
        super(EchoHandler, self).initialize(*args, **kwargs)

    @coroutine
    def get(self):
        yield sleep(self.sleep_for)
        self.write(self.request.body)

    @coroutine
    def post(self):
        yield sleep(self.sleep_for)
        self.write(self.request.body)


class EchoHeadersHandler(RequestHandler):

    def get(self):
        self.write(json.dumps(dict(self.request.headers)))


class StreamRSTHandler(RequestHandler):
    def get(self):
        self.request.connection.reset()


class ConnectionRSTHandler(RequestHandler):
    def get(self):
        conn = self.request.connection.conn
        conn._write_frame(conn._goaway_frame(ErrorCode.REFUSED_STREAM, 0, ''))


class FrameTooLargeHandler(RequestHandler):
    def get(self):
        stream = self.request.connection
        # Send a data frame that is 1 over the masx frame size setting
        # to force a protocol error
        stream.conn._write_frame(Frame(
            FrameType.DATA, 0, stream.stream_id, "a" * stream.conn.setting(Setting.MAX_FRAME_SIZE) + "a"))


class AsyncHTTP2TestCase(AsyncHTTPTestCase):
    def get_app(self):
        return Application([('/', EchoHandler),
                            ('/headers', EchoHeadersHandler),
                            ('/rst', StreamRSTHandler),
                            ('/connrst', ConnectionRSTHandler),
                            ('/frametoolarge', FrameTooLargeHandler), ], debug=True)

    def get_http_client(self):
        return AsyncHTTP2Client()

    def get_http_server(self):
        return Server(self._app, **self.get_httpserver_options())

    def get_httpserver_options(self):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        ssl_ctx.load_cert_chain(
            os.path.join(os.path.dirname(__file__), 'cert.pem'),
            os.path.join(os.path.dirname(__file__), 'key.pem'))
        return {'ssl_options': ssl_ctx}

    def get_protocol(self):
        return 'https'
