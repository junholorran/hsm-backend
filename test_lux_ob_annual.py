import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import scalp_engine as e

def ts(year, month=1):
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp()*1000)

class LuxObLifecycleTests(unittest.TestCase):
    def candles(self, n=25):
        return [dict(t=i*900000,o=5.,h=10.,l=2.,c=5.,v=1.) for i in range(n)]
    def event(self, index=5, direction='baixa'):
        return dict(index=index,t=index*900000,direcao=direction,tipo='BOS',broken_swing_origin_ts=0)
    def test_bear_wick_removes_even_close_inside(self):
        cs=self.candles(); cs[6].update(h=11.,c=9.)
        with patch.object(e,'compute_lux_structure_events',return_value=[self.event()]):
            z=e._kairos_order_blocks_map(cs)[0]
        self.assertEqual(z['state'],'INVALIDADA')
        self.assertEqual(z['invalidated_ts'],cs[6]['t'])
    def test_bull_exact_boundary_survives_then_wick_removes(self):
        cs=self.candles();cs[7].update(l=1.9,c=5.)
        with patch.object(e,'compute_lux_structure_events',return_value=[self.event(direction='alta')]):
            z=e._kairos_order_blocks_map(cs)[0]
        self.assertEqual(z['invalidated_ts'],cs[7]['t'])
    def test_old_active_ob_does_not_expire_by_lookback(self):
        cs=self.candles(400)
        with patch.object(e,'compute_lux_structure_events',return_value=[self.event()]):
            zs=e._kairos_order_blocks_map(cs,lookback=10)
        self.assertEqual(len(zs),1)
        self.assertEqual(zs[0]['state'],'ATIVA')
    def test_source_queue_keeps_100_active_even_identical_origins(self):
        cs=self.candles(140);events=[self.event(i,'alta') for i in range(20,130)]
        with patch.object(e,'compute_lux_structure_events',return_value=events):
            zs=e._kairos_order_blocks_map(cs)
        live=[z for z in zs if z['state']=='ATIVA']
        self.assertEqual(len(live),100)
        self.assertEqual(min(z['break_ts'] for z in live),30*900000)
    def test_retest_and_invalidation_same_bar_is_observed_without_fill(self):
        cs=[dict(t=900000,o=9.,h=11.,l=8.,c=9.)]
        z=dict(tipo='OB_bearish',direcao='baixa',bottom=8.,top=10.,break_ts=0)
        touch=e._kairos_retest_zone(cs,z,900000,tf='15')
        self.assertTrue(touch['poi_invalidated_same_bar'])
        self.assertFalse(touch['fill_assumed'])
        self.assertEqual(touch['t'],900000)
    def test_later_deletion_does_not_erase_first_retest(self):
        from kairos_lux_ob import ob_eligible_through_retest
        z=dict(tipo='OB_bearish',direcao='baixa',bottom=8.,top=10.,break_ts=0)
        cs=[dict(t=0,o=7.,h=7.,l=6.,c=7.),dict(t=900000,o=8.,h=9.,l=7.,c=8.),
            dict(t=1800000,o=9.,h=11.,l=8.,c=9.)]
        self.assertTrue(ob_eligible_through_retest(z,cs,900000))
        self.assertEqual(e._kairos_retest_zone(cs,z,900000,tf='15')['t'],900000)
    def test_ob_invalid_before_authorization_cannot_retest_later(self):
        from kairos_lux_ob import ob_eligible_through_retest
        z=dict(tipo='OB_bearish',direcao='baixa',bottom=8.,top=10.,break_ts=0)
        cs=[dict(t=900000,o=11.,h=12.,l=11.,c=11.),dict(t=1800000,o=8.,h=9.,l=7.,c=8.)]
        self.assertFalse(ob_eligible_through_retest(z,cs,1800000))
        self.assertIsNone(e._kairos_retest_zone(cs,z,1800000,tf='15'))
    def test_same_bar_deletion_is_explicit_in_telegram(self):
        from kairos_telegram_ui import format_retest_message
        message=format_retest_message('BTCUSD',dict(direction='SHORT',poi_invalidated_same_bar=True))
        self.assertIn('invalidado',message)
        self.assertIn('SEM NOVA ENTRADA',message)
    def test_capacity_before_touch_rejects_but_after_touch_preserves_evidence(self):
        from kairos_lux_ob import ob_eligible_through_retest
        z=dict(tipo='OB_bearish',direcao='baixa',bottom=8.,top=10.,break_ts=0)
        cs=[dict(t=900000,o=8.,h=9.,l=7.,c=8.),dict(t=1800000,o=8.,h=9.,l=7.,c=8.)]
        self.assertFalse(ob_eligible_through_retest(z,cs,900000,stream={'removed_ts':1}))
        self.assertTrue(ob_eligible_through_retest(z,cs,900000,stream={'removed_ts':900000}))
        self.assertTrue(ob_eligible_through_retest(z,cs,900000,stream={'removed_ts':1800000}))
    def test_same_bar_capacity_eviction_is_observed_without_new_entry(self):
        z=dict(tipo='OB_bearish',direcao='baixa',bottom=8.,top=10.,break_ts=0,capacity_removed_ts=900000)
        cs=[dict(t=900000,o=8.,h=9.,l=7.,c=8.)]
        touch=e._kairos_retest_zone(cs,z,900000,tf='15')
        self.assertEqual(touch['poi_removal_basis'],'LUX_CAPACITY')
        self.assertTrue(touch['poi_invalidated_same_bar'])
        self.assertFalse(touch['fill_assumed'])

