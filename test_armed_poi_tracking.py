import unittest
from types import SimpleNamespace

class TrackingTests(unittest.TestCase):
    def api(self):
        from kairos_setup_tracking import remember_armed, observe_armed
        return remember_armed, observe_armed
    def setup(self,key='A',bottom=1.3649,top=1.3767):
        return dict(key=key,tf='M5',ready_ts=600000,direction='SHORT',level=bottom,bottom=bottom,top=top,zone_type='FVG_bearish',capture_tf='D1',capture_level=1.3815,major_confirm_ts=600000,internal_confirm_ts=600000,poi=dict(id=key,tipo='FVG_bearish',direcao='baixa',bottom=bottom,top=top,created_ts=0,smc_raw_top=bottom))
    def engine(self,zones):
        return SimpleNamespace(_kairos_fvg_states=lambda cs,**kwargs:zones)
    def test_preserves_announced_xrp_touch_even_if_selector_changes(self):
        remember,observe=self.api();old=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],dict(entry_audit_only=True,entry_setup=old),old,600000)
        current=dict(entry_setup=self.setup('B',1.3639,1.3647))
        bars=[dict(t=600000,o=1.3565,h=1.3653,l=1.3564,c=1.3628)]
        observe(state,current,bars,900000,self.engine([dict(old['poi'],state='INVALIDADA',invalidated_ts=600000)]))
        signal=state['A']['signal_snapshot']
        self.assertEqual(signal['entry'],1.3649)
        self.assertTrue(signal['armed_before_retest'])
        self.assertTrue(signal['poi_invalidated_same_bar'])
        self.assertFalse(signal['fill_assumed'])
        self.assertEqual(signal['observed_retest_close'],1.3628)
        self.assertEqual(current['entry_setup']['key'],'B')
        observe(state,current,bars,900000,self.engine([]))
        self.assertEqual(state['A']['signal_snapshot'],signal)
    def test_no_touch_after_removal_in_earlier_bar(self):
        remember,observe=self.api();s=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],{'entry_setup':s},s,600000)
        bars=[dict(t=600000,o=1.38,h=1.39,l=1.38,c=1.38),dict(t=900000,o=1.36,h=1.37,l=1.35,c=1.36)]
        observe(state,{'entry_setup':s},bars,1200000,self.engine([dict(s['poi'],state='INVALIDADA',invalidated_ts=600000)]))
        self.assertNotIn('signal_snapshot',state['A'])
        self.assertTrue(state['A']['retired'])
    def test_unarmed_record_never_becomes_prearmed_observation(self):
        _,observe=self.api();state={'A':{'armed':False,'touched':False}}
        observe(state,{'entry_setup':self.setup()},[dict(t=600000,o=1.36,h=1.37,l=1.35,c=1.36)],900000,self.engine([]))
        self.assertNotIn('signal_snapshot',state['A'])
    def test_zone_replacement_records_previous_level_without_changing_detector(self):
        remember,_=self.api();state={'A':{'armed':True,'touched':False}};a=self.setup('A',1.3695,1.3708)
        remember(state,state['A'],{'entry_setup':a},a,600000)
        b=self.setup('B',1.3738,1.3762);state['B']={'armed':True,'touched':False}
        remember(state,state['B'],{'entry_setup':b},b,900000)
        self.assertEqual(state['B']['selection_change']['previous_level'],1.3695)
        self.assertTrue(state['A']['retired'])
    def test_observation_is_not_backdated_before_actual_arming(self):
        remember,observe=self.api();s=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],{'entry_setup':s},s,900001)
        observe(state,{'entry_setup':s},[dict(t=900000,o=1.36,h=1.37,l=1.35,c=1.36)],1200000,self.engine([]))
        self.assertNotIn('signal_snapshot',state['A'])

