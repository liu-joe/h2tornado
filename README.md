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
requests/hyper 1 threads: Did 1 requests in 0.0203619003296 seconds, 0.0203619003296s/op
requests/hyper 10 threads: Did 1 requests in 0.00990700721741 seconds, 0.00990700721741s/op
requests/hyper 25 threads: Did 1 requests in 0.00504803657532 seconds, 0.00504803657532s/op
requests/hyper 50 threads: Did 1 requests in 0.00499510765076 seconds, 0.00499510765076s/op
requests/hyper 100 threads: Did 1 requests in 0.00523900985718 seconds, 0.00523900985718s/op
h2tornado: Did 1 requests in 0.00545597076416 seconds, 0.00545597076416s/op
h2tornado is -8.45 percent faster
=====================================================

Testing 10 fast requests...
requests/hyper 1 threads: Did 10 requests in 0.0164549350739 seconds, 0.00164549350739s/op
requests/hyper 10 threads: Did 10 requests in 0.0255649089813 seconds, 0.00255649089813s/op
requests/hyper 25 threads: Did 10 requests in 0.0279428958893 seconds, 0.00279428958893s/op
requests/hyper 50 threads: Did 10 requests in 0.021938085556 seconds, 0.0021938085556s/op
requests/hyper 100 threads: Did 10 requests in 0.0242218971252 seconds, 0.00242218971252s/op
h2tornado: Did 10 requests in 0.00916481018066 seconds, 0.000916481018066s/op
h2tornado is 79.54 percent faster
=====================================================

Testing 100 fast requests...
requests/hyper 1 threads: Did 100 requests in 0.136910915375 seconds, 0.00136910915375s/op
requests/hyper 10 threads: Did 100 requests in 0.127348899841 seconds, 0.00127348899841s/op
requests/hyper 25 threads: Did 100 requests in 0.13174200058 seconds, 0.0013174200058s/op
requests/hyper 50 threads: Did 100 requests in 0.136562108994 seconds, 0.00136562108994s/op
requests/hyper 100 threads: Did 100 requests in 0.137073993683 seconds, 0.00137073993683s/op
h2tornado: Did 100 requests in 0.0640780925751 seconds, 0.000640780925751s/op
h2tornado is 98.74 percent faster
=====================================================

Testing 1000 fast requests...
requests/hyper 1 threads: Did 1000 requests in 1.25718712807 seconds, 0.00125718712807s/op
requests/hyper 10 threads: Did 1000 requests in 1.23970794678 seconds, 0.00123970794678s/op
requests/hyper 25 threads: Did 1000 requests in 1.22800397873 seconds, 0.00122800397873s/op
requests/hyper 50 threads: Did 1000 requests in 1.19560909271 seconds, 0.00119560909271s/op
requests/hyper 100 threads: Did 1000 requests in 1.22677111626 seconds, 0.00122677111626s/op
h2tornado: Did 1000 requests in 0.653217077255 seconds, 0.000653217077255s/op
h2tornado is 83.03 percent faster
=====================================================

Testing 5000 fast requests...
requests/hyper 1 threads: Did 5000 requests in 6.34008383751 seconds, 0.0012680167675s/op
requests/hyper 10 threads: Did 5000 requests in 6.05900192261 seconds, 0.00121180038452s/op
requests/hyper 25 threads: Did 5000 requests in 6.08251714706 seconds, 0.00121650342941s/op
requests/hyper 50 threads: Did 5000 requests in 6.03093504906 seconds, 0.00120618700981s/op
requests/hyper 100 threads: Did 5000 requests in 6.09232807159 seconds, 0.00121846561432s/op
h2tornado: Did 5000 requests in 3.34326696396 seconds, 0.000668653392792s/op
h2tornado is 80.39 percent faster
=====================================================

Testing 5000 requests with requests that take 1 seconds
requests/hyper 250 threads: Did 5000 requests in 23.5788249969 seconds, 0.00471576499939s/op
h2tornado: Did 5000 requests in 4.87788796425 seconds, 0.00097557759285s/op
h2tornado is 383.38 percent faster
=====================================================
```

For applications that need high concurrency, h2tornado outperforms a requests with hyper threadpool design by ~80% for 5000 concurrent requests. In addition, when 1 second of latency was injected into the request, h2tornado significantly outperforms requests/hyper. For applications that face downstreams with variable latency, h2tornado offers much higher throughput. In addition, requests/hyper does not provide support for opening multiple connections automatically, while h2tornado will open multiple connections to ensure maximum throughput.
