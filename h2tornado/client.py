import logging

from tornado import stack_context, httputil
from tornado.concurrent import Future
from tornado.httpclient import HTTPRequest, HTTPResponse, HTTPError
from tornado.ioloop import IOLoop
from tornado.gen import coroutine
from functools import partial
from urlparse import urlparse

from h2tornado.pool import H2ConnectionPool

logger = logging.getLogger('h2tornado')

class AsyncHTTP2Client(object):
    def __init__(self, default_max_connections=5):
        self.default_max_connections = default_max_connections
        self.pools = {}
        self._closed = False

    # Optional method to pre-add a connection pool, otherwise they will
    # be created on demand using the information from the http request
    def add_connection_pool(self, host, port, ssl_options, max_connections=5,
            connect_timeout=5):
        key = (host, port,)
        if key in self.pools:
            pool = self.pools[key]
            IOLoop.current().add_callback(pool.close)
        
        self.pools[key] = H2ConnectionPool(
            host, port, ssl_options, max_connections, connect_timeout
        )

    def fetch(self, request, callback=None, raise_error=True, **kwargs):
        if self._closed:
            raise RuntimeError("fetch() called on a closed AsyncHTTP2Client")
        if not isinstance(request, HTTPRequest):
            request = HTTPRequest(url=request, **kwargs)
        else:
            if kwargs:
                raise ValueError("kwargs can't be used if request is an HTTPRequest object")
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
                IOLoop.current().add_callback(partial(callback, response))
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
        key = self._parse_host_port(request.url)
        host, port = key
        if key not in self.pools:
            if request.ssl_options:
                ssl_options = request.ssl_options
            else:
                ssl_options = {
                    'validate_cert' : request.validate_cert,
                    'ca_certs' : request.ca_certs,
                    'client_key' : request.client_key,
                    'client_cert' : request.client_cert,
                }
            self.pools[key] = H2ConnectionPool(
                host, port, ssl_options, max_connections=self.default_max_connections
            )

        pool = self.pools[key]
        pool.request(request).add_done_callback(handle_response)
    
    def _parse_host_port(self, url):
        parsed = urlparse(url)
        port = parsed.port if parsed.port else 443
        host = parsed.hostname
        return (host, port,)

    def close(self, force=True):
        if self._closed:
            return

        self._closed = True
        for _, pool in self.pools.iteritems():
            IOLoop.current().add_callback(
                partial(pool.close, force=force)
            )

class HTTP2Client(object):
    def __init__(self, *args, **kwargs):
        self._client = AsyncHTTP2Client(*args, **kwargs)
        self._closed = False

    def fetch(self, *args, **kwargs):
        return IOLoop.current().run_sync(partial(self._client.fetch, *args, **kwargs))
    
    def close(self):
        if self._closed:
            return

        self._closed = True
        self._client.close()

if __name__ == '__main__':
    print HTTP2Client().fetch(HTTPRequest('https://localhost:5000/',connect_timeout=5, request_timeout=5, client_key="key.pem", client_cert="cert.pem"))
    import time
    from tornado.gen import sleep

    def print_it(start, res):
        print res.result(), time.time() - start

    conn =AsyncHTTP2Client()
    
    @coroutine
    def doit():
        while True:
            for i in xrange(10):
                IOLoop.current().add_callback(make_h2_conn)
            yield sleep(3)

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
        result = conn.fetch(HTTPRequest('https://localhost:5000/',connect_timeout=5, request_timeout=5, client_key="key.pem", client_cert="cert.pem"))
        result.add_done_callback(partial(print_it, start))

    IOLoop.current().add_callback(doit)
    IOLoop.current().start()
