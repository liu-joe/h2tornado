h2tornado
=============

This package contains an HTTP/2 client implementation for
[Tornado](http://www.tornadoweb.org) using [hyper-h2](https://github.com/python-hyper/hyper-h2) and some utilities from [python-hyper](https://github.com/Lukasa/hyper).

Installation
------------

    pip install git+https://github.com/ContextLogic/h2tornado.git

This package has only been tested with Tornado 4.5 on Python 2.7.10+.

Features
--------
* Asynchronous HTTP/2 Client
* Multiplexing streams of data over a single connection with managed flow control
* Automatic connection pooling and handling of exhausting stream ids on a connection
* Familiar high-level fetch() interface

TODO:
* Following redirects
* HTTP/2 upgrade or HTTP/1
* Server push
* Priority control
* Tests

Client-side usage
-----------------
h2tornado does not make a globally shared client instance, unlike the tornado built-in httpclients.

    client = h2tornado.client.AsyncHTTP2Client()
    future = client.fetch("https://localhost:5000/",method="GET")
    future.add_done_callback(handle_future)
