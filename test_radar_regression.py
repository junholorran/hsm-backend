import ast
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace


def load_functions(path, names, ns):
    tree = ast.parse(Path(path).read_text())
    for name in names:
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        if not nodes:
            ns[name] = lambda *a, **k: None
            continue
        node = nodes[0]
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), ns)
    return ns


class RadarRegression(unittest.TestCase):
    def math(self):
        return load_functions('scalp_engine.py', ['_kairos_candle_close_ts', '_kairos_candles_fechados_ate', '_extrair_swings_lux_algo', '_kairos_lux50_structural_levels'], {
            'datetime': datetime, 'timezone': timezone, 'KAIROS_STRUCTURAL_SWING_SIZE': 50,
            'INTERVALO_MS_POR_LABEL': {'60': 3600000, '240': 14400000, 'D': 86400000, 'W': 604800000},
        })

    def candles(self, duration):
        cs = [{'t': i * duration, 'h': 2., 'l': 1., 'o': 1.5, 'c': 1.5} for i in range(65)]
        cs[5]['l'] = 0.
        return cs

    def test_unclosed_confirmation_is_not_known(self):
        ns = self.math()
        for tf, dur in [('H1', 3600000), ('H4', 14400000), ('D1', 86400000), ('W1', 604800000)]:
            with self.subTest(tf=tf):
                self.assertEqual(ns['_kairos_lux50_structural_levels'](self.candles(dur), tf, 55 * dur + 1), [])

    def test_confirmation_records_close_and_preserves_price(self):
        ns = self.math(); dur = 3600000
        levels = ns['_kairos_lux50_structural_levels'](self.candles(dur), 'H1', 56 * dur)
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0]['confirmed_ts'], 56 * dur)
        self.assertEqual(levels[0]['level'], 0.)

    def telegram(self, ok):
        response = SimpleNamespace(status_code=200, json=lambda: {'ok': ok})
        ns = load_functions('app.py', ['send_telegram'], {
            'requests': SimpleNamespace(post=lambda *a, **k: response),
            'TELEGRAM_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': 'test-chat',
        })
        return ns['send_telegram']('test')

    def test_telegram_success_is_confirmed(self):
        self.assertIs(self.telegram(True), True)

    def test_telegram_rejection_is_failure(self):
        self.assertIs(self.telegram(False), False)

    def test_capture_radar_has_htf_context_without_trade(self):
        ns = load_functions('scalp_engine.py', ['_kairos_capture_radar_events'], {'_kairos_monthly_capture_context':lambda *a:[], 'compute_lux_structure_bias': lambda candles, swing_size: 'alta' if candles else 'neutro', '_kairos_candles_fechados_ate':lambda cs, iv, cutoff:cs})
        candidate = {'liquidity_tf': 'H1', 'liquidity_type': 'SWING_LOW', 'nivel': 100., 'liquidity_origin_ts': 0, 'first_capture_ts': 900000, 'post_capture_state': 'UNRESOLVED_REACTION'}
        result = {'valid': False, 'structural_sweep_audit': {'candidates': [candidate]}}
        event = ns['_kairos_capture_radar_events'](result, {'W1': [1], 'D1': [1], 'H4': [], 'H1': [1]}, 1800000)[0]
        self.assertIsInstance(event, dict)
        self.assertEqual(event['timestamp'], 1800000)
        self.assertEqual(event['context']['W1'], 'alta')
        self.assertNotIn('entry', event)
        self.assertNotIn('direction', event)
        self.assertEqual(ns['_kairos_capture_radar_events'](result, {}, 1799999), [])

    def test_capture_at_confirmation_boundary_is_not_skipped(self):
        liq = {'tf':'H1','type':'SWING_HIGH','level':100.,'confirmed_ts':900000,'state':'CAPTURED','captured_ts':900000}
        ns = load_functions('scalp_engine.py', ['_kairos_select_structural_first_capture_sweep'], {
            '_kairos_structural_registry': lambda *a: [liq], 'KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS': ('H1',), 'KAIROS_PRIMARY_LIQUIDITY_PRIORITY': {'H1':1},
        })
        cs = [{'t':0,'h':99.,'l':98.,'c':99.}, {'t':900000,'h':101.,'l':98.,'c':99.}, {'t':1800000,'h':99.,'l':97.,'c':98.}, {'t':2700000,'h':99.,'l':97.,'c':98.}]
        sweep, _ = ns['_kairos_select_structural_first_capture_sweep']({'M15':cs}, 3600000)
        self.assertIsNotNone(sweep)
        self.assertEqual(sweep['first_capture_ts'], 900000)

    def test_radar_delayed_native_recognition_uses_availability(self):
        ns = load_functions('scalp_engine.py', ['_kairos_capture_radar_events'], {'_kairos_monthly_capture_context':lambda *a:[], 'compute_lux_structure_bias': lambda *a, **k: 'neutro', '_kairos_candles_fechados_ate':lambda cs, iv, cutoff:cs})
        captures = [{'liquidity_tf':tf,'liquidity_type':'SWING_LOW','nivel':100.,'first_capture_ts':900000,'native_capture_confirm_ts':14400000,'post_capture_state':'UNRESOLVED_REACTION'} for tf in ('H1','H4')]
        events = ns['_kairos_capture_radar_events']({'structural_sweep_audit':{'candidates':captures}}, {}, 14400000)
        self.assertIsInstance(events, list)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['timestamp']==14400000 for e in events))
        self.assertTrue(all(e['capture_confirm_ts']==1800000 for e in events))

    def test_failed_delivery_retries_and_success_deduplicates(self):
        calls = []
        success = [False, True]
        def send(message):
            calls.append(message)
            return success.pop(0)
        ns = load_functions('app.py', ['_kairos_send_capture_events'], {'datetime':datetime,'timezone':timezone,'send_telegram':send, '_KAIROS_LIVE_LAST_TS':{'BTCUSD':0}, '_KAIROS_RADAR_SEEN':{'BTCUSD':set()}})
        event={'key':'capture','timestamp':1800000,'context':{tf:'alta' for tf in ('W1','D1','H4','H1')},'reaction':'UNRESOLVED_REACTION','liquidity_tf':'H1','liquidity_type':'SWING_LOW','level':100.}
        result={'radar_captures':[event]}
        self.assertFalse(ns['_kairos_send_capture_events']('BTCUSD',result,1800000))
        self.assertNotIn('capture',ns['_KAIROS_RADAR_SEEN']['BTCUSD'])
        self.assertTrue(ns['_kairos_send_capture_events']('BTCUSD',result,1800000))
        self.assertTrue(ns['_kairos_send_capture_events']('BTCUSD',result,1800000))
        self.assertEqual(len(calls),2)

    def test_evaluator_receives_actual_close_cutoff(self):
        captured = []
        class StopAfterClock(Exception):
            pass
        def previous(cs, now):
            captured.append(now)
            raise StopAfterClock
        ns = load_functions('scalp_engine.py', ['avaliar_vortex_decision_layer_v2'], {
            '_kairos_build_mtf_map':lambda cs:{}, '_kairos_context_bias':lambda cs:{},
            '_kairos_previous_day_dealing_range':previous,
        })
        c={'t':14340000,'h':2.,'l':1.,'c':1.5,'o':1.5}
        with self.assertRaises(StopAfterClock):
            ns['avaliar_vortex_decision_layer_v2']([c],[c],candles_por_tf={'M1':[c]},cutoff_ts=14400000)
        self.assertEqual(captured,[14400000])

    def test_previous_day_capture_does_not_wait_an_extra_day(self):
        liq = {'tf':'D1','type':'PDH','level':100.,'confirmed_ts':0,'state':'CAPTURED','captured_ts':900000,'captured_tf':'M15'}
        ns = load_functions('scalp_engine.py', ['_kairos_select_structural_first_capture_sweep'], {
            '_kairos_structural_registry': lambda *a: [liq], 'KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS': ('D1',), 'KAIROS_PRIMARY_LIQUIDITY_PRIORITY': {'D1':1},
        })
        cs = [{'t':0,'h':99.,'l':98.,'c':99.}, {'t':900000,'h':101.,'l':98.,'c':99.}, {'t':1800000,'h':99.,'l':97.,'c':98.}, {'t':2700000,'h':99.,'l':97.,'c':98.}]
        sweep, _ = ns['_kairos_select_structural_first_capture_sweep']({'M15':cs}, 3600000)
        self.assertEqual(sweep['native_capture_confirm_ts'], 1800000)


if __name__ == '__main__':
    unittest.main()
