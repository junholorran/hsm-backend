import unittest
import scalp_engine as e

class FirstLuxPivotTests(unittest.TestCase):
    def candles(self,size):
        rows=[dict(t=i*60000,o=8.,h=10.,l=5.,c=8.) for i in range(size+2)]
        rows[0].update(l=0.)
        rows[-1].update(o=1.,h=2.,l=-2.,c=-1.)
        return rows
    def test_first_confirmed_low_is_not_skipped(self):
        for size in [5,50]:
            with self.subTest(size=size):
                swings=e._extrair_swings_lux_algo(self.candles(size),size)
                self.assertIn({'tipo':'low','valor':0.,'t':0},swings)
    def test_break_of_first_pivot_is_classified_and_origin_preserved(self):
        for size in [5,50]:
            with self.subTest(size=size):
                events=e.compute_lux_structure_events(self.candles(size),size)
                self.assertEqual(len(events),1)
                event=events[0]
                self.assertEqual((event['tipo'],event['direcao'],event['nivel'],event['broken_swing_origin_ts']),('BOS','baixa',0.,0))
                self.assertEqual(event['t'],(size+1)*60000)
    def test_no_pivot_before_confirmation(self):
        for size in [5,50]:
            self.assertEqual(e._extrair_swings_lux_algo(self.candles(size)[:size],size),[])
    def test_first_pivot_is_available_in_registry_at_its_actual_close(self):
        rows=self.candles(50)[:51]
        cutoff=rows[-1]['t']+3600000
        levels=e._kairos_lux50_structural_levels(rows,'H1',cutoff)
        self.assertEqual(len(levels),1)
        self.assertEqual((levels[0]['origin_ts'],levels[0]['confirmed_ts'],levels[0]['level']),(0,cutoff,0.))
    def test_short_history_exposes_two_confirmed_legs_as_dealing_range(self):
        rows=self.candles(50);rows[1]['h']=20.;rows[-1].update(l=5.,c=8.,o=8.,h=10.)
        r=e.avaliar_vortex_decision_layer_v2(rows,rows,[],candles_por_tf={'H1':rows,'M15':rows,'M5':rows},cutoff_ts=100000000,audit_entries_only=True)
        self.assertIsNotNone(r['dealing_ranges'].get('H1'))
        self.assertEqual(r['dealing_ranges']['H1']['high'],20.)
        self.assertEqual(r['dealing_ranges']['H1']['low'],0.)
