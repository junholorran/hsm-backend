import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
import scalp_engine as e
from test_radar_regression import load_functions

def ts(y,m,d=1):
    return int(datetime(y,m,d,tzinfo=timezone.utc).timestamp()*1000)

def bar(t,h,l,c):
    return dict(t=t,o=c,h=h,l=l,c=c,v=1.)

class MonthlyCaptureRadarTests(unittest.TestCase):
    def fixtures(self, high=True):
        start=ts(2026,10)
        candles=[bar(start,121,90,119),bar(start+900000,119,90,118),bar(start+1800000,119,90,118)] if high else [bar(start,110,79,81),bar(start+900000,110,81,82),bar(start+1800000,110,81,82)]
        return start,{'MN':[bar(ts(2026,9),120,80,100)],'M15':candles}

    def test_monthly_capture_waits_for_close_and_reaction_waits_for_next_close(self):
        start,data=self.fixtures()
        self.assertEqual(e._kairos_capture_radar_events({},data,start+899999),[])
        events=e._kairos_capture_radar_events({},data,start+900000)
        self.assertEqual(len(events),1)
        self.assertEqual((events[0]['liquidity_type'],events[0]['level'],events[0]['timestamp'],events[0]['reaction']),('PMH',120,start+900000,'UNRESOLVED_REACTION'))
        event=e._kairos_capture_radar_events({},data,start+1800000)[0]
        self.assertEqual((event['timestamp'],event['reaction']),(start+1800000,'REJECTION_RECLAIM'))
        self.assertTrue(event['context_only'])
        self.assertEqual(event['capture_evidence_tf'],'M15')
        self.assertIn('MN',event['context'])
        self.assertNotIn('entry',event)
        self.assertNotIn('direction',event)

    def test_low_capture_and_acceptance_do_not_choose_a_trade_direction(self):
        start,data=self.fixtures(False)
        event=e._kairos_capture_radar_events({},data,start+1800000)[0]
        self.assertEqual((event['liquidity_type'],event['level'],event['reaction']),('PML',80,'REJECTION_RECLAIM'))
        data['M15'][0]['c']=78;data['M15'][1]['c']=77
        event=e._kairos_capture_radar_events({},data,start+1800000)[0]
        self.assertEqual(event['reaction'],'ACCEPTANCE_CONTINUATION')
        self.assertNotIn('direction',event)
        self.assertNotIn('MN',e.KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS)

    def test_prior_daily_evidence_keeps_its_real_time_and_precision(self):
        start,data=self.fixtures()
        data['D1']=[bar(start+86400000,125,90,100)]
        data['M15']=[bar(start+5*86400000,126,90,100)]
        event=e._kairos_capture_radar_events({},data,start+6*86400000)[0]
        self.assertEqual(event['timestamp'],start+2*86400000)
        self.assertEqual(event['capture_evidence_tf'],'D1')
        self.assertEqual(event['reaction'],'UNRESOLVED_REACTION')

    def test_telegram_all_pairs_has_monthly_context_without_management(self):
        start,data=self.fixtures();event=e._kairos_capture_radar_events({},data,start+1800000)[0]
        tree=ast.parse(Path('app.py').read_text())
        pairs=ast.literal_eval(next(n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_KAIROS_LIVE_PAIRS' for t in n.targets)))
        self.assertEqual(len(pairs),13)
        messages=[]
        ns=load_functions('app.py',['_kairos_send_capture_events'],{'send_telegram':lambda msg:messages.append(msg) or True,'datetime':datetime,'timezone':timezone,'_KAIROS_LIVE_LAST_TS':{p:0 for p in pairs},'_KAIROS_RADAR_SEEN':{p:set() for p in pairs}})
        for pair in pairs:
            self.assertTrue(ns['_kairos_send_capture_events'](pair,{'radar_captures':[event]},start+1800000))
        self.assertEqual(len(messages),13)
        for msg in messages:
            self.assertIn('MN:',msg)
            self.assertIn('MN PMH @ 120',msg)
            self.assertIn('UTC',msg)
            self.assertIn('contexto mensal',msg)
            self.assertNotIn('SL',msg);self.assertNotIn('TP1',msg)
        # Historical evidence must not be re-announced at the scanner's current time.
        pair=pairs[0];ns['_KAIROS_LIVE_LAST_TS'][pair]=start+3*86400000
        ns['_KAIROS_RADAR_SEEN'][pair].clear()
        ns['_kairos_send_capture_events'](pair,{'radar_captures':[event]},start+4*86400000)
        self.assertEqual(len(messages),13)

    def test_overlapping_daily_close_does_not_reannounce_observed_capture(self):
        day=ts(2026,10,10);opened=day+13*3600000
        data={'MN':[bar(ts(2026,9),120,80,100)],
              'D1':[bar(day,125,90,110)],
              'M15':[bar(day+12*3600000,110,90,100),bar(opened,121,90,119),bar(opened+900000,119,90,118)]}
        before=e._kairos_capture_radar_events({},data,opened+1800000)[0]
        after=e._kairos_capture_radar_events({},data,day+86400000)[0]
        self.assertEqual((after['key'],after['timestamp'],after['reaction'],after['capture_evidence_tf']),
                         (before['key'],before['timestamp'],before['reaction'],before['capture_evidence_tf']))

if __name__=='__main__':unittest.main()
