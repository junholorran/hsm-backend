import unittest
import scalp_engine as e

class LuxLifecycleTests(unittest.TestCase):
    def candles(self):
        cs=[dict(t=i*3600000,o=100.,h=101.,l=99.,c=100.) for i in range(220)]
        cs.extend([dict(t=220*3600000,o=100.,h=101.,l=99.,c=100.),
                   dict(t=221*3600000,o=100.,h=106.,l=100.,c=105.),
                   dict(t=222*3600000,o=105.,h=107.,l=104.,c=106.)])
        return cs
    def mother(self,cs):
        return next(z for z in e._kairos_fvg_states(cs) if z['id']=='FVG_795600000_B')
    def test_bull_smc_wick_removes_without_close_invalidation(self):
        cs=self.candles();cs.append(dict(t=223*3600000,o=103.,h=104.,l=100.,c=102.))
        z=self.mother(cs)
        self.assertEqual(z['state'],'INVALIDADA')
        self.assertEqual(z['invalidated_ts'],223*3600000)
        self.assertEqual(z['tipo'],'FVG_bullish')
    def test_bull_smc_exact_bottom_contact_does_not_remove(self):
        cs=self.candles();cs.append(dict(t=223*3600000,o=103.,h=104.,l=101.,c=102.))
        self.assertNotEqual(self.mother(cs)['state'],'INVALIDADA')
    def test_bear_smc_uses_literal_raw_pine_top_not_normalized_upper(self):
        cs=[{**c,'o':200-c['o'],'c':200-c['c'],'h':200-c['l'],'l':200-c['h']} for c in self.candles()]
        cs.append(dict(t=223*3600000,o=95.,h=97.,l=94.,c=95.))
        z=next(z for z in e._kairos_fvg_states(cs) if z['id']=='FVG_795600000_S')
        self.assertEqual(z['state'],'INVALIDADA')
    def test_ifvg_reference_has_distinct_atr_filtered_mother(self):
        cs=self.candles();cs.append(dict(t=223*3600000,o=103.,h=104.,l=100.,c=102.))
        cs.append(dict(t=224*3600000,o=102.,h=103.,l=100.,c=100.5))
        zs=e._kairos_fvg_states(cs)
        inv=next(z for z in zs if z.get('lux_ifvg') and z['source_mid']['t']==221*3600000)
        self.assertEqual((inv['tipo'],inv['state'],inv['flip_ts']),('IFVG_bearish','IFVG',224*3600000))
        self.assertTrue(inv['mother_fvg_id']);self.assertGreater(inv['top']-inv['bottom'],inv['lux_ifvg_width_threshold'])
    def test_ifvg_open_body_can_invalidate_even_close_inside(self):
        cs=self.candles();cs.extend([dict(t=223*3600000,o=102.,h=103.,l=100.,c=100.5),
                                    dict(t=224*3600000,o=104.1,h=104.2,l=102.,c=103.)])
        inv=next(z for z in e._kairos_fvg_states(cs) if z.get('lux_ifvg') and z['source_mid']['t']==221*3600000)
        self.assertEqual(inv['state'],'INVALIDADA')
    def test_ifvg_width_threshold_is_strict(self):
        from kairos_lux_fvg import inversion_fvg_states
        cs=self.candles()
        self.assertEqual(inversion_fvg_states(cs,atr_mult=100),[])
    def test_active_smc_mother_does_not_expire_by_age(self):
        from kairos_lux_fvg import smc_fvg_states
        cs=self.candles()
        cs.extend(dict(t=i*3600000,o=105.2,h=106.,l=105.,c=105.5) for i in range(223,523))
        self.assertTrue(any(z['id']=='FVG_795600000_B' and z['state']!='INVALIDADA' for z in smc_fvg_states(cs)))

    def test_map_keeps_active_zones_beyond_old_display_slice(self):
        from unittest.mock import patch
        cs=self.candles()
        zones=[dict(id=str(i),tipo='FVG_bullish',state='ATIVA',top=110.,bottom=108.,created_ts=i) for i in range(100)]
        with patch.object(e,'_kairos_fvg_states',return_value=zones):
            out=e._kairos_liquidity_map_tf(cs,'H1')
        self.assertEqual(len(out['zones']),100)

    def test_native_export_can_report_live_detector_states_without_trade_evaluation(self):
        from unittest.mock import patch
        import kairos_ob_2r_replay as api
        cs=self.candles()
        cs.append(dict(t=223*3600000,o=103.,h=104.,l=100.,c=102.))
        end=224*3600000
        with patch.object(api,'fetch_inputs',return_value=({'H1':cs},{'H1':[]})):
            out=api.native_export(e,'BTCUSD',end,include_fvg=True)
        z=next(z for z in out['fvg_states']['H1'] if z['id']=='FVG_795600000_B')
        self.assertEqual(z['state'],'INVALIDADA')
        self.assertFalse(out['evaluation_performed'])

    def test_btc_h4_real_wick_regression(self):
        import json
        from pathlib import Path
        cs=json.loads(Path('btc_h4_fvg_wick_fixture.json').read_text())
        zs=e._kairos_fvg_states(cs)
        z=next(z for z in zs if z['bottom']==83118.2 and z['top']==83217.7 and z['tipo']=='FVG_bullish')
        self.assertEqual(z['state'],'INVALIDADA')
        self.assertEqual(z['invalidated_ts'],1791878400000)

if __name__=='__main__':unittest.main()
