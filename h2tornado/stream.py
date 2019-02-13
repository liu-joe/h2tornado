import httplib
import io
import logging
from functools import partial
from urlparse import urlsplit

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from hyper.http20.window import FlowControlManager
from tornado import httputil
from tornado.escape import to_unicode
from tornado.httpclient import HTTPResponse

from h2tornado.config import DEFAULT_WINDOW_SIZE
from h2tornado.exceptions import StreamResetException

logger = logging.getLogger('h2tornado.stream')


class H2Stream(object):
    def __init__(self, request, stream_id, io_loop, h2conn, callback_response,
                 send_outstanding_data_cb, close_cb):
        self.stream_id = stream_id
        self.io_loop = io_loop
        self.h2conn = h2conn
        self.callback_response = callback_response
        self.send_outstanding_data_cb = send_outstanding_data_cb
        self.close_cb = close_cb

        self.data = []
        self.remote_closed = False
        self.local_closed = False

        self.request = request
        self.flow_control_manager = FlowControlManager(DEFAULT_WINDOW_SIZE)

        self.response_headers = None
        self.response_trailers = None

        self._finished = False
        self._timeout_handle = None

    def start(self):
        """Send the http request to the remote"""
        timeout = self.request.request_timeout
        if not timeout:
            timeout = 30
        self._timeout_handle = self.io_loop.add_timeout(
            self.io_loop.time() + timeout,
            self._on_timeout
        )
        parsed = urlsplit(to_unicode(self.request.url))

        if 'Host' not in self.request.headers:
            if not parsed.netloc:
                self.request.headers['Host'] = self.connection.host
            elif '@' in parsed.netloc:
                self.request.headers['Host'] = parsed.netloc.rpartition(
                    '@')[-1]
            else:
                self.request.headers['Host'] = parsed.netloc

        if self.request.user_agent:
            self.request.headers['User-Agent'] = self.request.user_agent

        if self.request.body is not None:
            self.request.headers['Content-Length'] = str(
                len(self.request.body))

        if (
            self.request.method == 'POST'
            and 'Content-Type' not in self.request.headers
        ):
            self.request.headers['Content-Type'] = (
                'application/x-www-form-urlencoded'
            )

        self.request.url = (
            (parsed.path or '/') +
            (('?' + parsed.query) if parsed.query else '')
        )

        self.scheme = parsed.scheme

        http2_headers = [
            (':authority', self.request.headers.pop('Host')),
            (':path', self.request.url),
            (':scheme', self.scheme),
            (':method', self.request.method),
        ] + self.request.headers.items()

        self.h2conn.send_headers(
            self.stream_id, http2_headers, end_stream=not self.request.body
        )
        self.send_outstanding_data_cb()

        # send body, if any
        if self.request.body:
            self.send_body()

    def send_body(self):
        """Send the body of the http request"""
        self.total = len(self.request.body)
        self.sent = 0

        if not self._finished:
            self._send_data()

    def _send_data(self, sent_bytes=0, *args):
        """Send the data in chunks according to flow control

        :param sent_bytes: how many bytes were sent on the last call
        :param *args: this function can be called with a future from
            add_done_callback.
        """
        self.sent += sent_bytes

        if self._finished:
            return

        if self.sent < self.total:
            to_send = min(
                self.h2conn.local_flow_control_window(self.stream_id),
                self.h2conn.max_outbound_frame_size)

            end_stream = False
            if self.sent + to_send >= self.total:
                end_stream = True
            data_chunk = self.request.body[self.sent:self.sent + to_send]

            self.h2conn.send_data(
                self.stream_id, data_chunk, end_stream=end_stream
            )
            future = self.send_outstanding_data_cb()
            # Have the callback yield to the IOLoop, so that we can process
            # events in the case the stream dies
            future.add_done_callback(partial(
                self.io_loop.add_callback,
                partial(self._send_data, to_send)))
        else:
            self.local_closed = True

    def handle_event(self, event):
        """Handle any http2 events"""
        if isinstance(event, h2.events.DataReceived):
            size = event.flow_controlled_length
            increment = self.flow_control_manager._handle_frame(size)
            self.data.append(event.data)
            if increment:
                try:
                    self.h2conn.increment_flow_control_window(
                        increment, stream_id=self.stream_id
                    )
                except h2.exceptions.StreamClosedError:
                    pass
                else:
                    self.send_outstanding_data_cb()
        elif isinstance(event, h2.events.ResponseReceived):
            self.response_headers = event.headers
        elif isinstance(event, h2.events.TrailersReceived):
            self.response_trailers = event.headers
        elif isinstance(event, h2.events.StreamEnded):
            self.remote_closed = True
            self.finish()
        elif isinstance(event, h2.events.StreamReset):
            self.remote_closed = True
            self.finish(exc=StreamResetException("Stream reset"))
        else:
            logger.info(
                "Got unhandled event on stream %s: %s",
                self.stream_id,
                event)

    def _on_timeout(self):
        """Handle a request timeout, sends RST to the remote for this stream"""
        self.io_loop.remove_timeout(self._timeout_handle)
        self._timeout_handle = None
        # Let the other end know we're cancelling this stream
        try:
            self.h2conn.reset_stream(stream_id=self.stream_id, error_code=8)
            self.send_outstanding_data_cb()
        except Exception as e:
            pass
        self.finish(exc=None, timed_out=True)

    def finish(self, exc=None, timed_out=False):
        """Finish the request.

        :param exc: exception encountered while processing this request, if any
        :param timed_out: whether this request timed out or not
        """
        if self._finished:
            return

        self._finished = True
        if self._timeout_handle:
            self.io_loop.remove_timeout(self._timeout_handle)
            self._timeout_handle = None

        headers = {}
        data = io.BytesIO()
        # Stream reset
        if exc:
            code = 500
            reason = exc.message if hasattr(exc, "message") else str(exc)
        elif not timed_out:
            data = io.BytesIO(b''.join(self.data))
            headers = {}
            for header, value in self.response_headers:
                headers[header] = value
            code = int(headers.pop(':status'))
            reason = httplib.responses.get(code, 'Unknown')
        else:
            code = 599
            reason = "CLIENT_SIDE_TIMEOUT"

        response = HTTPResponse(
            self.request,
            code,
            reason=reason,
            headers=httputil.HTTPHeaders(headers),
            buffer=data,
            request_time=self.io_loop.time() - self.request.start_time,
            effective_url=self.request.url
        )
        response.request.response = response

        self.close_cb(stream_id=self.stream_id)
        self.callback_response(response)
