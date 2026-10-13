import copy
import importlib.util
import unittest
from types import SimpleNamespace
import scalp_engine as e


def module():
    spec=importlib.util.find_spec('kairos_ob_2r_replay')
    if spec is None:
        return SimpleNamespace(ob_plan=lambda *a,**k:None,resolve_trade=lambda *a,**k:None)
    return __import__('kairos_ob_2r_replay')


class OBReplayTests(unittest.TestCase):
    def setUp(self):
        self.api=module()
        self.event={'key':'S:ARMED','setup_key':'S','phase':'ARMED','timestamp':6000000,
                    'ready_ts':6000000,'tf':'M5','zone_type':'OB_bearish','direction':'SHORT',
                    'bottom':100.,'top':102.,'level':100.}
        self.cs=[{'t':i*300000,'o':101.,'h':102.,'l':100.,'c':101.} for i in range(20)]
        self.plan={'entry':100.,'sl':102.,'tp_2r':96.,'be_trigger':98.,'risk':2.,
                   'direction':'SHORT','armed_ts':6000000,'key':'S'}
        self.touch={'t':6000000,'o':99.5,'h':101.,'l':99.,'c':100.5}
        self.minute={'t':6000000,'o':99.5,'h':101.,'l':99.,'c':100.5}

    def test_short_ob_top_plus_atr_and_exact_two_r(self):
        p=self.api.ob_plan(self.event,self.cs,e)
        self.assertIsNotNone(p)
        self.assertEqual((p['entry'],p['sl'],p['tp_2r']),(100.,102.5,95.))
        self.assertEqual(p['be_trigger'],97.5)

    def test_long_ob_bottom_minus_atr_and_top_entry(self):
        event={**self.event,'direction':'LONG','level':102.,'zone_type':'OB_bullish'}
        p=self.api.ob_plan(event,self.cs,e)
        self.assertIsNotNone(p)
        self.assertEqual((p['entry'],p['sl'],p['tp_2r']),(102.,99.5,107.))

    def test_future_extreme_never_changes_frozen_plan(self):
        a=self.api.ob_plan(self.event,self.cs,e)
        b=self.api.ob_plan(self.event,self.cs+[{'t':6000000,'o':1.,'c':500.,'h':1000.,'l':0.}],e)
        self.assertIsNotNone(a)
        self.assertEqual(a,b)

    def test_non_ob_and_non_m5_not_in_cohort(self):
        self.assertIsNone(self.api.ob_plan({**self.event,'zone_type':'FVG_bearish'},self.cs,e))
        self.assertIsNone(self.api.ob_plan({**self.event,'tf':'M15'},self.cs,e))

    def test_short_tp_and_stop_use_price_order_not_final_profit(self):
        for low,high,expected in [(95.,101.,'TP_2R'),(99.,103.,'SL')]:
            with self.subTest(expected=expected):
                bars=[self.minute,{'t':6060000,'o':100.,'h':high,'l':low,'c':100.}]
                r=self.api.resolve_trade(self.plan,self.touch,bars,6120000)
                self.assertIsNotNone(r)
                self.assertEqual(r['outcome'],expected)

    def test_same_minute_tp_sl_is_ambiguous(self):
        bars=[self.minute,{'t':6060000,'o':100.,'h':103.,'l':95.,'c':100.}]
        r=self.api.resolve_trade(self.plan,self.touch,bars,6120000)
        self.assertIsNotNone(r)
        self.assertEqual(r['outcome'],'AMBIGUOUS')
        self.assertIsNone(r['R'])

    def test_missing_m1_cannot_fabricate_outcome(self):
        r=self.api.resolve_trade(self.plan,self.touch,[self.minute,{'t':6120000,'o':100.,'h':103.,'l':99.,'c':100.}],6180000)
        self.assertIsNotNone(r)
        self.assertEqual(r['outcome'],'DATA_GAP')

    def test_unclosed_future_minute_is_not_used(self):
        bars=[self.minute,{'t':6060000,'o':100.,'h':103.,'l':99.,'c':100.}]
        r=self.api.resolve_trade(self.plan,self.touch,bars,6119999)
        self.assertIsNotNone(r)
        self.assertEqual(r['outcome'],'PENDING')

    def test_be_is_separate_and_does_not_change_fixed_sl_case(self):
        bars=[self.minute,{'t':6060000,'o':99.,'h':99.5,'l':97.,'c':97.5},
              {'t':6120000,'o':98.,'h':101.,'l':97.5,'c':100.}]
        fixed=self.api.resolve_trade(self.plan,self.touch,bars,6180000)
        be=self.api.resolve_trade(self.plan,self.touch,bars,6180000,be=True)
        self.assertIsNotNone(fixed);self.assertIsNotNone(be)
        self.assertEqual(fixed['outcome'],'PENDING')
        self.assertEqual(be['outcome'],'BE')
        self.assertEqual(be['R'],0.)

    def test_entry_minute_extremes_are_not_assumed_after_fill(self):
        bars=[{**self.minute,'l':95.}]
        r=self.api.resolve_trade(self.plan,self.touch,bars,6060000)
        self.assertIsNotNone(r)
        self.assertEqual(r['outcome'],'AMBIGUOUS_ENTRY_MINUTE')

    def test_retest_before_arming_is_rejected(self):
        r=self.api.resolve_trade({**self.plan,'armed_ts':6300000},self.touch,[self.minute],6360000)
        self.assertIsNotNone(r)
        self.assertEqual(r['outcome'],'NOT_ARMED_BEFORE_RETEST')


