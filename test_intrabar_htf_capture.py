import unittest
import scalp_engine as e
from datetime import datetime,timezone
from test_radar_regression import load_functions

class IntrabarHtfCaptureTests(unittest.TestCase):
    def ns(self,levels):
        return load_functions('scalp_engine.py',['_kairos_candle_close_ts','_kairos_candles_fechados_ate','_kairos_select_structural_first_capture_sweep','_kairos_capture_radar_events'],{
            'datetime':datetime,'timezone':timezone,'INTERVALO_MS_POR_LABEL':{'15':900000},
            '_kairos_structural_registry':lambda *a:levels,
            'KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS':('W1','D1','H4','H1'),
            'KAIROS_PRIMARY_LIQUIDITY_PRIORITY':{'W1':4,'D1':3,'H4':2,'H1':1},
            '_kairos_monthly_capture_context':lambda *a:[],
            'compute_lux_structure_bias':lambda *a,**k:'neutro'})
    def candles(self):
        return [dict(t=t,o=99.,h=99.,l=98.,c=99.) for t in [0,900000,1800000]]+[
            dict(t=2700000,o=99.,h=101.,l=98.,c=99.),
            dict(t=3600000,o=99.,h=99.,l=98.,c=99.)]
    def level(self,tf='H1',**kwargs):
        return dict(tf=tf,type='SWING_HIGH',level=100.,origin_ts=-100000000,
                    confirmed_ts=0,state='ACTIVE',captured_ts=None,**kwargs)

    def test_closed_m15_capture_observed_without_macro_close_all_primary_tfs(self):
        for tf in ['W1','D1','H4','H1']:
            with self.subTest(tf=tf):
                ns=self.ns([self.level(tf)]);data={'M15':self.candles()}
                selected,audit=ns['_kairos_select_structural_first_capture_sweep'](data,3600000)
                self.assertIsNone(selected)
                self.assertEqual(len(audit['candidates']),1)
                capture=audit['candidates'][0]
                self.assertEqual(capture['first_capture_ts'],2700000)
                self.assertEqual(capture['native_capture_confirm_ts'],3600000)
                events=ns['_kairos_capture_radar_events']({'structural_sweep_audit':audit},data,3600000)
                self.assertEqual(len(events),1)
                self.assertEqual(events[0]['timestamp'],3600000)
                selected,audit=ns['_kairos_select_structural_first_capture_sweep'](data,4500000)
                self.assertIsNotNone(selected)
                self.assertEqual(selected['confirm_ts'],3600000)

    def test_open_m15_and_open_reaction_cannot_authorize(self):
        ns=self.ns([self.level()]);data={'M15':self.candles()}
        self.assertEqual(ns['_kairos_select_structural_first_capture_sweep'](data,3599999)[1]['candidates'],[])
        self.assertIsNone(ns['_kairos_select_structural_first_capture_sweep'](data,4499999)[0])

    def test_native_close_does_not_change_capture_identity_or_delay(self):
        liq=self.level();ns=self.ns([liq]);data={'M15':self.candles()}
        before=ns['_kairos_select_structural_first_capture_sweep'](data,4500000)[0]
        liq.update(state='CAPTURED',captured_ts=0)
        after=ns['_kairos_select_structural_first_capture_sweep'](data,7200000)[0]
        self.assertEqual(before['first_capture_ts'],after['first_capture_ts'])
        self.assertEqual(after['native_capture_confirm_ts'],3600000)

    def test_consumed_native_capture_before_m15_history_is_not_recycled(self):
        liq=self.level('W1');liq.update(state='CAPTURED',captured_ts=-604800000)
        ns=self.ns([liq]);data={'M15':self.candles()}
        self.assertEqual(ns['_kairos_select_structural_first_capture_sweep'](data,4500000)[1]['candidates'],[])

    def test_overlapping_native_capture_keeps_m15_identity_after_macro_close(self):
        liq=self.level('H4');liq['confirmed_ts']=900000
        ns=self.ns([liq]);data={'M15':self.candles()[1:]}
        before=ns['_kairos_select_structural_first_capture_sweep'](data,4500000)[0]
        liq.update(state='CAPTURED',captured_ts=0)
        after=ns['_kairos_select_structural_first_capture_sweep'](data,14400000)[0]
        self.assertIsNotNone(after)
        self.assertEqual(before['first_capture_ts'],after['first_capture_ts'])
        self.assertEqual(before['capture_confirm_ts'],after['capture_confirm_ts'])

    def test_partial_native_history_cannot_prove_first_capture(self):
        liq=self.level('H4');ns=self.ns([liq]);data={'M15':self.candles()[1:]}
        self.assertIsNone(ns['_kairos_select_structural_first_capture_sweep'](data,4500000)[0])
        liq.update(state='CAPTURED',captured_ts=0)
        self.assertIsNone(ns['_kairos_select_structural_first_capture_sweep'](data,14400000)[0])

    def test_gap_before_crossing_cannot_prove_first_capture(self):
        data={'M15':[c for c in self.candles() if c['t']!=1800000]}
        self.assertIsNone(self.ns([self.level('H4')])['_kairos_select_structural_first_capture_sweep'](data,4500000)[0])

    def test_exact_m15_pre_history_consumption_is_not_recycled(self):
        liq=self.level('W1');liq.update(type='PWH',state='CAPTURED',captured_ts=-900000,captured_tf='M15')
        self.assertEqual(self.ns([liq])['_kairos_select_structural_first_capture_sweep']({'M15':self.candles()},4500000)[1]['candidates'],[])

    def test_unconfirmed_level_and_exact_touch_are_not_captures(self):
        liq=self.level();liq['confirmed_ts']=7200000
        self.assertEqual(self.ns([liq])['_kairos_select_structural_first_capture_sweep']({'M15':self.candles()},4500000)[1]['candidates'],[])
        liq['confirmed_ts']=0;data={'M15':self.candles()};data['M15'][3]['h']=100.
        self.assertEqual(self.ns([liq])['_kairos_select_structural_first_capture_sweep'](data,4500000)[1]['candidates'],[])

    def test_real_registry_uses_closed_native_level_and_closed_m15_evidence(self):
        hour=3600000;start=64*hour
        h1=[dict(t=i*hour,o=1.5,h=2.,l=1.,c=1.5) for i in range(65)]
        h1[5]['l']=0.;h1[-1].update(h=999.,l=-999.,c=500.)
        m15=[dict(t=t,o=1.,h=2.,l=1.,c=1.) for t in [start-2700000,start-1800000,start-900000]]
        m15.extend([dict(t=start,o=1.,h=1.,l=-1.,c=-.5),dict(t=start+900000,o=-.5,h=1.,l=-1.,c=-.5)])
        data={'H1':h1,'M15':m15};cut=start+1800000
        level=next(x for x in e._kairos_structural_registry(data,cut) if x['tf']=='H1')
        self.assertEqual((level['level'],level['state']),(0.,'ACTIVE'))
        selected,audit=e._kairos_select_structural_first_capture_sweep(data,cut)
        self.assertEqual(selected['nivel'],0.)
        self.assertEqual(selected['native_capture_confirm_ts'],start+900000)
        self.assertEqual(selected['post_capture_state'],'ACCEPTANCE_CONTINUATION')

    def test_gap_after_capture_does_not_substitute_for_next_m15_reaction(self):
        data={'M15':self.candles()};data['M15'][-1]['t']+=900000
        selected,audit=self.ns([self.level()])['_kairos_select_structural_first_capture_sweep'](data,5400000)
        self.assertIsNone(selected)
        self.assertEqual(audit['candidates'][0]['post_capture_state'],'UNRESOLVED_REACTION')

if __name__=='__main__':unittest.main()
