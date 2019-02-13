import random
import string
import time
from functools import partial
from multiprocessing.pool import ThreadPool

import requests
from hyper.contrib import HTTP20Adapter

from h2tornado.client import AsyncHTTP2Client
from tornado.gen import coroutine, sleep
from tornado.ioloop import IOLoop

BODY_STR = ''.join(random.choice(string.ascii_uppercase +
                                 string.digits) for _ in xrange(1000))


class Counter(object):
    def __init__(self, value):
        self.value = value

    def decrement(self):
        self.value -= 1
        if self.value <= 0:
            return True
        return False


def run_h2tornado_benchmark(num_requests, sleep_time):
    c = AsyncHTTP2Client()

    def finish_request(f):
        try:
            f.result()
        except Exception as e:
            print e
        if counter.decrement():
            IOLoop.current().stop()

    def make_request():
        future = c.fetch("https://localhost:5000/", method="POST",
                         body=BODY_STR, client_cert="cert.pem", client_key="key.pem", request_timeout=60, connect_timeout=60,
                         headers={'Sleep-Before-Return': str(sleep_time)})
        future.add_done_callback(finish_request)

    @coroutine
    def do_test(num_requests):
        for i in xrange(num_requests):
            IOLoop.current().add_callback(make_request)
            yield sleep(0)

    counter = Counter(num_requests)
    IOLoop.current().add_callback(partial(do_test, num_requests))
    start = time.time()
    IOLoop.current().start()
    end = time.time()

    return end-start


# Can't be in local scope since it must be pickled
def run_hyper_request(s, sleep_time):
    req = requests.Request('POST',
                           url="https://localhost:5000/",
                           headers={'Sleep-Before-Return': str(sleep_time)},
                           data=BODY_STR,)
    prepped = req.prepare()
    result = s.send(prepped, timeout=10, verify=False)
    result


def run_requests_hyper_benchmark(num_requests, poolsize, sleep_time):
    s = requests.Session()
    s.mount("https://localhost:5000/", HTTP20Adapter())
    pool = ThreadPool(poolsize)
    funcs = [None] * num_requests
    start = time.time()
    for i in xrange(num_requests):
        funcs[i] = pool.apply_async(run_hyper_request, (s, sleep_time))

    for func in funcs:
        func.get()

    end = time.time()
    pool.close()
    pool.join()

    return end-start


if __name__ == '__main__':
    for num_requests in [1, 10, 100, 1000, 5000]:
        print "Testing %d fast requests..." % num_requests
        best_hyper_bench_time = 99999
        for poolsize in [1, 10, 25, 50, 100]:
            hyper_bench_time = run_requests_hyper_benchmark(
                num_requests, poolsize, 0)
            print "requests/hyper %d threads: Did %s requests in %s seconds, %ss/op" % (
                poolsize, num_requests, hyper_bench_time, float(hyper_bench_time) / num_requests)
            best_hyper_bench_time = min(
                best_hyper_bench_time, hyper_bench_time)
        h2tornado_bench_time = run_h2tornado_benchmark(num_requests, 0)
        print "h2tornado: Did %s requests in %s seconds, %ss/op" % (
            num_requests, h2tornado_bench_time, float(h2tornado_bench_time) / num_requests)
        #assert h2tornado_bench_time < hyper_bench_time
        print "h2tornado is %0.2f percent faster" % (
            100 * (best_hyper_bench_time / h2tornado_bench_time - 1))
        print "====================================================="
        print ""
    num_requests = 5000
    sleep_time = 1
    poolsize = 250

    print "Testing %d requests with requests that take %d seconds" % (
        num_requests, sleep_time)
    best_hyper_bench_time = run_requests_hyper_benchmark(
        num_requests, poolsize, sleep_time)
    print "requests/hyper %d threads: Did %s requests in %s seconds, %ss/op" % (
        poolsize, num_requests, best_hyper_bench_time, float(best_hyper_bench_time) / num_requests)
    h2tornado_bench_time = run_h2tornado_benchmark(num_requests, sleep_time)
    print "h2tornado: Did %s requests in %s seconds, %ss/op" % (
        num_requests, h2tornado_bench_time, float(h2tornado_bench_time) / num_requests)
    #assert h2tornado_bench_time < hyper_bench_time
    print "h2tornado is %0.2f percent faster" % (
        100 * (best_hyper_bench_time / h2tornado_bench_time - 1))
    print "====================================================="