if __name__=='__main__':unittest.main()

class OBReplayIntegrationTests(unittest.TestCase):
    def fixture(self, prospective=True):
        api=module(); start=60000000; end=start+600000
        raw={'M15':[{'t':i*900000,'o':101.,'h':102.,'l':100.,'c':101.} for i in range(68)],
             'M5':[{'t':i*300000,'o':101.,'h':102.,'l':100.,'c':101.} for i in range(203)],
             'M1':[{'t':t,'o':99.5,'h':101.,'l':99.,'c':100.5} for t in range(start,end,60000)]}
        raw['M1'][1].update(l=94.,c=95.)
        calls=[]
        def evaluate(*args,**kw):
            calls.append(kw)
            self.assertTrue(kw['audit_entries_only'])
            self.assertEqual(kw['experimental_poi_policy'],'A_CURRENT')
            self.assertTrue(all(e._kairos_candle_close_ts(c['t'],api.LIVE_SPECS[tf][0])<=kw['cutoff_ts']
                                for tf,cs in kw['candles_por_tf'].items() for c in cs))
            if not prospective and kw['cutoff_ts']==start:
                return {'valid':False,'failure_reason':'NO_SETUP'}
            touch=None if kw['cutoff_ts']==start else {'t':start,'o':99.5,'h':101.,'l':94.,'c':95.}
            return {'valid':False,'failure_reason':'AUDIT_ENTRY_IDENTIFIED' if touch else 'AGUARDANDO_RETESTE_ZONA',
                    'entry_audit_only':bool(touch),'entry_setup':{'key':'S','ready_ts':start,'tf':'M5',
                    'zone_type':'OB_bearish','direction':'SHORT','bottom':100.,'top':102.,'level':100.,
                    'retest':touch,'capture_tf':'D1','capture_level':110.}}
        fake=SimpleNamespace(compute_atr=e.compute_atr,
            _kairos_candles_fechados_ate=e._kairos_candles_fechados_ate,
            _kairos_candle_close_ts=e._kairos_candle_close_ts,
            _kairos_snapshot_freshness=lambda *a:{},
            avaliar_vortex_decision_layer_v2=evaluate,_kairos_apply_entry_phase=e._kairos_apply_entry_phase)
        return api,fake,raw,start,end,calls

    def test_armed_ob_retest_is_counted_once_and_resolved_inside_m5_with_m1(self):
        api,fake,raw,start,end,calls=self.fixture()
        r=api.replay_from_candles(fake,'NEARUSD',raw,start,end)
        self.assertEqual(r['armed_ob_setups'],1)
        self.assertEqual(r['retest_ob_entries'],1)
        self.assertEqual(r['fixed_2r']['TP_2R'],1)
        self.assertEqual(r['fixed_2r']['resolved_only_net_R'],2.)
        self.assertEqual(len(calls),3)

    def test_first_observation_after_touch_is_not_a_prospective_fill(self):
        api,fake,raw,start,end,_=self.fixture(prospective=False)
        r=api.replay_from_candles(fake,'NEARUSD',raw,start,end)
        self.assertEqual(r['retest_ob_entries'],0)
        self.assertTrue(r['exclusions'])

    def test_gap_in_structure_history_marks_incomplete_not_zero_clean_signals(self):
        api,fake,raw,start,end,calls=self.fixture()
        raw['M5'].pop(150)
        r=api.replay_from_candles(fake,'NEARUSD',raw,start,end)
        self.assertFalse(r['coverage_complete'])
        self.assertTrue(r['coverage_issues'])
        self.assertEqual(calls,[])

    def test_gap_in_htf_history_marks_incomplete(self):
        api,fake,raw,start,end,calls=self.fixture()
        raw['H1']=[{'t':t,'o':101.,'h':102.,'l':100.,'c':101.}
                   for t in (0,3600000,10800000)]
        r=api.replay_from_candles(fake,'NEARUSD',raw,start,end)
        self.assertFalse(r['coverage_complete'])
        self.assertEqual(calls,[])

    def test_snapshot_history_spec_matches_current_live_scanner(self):
        import ast
        from pathlib import Path
        tree=ast.parse(Path('scalp_engine.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_kairos_scan_latest_closed')
        spec=next(n.value for n in ast.walk(fn) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='specs' for t in n.targets))
        self.assertEqual(module().LIVE_SPECS,ast.literal_eval(spec))

    def test_failed_fetch_is_not_a_complete_backtest(self):
        fake=SimpleNamespace(_kairos_normalize_instrument=lambda p:'NEARUSDT',
                             _fetch_bybit_klines_historico=lambda *a,**kw:[])
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_BYBIT_DATA'):
            module().fetch_inputs(fake,'NEARUSD',60000000,146400000)

    def test_route_rejects_unsupported_pair_and_future_cutoff_without_job(self):
        from flask import Flask
        app=Flask('test_ob_replay');module().register(app,SimpleNamespace())
        client=app.test_client()
        self.assertEqual(client.get('/experiment/ob_2r_replay?start=1&pair=WRONG').status_code,400)
        self.assertEqual(client.get('/experiment/ob_2r_replay?start=1&fim_ts_ms=9999999999999').status_code,400)
        self.assertEqual(client.get('/experiment/ob_2r_replay').status_code,200)

if __name__ == '__main__':
    unittest.main()
