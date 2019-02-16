import collections
import logging
import httplib
import ssl
import socket
import io

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings

from tornado import stack_context, httputil
from tornado.concurrent import Future
from tornado.httpclient import HTTPRequest, HTTPResponse, _RequestProxy
from tornado.ioloop import IOLoop
from tornado.tcpclient import TCPClient
from tornado.gen import coroutine, Return
from functools import partial
from urlparse import urlsplit
from tornado.escape import to_unicode
from hyper.http20.window import FlowControlManager
from hyper.http20.errors import get_data as get_error_data

from h2tornado.exceptions import StreamResetException, ConnectionError

logger = logging.getLogger('h2tornado')

class CancelContext(object):
    def __init__(self):
        self.cancelled = False

    def __call__(self):
        return self.cancelled

    def cancel(self):
        self.cancelled = True

ALPN_PROTOCOLS = ['h2']
DEFAULT_WINDOW_SIZE = 65536

class AsyncHTTP2Client(object):
    pass

class H2ConnectionPool(object):
    pass

class H2Connection(object):

    def __init__(self, host, port):
        self.host = host
        self.port = port
        
        self.tcp_client = TCPClient()

        self.h2conn = None
        self.io_stream = None
        self.window_manager = None
        self.connect_timeout = 5
        self._connect_timeout_handle = None
        self._connect_future = None

        self._streams = {}
        self.ssl_context = None
        self.ssl_options = {}

        self.parse_ssl_opts()

        # TODO: config options
        self.initial_window_size = 65536
        self.max_backoff_seconds = 60
        self.consecutive_connect_fails = 0
        self.max_concurrent_streams = 1
        
        self.connect()

    def has_available_streams(self):
        return self.h2conn.open_outbound_streams + 1 <= \
            self.h2conn.remote_settings.max_concurrent_streams

    def is_connected(self):
        return self._connect_future

    def request(self, request):
        future = Future()
        def callback(result):
            if isinstance(result, Exception):
                future.set_exception(result)
            future.set_result(result)

        stream_id = self.h2conn.get_next_available_stream_id()
        self._streams[stream_id] = H2Stream(
            request, stream_id, self.h2conn, callback,
            self.flush, self._close_stream_callback
        )
        self._streams[stream_id].start()
        return future

    def _close_stream_callback(self, stream_id):
        del self._streams[stream_id]

    def parse_ssl_opts(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.options |= ssl.OP_NO_TLSv1
        ssl_context.options |= ssl.OP_NO_TLSv1_1

        if True or not self.ssl_options.get('verify_certificate', True):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.ssl_options.get('client_key') or self.ssl_options.get('client_cert'):
            ssl_context.load_cert_chain(
                self.ssl_options.get('client_cert'),
                keyfile=self.ssl_options.get('client_key')
            )

        ssl_context.set_ciphers('ECDHE+AESGCM')
        ssl_context.set_alpn_protocols(ALPN_PROTOCOLS)

        self.ssl_context = ssl_context
    
    def connect(self):
        if self._connect_timeout_handle:
            return
        
        self._connect_future = Future()
        # Shared context to cleanly cancel operations
        cancelled = CancelContext()
        start_time = IOLoop.current().time()
        self._connect_timeout_handle = IOLoop.current().add_timeout(
            start_time + self.connect_timeout, partial(self.on_connect_timeout, cancelled))
        
        def _on_tcp_client_connected(f):
            exc = f.exc_info()
            if exc is not None:
                self._connect_future.set_result(False)
                if not cancelled():
                    self.consecutive_connect_fails += 1
                    self.on_error('during connection', cancelled, *exc)
            else:
                self._connect_future.set_result(True)
                if not cancelled():
                    self.on_connect(cancelled, f.result())

        ft = self.tcp_client.connect(
            self.host, self.port, af=socket.AF_UNSPEC,
            ssl_options=self.ssl_context,
        )
        ft.add_done_callback(_on_tcp_client_connected)
        return self._connect_future
    
    def _backoff_reconnect(self):
        IOLoop.current().add_timeout(
            IOLoop.current().time() + \
                min(self.max_backoff_seconds, self.consecutive_connect_fails**1.5),
            self.connect)

    def on_connect_timeout(self, cancelled):
        """ Connection timed out. """
        self.consecutive_connect_fails += 1
        cancelled.cancel()

        exc = ConnectionTimeout('Connection could not be established!')

        self.close(exc)
    
    def on_error(self, phase, cancelled, typ, val, tb):
        """
        Connection error.
        :param phase: phase we encountered the error in
        :param typ: type of error
        :param val: error
        :param tb: traceback information
        """
        cancelled.cancel()
        self.close(val)

    def on_close(self, cancelled):
        # Already closed, so no cleanup needed
        if cancelled():
            return
        
        err = self.io_stream.error
        if not err:
            err = ConnectionError("Error closed by remote end")

        # No need to clean up the iostream as its closed
        self.io_stream = None

        cancelled.cancel()
        self.close(err)
    
    def close(self, reason):
        """ Closes the connection, sending a GOAWAY frame. """
        logger.debug('Closing HTTP2Connection with reason %s', reason)

        if self._connect_timeout_handle:
            IOLoop.current().remove_timeout(self._connect_timeout_handle)
            self._connect_timeout_handle = None

        if self.h2conn:
            try:
                self.h2conn.close_connection()
                self.flush()
            except Exception:
                log.error(
                    'Could not send GOAWAY frame, connection terminated!',
                    exc_info=True
                )
            finally:
                self.h2conn = None

        if self.io_stream:
            try:
                self.io_stream.close()
            except Exception:
                log.error('Could not close IOStream!', exc_info=True)
            finally:
                self.io_stream = None

        self.window_manager = None
        self.end_all_streams(reason)
        self._backoff_reconnect()

    def end_all_streams(self, exc=None):
        for _, stream in self._streams.iteritems():
            stream.finish(exc)

    def on_connect(self, cancelled, io_stream):
        if cancelled():
            logger.debug('Connection timeout before socket connected!')
            io_stream.close()
            return

        self.consecutive_connect_fails = 0
        if io_stream.socket.selected_alpn_protocol() not in ALPN_PROTOCOLS:
            log.error(
                'Negotiated protocols mismatch, got %s, expected one of %s',
                io_stream.socket.selected_alpn_protocol(),
                ALPN_PROTOCOLS
            )
            raise ConnectionError('Negotiated protocols mismatch, got %s but not in supported protos %s',
                io_stream.socket.selected_alpn_protocol(), ALPN_PROTOCOLS)

        # remove the connection timeout
        IOLoop.current().remove_timeout(self._connect_timeout_handle)
        self._connect_timeout_handle = None

        self.io_stream = io_stream
        self.io_stream.set_nodelay(True)

        # set the close callback
        self.io_stream.set_close_callback(
            partial(self.on_close, cancelled))

        # initialize the connection
        self.h2conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True)
        )

        # initiate the h2 connection
        self.h2conn.initiate_connection()

        # disable server push
        self.h2conn.update_settings({
            h2.settings.SettingCodes.ENABLE_PUSH: 0,
            h2.settings.SettingCodes.INITIAL_WINDOW_SIZE:
                self.initial_window_size
        })

        self.window_manager = FlowControlManager(self.initial_window_size)

        # set the stream reading callback. We don't care whats
        # passed into this function, so prepare to get called
        # with anything from the iostream callback (should be
        # an empty data frame)
        def read_until_cancelled(*args, **kwargs):
            if cancelled():
                return

            with stack_context.ExceptionStackContext(
                    partial(self.on_error, 'during read', cancelled)
            ):
                self.io_stream.read_bytes(
                    num_bytes=65535,
                    streaming_callback=partial(self.receive_data_until_cancelled, cancelled),
                    callback=read_until_cancelled
                )
        
        read_until_cancelled()
        self.flush()

    def _adjust_window(self, frame_len):
        increment = self.window_manager._handle_frame(frame_len)
        if increment:
            self.h2conn.increment_flow_control_window(increment)
        self.flush()

    def receive_data_until_cancelled(self, cancelled, data):
        # If we got cancelled, that means the connection died and
        # we're making a new one. Don't process any events from the
        # (now) old connection
        if cancelled():
            return

        try:
            events = self.h2conn.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.DataReceived):
                    self._adjust_window(event.flow_controlled_length)
                    self._streams[event.stream_id].handle_event(event)
                elif isinstance(event, h2.events.PushedStreamReceived):
                    # We don't handle server push, and we say so in the
                    # settings configuration on connect, so close the connection
                    # and continue with our business
                    conn.reset_stream(event.stream_id, error_code=7)
                    self.flush()
                elif isinstance(event, (h2.events.ResponseReceived,
                                        h2.events.TrailersReceived,
                                        h2.events.StreamEnded,
                                        h2.events.StreamReset,)):
                    self._streams[event.stream_id].handle_event(event)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    cancelled.cancel()
                    error_string = "Connection closed by remote end"
                    if event.error_code != 0:
                        try:
                            name, number, description = get_data_errors(event.error_code)
                        except ValueError:
                            error_string = (
                                "Encountered error code %d" % event.error_code
                            )
                        else:
                            error_string = (
                                "Encountered error %s %s: %s" % \
                                 (name, number, description)
                            )
                    self.close(ConnectionError(error_string))
                else:
                    logger.info("Received unhandled event %s", event)
        except Exception:
            logger.exception("Exception while receiving data from %s", (self.host, self.port,))

    def flush(self):
        data_to_send = self.h2conn.data_to_send()
        future = Future()
        if data_to_send:
            try:
                future = self.io_stream.write(data_to_send)
            except Exception as e:
                future.set_exception(e)
        else:
            # Since the data we're sending can be from multiple streams
            # it doesn't make sense to return the number of bytes written
            # for example, when any individual stream could call this
            future.set_result(None)
        return future

