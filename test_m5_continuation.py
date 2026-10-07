import json
import unittest
from pathlib import Path
import scalp_engine as e


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(Path('continuation_btc_fixture.json').read_text())
        cls.auth = 1791362700000  # 09:45 Lisbon

    def select(self, cutoff, m15=None):
        return getattr(e, '_kairos_m5_continuation_zone', lambda *a: None)(
            self.data['M5'], self.data['M15'] if m15 is None else m15,
            self.auth, 'baixa', cutoff)

    def test_btc_new_ob_is_available_only_after_break_close(self):
        before = self.select(1791373199999)
        after = self.select(1791373200000)
        self.assertFalse(before and before.get('break_ts') == 1791372900000)
        self.assertIsNotNone(after)
        self.assertEqual((after['bottom'], after['top']), (83684.9, 83808.2))
        self.assertEqual(after['ready_ts'], 1791373200000)
        self.assertIsNone(after['retest'])

    def test_btc_first_touch_is_observed_at_1300_not_at_open(self):
        before = self.select(1791374399999)
        after = self.select(1791374400000)
        self.assertIsNone(before['retest'])
        self.assertEqual(after['retest']['t'], 1791374100000)
        self.assertEqual(after['refinement_basis'], 'M15_AUTHORIZED_M5_CONTINUATION_BOS_OB')

    def test_missing_m15_authorization_does_not_allow_continuation(self):
        self.assertIsNone(self.select(1791374400000, []))

    def test_opposing_m15_break_before_touch_blocks_new_zone(self):
        modified=[dict(c) for c in self.data['M15']]
        candle=next(c for c in modified if c['t']==1791372600000)  # 12:30
        candle.update(h=90001., c=90000.)
        selected=self.select(1791374400000, modified)
        self.assertIsNone(selected)

    def test_missing_m5_bar_cannot_prove_first_retest(self):
        data=[c for c in self.data['M5'] if c['t'] != 1791373500000]
        self.assertIsNone(e._kairos_m5_continuation_zone(
            data,self.data['M15'],self.auth,'baixa',1791374400000))

    def test_later_m15_invalidation_keeps_prior_observed_retest(self):
        modified=[dict(c) for c in self.data['M15']]
        candle=next(c for c in modified if c['t']==1791374400000)  # 13:00
        candle.update(h=90001., c=90000.)
        selected=self.select(1791375300000, modified)
        self.assertIsNotNone(selected)
        self.assertEqual(selected['retest']['t'], 1791374100000)

    def test_long_continuation_uses_opposite_boundary_and_proximal(self):
        def mirror(cs):
            return [{**c,'o':100000-c['o'],'c':100000-c['c'],
                     'h':100000-c['l'],'l':100000-c['h']} for c in cs]
        selected=e._kairos_m5_continuation_zone(mirror(self.data['M5']),
            mirror(self.data['M15']),self.auth,'alta',1791374400000)
        self.assertIsNotNone(selected)
        self.assertAlmostEqual(selected['bottom'], 16191.8)
        self.assertAlmostEqual(selected['top'], 16315.1)
        self.assertEqual(selected['retest']['t'], 1791374100000)

    def test_future_m5_candles_do_not_change_prefix(self):
        cutoff = 1791373200000
        a = self.select(cutoff)
        b = e._kairos_m5_continuation_zone(
            [c for c in self.data['M5'] if c['t']+300000 <= cutoff],
            [c for c in self.data['M15'] if c['t']+900000 <= cutoff],
            self.auth, 'baixa', cutoff)
        self.assertEqual(a, b)

if __name__ == '__main__':
    unittest.main()
