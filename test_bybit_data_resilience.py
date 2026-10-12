import copy
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from test_radar_regression import load_functions


class Response:
    status_code = 200
    headers = {}

    def __init__(self, code=0):
        self.code = code

    def raise_for_status(self):
        pass

    def json(self):
        return {'retCode': self.code, 'result': {'list': [] if self.code else
                [['1791808800000', '10', '12', '9', '11', '100']]}}


class BybitDataResilienceTests(unittest.TestCase):
    def setUp(self):
        try:
            import kairos_bybit_data as data
            data.reset_cache()
        except ImportError:
            pass

    def fetcher(self, responses):
        calls = []
        def get(url, timeout):
            calls.append(url)
            return responses.pop(0)
        ns = {'time': time, 'requests': SimpleNamespace(get=get)}
        load_functions('scalp_engine.py', ['_fetch_bybit_klines_historico'], ns)
        return ns['_fetch_bybit_klines_historico'], calls

    def test_rate_limited_page_is_recovered_in_same_fetch(self):
        fetch, calls = self.fetcher([Response(10006), Response()])
        rows = fetch('BTCUSDT', '5', 1, 1791809100000)
        self.assertEqual(rows, [{'t': 1791808800000, 'o': 10., 'h': 12.,
                                 'l': 9., 'c': 11., 'v': 100.}])
        self.assertEqual(len(calls), 2)

    def test_same_cutoff_reuses_data_without_caller_mutation(self):
        fetch, calls = self.fetcher([Response(), Response()])
        original = fetch('BTCUSDT', '5', 1, 1791809100000)
        expected = copy.deepcopy(original)
        original[0]['h'] = 999
        again = fetch('BTCUSDT', '5', 1, 1791809100000)
        self.assertEqual(again, expected)
        self.assertEqual(len(calls), 1)

    def test_new_cutoff_and_other_pair_require_new_data(self):
        fetch, calls = self.fetcher([Response(), Response(), Response()])
        fetch('BTCUSDT', '5', 1, 1791809100000)
        fetch('BTCUSDT', '5', 1, 1791809400000)
        fetch('ETHUSDT', '5', 1, 1791809400000)
        self.assertEqual(len(calls), 3)

    def test_exhausted_limit_remains_fail_closed_and_is_not_cached(self):
        fetch, calls = self.fetcher([Response(10006)] * 3 + [Response()])
        self.assertEqual(fetch('BTCUSDT', '5', 1, 1791809100000), [])
        self.assertTrue(fetch('BTCUSDT', '5', 1, 1791809100000))
        self.assertEqual(len(calls), 4)

    def test_unanchored_request_is_never_cached(self):
        fetch, calls = self.fetcher([Response(), Response()])
        fetch('BTCUSDT', '5', 1)
        fetch('BTCUSDT', '5', 1)
        self.assertEqual(len(calls), 2)

    def test_cached_success_is_expired_before_reuse(self):
        import kairos_bybit_data as data
        fetch, calls = self.fetcher([Response(), Response()])
        with patch.object(data, 'CACHE_TTL', -1):
            fetch('BTCUSDT', '5', 1, 1791809100000)
        fetch('BTCUSDT', '5', 1, 1791809100000)
        self.assertEqual(len(calls), 2)

    def test_unknown_api_failure_is_not_retried_or_cached(self):
        fetch, calls = self.fetcher([Response(10001), Response()])
        self.assertEqual(fetch('BTCUSDT', '5', 1, 1791809100000), [])
        self.assertTrue(fetch('BTCUSDT', '5', 1, 1791809100000))
        self.assertEqual(len(calls), 2)

    def test_all_13_pairs_repeated_cutoff_preserves_rows_and_reduces_requests(self):
        import kairos_bybit_data as data
        pairs = ['BTC', 'ETH', 'SOL', 'XRP', 'LINK', 'ADA', 'AVAX', 'BNB',
                 'AAVE', 'NEAR', 'PENDLE', 'INJ', 'ONDO']
        fetch, calls = self.fetcher([Response() for _ in range(260)])
        with patch.object(data._pacer, 'wait', lambda: None):
            initial = {p: fetch(p + 'USDT', '5', 1, 1791809100000) for p in pairs}
            for _ in range(19):
                for p in pairs:
                    self.assertEqual(fetch(p + 'USDT', '5', 1, 1791809100000), initial[p])
        self.assertEqual(len(calls), 13)  # 260 lookups, 13 real requests.

    def test_request_slots_are_spaced_across_callers(self):
        from kairos_bybit_data import RequestPacer
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        pacer = RequestPacer(interval=0.125, clock=lambda: now[0], sleep=sleep)
        slots = []
        for _ in range(6):
            pacer.wait()
            slots.append(now[0])
        self.assertEqual(slots, [0, 0.125, 0.25, 0.375, 0.5, 0.625])

    def test_concurrent_identical_requests_share_one_successful_page(self):
        import kairos_bybit_data as data
        from concurrent.futures import ThreadPoolExecutor
        url = 'https://api.bybit.com/v5/market/kline?symbol=BTCUSDT&interval=5&end=1791809100000'
        calls = []
        def get(*args, **kwargs):
            calls.append(1)
            time.sleep(0.01)
            return Response()
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: data.get_kline_json(url, get)[0], range(6)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(row == rows[0] for row in rows))

    def test_stale_snapshot_is_refetched_and_recovers_at_same_cutoff(self):
        import kairos_bybit_data as data
        from urllib.parse import parse_qs, urlparse
        from test_live_snapshot import LiveSnapshotTests
        seen = []
        ns, cutoff = LiveSnapshotTests().ns(
            lambda *a, **kw: seen.append(kw) or {'failure_reason': 'NO_SETUP'})
        seed = ns['_fetch_bybit_klines_historico']
        m5_calls = []
        def get(url, timeout):
            query = parse_qs(urlparse(url).query)
            iv = query['interval'][0]
            rows = seed('BTCUSDT', iv, 1, fim_ts_ms=cutoff)
            if iv == '5':
                m5_calls.append(1)
                if len(m5_calls) == 1:
                    rows.pop(-2)  # Bybit has not published latest closed M5 yet.
            raw = [[str(c['t']), str(c['o']), str(c['h']), str(c['l']),
                    str(c['c']), '100'] for c in rows]
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {'retCode': 0, 'result': {'list': raw}})
        ns.update(time=time, requests=SimpleNamespace(get=get))
        load_functions('scalp_engine.py', ['_fetch_bybit_klines_historico'], ns)
        with patch.object(data._pacer, 'wait', lambda: None):
            first = ns['_kairos_scan_latest_closed']('BTCUSD', cutoff, {})
            second = ns['_kairos_scan_latest_closed']('BTCUSD', cutoff, {})
        self.assertEqual(first['erro'], 'STALE_CLOSED_CANDLES')
        self.assertNotIn('erro', second)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(m5_calls), 2)


if __name__ == '__main__':
    unittest.main()