class H2Stream(object):
    def __init__(self, request, stream_id, h2conn, callback_response,
            send_outstanding_data_cb, close_cb):
        self.stream_id = stream_id
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
        timeout = self.request.request_timeout
        if not timeout:
            timeout = 30
        self._timeout_handle = IOLoop.current().add_timeout(
            IOLoop.current().time() + timeout,
            self._on_timeout
        )
        parsed = urlsplit(to_unicode(self.request.url))

        if 'Host' not in self.request.headers:
            if not parsed.netloc:
                self.request.headers['Host'] = self.connection.host
            elif '@' in parsed.netloc:
                self.request.headers['Host'] = parsed.netloc.rpartition('@')[-1]
            else:
                self.request.headers['Host'] = parsed.netloc

        if self.request.user_agent:
            self.request.headers['User-Agent'] = self.request.user_agent

        if self.request.body is not None:
            self.request.headers['Content-Length'] = str(len(self.request.body))

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

        # send headers
        logger.debug('STREAM %d Sending headers', self.stream_id)
        self.h2conn.send_headers(
            self.stream_id, http2_headers, end_stream=not self.request.body
        )
        self.send_outstanding_data_cb()

        # send body, if any
        if self.request.body:
            self.send_body()

    def send_body(self):
        self.total = len(self.request.body)
        self.sent = 0

        if not self._finished:
            self._send_data()

    def _send_data(self, sent_bytes=0, *args):
        self.sent += sent_bytes

        if self._finished:
            return

        if self.sent < self.total:
            to_send = self.h2conn.local_flow_control_window(self.stream_id)

            end_stream = False
            if self.sent + to_send >= self.total:
                end_stream = True
            data_chunk = self.request.body[self.sent:self.sent + to_send]

            self.h2conn.send_data(
                self.stream_id, data_chunk, end_stream=end_stream
            )
            future = self.send_outstanding_data_cb()
            future.add_done_callback(partial(self._send_data, to_send))
        else:
            self.local_closed = True

    def handle_event(self, event):
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
            logger.info("Got unhandled event on stream %s: %s", self.stream_id, event)

    def _on_timeout(self):
        IOLoop.current().remove_timeout(self._timeout_handle)
        self._timeout_handle = None
        self.finish(exc=None, timed_out=True)

    def finish(self, exc=None, timed_out=False):
        if self._finished:
            return
        
        self._finished = True
        if self._timeout_handle:
            IOLoop.current().remove_timeout(self._timeout_handle)
            self._timeout_handle = None

        if exc:
            response = exc
        else:
            # compose the body
            headers = None
            if not timed_out:
                data = io.BytesIO(b''.join(self.data))
                headers = {}
                for header, value in self.response_headers:
                    headers[header] = value
                code = int(headers.pop(':status'))
                reason = httplib.responses.get(code, 'Unknown')
            else:
                data = io.BytesIO()
                code = 599
                reason = "CLIENT_SIDE_TIMEOUT"

            response = HTTPResponse(
                self.request,
                code,
                reason=reason,
                headers=headers,
                buffer=data,
                request_time=IOLoop.current().time() - self.request.start_time,
                effective_url=self.request.url
            )

        self.close_cb(stream_id=self.stream_id)
        self.callback_response(response) 

if __name__ == '__main__':
    import time
    from tornado.gen import sleep

    def print_it(start, res):
        print res.result(), time.time() - start

    conn = H2Connection('localhost',5000)
    
    @coroutine
    def doit():
        while True:
            for i in xrange(1):
                IOLoop.current().add_callback(wait_for_connected)
            yield sleep(1)


    @coroutine
    def wait_for_connected(connected=None):
        print connected
        if not connected or not connected.result():
            yield sleep(1)
            conn.is_connected().add_done_callback(wait_for_connected)
        else:
            make_h2_conn()

    
    def make_h2_conn():
        start = time.time()
        result = conn.request(HTTPRequest('https://localhost:5000/'))
        result.add_done_callback(partial(print_it, start))

    IOLoop.current().add_callback(doit)
    IOLoop.current().start()
