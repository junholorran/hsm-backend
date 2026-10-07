import sqlite3
import tempfile
import unittest
from pathlib import Path
from test_radar_regression import load_functions

class SetupDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.sent=[]
        self.db=str(Path(self.tmp.name)/'alerts.db')
    def tearDown(self): self.tmp.cleanup()
    def ns(self,success=True):
        return load_functions('app.py',['_kairos_send_setup_events','_kairos_deliver_setup_once'],{
            'sqlite3':sqlite3,'DB_FILE':self.db,'_KAIROS_LIVE_STARTED_TS':1000,
            '_KAIROS_SETUP_SEEN':{'ADAUSD':set(),'BTCUSD':set()},
            '_KAIROS_LIVE_LAST_TS':{'ADAUSD':1000,'BTCUSD':1000},
            '_KAIROS_LIVE_PHASE_STATE':{'ADAUSD':{},'BTCUSD':{}},
            'send_telegram':lambda msg:(self.sent.append(msg) or success)})
    def event(self,key='A',ready=1100):
        return {'key':key+':ARMED','setup_key':key,'phase':'ARMED','tf':'M5','timestamp':1200,
            'ready_ts':ready,'direction':'SHORT','level':.2565,'bottom':.2565,'top':.2575,
            'zone_type':'FVG_bearish','capture_tf':'D1','capture_level':.2654}
    def send(self,ns,event,pair='ADAUSD'):
        return ns['_kairos_send_setup_events'](pair,{'entry_phase_events':[event],
            'pending_setup_keys':[event['setup_key']]},1300)
    def test_restart_does_not_resend_delivered_setup(self):
        self.assertTrue(self.send(self.ns(),self.event()))
        self.assertTrue(self.send(self.ns(),self.event()))
        self.assertEqual(len(self.sent),1)
    def test_failure_keeps_delivery_retryable_after_restart(self):
        self.assertFalse(self.send(self.ns(False),self.event()))
        ns=self.ns();ns['_KAIROS_LIVE_STARTED_TS']=1250
        self.assertTrue(self.send(ns,self.event()))
        self.assertEqual(len(self.sent),2)
    def test_distinct_setups_and_pairs_are_not_suppressed(self):
        self.send(self.ns(),self.event())
        self.send(self.ns(),self.event('B'))
        self.send(self.ns(),self.event(),pair='BTCUSD')
        self.assertEqual(len(self.sent),3)
    def test_boot_does_not_reannounce_historical_pending_setup(self):
        self.assertTrue(self.send(self.ns(),self.event(ready=900)))
        self.assertEqual(self.sent,[])
    def test_storage_failure_does_not_send_untracked_alert(self):
        ns=self.ns();ns['DB_FILE']=str(Path(self.tmp.name)/'missing'/'alerts.db')
        self.assertFalse(self.send(ns,self.event()))
        self.assertEqual(self.sent,[])
