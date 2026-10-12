"""Bybit transport only: exact-cutoff page reuse, pacing and bounded retries.

Never supplies an older cutoff as a fallback and never caches API failures.
Compressed payloads bound memory use; decoding gives each caller its own copy.
"""
import json
import random
import threading
import time
import zlib
from collections import OrderedDict
from urllib.parse import parse_qs, urlparse


class RequestPacer:
    def __init__(self, interval=0.125, clock=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self):
        with self.lock:
            now = self.clock()
            delay = self.next_slot - now
            if delay > 0:
                self.sleep(delay)
            self.next_slot = self.clock() + self.interval


_pacer = RequestPacer()
_lock = threading.Lock()
_key_locks = [threading.Lock() for _ in range(64)]
_cache = OrderedDict()
_cache_bytes = 0
_stats = dict(network_requests=0, cache_hits=0, rate_limit_retries=0, exhausted=0)
_last_report = time.monotonic()
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_PAGES = 256
CACHE_TTL = 300.0


def reset_cache():
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0


def invalidate_pages(symbol, intervals):
    """Allow a failed freshness/coverage check to recover on the very next scan."""
    global _cache_bytes
    with _lock:
        for url in list(_cache):
            query = parse_qs(urlparse(url).query)
            if (query.get('symbol') == [symbol] and
                    query.get('interval', [None])[0] in intervals):
                _, packed = _cache.pop(url)
                _cache_bytes -= len(packed)


def _record(name):
    global _last_report
    with _lock:
        _stats[name] += 1
        now = time.monotonic()
        if now - _last_report < 60:
            return
        report = dict(_stats, cache_pages=len(_cache), cache_bytes=_cache_bytes)
        for key in _stats:
            _stats[key] = 0
        _last_report = now
    print('[KAIROS_BYBIT_DATA] ' + json.dumps(report, sort_keys=True), flush=True)


def _cached(url):
    global _cache_bytes
    with _lock:
        item = _cache.get(url)
        if item is None:
            return None
        expiry, packed = item
        if time.monotonic() >= expiry:
            _cache.pop(url)
            _cache_bytes -= len(packed)
            return None
        _cache.move_to_end(url)
    return json.loads(zlib.decompress(packed))


def _store(url, data):
    global _cache_bytes
    packed = zlib.compress(json.dumps(data, separators=(',', ':')).encode())
    if len(packed) > MAX_CACHE_BYTES:
        return
    with _lock:
        old = _cache.pop(url, None)
        if old:
            _cache_bytes -= len(old[1])
        _cache[url] = (time.monotonic() + CACHE_TTL, packed)
        _cache_bytes += len(packed)
        while len(_cache) > MAX_CACHE_PAGES or _cache_bytes > MAX_CACHE_BYTES:
            _, old = _cache.popitem(last=False)
            _cache_bytes -= len(old[1])


def get_kline_json(url, request_get, timeout=10):
    """Return (JSON, HTTP status, transport metrics); original decoder stays native."""
    query = parse_qs(urlparse(url).query)
    cacheable = bool(query.get('end'))
    with _key_locks[hash(url) % len(_key_locks)]:
        cached = _cached(url) if cacheable else None
        if cached is not None:
            _record('cache_hits')
            return cached, 200, {'cache_hit': True, 'attempts': 0}
        attempts = []
        for attempt in range(3):
            _pacer.wait()
            _record('network_requests')
            response = request_get(url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            code = data.get('retCode') if isinstance(data, dict) else None
            attempts.append(code)
            if code != 10006:
                if cacheable and code == 0 and data.get('result', {}).get('list'):
                    _store(url, data)
                return data, response.status_code, {'cache_hit': False,
                        'attempts': len(attempts), 'retcodes': attempts}
            if attempt == 2:
                _record('exhausted')
                return data, response.status_code, {'cache_hit': False,
                        'attempts': len(attempts), 'retcodes': attempts}
            _record('rate_limit_retries')
            # Respect reset hints when supplied, but do not block a worker indefinitely.
            delay = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
            try:
                reset_ms = float(getattr(response, 'headers', {}).get(
                    'X-Bapi-Limit-Reset-Timestamp', 0))
                delay = max(delay, reset_ms / 1000 - time.time())
            except (TypeError, ValueError):
                pass
            if delay > 5:
                _record('exhausted')
                return data, response.status_code, {'cache_hit': False,
                        'attempts': len(attempts), 'retcodes': attempts,
                        'retry_deferred': True}
            time.sleep(max(0, delay))


print('[KAIROS_BYBIT_DATA] installed=true exact_cutoff_cache=true '
      'paced_requests=true bounded_retry=true math_changed=false', flush=True)
