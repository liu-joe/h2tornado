import collections
import logging
import random
from functools import partial

from tornado import stack_context
from tornado.concurrent import Future
from tornado.httpclient import HTTPError, HTTPResponse
from tornado.ioloop import IOLoop

from h2tornado.connection import H2Connection

logger = logging.getLogger('h2tornado.pool')


class H2ConnectionPool(object):

    def __init__(
            self,
            host,
            port,
            io_loop,
            ssl_options,
            max_connections=5,
            connect_timeout=5):
        self.host = host
        self.port = port
        self.io_loop = io_loop
        self.ssl_options = ssl_options
        self.connect_timeout = connect_timeout

        # Maximum number of http2 connections to open (in the case of
        # us queueing requests due to maxing out the number of outbound
        # streams)
        self.max_connections = max_connections

        # Queue to hold pending requests
        self.queue = collections.deque()
        self.waiters = {}

        self.h2_connections = []

    def get_or_create_connection(self):
        # Gracefully close any connections that have exhausted the number of streams
        # allowed for a single connection. Remove them from our pool
        no_more_available_streams = [
            c for c in self.h2_connections if not c.has_available_streams]
        for done_conn in no_more_available_streams:
            self.io_loop.add_callback(done_conn.graceful_close)
            self.h2_connections.remove(done_conn)

        ready_conns = [c for c in self.h2_connections if c.ready]
        if len(ready_conns) > 0:
            return random.choice(ready_conns)

        # None of the connections were ready, check to see if any
        # are not connected, if so, we can wait for one of them
        # to connect
        connecting_conns = [
            c for c in self.h2_connections if not c.is_connected]

        # If there are none, then that means we have reached the maximum outbound
        # streams (or there are no connections). Make a new connection as long as
        # we're under self.max_connections connections
        if len(connecting_conns) <= 0:
            if len(self.h2_connections) < self.max_connections:
                h2conn = H2Connection(self.host, self.port, self.io_loop, self.ssl_options)
                connect_future = h2conn.connect()
                # When this connection is connected again, process the queue
                connect_future.add_done_callback(self._process_queue)
                self.h2_connections.append(h2conn)

        return None

    def _on_timeout(self, key):
        timeout_handle, request, future = self.waiters[key]
        self.queue.remove((key, request, future,))
        future.set_result(
            HTTPResponse(
                request,
                599,
                error=HTTPError(
                    599,
                    "Timed out in queue"),
                request_time=self.io_loop.time() -
                request.start_time))
        del self.waiters[key]

    def request(self, request):
        future = Future()
        key = object()
        self.queue.append((key, request, future))
        if not self.get_or_create_connection():
            timeout_handle = self.io_loop.add_timeout(self.io_loop.time() +
                                                          min(request.connect_timeout, request.request_timeout),
                                                          partial(self._on_timeout, key))
        else:
            timeout_handle = None
        self.waiters[key] = (timeout_handle, request, future)
        self._process_queue()
        return future

    def _remove_timeout(self, key):
        if key in self.waiters:
            handle, request, future = self.waiters[key]
            if handle is not None:
                self.io_loop.remove_timeout(handle)
            del self.waiters[key]

    def _process_queue(self, *args):
        with stack_context.NullContext():
            while self.queue and self.get_or_create_connection():
                connection = self.get_or_create_connection()
                key, request, request_future = self.queue.popleft()
                if key not in self.waiters:
                    continue
                self._remove_timeout(key)

                def chain_futures(req_future, f):
                    if f.exception():
                        req_future.set_exception(f.exception())
                    else:
                        req_future.set_result(f.result())
                    self.io_loop.add_callback(self._process_queue)

                done_future = connection.request(request)
                done_future.add_done_callback(
                    partial(chain_futures, request_future))

    def close(self, force=False):
        for conn in self.h2_connections:
            if force:
                conn.close("Close called", reconnect=False)
            else:
                self.io_loop.add_callback(conn.graceful_close)
