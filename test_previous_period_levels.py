import unittest
from datetime import datetime,timezone
from test_radar_regression import load_functions

def ts(y,m,d=1):return int(datetime(y,m,d,tzinfo=timezone.utc).timestamp()*1000)
def candle(t,h=120.,l=80.):return dict(t=t,o=100.,h=h,l=l,c=100.,v=1.)

class PreviousPeriodTests(unittest.TestCase):
    def ns(self):
        return load_functions('scalp_engine.py',['_kairos_candle_close_ts','_kairos_candles_fechados_ate','_kairos_periodo_completo','_kairos_previous_period_refs','_kairos_previous_period_liquidity','_kairos_structural_registry'],{
            'datetime':datetime,'timezone':timezone,'INTERVALO_MS_POR_LABEL':{'D':86400000,'W':604800000,'15':900000},
            'KAIROS_STRUCTURAL_LIQUIDITY_TFS':(), '_kairos_lux50_structural_levels':lambda *a:[]})
    def test_six_previous_closed_extremes_exclude_current_candles(self):
        f=self.ns()['_kairos_previous_period_refs']
        refs=f({'D1':[candle(ts(2026,10,6),110,90),candle(ts(2026,10,7),999,1)],
            'W1':[candle(ts(2026,9,28),130,70),candle(ts(2026,10,5),999,1)],
            'MN':[candle(ts(2026,9),150,50),candle(ts(2026,10),999,1)]},ts(2026,10,7)+3600000)
        self.assertEqual({k:(refs.get(k) or {}).get('level') for k in ['PDH','PDL','PWH','PWL','PMH','PML']},
            dict(PDH=110,PDL=90,PWH=130,PWL=70,PMH=150,PML=50))
        self.assertEqual(refs['PMH']['confirmed_ts'],ts(2026,10))
    def test_month_closes_at_calendar_boundary_including_leap_year_and_december(self):
        ns=self.ns()
        for start,end in [(ts(2024,2),ts(2024,3)),(ts(2026,2),ts(2026,3)),(ts(2026,12),ts(2027,1))]:
            with self.subTest(start=start):
                self.assertFalse(ns['_kairos_periodo_completo'](candle(start),'MN',end-1))
                self.assertTrue(ns['_kairos_periodo_completo'](candle(start),'MN',end))
                r=ns['_kairos_previous_period_refs']({'MN':[candle(start)]},end)
                self.assertEqual((r.get('PMH') or {}).get('confirmed_ts'),end)
    def test_monthly_missing_history_is_absent(self):
        r=self.ns()['_kairos_previous_period_refs']({'MN':[candle(ts(2026,10))]},ts(2026,10,7))
        self.assertIn('PMH',r);self.assertIsNone(r['PMH']);self.assertIsNone(r['PML'])
    def test_registry_includes_monthly_and_first_m15_at_boundary(self):
        ns=self.ns();start=ts(2026,10)
        r=ns['_kairos_structural_registry']({'MN':[candle(ts(2026,9))],
            'M15':[candle(start,121,90)]},start+900000)
        high=next((x for x in r if x['type']=='PMH'),{})
        self.assertEqual(high.get('state'),'CAPTURED');self.assertEqual(high.get('captured_ts'),start)
    def test_unclosed_m15_does_not_capture_period_level(self):
        ns=self.ns();start=ts(2026,10)
        r=ns['_kairos_structural_registry']({'MN':[candle(ts(2026,9))],
            'M15':[candle(start,121,90)]},start+899999)
        high=next((x for x in r if x['type']=='PMH'),{})
        self.assertEqual(high.get('state'),'ACTIVE')

    def test_monthly_capture_before_m15_history_remains_consumed(self):
        ns=self.ns()
        r=ns['_kairos_structural_registry']({'MN':[candle(ts(2026,9))],
            'D1':[candle(ts(2026,10,2),125,90)],
            'M15':[candle(ts(2026,10,19),110,90)]},ts(2026,10,20))
        high=next((x for x in r if x['type']=='PMH'),{})
        self.assertEqual(high.get('state'),'CAPTURED')
        self.assertEqual(high.get('captured_ts'),ts(2026,10,2))
        self.assertEqual(high.get('captured_tf'),'D1')
    def test_telemetry_reports_same_consumed_monthly_state_as_registry(self):
        ns=self.ns();ns['_kairos_structural_poi_overlaps']=lambda *a:[]
        load_functions('scalp_engine.py',['_kairos_build_structural_liquidity_telemetry'],ns)
        start=ts(2026,10)
        r=ns['_kairos_build_structural_liquidity_telemetry']({'MN':[candle(ts(2026,9))],
            'M15':[candle(start,121,90)]},start+900000)
        high=next((x for x in r['structural_liquidity'] if x['type']=='PMH'),{})
        self.assertEqual(high.get('state'),'CAPTURED')
        self.assertEqual(r['previous_periods']['PMH']['level'],120.)
    def test_overlapping_closed_day_proves_monthly_capture_in_missing_m15_hours(self):
        ns=self.ns();day=ts(2026,10,10)
        r=ns['_kairos_structural_registry']({'MN':[candle(ts(2026,9))],
            'D1':[candle(day,125,90)],
            'M15':[candle(day+12*3600000,110,90)]},ts(2026,10,11))
        high=next((x for x in r if x['type']=='PMH'),{})
        self.assertEqual(high.get('state'),'CAPTURED')
        self.assertEqual(high.get('captured_tf'),'D1')
        self.assertEqual(high.get('capture_confirm_ts'),ts(2026,10,11))
