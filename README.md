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
* Highly performant with this version of [kahuang/hyper-h2](https://github.com/kahuang/hyper-h2). See benchmarks below

TODO:
* Following redirects
* HTTP/2 upgrade or HTTP/1
* Server push
* Priority control
* Tests
* Uni/Bidirectional Streaming

Client-side usage
-----------------
h2tornado does not make a globally shared client instance, unlike the tornado built-in httpclients.

    client = h2tornado.client.AsyncHTTP2Client()
    future = client.fetch("https://localhost:5000/",method="GET")
    future.add_done_callback(handle_future)

Benchmarking
------------
There is a benchmark file `tests/bench_test.py` that is meant to be run with [this http2server](https://github.com/kahuang/simplehttp2server). Running that httpserver will generate a self-signed client certificate and key which then must be copied into the `tests` directory for the test to pick it up.

```
Testing 1 fast requests...
requests/hyper 1 threads: Did 1 requests in 0.0222859382629 seconds, 0.0222859382629s/op
requests/hyper 10 threads: Did 1 requests in 0.0109169483185 seconds, 0.0109169483185s/op
requests/hyper 25 threads: Did 1 requests in 0.00705504417419 seconds, 0.00705504417419s/op
requests/hyper 50 threads: Did 1 requests in 0.00883889198303 seconds, 0.00883889198303s/op
requests/hyper 100 threads: Did 1 requests in 0.0120551586151 seconds, 0.0120551586151s/op
h2tornado: Did 1 requests in 0.0179319381714 seconds, 0.0179319381714s/op
h2tornado is -60.66 percent faster
=====================================================

Testing 10 fast requests...
requests/hyper 1 threads: Did 10 requests in 0.0221049785614 seconds, 0.00221049785614s/op
requests/hyper 10 threads: Did 10 requests in 0.0253479480743 seconds, 0.00253479480743s/op
requests/hyper 25 threads: Did 10 requests in 0.0207359790802 seconds, 0.00207359790802s/op
requests/hyper 50 threads: Did 10 requests in 0.0433709621429 seconds, 0.00433709621429s/op
requests/hyper 100 threads: Did 10 requests in 0.0251770019531 seconds, 0.00251770019531s/op
h2tornado: Did 10 requests in 0.0261948108673 seconds, 0.00261948108673s/op
h2tornado is -20.84 percent faster
=====================================================

Testing 100 fast requests...
requests/hyper 1 threads: Did 100 requests in 0.168220043182 seconds, 0.00168220043182s/op
requests/hyper 10 threads: Did 100 requests in 0.183554172516 seconds, 0.00183554172516s/op
requests/hyper 25 threads: Did 100 requests in 0.178300857544 seconds, 0.00178300857544s/op
requests/hyper 50 threads: Did 100 requests in 0.16913485527 seconds, 0.0016913485527s/op
requests/hyper 100 threads: Did 100 requests in 0.164309978485 seconds, 0.00164309978485s/op
h2tornado: Did 100 requests in 0.128261089325 seconds, 0.00128261089325s/op
h2tornado is 28.11 percent faster
=====================================================

Testing 1000 fast requests...
requests/hyper 1 threads: Did 1000 requests in 1.42091488838 seconds, 0.00142091488838s/op
requests/hyper 10 threads: Did 1000 requests in 1.36811995506 seconds, 0.00136811995506s/op
requests/hyper 25 threads: Did 1000 requests in 1.35694313049 seconds, 0.00135694313049s/op
requests/hyper 50 threads: Did 1000 requests in 1.35142612457 seconds, 0.00135142612457s/op
requests/hyper 100 threads: Did 1000 requests in 1.38814592361 seconds, 0.00138814592361s/op
h2tornado: Did 1000 requests in 1.01176095009 seconds, 0.00101176095009s/op
h2tornado is 33.57 percent faster
=====================================================

Testing 5000 fast requests...
requests/hyper 1 threads: Did 5000 requests in 7.00716900826 seconds, 0.00140143380165s/op
requests/hyper 10 threads: Did 5000 requests in 6.7548160553 seconds, 0.00135096321106s/op
requests/hyper 25 threads: Did 5000 requests in 6.92324304581 seconds, 0.00138464860916s/op
requests/hyper 50 threads: Did 5000 requests in 6.9481139183 seconds, 0.00138962278366s/op
requests/hyper 100 threads: Did 5000 requests in 6.91572380066 seconds, 0.00138314476013s/op
h2tornado: Did 5000 requests in 5.06973981857 seconds, 0.00101394796371s/op
h2tornado is 33.24 percent faster
=====================================================

Testing 5000 requests with requests that take 1 seconds
requests/hyper 250 threads: Did 5000 requests in 25.8569068909 seconds, 0.00517138137817s/op
h2tornado: Did 5000 requests in 5.48314499855 seconds, 0.00109662899971s/op
h2tornado is 371.57 percent faster
=====================================================
```

For applications that need high concurrency, h2tornado outperforms a requests with hyper threadpool design by ~33% for 5000 concurrent requests. In addition, when 1 second of latency was injected into the request, h2tornado significantly outperforms requests/hyper. For applications that face downstreams with variable latency, h2tornado offers much higher throughput. In addition, requests/hyper does not provide support for opening multiple connections automatically, while h2tornado will open multiple connections to ensure maximum throughput.
