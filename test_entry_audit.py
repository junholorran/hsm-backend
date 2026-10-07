import ast
import unittest
from pathlib import Path
from test_radar_regression import load_functions

class EntryAuditTests(unittest.TestCase):
    def helper(self):
        return load_functions('scalp_engine.py',['_kairos_record_entry_audit'],{})['_kairos_record_entry_audit']
    def result(self, ts=1000):
        return {'entry_audit_only':True,'timestamp':ts,'entry_tf':'M5','entry':100.,'direction':'LONG','choch_timestamp':500,'zone_type':'OB_BULL','zone_bottom':99.,'zone_top':100.,'m15_major_confirmation_close_ts':400,'m15_internal_confirmation_close_ts':600,'m5_refinement_created_ts':700,'entry_audit_evidence':{'retest':{'t':ts,'l':99.5,'h':101.},'authorization_ts':700}}
    def test_stale_entry_is_exposed_separately(self):
        store={};self.helper()(store,self.result(),1500,1200)
        self.assertEqual(next(iter(store.values()))['window_status'],'ENTRY_BEFORE_WINDOW')
    def test_first_seen_does_not_move_when_repeated(self):
        store={};f=self.helper();f(store,self.result(),1100,0);f(store,self.result(),1200,0)
        self.assertEqual(len(store),1)
        self.assertEqual(next(iter(store.values()))['first_seen_cutoff_ts'],1100)
    def test_future_entry_is_not_accepted(self):
        store={};self.helper()(store,self.result(2000),1500,0)
        self.assertFalse(next(iter(store.values()))['timing_geometry_pass'])
    def test_retest_before_authorization_is_flagged(self):
        store={};r=self.result(500);self.helper()(store,r,1500,0)
        self.assertFalse(next(iter(store.values()))['timing_geometry_pass'])
    def test_no_audit_candidate_without_explicit_mode(self):
        store={};r=self.result();r['entry_audit_only']=False;self.helper()(store,r,1500,0)
        self.assertEqual(store,{})
