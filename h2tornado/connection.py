import logging
import socket
import ssl
from functools import partial

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from window import FlowControlManager
from errors import get_data as get_data_errors
from tornado import stack_context
from tornado.concurrent import Future
from tornado.gen import coroutine
from tornado.ioloop import IOLoop
from tornado.tcpclient import TCPClient

from h2tornado.config import ALPN_PROTOCOLS, DEFAULT_WINDOW_SIZE, \
    MAX_CONNECT_BACKOFF, MAX_CLOSE_BACKOFF
from h2tornado.exceptions import ConnectionError
from h2tornado.stream import H2Stream
from h2tornado.utils import CancelContext

logger = logging.getLogger('h2tornado.connection')


class H2Connection(object):

    def __init__(self, host, port, io_loop, ssl_options,
                 max_connect_backoff=MAX_CONNECT_BACKOFF,
                 initial_window_size=DEFAULT_WINDOW_SIZE,
                 connect_timeout=5, connect_callback=None):
        self.host = host
        self.port = port
        self.io_loop = io_loop

        self.tcp_client = TCPClient()

        self.h2conn = None
        self.io_stream = None
        self.window_manager = None
        self.connect_timeout = connect_timeout
        self._connect_timeout_handle = None
        self._connect_future = None

        self._streams = {}
        self.ssl_context = None
        self.ssl_options = ssl_options

        self.parse_ssl_opts()

        self.initial_window_size = initial_window_size
        self.max_connect_backoff = max_connect_backoff
        self.consecutive_connect_fails = 0
        self.connect_callback = connect_callback

        self._closed = False

    @property
    def drained(self):
        """Whether this connection has any inflight streams"""
        return len(self._streams) <= 0

    @property
    def ready(self):
        """Whether this connection is capable of serving more requests"""
        return self.is_connected and self.has_outbound_capacity

    @property
    def has_outbound_capacity(self):
        """Whether this connection has more outbound streams available.

        This is negotiated via http2 settings updates with the remote.
        """
        if not self.h2conn:
            return True

        has_outbound_capacity = self.h2conn.open_outbound_streams + 1 <= \
            self.h2conn.remote_settings.max_concurrent_streams
        return has_outbound_capacity

    @property
    def has_available_streams(self):
        """Whether there are more client stream ids available.

        See https://http2.github.io/http2-spec/#StreamIdentifiers for more
        infomation.
        """
        if not self.h2conn:
            return True

        has_available_stream_ids = self.h2conn.highest_outbound_stream_id is None or \
            self.h2conn.highest_outbound_stream_id + \
            2 <= self.h2conn.HIGHEST_ALLOWED_STREAM_ID
        return has_available_stream_ids

    @property
    def is_connected(self):
        """Whether this connection is connected to the remote, or not"""
        if self._connect_future.done():
            return self._connect_future.result() and self.h2conn
        return False

    def request(self, request):
        """Make a new stream on this connection and sends the request"""
        future = Future()

        def callback(result):
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result)

        stream_id = self.h2conn.get_next_available_stream_id()
        self._streams[stream_id] = H2Stream(
            request, stream_id, self.io_loop, self.h2conn, callback,
            self.flush, self._close_stream_callback
        )
        self._streams[stream_id].start()
        return future

    def _close_stream_callback(self, stream_id):
        """Called by H2Stream objects to clean up their state after they are
        finished.
        """
        del self._streams[stream_id]

    def parse_ssl_opts(self):
        """Parses the ssl_options passed into this connection.

        If self.ssl_options is an ssl.SSLContext, it uses it. Otherwise it's
        assumed to be a dictionary and an ssl.SSLContext is created according
        to the options.
        """
        if isinstance(self.ssl_options, ssl.SSLContext):
            self.ssl_context = ssl_options
            return

        ssl_context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=self.ssl_options.get('ca_certs')
        )
        ssl_context.options |= ssl.OP_NO_TLSv1
        ssl_context.options |= ssl.OP_NO_TLSv1_1

        if not self.ssl_options.get('validate_cert', True):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.ssl_options.get(
                'client_key') or self.ssl_options.get('client_cert'):
            ssl_context.load_cert_chain(
                self.ssl_options.get('client_cert'),
                keyfile=self.ssl_options.get('client_key')
            )

        ssl_context.set_ciphers('ECDHE+AESGCM')
        ssl_context.set_alpn_protocols(ALPN_PROTOCOLS)

        self.ssl_context = ssl_context

    def connect(self):
        """Connect to the remote"""
        if self._connect_timeout_handle:
            return

        self._connect_future = Future()
        # Shared context to cleanly cancel inflight operations
        cancelled = CancelContext()
        start_time = self.io_loop.time()
        self._connect_timeout_handle = self.io_loop.add_timeout(
            start_time + self.connect_timeout, partial(self.on_connect_timeout, cancelled))

        def _on_tcp_client_connected(f):
            exc = f.exc_info()
            if exc is not None:
                self._connect_future.set_result(False)
                if not cancelled():
                    self.consecutive_connect_fails += 1
                    self.on_error('during connection', cancelled, *exc)
            else:
                if not cancelled():
                    self.on_connect(cancelled, f.result())

        ft = self.tcp_client.connect(
            self.host, self.port, af=socket.AF_UNSPEC,
            ssl_options=self.ssl_context,
        )
        ft.add_done_callback(_on_tcp_client_connected)
        return self._connect_future

    def _backoff_reconnect(self):
        """Try to reconnect to the remote, backoff as appropriate"""
        self.io_loop.add_timeout(
            IOLoop.current().time() +
            min(self.max_connect_backoff, self.consecutive_connect_fails**1.5),
            self.connect)

    def on_connect_timeout(self, cancelled):
        """Handle a connection timeout.

        :param cancelled: A CancelContext to cancel inflight operations
        associated with this connection or connection attempt.
        """
        self.consecutive_connect_fails += 1
        cancelled.cancel()
        exc = ConnectionError('Connection could not be established in time!')
        self.close(exc)

    def on_error(self, phase, cancelled, typ, val, tb):
        """An unrecoverable connection error happened, close the connection.

        :param phase: phase we encountered the error in
        :param cancelled: A CancelContext to cancel inflight operations
        associated with this connection or connection attempt.
        :param typ: type of error
        :param val: error
        :param tb: traceback information
        """
        cancelled.cancel()
        self.close(val)

    def on_close(self, cancelled):
        """Handle the underlying iostream getting closed.

        :param cancelled: A CancelContext to cancel inflight operations
        associated with this connection or connection attempt.
        """
        # Already closed, so no cleanup needed
        if cancelled():
            return

        err = None
        if self.io_stream:
            err = self.io_stream.error
        if not err:
            err = ConnectionError("Error closed by remote end")

        cancelled.cancel()
        self.close(err)

    @coroutine
    def graceful_close(self, max_backoff=MAX_CLOSE_BACKOFF):
        """Gracefully close this connection by waiting for inflight operations
        to finish.

        :param max_backoff: maximum backoff time for polling this connection
        for inflight operations and deciding to call close.
        """
        i = 0
        while True:
            if self._closed:
                return

            if self.drained:
                self.close("Graceful close called", reconnect=False)
                return

            yield sleep(min(max_backoff, i**1.5))
            i += 1

    def close(self, reason, reconnect=True):
        """Closes the connection, sending a GOAWAY frame.

        :param reason: why this connection was closed
        :param reconnect: whether to try to reconnect
        """
        logger.debug('Closing HTTP2Connection with reason %s', reason)

        if self._connect_timeout_handle:
            self.io_loop.remove_timeout(self._connect_timeout_handle)
            self._connect_timeout_handle = None

        if self.h2conn:
            try:
                self.h2conn.close_connection()
                self.flush()
            except Exception:
                logging.error(
                    'Could not send GOAWAY frame, connection terminated!',
                    exc_info=True
                )
            finally:
                self.h2conn = None

        if self.io_stream:
            try:
                self.io_stream.close()
            except Exception:
                logging.error('Could not close IOStream!', exc_info=True)
            finally:
                self.io_stream = None

        self.window_manager = None
        self.end_all_streams(reason)
        if reconnect:
            self._backoff_reconnect()
        else:
            self._closed = True

    def end_all_streams(self, exc=None):
        """Ends all inflight streams with the given exception.

        :param exc: exception for why we're ending all streams.
        """
        for _, stream in self._streams.iteritems():
            self.io_loop.add_callback(partial(stream.finish, exc))

    def on_connect(self, cancelled, io_stream):
        """Initiate this connections state after we're connected.

        :param cancelled: A CancelContext to cancel inflight operations
        associated with this connection or connection attempt.
        :param io_stream: IOStream object that was successfully connected.
        """
        try:
            if cancelled():
                io_stream.close()
                raise ConnectionError("Connected timed out!")

            self.consecutive_connect_fails = 0
            if io_stream.socket.selected_alpn_protocol() not in ALPN_PROTOCOLS:
                logging.error(
                    'Negotiated protocols mismatch, got %s, expected one of %s',
                    io_stream.socket.selected_alpn_protocol(),
                    ALPN_PROTOCOLS)
                raise ConnectionError(
                    'Negotiated protocols mismatch, got %s but not in supported protos %s',
                    io_stream.socket.selected_alpn_protocol(),
                    ALPN_PROTOCOLS)

            # remove the connection timeout
            self.io_loop.remove_timeout(self._connect_timeout_handle)
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
                        streaming_callback=partial(
                            self.receive_data_until_cancelled,
                            cancelled),
                        callback=read_until_cancelled)

            read_until_cancelled()
            self.flush()
        except Exception as e:
            self._connect_future.set_exception(e)
        else:
            self._connect_future.set_result(True)
            self.io_loop.add_callback(self.connect_callback)

    def _adjust_window(self, frame_len):
        """Adjust the flow control window"""
        increment = self.window_manager._handle_frame(frame_len)
        if increment:
            self.h2conn.increment_flow_control_window(increment)
        self.flush()

    def receive_data_until_cancelled(self, cancelled, data):
        """Handle the received data over the wire.

        :param cancelled: A CancelContext to cancel inflight operations
        associated with this connection or connection attempt.
        :param data: data received over the wire from the remote
        """
        # If we got cancelled, that means the connection died and
        # we're making a new one. Don't process any events from the
        # (now) old connection
        if cancelled():
            return

        try:
            events = self.h2conn.receive_data(data)
            for event in events:
                try:
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
                                name, number, description = get_data_errors(
                                    event.error_code)
                            except ValueError:
                                error_string = (
                                    "Encountered error code %d" %
                                    event.error_code)
                            else:
                                error_string = (
                                    "Encountered error %s %s: %s" %
                                    (name, number, description)
                                )
                        self.close(ConnectionError(error_string))
                    else:
                        logger.info("Received unhandled event %s", event)
                except Exception:
                    logger.exception("Error while handling event %s", event)
        except h2.exceptions.StreamClosedError:
            logger.info("Got stream closed on connection, reconnecting...")
            cancelled.cancel()
            self.close(ConnectionError("Stream closed by remote peer"))
        except h2.exceptions.ProtocolError as e:
            logger.exception(
                "Exception while receiving data from %s, closing connection",
                (self.host, self.port,))
            self.close(ConnectionError(str(e)))
        except Exception:
            logger.exception(
                "Unhandled exception while receiving data from %s",
                (self.host, self.port,))

    def flush(self):
        """Send any outstanding data to the remote"""
        future = Future()
        if self._closed:
            future.set_result(None)
            return future

        data_to_send = self.h2conn.data_to_send()
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