class MessageTests(unittest.TestCase):
    def test_unarmed_touch_never_claims_already_armed(self):
        from kairos_telegram_ui import format_retest_message
        msg=format_retest_message('XRPUSD',dict(direction='SHORT',zone_type='IFVG_bearish',zone_bottom=1.3639,zone_top=1.3647,entry=1.3639,armed_before_retest=False))
        self.assertNotIn('já estava armado',msg)
        self.assertIn('SEM NOVA ENTRADA',msg)
    def test_fvg_same_bar_delete_is_not_called_an_ob(self):
        from kairos_telegram_ui import format_retest_message
        msg=format_retest_message('XRPUSD',dict(direction='SHORT',zone_type='FVG_bearish',zone_bottom=1.3649,zone_top=1.3767,entry=1.3649,armed_before_retest=True,poi_invalidated_same_bar=True))
        self.assertIn('FVG tocado',msg)
        self.assertIn('SEM NOVA ENTRADA',msg)
    def test_replacement_message_shows_old_and_new_zone(self):
        from kairos_telegram_ui import format_setup_message
        msg=format_setup_message('XRPUSD',dict(direction='SHORT',zone_type='IFVG_bearish',bottom=1.3738,top=1.3762,level=1.3738,selection_change=dict(previous_bottom=1.3695,previous_top=1.3708,previous_level=1.3695,reason='UPSTREAM_POI_CHANGED')))
        self.assertIn('POI ATUALIZADO',msg)
        self.assertIn('1.3695',msg)

class ReviewRegressionTests(unittest.TestCase):
    api=TrackingTests.api
    setup=TrackingTests.setup
    engine=TrackingTests.engine
    def test_ob_deleted_during_arming_bar_cannot_retest_later(self):
        remember,observe=self.api();s=self.setup();s.update(zone_type='OB_bearish');s['poi'].update(tipo='OB_bearish')
        state={'A':{'armed':True,'touched':False}};remember(state,state['A'],{'entry_setup':s},s,600001)
        bars=[dict(t=600000,o=1.38,h=1.39,l=1.38,c=1.38),dict(t=900000,o=1.36,h=1.37,l=1.35,c=1.36)]
        observe(state,{'entry_setup':s},bars,1200000,self.engine([]))
        self.assertNotIn('signal_snapshot',state['A'])
    def test_full_lifecycle_archive_requested_for_old_active_fvg(self):
        remember,observe=self.api();s=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],{'entry_setup':s},s,600000)
        bars=[dict(t=600000,o=1.3565,h=1.3653,l=1.3564,c=1.3628)]
        seen=[]
        def detector(cs,lookback=250):
            seen.append(lookback)
            return [dict(s['poi'],state='INVALIDADA',invalidated_ts=600000)] if lookback==len(cs) else []
        observe(state,{'entry_setup':s},bars,900000,SimpleNamespace(_kairos_fvg_states=detector))
        self.assertEqual(seen,[len(bars)])
        self.assertTrue(state['A']['signal_snapshot']['poi_invalidated_same_bar'])
    def test_later_thesis_change_does_not_erase_earlier_touch(self):
        remember,observe=self.api();s=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],{'entry_setup':s},s,600000)
        new=self.setup('B');new['major_confirm_ts']=1800000;new['internal_confirm_ts']=1800000
        observe(state,{'entry_setup':new},[dict(t=900000,o=1.36,h=1.37,l=1.35,c=1.36)],2100000,self.engine([s['poi']]))
        self.assertEqual(state['A']['signal_snapshot']['retest_confirm_close_ts'],1200000)
    def test_missing_native_zone_is_not_a_normal_retest(self):
        remember,observe=self.api();s=self.setup();state={'A':{'armed':True,'touched':False}}
        remember(state,state['A'],{'entry_setup':s},s,600000)
        observe(state,{'entry_setup':s},[dict(t=600000,o=1.36,h=1.37,l=1.35,c=1.36)],900000,self.engine([]))
        self.assertNotIn('signal_snapshot',state['A'])
    def test_selected_but_not_delivered_level_does_not_claim_user_received_it(self):
        from kairos_telegram_ui import format_retest_message
        msg=format_retest_message('XRPUSD',dict(direction='SHORT',zone_type='FVG_bearish',zone_bottom=1.3649,zone_top=1.3767,entry=1.3649,armed_before_retest=True,armed_alert_delivered_before_retest=False))
        self.assertNotIn('já estava armado',msg)
        self.assertIn('envio prévio',msg)
