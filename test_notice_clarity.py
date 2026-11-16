import unittest
from kairos_notice_context import format_pair_update
from kairos_notice_brief import format_event_group

CUTOFF = 1794819600000

class NoticeClarity(unittest.TestCase):
    def test_batches_two_history_losses_with_capture_ids_levels_and_times(self):
        def event(cid, level):
            return dict(pair='BTCUSD', stage='HISTORICO_INSUFICIENTE', observed_ts=CUTOFF,
                        capture_id=cid, capture=dict(liquidity_tf='H1', liquidity_type='SWING_LOW',
                        nivel=level, native_capture_confirm_ts=CUTOFF-1800000),
                        history_coverage=dict(required_start_ts=CUTOFF-2700000,
                                              available_start_ts=CUTOFF-1800000))
        pages=format_pair_update('BTCUSD',[{'events':[event('btc-first', 61000)]},
                                          {'events':[event('btc-second', 62000)]}],{},CUTOFF)
        self.assertEqual(len(pages),1)
        for fragment in ('btc-first','btc-second','61000,00','62000,00','08:15','08:30'):
            self.assertIn(fragment,pages[0])

    def test_primary_break_shows_actual_close_and_third_candle_deadline(self):
        e=dict(pair='BTCUSD', stage='M15_QUEBRA', direction='LONG', kind='CHoCH', level=64800,
               break_ts=CUTOFF, observed_ts=CUTOFF, primary_policy='test', break_close=64910.7)
        notice=' '.join(format_event_group([e], CUTOFF))
        self.assertIn('09:15',notice)
        self.assertIn('64910,70',notice)
