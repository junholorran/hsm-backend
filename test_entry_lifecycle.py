import unittest
from test_radar_regression import load_functions

class EntryLifecycleTests(unittest.TestCase):
    def apply(self, state, result, cutoff, start, events):
        f=load_functions('scalp_engine.py',['_kairos_apply_entry_phase'],{})['_kairos_apply_entry_phase']
        return f(state,result,cutoff,start,events)
    def setup(self, retest=None):
        return {'key':'setup-A','ready_ts':600000,'tf':'M5','level':100.,'bottom':99.,'top':100.,'direction':'LONG','retest':retest}
    def result(self, retest=None):
        return {'valid':False,'failure_reason':'AGUARDANDO_RETESTE_ZONA','entry_setup':self.setup(retest)}
    def test_armed_before_touch_and_only_once(self):
        state={};events=[];r=self.result()
        self.apply(state,r,600000,0,events);self.apply(state,self.result(),900000,0,events)
        self.assertEqual([e['phase'] for e in events],['ARMED'])
        self.assertEqual(events[0]['timestamp'],600000)
        self.assertEqual(events[0]['level'],100.)
    def test_future_setup_is_not_armed(self):
        events=[];self.apply({},self.result(),500000,0,events);self.assertEqual(events,[])
    def test_past_entry_never_becomes_new_armed_setup(self):
        events=[];r=self.result({'t':900000,'c':98.})
        self.apply({},r,2000000,1500000,events)
        self.assertEqual(events,[]);self.assertEqual(r['failure_reason'],'ENTRY_BEFORE_REPLAY_WINDOW')
    def test_touch_keeps_limit_separate_from_observed_close(self):
        state={};events=[];self.apply(state,self.result(),600000,0,events)
        self.apply(state,self.result({'t':900000,'c':98.}),1200000,0,events)
        self.assertEqual(events[-1]['phase'],'RETEST_OBSERVED')
        self.assertEqual(events[-1]['level'],100.);self.assertEqual(events[-1]['observed_close'],98.)
    def test_touch_is_not_reissued_on_next_candle(self):
        state={};events=[];r=self.result({'t':900000,'c':98.})
        self.apply(state,r,1200000,0,events)
        r=self.result({'t':900000,'c':98.});r['valid']=True
        self.apply(state,r,1500000,0,events)
        self.assertEqual(len(events),1);self.assertFalse(r['valid'])
        self.assertEqual(r['failure_reason'],'ENTRY_ALREADY_OBSERVED')

class SetupDeliveryTests(unittest.TestCase):
    def sender(self, success):
        self.sent=[];self.seen={'BTCUSD':set()}
        ns={'_KAIROS_SETUP_SEEN':self.seen,'_KAIROS_LIVE_LAST_TS':{'BTCUSD':0},
            'send_telegram':lambda msg:(self.sent.append(msg) or success)}
        return load_functions('app.py',['_kairos_send_setup_events'],ns)['_kairos_send_setup_events']
    def event(self):
        return {'key':'A:ARMED','setup_key':'A','phase':'ARMED','timestamp':100,'tf':'M5',
                'direction':'LONG','level':100.,'bottom':99.,'top':100.,'zone_type':'OB_bullish',
                'capture_tf':'H1','capture_level':105.}
    def test_no_pending_alert_after_retest(self):
        f=self.sender(True);ev=self.event();touch={**ev,'phase':'RETEST_OBSERVED'}
        self.assertTrue(f('BTCUSD',{'entry_phase_events':[ev,touch]},200));self.assertEqual(self.sent,[])
    def test_failed_delivery_is_retryable(self):
        f=self.sender(False);ev=self.event()
        self.assertFalse(f('BTCUSD',{'entry_phase_events':[ev],'pending_setup_keys':['A']},200));self.assertEqual(self.seen['BTCUSD'],set())
    def test_successful_delivery_is_deduplicated(self):
        f=self.sender(True);ev=self.event();r={'entry_phase_events':[ev],'pending_setup_keys':['A']}
        f('BTCUSD',r,200);f('BTCUSD',r,200);self.assertEqual(len(self.sent),1)
    def test_no_armed_alert_when_final_state_reports_late_touch(self):
        f=self.sender(True);ev=self.event()
        r={'entry_phase_events':[ev],'pending_setup_keys':['A'],'entry_setup_states':{'A':{'touched':True}}}
        self.assertTrue(f('BTCUSD',r,200));self.assertEqual(self.sent,[])
    def test_obsolete_setup_is_not_delivered(self):
        f=self.sender(True);ev=self.event()
        self.assertTrue(f('BTCUSD',{'entry_phase_events':[ev],'pending_setup_keys':[]},200));self.assertEqual(self.sent,[])

class RetestBoundaryTests(unittest.TestCase):
    def retest(self):
        return load_functions('scalp_engine.py',['_kairos_retest_zone'],{})['_kairos_retest_zone']
    def test_first_bar_opening_at_confirmation_close_is_eligible(self):
        c={'t':600000,'h':101.,'l':99.};z={'top':100.,'bottom':99.,'tipo':'OB_bullish','created_ts':0}
        self.assertEqual(self.retest()([c],z,600000,tf='5'),c)
    def test_fvg_formation_bar_cannot_be_its_own_retest(self):
        c={'t':600000,'h':101.,'l':99.};z={'top':100.,'bottom':99.,'tipo':'FVG_bullish','created_ts':600000}
        self.assertIsNone(self.retest()([c],z,600000,tf='5'))
