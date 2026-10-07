import unittest
import scalp_engine as engine


class LuxZoneParityTests(unittest.TestCase):
    def test_ob_uses_broken_pivot_interval_extreme_not_last_opposite(self):
        cs = [dict(t=i, o=10, c=9, h=h, l=7) for i, h in enumerate([12, 18, 13, 14, 11])]
        cs[3].update(o=9, c=10)
        zone = engine._kairos_ob_from_break(cs, 4, 'baixa', structure={'broken_swing_origin_ts': 1})
        self.assertEqual((zone['idx'], zone['bottom'], zone['top']), (1, 7, 18))

    def test_ob_excludes_break_candle_and_future_volatility(self):
        cs = [dict(t=i, o=10, c=9, h=12, l=8) for i in range(205)]
        cs[201].update(h=40, l=7)
        cs[202].update(h=15, l=8)
        cs[204].update(h=1000, l=0)
        st = {'broken_swing_origin_ts': 200}
        zone = engine._kairos_ob_from_break(cs, 204, 'baixa', structure=st)
        self.assertEqual(zone['idx'], 202)
        self.assertEqual(zone, engine._kairos_ob_from_break(cs[:204], 203, 'baixa', structure=st) | {'break_idx': 204, 'break_ts': 204})

    def test_fvg_filter_retains_full_history_and_first_delta(self):
        cs = [dict(t=i, o=100, c=100, h=101, l=99) for i in range(300)]
        cs[0].update(c=110, h=111)
        cs[297].update(o=100, c=100.1, h=100.2, l=99.9)
        cs[298].update(o=100, c=102, h=102.1, l=99.9)
        cs[299].update(o=103, c=103, h=104, l=102.5)
        full = engine._kairos_fvg_states(cs, lookback=300)
        tail = engine._kairos_fvg_states(cs, lookback=3)
        self.assertEqual([z for z in full if z['created_ts'] >= 297], tail)
        zone = next(z for z in tail if z['created_ts'] == 299)
        self.assertAlmostEqual(zone['lux_threshold'], (0.001 + 0.00001 + 0.0002) / 299 * 2)

    def test_m5_ob_refinement_uses_parent_broken_pivot_extreme(self):
        cs = [dict(t=i*300000, o=10, c=9, h=h, l=7) for i, h in enumerate([12,18,13,14,11])]
        cs[3].update(o=9,c=10)
        cs[4]['c']=6
        parent={'top':18,'bottom':7,'tipo':'OB_bearish'}
        zone=engine._kairos_m5_refine_zone(cs,parent,0,1500000,'baixa',7,structure_origin_ts=300000)
        self.assertEqual((zone['origin_ts'],zone['top']), (300000,18))

    def test_m5_cannot_substitute_extreme_that_predates_causal_leg(self):
        cs=[dict(t=i*300000,o=10,c=9,h=h,l=7) for i,h in enumerate([12,18,13,14,11])]
        cs[4]['c']=6
        zone=engine._kairos_m5_refine_zone(cs,{'top':18,'bottom':7},600000,1500000,'baixa',7,structure_origin_ts=0)
        self.assertIsNone(zone)

    def test_shadow_does_not_authorize_inverted_parsed_ob(self):
        cs=[dict(t=i,o=10,c=10,h=12,l=8) for i in range(202)]
        cs[200].update(h=100,l=0)
        st={'broken_swing_origin_ts':200,'t':201}
        zone=engine._kairos_ob_from_break(cs,201,'baixa',structure=st)
        self.assertGreater(zone['bottom'],zone['top'])
        self.assertFalse(engine._kairos_shadow_validate_poi(zone,cs,structure=st)['pass'])


if __name__ == '__main__':
    unittest.main()
