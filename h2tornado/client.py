import logging
from functools import partial
from urlparse import urlparse

from tornado import httputil, stack_context
from tornado.concurrent import Future
from tornado.httpclient import HTTPError, HTTPRequest, HTTPResponse
from tornado.ioloop import IOLoop

from h2tornado.config import DEFAULT_WINDOW_SIZE, MAX_CONNECT_BACKOFF, \
    MAX_CLOSE_BACKOFF
from h2tornado.pool import H2ConnectionPool

logger = logging.getLogger('h2tornado')


class AsyncHTTP2Client(object):
    def __init__(self, io_loop=None, default_max_connections=5):
        self.io_loop = io_loop if io_loop else IOLoop.current()
        self.default_max_connections = default_max_connections
        self.pools = {}
        self._closed = False

    def add_connection_pool(self, host, port, ssl_options, max_connections=5,
                            connect_timeout=5,
                            initial_window_size=DEFAULT_WINDOW_SIZE,
                            max_connect_backoff=MAX_CONNECT_BACKOFF,
                            max_close_backoff=MAX_CLOSE_BACKOFF,):
        """Optional method to pre-add a connection pool with various options.

        Otherwise connection pools will be added on-demand using the built-in
        defaults and information from the http request.

        :param host: Host to connect to
        :param port: Port to connect to host on
        :param ssl_options: ssl options to use for tls
        :param max_connections: maximum number of http2 connections to create
        :param initial_window_size: default http2 window size for the
            connections and streams created
        :param max_connect_backoff: maximum backoff time in seconds for
            attempting to reconnect to this host when the connect attempt
            fails
        :param max_close_backoff: maximum backoff time in seconds for
            attempting to gracefully close this connection pool when there
            are still inflight requests
        """
        key = (host, port, frozenset(sorted(ssl_options.items())),)
        if key in self.pools:
            pool = self.pools[key]
            self.io_loop.add_callback(partial(pool.close, force=False))

        self.pools[key] = H2ConnectionPool(
            host, port, ssl_options, max_connections, connect_timeout,
            initial_window_size, max_connect_backoff, max_close_backoff,
        )

    def fetch(self, request, callback=None, raise_error=True, **kwargs):
        """Executes a request, asynchronously returning an `HTTPResponse`.

        The request may be either a string URL or an `HTTPRequest` object.
        If it is a string, we construct an `HTTPRequest` using any additional
        kwargs: ``HTTPRequest(request, **kwargs)``

        This method returns a `.Future` whose result is an
        `HTTPResponse`. By default, the ``Future`` will raise an
        `HTTPError` if the request returned a non-200 response code
        (other errors may also be raised if the server could not be
        contacted). Instead, if ``raise_error`` is set to False, the
        response will always be returned regardless of the response
        code.

        If a ``callback`` is given, it will be invoked with the `HTTPResponse`.
        In the callback interface, `HTTPError` is not automatically raised.
        Instead, you must check the response's ``error`` attribute or
        call its `~HTTPResponse.rethrow` method.
        """
        if self._closed:
            raise RuntimeError("fetch() called on a closed AsyncHTTP2Client")
        if not isinstance(request, HTTPRequest):
            request = HTTPRequest(url=request, **kwargs)
        else:
            if kwargs:
                raise ValueError(
                    "kwargs can't be used if request is an HTTPRequest object")
        request.headers = httputil.HTTPHeaders(request.headers)
        future = Future()
        if callback is not None:
            callback = stack_context.wrap(callback)

            def handle_future(future):
                exc = future.exception()
                if isinstance(exc, HTTPError) and exc.response is not None:
                    response = exc.response
                elif exc is not None:
                    response = HTTPResponse(
                        request, 599, error=exc,
                        request_time=time.time() - request.start_time)
                else:
                    response = future.result()
                self.io_loop.add_callback(partial(callback, response))
            future.add_done_callback(handle_future)

        def handle_response(f):
            response = f.result()
            if raise_error and response.error:
                if isinstance(response.error, HTTPError):
                    response.error.response = response
                future.set_exception(response.error)
            else:
                future.set_result(response)
        self.fetch_impl(request, handle_response)
        return future

    def fetch_impl(self, request, handle_response):
        (host, port,) = self._parse_host_port(request.url)
        ssl_options = {
            'validate_cert': request.validate_cert,
            'ca_certs': request.ca_certs,
            'client_key': request.client_key,
            'client_cert': request.client_cert,
        }
        key = (host, port, frozenset(sorted(ssl_options.items())))
        if key not in self.pools:
            self.pools[key] = H2ConnectionPool(
                host, port, self.io_loop, ssl_options,
                max_connections=self.default_max_connections)

        pool = self.pools[key]
        pool.request(request).add_done_callback(handle_response)

    def _parse_host_port(self, url):
        parsed = urlparse(url)
        port = parsed.port if parsed.port else 443
        host = parsed.hostname
        return (host, port,)

    def close(self, force=True):
        """Closes this client, releasing any resources used.

        :param force: if force is True close will forcibly close all
            connections and inflight requests. Otherwise it will wait
            for inflight requests to finish before closing any connections.
        """
        if self._closed:
            return

        self._closed = True
        for _, pool in self.pools.iteritems():
            self.io_loop.add_callback(
                partial(pool.close, force=force)
            )


class HTTP2Client(object):
    def __init__(self, io_loop=None, **kwargs):
        self.io_loop = io_loop if io_loop else IOLoop.current()
        kwargs['io_loop'] = self.io_loop
        self._client = AsyncHTTP2Client(**kwargs)
        self._closed = False

    def fetch(self, *args, **kwargs):
        return self.io_loop.run_sync(partial(self._client.fetch, *args, **kwargs))

    def close(self):
        if self._closed:
            return

        self._closed = True
        self._client.close()
