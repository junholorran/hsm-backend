import unittest
from pathlib import Path
from test_radar_regression import load_functions

class ForwardContractTests(unittest.TestCase):
    def segment(self, refined=True, touch=False, direction='SHORT'):
        src=Path('scalp_engine.py').read_text()
        start=src.index('    retest=None; active_zone=zone; entry_tf=exec_tf')
        end=src.index('    # M15 confirma a tese; M5 apenas refina. O SL',start)
        code='def probe():\n'+src[start:end]+'    return resultado\n'
        zone={'tipo':'FVG_bearish','bottom':.2562,'top':.2573,'created_ts':0}
        rz={'tipo':'FVG_bearish','bottom':.2565,'top':.2575,'created_ts':0}
        if direction=='LONG':
            zone={'tipo':'FVG_bullish','bottom':99.,'top':101.,'created_ts':0}
            rz={'tipo':'FVG_bullish','bottom':99.5,'top':100.5,'created_ts':0}
        m5=[{'t':900000,'o':.254,'h':.2565 if touch else .2562,'l':.254,'c':.2559}]
        if direction=='LONG':m5=[{'t':900000,'o':101.,'h':101.,'l':100.5 if touch else 100.8,'c':100.9}]
        ns={'resultado':{'zone_type':zone['tipo']},'refined':rz if refined else None,'zone':zone,'exec_tf':'M15','m5':m5,
            'structure_confirm_ts':900000,'structure':{'t':0,'tipo':'BOS','nivel':.2531},
            'internal_structure':{'t':0},'sweep':{'confirm_ts':0,'sweep_ts':0,'first_capture_ts':0,'liquidity_tf':'D1','liquidity_type':'PDL','liquidity_origin_ts':0},
            'exec_candles':[{'t':900000,'h':.2562,'l':.254,'c':.2559}],
            'after_ts':900000,'direction':direction,'preview_tf':'M5','major_confirm_ts':900000,'internal_confirm_ts':900000,'audit_entries_only':True,'intent':{}}
        ns.update(load_functions('scalp_engine.py',['_kairos_retest_zone'],{}))
        exec(code,ns)
        return ns['probe']()
    def test_parent_touch_does_not_execute_or_cancel_refined_setup(self):
        r=self.segment()
        self.assertIsNone(r.get('entry'))
        self.assertEqual(r['failure_reason'],'AGUARDANDO_RETESTE_ZONA')
        self.assertIsNone(r['entry_setup'].get('parent_retest'))
        self.assertEqual(r['entry_setup']['level'],.2565)
    def test_real_short_touch_uses_proximal_not_close_and_no_management(self):
        r=self.segment(touch=True)
        self.assertEqual(r['entry'],.2565)
        self.assertEqual(r['entry_tf'],'M5')
        self.assertTrue(r['entry_audit_only'])
        self.assertIsNone(r.get('sl'));self.assertIsNone(r.get('tp1'))
    def test_real_long_touch_uses_top(self):
        self.assertEqual(self.segment(touch=True,direction='LONG')['entry'],100.5)
    def test_audit_requires_m5_refinement(self):
        r=self.segment(refined=False)
        self.assertIsNone(r.get('entry'))
        self.assertEqual(r['failure_reason'],'SEM_REFINAMENTO_M5')
    def test_parent_result_is_defensively_suppressed_without_cancelling_m5(self):
        f=load_functions('scalp_engine.py',['_kairos_apply_entry_phase'],{})['_kairos_apply_entry_phase']
        r={'valid':True,'entry_setup':{'key':'A','tf':'M5','ready_ts':900000,'retest':None,'parent_retest':{'t':900000,'c':.2559}}}
        state={};f(state,r,1800000,0,[])
        self.assertFalse(r['valid']);self.assertFalse(state['A']['touched'])
    def test_live_scanner_requests_entry_only_mode(self):
        from test_live_snapshot import LiveSnapshotTests
        seen=[]
        ns,t=LiveSnapshotTests().ns(lambda *a,**kw:(seen.append(kw) or {'valid':False}))
        ns['_kairos_scan_latest_closed']('ADAUSD',t,{})
        self.assertTrue(seen[0].get('audit_entries_only'))
    def test_message_separates_zone_and_close_without_management(self):
        from datetime import datetime,timezone
        ns=load_functions('app.py',['_kairos_format_entry_observation'],{'datetime':datetime,'timezone':timezone})
        s={'entry_audit_only':True,'entry_tf':'M5','direction':'SHORT','entry':.2565,'zone_bottom':.2565,'zone_top':.2575,'zone_type':'FVG_bearish','observed_retest_close':.2559,'sl':.283031,'tp1':.228769,'tp2':.2421}
        msg=ns['_kairos_format_entry_observation']('ADAUSD',s)
        self.assertIn('M5',msg);self.assertIn('0.2565',msg);self.assertIn('0.2559',msg)
        for term in ['SL','TP1','TP2','BE','0.283031','0.228769']:self.assertNotIn(term,msg)
    def test_closed_m5_observation_is_cached_even_without_trade_management(self):
        from test_live_snapshot import LiveSnapshotTests
        ns,t=LiveSnapshotTests().ns(lambda *a,**kw:{'valid':False,'entry_audit_only':True,'entry':.2565,'entry_tf':'M5',
            'entry_setup':{'key':'A','tf':'M5','ready_ts':kw['cutoff_ts']-900000,'level':.2565,
                           'retest':{'t':kw['cutoff_ts']-300000,'c':.2559}}})
        ns.update(load_functions('scalp_engine.py',['_kairos_apply_entry_phase'],{}))
        r=ns['_kairos_scan_latest_closed']('ADAUSD',t,{})
        self.assertEqual(len(r['sinais_unicos_completos']),1)
        self.assertTrue(r['sinais_unicos_completos'][0]['entry_observation_confirmed'])
    def test_non_m5_message_is_rejected(self):
        f=load_functions('app.py',['_kairos_format_entry_observation'],{})['_kairos_format_entry_observation']
        self.assertIsNone(f('ADAUSD',{'entry_audit_only':True,'entry_tf':'M15'}))

if __name__=='__main__':unittest.main()
