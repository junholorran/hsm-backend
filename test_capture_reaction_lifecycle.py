import copy
import unittest
from unittest.mock import patch
import scalp_engine as e

STEP = 900000

class CaptureReactionLifecycleTests(unittest.TestCase):
    def snapshot(self, closes, cutoff=None, side='HIGH', gap=None):
        level = dict(tf='D1', type='PDH' if side=='HIGH' else 'PDL', level=100.,
                     origin_ts=-86400000, confirmed_ts=0, captured_ts=None)
        rows = [dict(t=i*STEP,o=c,h=max(c,101.),l=min(c,99.),c=c) for i,c in enumerate(closes)]
        if gap is not None:
            rows.pop(gap)
        pre = 99. if side=='HIGH' else 101.
        rows = [dict(t=i*STEP,o=pre,h=pre,l=pre,c=pre) for i in (-3,-2,-1)] + rows
        with patch.object(e, '_kairos_structural_registry', return_value=[level]):
            selected, audit = e._kairos_select_structural_first_capture_sweep(
                {'M15':rows},len(closes)*STEP if cutoff is None else cutoff)
        return selected, audit, rows

    def test_mixed_capture_resolves_only_when_second_consecutive_close_is_known(self):
        s,a,_=self.snapshot([99.,101.,102.],cutoff=3*STEP-1)
        self.assertIsNone(s)
        self.assertEqual(a['candidates'][0]['post_capture_state'],'UNRESOLVED_REACTION')
        s,_,_=self.snapshot([99.,101.,102.])
        self.assertEqual(s['post_capture_state'],'ACCEPTANCE_CONTINUATION')
        self.assertEqual((s['first_capture_ts'],s['capture_confirm_ts'],s['confirm_ts']),(0,STEP,2*STEP))

    def test_confirmed_acceptance_changes_to_rejection_without_moving_capture(self):
        s,_,_=self.snapshot([101.,102.,99.,98.,97.])
        self.assertEqual(s['post_capture_state'],'REJECTION_RECLAIM')
        self.assertEqual((s['first_capture_ts'],s['confirm_ts']),(0,3*STEP))
        self.assertEqual(s['reaction_confirm_ts'],4*STEP)

    def test_low_capture_is_symmetric_and_exact_close_does_not_confirm(self):
        s,_,_=self.snapshot([101.,99.,100.,98.,97.],side='LOW')
        self.assertEqual(s['post_capture_state'],'ACCEPTANCE_CONTINUATION')
        self.assertEqual(s['confirm_ts'],4*STEP)

    def test_gap_is_not_a_pair_but_later_adjacent_pair_can_confirm(self):
        s,a,_=self.snapshot([99.,101.,102.],gap=1)
        self.assertIsNone(s)
        self.assertEqual(a['candidates'][0]['post_capture_state'],'UNRESOLVED_REACTION')
        s,_,_=self.snapshot([101.,102.,103.,99.,98.],gap=2)
        self.assertEqual(s['post_capture_state'],'REJECTION_RECLAIM')
        self.assertEqual(s['confirm_ts'],4*STEP)

    def test_repeated_state_does_not_move_notification_but_new_transition_has_new_key(self):
        states=[]
        for closes in ([101.,102.],[101.,102.,103.],[101.,102.,99.,98.],
                       [101.,102.,99.,98.,101.,102.]):
            s,a,rows=self.snapshot(closes)
            event=e._kairos_capture_radar_events({'structural_sweep_audit':a},{'M15':rows},len(closes)*STEP)[0]
            states.append(event)
        self.assertEqual(states[0]['timestamp'],states[1]['timestamp'])
        self.assertEqual(states[0]['key'],states[1]['key'])
        self.assertNotEqual(states[0]['key'],states[3]['key'])
        self.assertEqual(states[2]['timestamp'],4*STEP)

    def test_new_reaction_cannot_use_a_structure_break_from_before_its_confirmation(self):
        capture=dict(liquidity_side='HIGH',post_capture_state='REJECTION_RECLAIM',
                     sweep_ts=0,confirm_ts=3*STEP,reaction_confirm_ts=4*STEP)
        major=[dict(t=0,tipo='CHoCH',direcao='baixa')]
        internal=[dict(t=STEP,tipo='CHoCH',direcao='baixa')]
        rows=[dict(t=i*STEP,c=99.,h=101.,l=98.) for i in range(5)]
        with patch.object(e,'compute_lux_structure_events',return_value=major), \
             patch.object(e,'compute_lux_internal_structure',return_value=internal):
            self.assertIsNone(e._kairos_direction_after_first_capture(rows,copy.deepcopy(capture)))
            internal.append(dict(t=3*STEP,tipo='BOS',direcao='baixa'))
            intent=e._kairos_direction_after_first_capture(rows,copy.deepcopy(capture))
            self.assertEqual(intent['structure']['t'],3*STEP)


class SolNativeReactionTests(unittest.TestCase):
    def test_native_sol_updates_prospectively_and_preserves_first_capture(self):
        import json
        from pathlib import Path
        from datetime import datetime
        from zoneinfo import ZoneInfo
        fixture=json.loads(Path('tests/fixtures/sol_reaction_20261021.json').read_text())
        def ts(clock):
            return int(datetime.strptime('2026-10-21 '+clock,'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/Lisbon')).timestamp()*1000)
        level=dict(tf='D1',type='PDH',level=108.22,origin_ts=ts('00:00')-86400000,
                   confirmed_ts=ts('00:00'),captured_ts=None)
        snapshots=[]
        with patch.object(e,'_kairos_structural_registry',return_value=[level]):
            for clock in ('16:30','16:45','20:00','20:15'):
                selected,audit=e._kairos_select_structural_first_capture_sweep({'M15':fixture['candles']},ts(clock))
                snapshots.append(audit['candidates'][0])
        self.assertEqual([s['post_capture_state'] for s in snapshots],
                         ['UNRESOLVED_REACTION','ACCEPTANCE_CONTINUATION',
                          'ACCEPTANCE_CONTINUATION','REJECTION_RECLAIM'])
        self.assertTrue(all(s['first_capture_ts']==ts('16:00') for s in snapshots))
        self.assertEqual(snapshots[-1]['reaction_confirm_ts'],ts('20:15'))

if __name__=='__main__':unittest.main()
