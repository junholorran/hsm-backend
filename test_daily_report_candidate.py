import copy
import sqlite3
import unittest

from kairos_daily_report import DAY, daily_hypothesis, build_report, notification_parts, prepare_notification


def candle(t, h=110, l=90, c=100, o=100):
    return dict(t=t, o=o, h=h, l=l, c=c)


def level(tf='H1', typ='SWING_HIGH', value=120, state='ACTIVE'):
    return dict(tf=tf, type=typ, level=value, state=state, origin_ts=0, confirmed_ts=DAY)


class DailyRuleTests(unittest.TestCase):
    def classify(self, h, l, c):
        return daily_hypothesis([candle(0), candle(DAY, h, l, c)], 2*DAY)

    def test_all_scenarios_and_boundary_priority(self):
        cases = [(115,95,112,'BULLISH'), (105,85,88,'BEARISH'),
                 (115,95,105,'BEARISH'), (105,85,95,'BULLISH'),
                 (115,85,100,'NEUTRO'), (115,85,112,'NEUTRO'),
                 (105,95,100,'NEUTRO'), (110,90,100,'NEUTRO'),
                 (115,95,110,'NEUTRO'), (105,85,90,'NEUTRO')]
        for h,l,c,bias in cases:
            with self.subTest(h=h,l=l,c=c):
                self.assertEqual(self.classify(h,l,c)['bias'],bias)

    def test_forming_and_future_days_do_not_change_past_result(self):
        cs=[candle(0),candle(DAY,115,95,112)]
        before=daily_hypothesis(cs,2*DAY)
        cs += [candle(2*DAY,900,1,20),candle(3*DAY,999,1,30)]
        self.assertEqual(daily_hypothesis(cs,2*DAY),before)
        self.assertEqual(daily_hypothesis(cs,2*DAY-1)['status'],'INSUFFICIENT_D1')

    def test_missing_stale_invalid_and_duplicate_days_block(self):
        for cs,cut,status in [([],2*DAY,'INSUFFICIENT_D1'),
            ([candle(0),candle(2*DAY)],3*DAY,'D1_GAP'),
            ([candle(0),candle(DAY)],3*DAY,'STALE_D1'),
            ([candle(0),candle(DAY,c=float('nan'))],2*DAY,'INVALID_D1'),
            ([candle(0),candle(DAY,h=99)],2*DAY,'INVALID_D1'),
            ([candle(0),candle(DAY+1)],2*DAY+1,'D1_GAP'),
            ([candle(DAY),candle(DAY)],2*DAY,'D1_GAP')]:
            with self.subTest(status=status,cs=cs):
                self.assertEqual(daily_hypothesis(cs,cut)['status'],status)


class ReportTests(unittest.TestCase):
    def report(self, registry=None):
        return build_report('BNBUSD',[candle(0),candle(DAY,115,95,112)],
                            registry if registry is not None else [level()],2*DAY,2*DAY)

    def test_pure_consumer_keeps_map_provenance_and_capture_states(self):
        registry=[level(),level('H4','SWING_LOW',80,'CAPTURED'),level('D1','PDH',115),
                  level('W1','PWL',70),level('MN','PMH',150),level('MN','PML',60),
                  level('H1','EQH',125),level('H4','EQL',85)]
        old=copy.deepcopy(registry);r=self.report(registry)
        self.assertEqual(registry,old)
        self.assertEqual(len(r['levels']),len(registry))
        self.assertFalse(r['entry_authorized'])
        self.assertFalse(r['production_decision_changed'])
        self.assertEqual(r['nearest_available']['above']['level'],115)
        self.assertEqual(r['nearest_available']['below']['level'],85)
        self.assertIsNone(r['nearest_available']['predicted_destination'])

    def test_future_or_invalid_map_evidence_fails_closed(self):
        for changes in [dict(confirmed_ts=3*DAY), dict(captured_ts=3*DAY),
                        dict(capture_confirm_ts=3*DAY),dict(level=float('inf')),
                        dict(state='UNKNOWN'),dict(type='FVG')]:
            row=level();row.update(changes)
            with self.subTest(changes=changes):
                r=self.report([row]);self.assertEqual(r['status'],'INVALID_REGISTRY_EVIDENCE')
                self.assertEqual(notification_parts(r),[])

    def test_cannot_rewind_current_map_into_previous_day(self):
        r=build_report('BNBUSD',[candle(0),candle(DAY)],[level()],3*DAY,2*DAY)
        self.assertEqual(r['status'],'REGISTRY_TIME_MISMATCH')
        self.assertEqual(notification_parts(r),[])

    def test_empty_map_and_missing_day_do_not_prepare_notification(self):
        self.assertEqual(self.report([])['status'],'INCOMPLETE')
        r=build_report('BNBUSD',[],[level()],2*DAY,2*DAY)
        db=sqlite3.connect(':memory:')
        self.assertFalse(prepare_notification(db,r))

    def test_pages_include_all_supplied_levels_without_truncation(self):
        r=self.report([level(value=120+i/100) for i in range(200)])
        parts=notification_parts(r,max_chars=1000,detailed=True)
        self.assertGreater(len(parts),1)
        self.assertTrue(all(len(p)<=1000 for p in parts))
        self.assertEqual(sum(p.count('H1 SWING_HIGH @') for p in parts),200)

    def test_persistent_preparation_dedup_is_not_delivery_or_global_suppression(self):
        db=sqlite3.connect(':memory:');r=self.report()
        self.assertTrue(prepare_notification(db,r));db.commit()
        self.assertFalse(prepare_notification(db,r))
        other=copy.deepcopy(r);other['pair']='ETHUSD'
        self.assertTrue(prepare_notification(db,other))
        other=copy.deepcopy(r);other['daily']['available_ts']+=DAY
        self.assertTrue(prepare_notification(db,other))
        self.assertEqual(db.execute('SELECT count(*) FROM daily_brief_candidate').fetchone()[0],3)


if __name__ == '__main__':
    unittest.main()