class LuxInternalSourceTests(unittest.TestCase):
    def test_internal_does_not_repeat_external_level_and_does_not_flip_bias(self):
        import json
        from pathlib import Path
        cs=json.loads(Path('inj_m1_lux_internal_fixture.json').read_text())
        major=e.compute_lux_structure_events(cs,50)
        self.assertTrue(any(x['t']==1791702120000 and x['nivel']==7.697 for x in major))
        inner=e.compute_lux_internal_structure(cs,5)
        self.assertFalse(any(x['t']==1791702120000 and x['nivel']==7.697 for x in inner))
    def test_internal_waits_for_external_pivot_history(self):
        cs=[dict(t=i,o=10.,h=12.,l=8.,c=10.) for i in range(15)]
        cs[0]['l']=1.;cs[7]['c']=.5;cs[7]['l']=.4
        self.assertTrue(e.compute_lux_structure_events(cs,5))
        self.assertEqual(e.compute_lux_internal_structure(cs,5),[])

class AnnualCalendarTests(unittest.TestCase):
    def months(self,year):
        return [dict(t=ts(year,m),o=float(m),h=float(m+20),l=float(m-1),c=float(m+1),v=float(m)) for m in range(1,13)]
    def aggregate(self,cs,end):
        from kairos_calendar_map import closed_calendar_years
        return closed_calendar_years(cs,end)
    def test_complete_year_exact_ohlcv_and_january_boundary(self):
        cs=self.months(2024)
        self.assertEqual(self.aggregate(cs,ts(2025)-1),[])
        out=self.aggregate(cs,ts(2025));self.assertEqual(len(out),1)
        self.assertEqual({k:out[0][k] for k in ('t','o','h','l','c','v')},dict(t=ts(2024),o=1.,h=32.,l=0.,c=13.,v=78.))
    def test_missing_duplicate_or_noncalendar_month_is_fail_closed(self):
        cs=self.months(2024)
        for broken in (cs[:-1],cs+[cs[0]],[dict(c,t=c['t']+1) if i==0 else c for i,c in enumerate(cs)]):
            self.assertEqual(self.aggregate(broken,ts(2025)),[])
    def test_current_year_never_used_as_closed(self):
        cs=self.months(2025)+self.months(2026)
        self.assertEqual([c['t'] for c in self.aggregate(cs,ts(2026,10))],[ts(2025)])
    def test_missing_middle_year_cannot_create_three_year_fvg(self):
        cs=self.months(2023)+self.months(2024)[:-1]+self.months(2025)
        self.assertEqual([c['t'] for c in self.aggregate(cs,ts(2026))],[ts(2025)])
    def test_readonly_export_uses_all_native_tfs_and_annual_same_detectors(self):
        import kairos_ob_2r_replay as api
        cs=self.months(2025)
        with patch.object(api,'fetch_inputs',return_value=({'MN':cs,'M15':[]},{})):
            x=api.native_export(e,'BTCUSD',ts(2026),include_fvg=True)
        self.assertIn('MN',x['fvg_states']);self.assertIn('Y1',x['fvg_states'])
        self.assertIn('M15',x['ob_states']);self.assertIn('Y1',x['ob_states'])
        self.assertEqual(x['calendar_year_map']['candles'][0]['t'],ts(2025))
        self.assertFalse(x['evaluation_performed']);self.assertFalse(x['fill_assumed'])
        self.assertEqual(x['calendar_year_map']['structure_major'],[])

if __name__=='__main__':unittest.main()
