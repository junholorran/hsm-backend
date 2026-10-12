import copy
import unittest
from unittest.mock import patch
import scalp_engine as e
from kairos_telegram_ui import format_setup_message


class HTFFVGEntryTests(unittest.TestCase):
    def setUp(self):
        self.touch = 3600000
        self.cutoff = 8100000
        self.zone = {'id':'mother','tipo':'FVG_bearish','direcao':'baixa',
                     'bottom':100.,'top':102.,'created_ts':0,'state':'TOCADA','lux_fvg':True}
        self.zone.update(self.sources(0,3600000))
        self.maps = {'H1':{'zones':[self.zone]}}
        self.m5 = [{'t':t,'o':99.,'h':99.5,'l':98.,'c':99.} for t in range(3300000,self.cutoff,300000)]
        self.m5[1].update(h=101.,c=100.5)
        self.m15 = [{'t':t,'o':99.,'h':101.,'l':97.,'c':98.} for t in range(2700000,self.cutoff,900000)]
        self.major = {'t':4500000,'tipo':'CHoCH','direcao':'baixa','nivel':99.,'leg_start_ts':3600000}
        self.internal = {'t':5400000,'tipo':'BOS','direcao':'baixa','nivel':98.,'leg_start_ts':3600000}

    @staticmethod
    def sources(born,duration):
        return {'source_a':{'t':born-2*duration,'o':103.,'c':103.,'h':104.,'l':102.},
                'source_mid':{'t':born-duration,'o':103.,'c':99.,'h':103.,'l':98.},
                'source_c':{'t':born,'o':99.,'c':99.,'h':100.,'l':98.}}

    def select(self, maps=None, m5=None, major=None, internal=None, cutoff=None, native=None):
        with patch.object(e,'compute_lux_structure_events',return_value=[self.major] if major is None else major), \
             patch.object(e,'compute_lux_internal_structure',return_value=[self.internal] if internal is None else internal):
            fn=getattr(e,'_kairos_fvg_touch_intent',lambda *a:(None,None,None,{}))
            return fn(self.maps if maps is None else maps,self.m15,self.m5 if m5 is None else m5,self.cutoff if cutoff is None else cutoff,candles_por_tf=native)

    def test_touch_then_major_internal_authorizes_without_liquidity(self):
        anchor,intent,location,_ = self.select()
        self.assertIsNotNone(intent)
        self.assertEqual(intent['direction'],'SHORT')
        self.assertEqual(location['touch_ts'],self.touch)
        self.assertNotIn('liquidity_side',anchor)
        self.assertEqual(anchor['event_confirmation_close_ts'],self.touch+300000)

    def test_fvg_polarity_does_not_choose_trade_direction(self):
        major=[{**self.major,'direcao':'alta'}]; internal=[{**self.internal,'direcao':'alta'}]
        self.assertEqual(self.select(major=major,internal=internal)[1]['direction'],'LONG')

    def test_touch_alone_or_internal_against_major_never_authorizes(self):
        self.assertIsNone(self.select(major=[],internal=[])[1])
        self.assertIsNone(self.select(internal=[{**self.internal,'direcao':'alta'}])[1])
        self.assertIsNone(self.select(major=[{**self.major,'t':2700000}])[1])

    def test_unclosed_major_or_internal_is_not_available(self):
        self.assertIsNone(self.select(cutoff=6299999)[1])
        self.assertIsNotNone(self.select(cutoff=6300000)[1])

    def test_missing_touch_coverage_or_unknown_initial_overlap_fails_closed(self):
        self.assertIsNone(self.select(m5=[c for c in self.m5 if c['t']!=4200000])[1])
        self.assertIsNone(self.select(m5=self.m5[1:])[1])

    def test_unclosed_htf_fvg_and_invalid_or_ifvg_are_excluded(self):
        for changes in ({'created_ts':3600000},{'state':'INVALIDADA'}, {'tipo':'IFVG_bearish'}, {'lux_fvg':False}):
            with self.subTest(changes=changes):
                maps={'H4':{'zones':[{**self.zone,**changes}]}}
                self.assertIsNone(self.select(maps=maps)[1])

    def test_all_four_htf_sources_and_stable_zone_identity(self):
        for tf,duration in [('H1',3600000),('H4',14400000),('D1',86400000),('W1',604800000)]:
            with self.subTest(tf=tf):
                maps={tf:{'zones':[{**self.zone,'created_ts':3600000-duration,**self.sources(3600000-duration,duration)}]}}
                anchor,intent,location,_=self.select(maps=maps)
                self.assertIsNotNone(intent)
                self.assertEqual(location['tf'],tf)
                self.assertEqual(anchor['origin_key'],tf+'|mother')

    def test_multiple_overlapping_fvgs_do_not_hide_authorized_older_touch(self):
        newer={**self.zone,'id':'new','bottom':97.,'top':98.5}
        maps={'H1':{'zones':[self.zone,newer]}}
        anchor,intent,location,_=self.select(maps=maps)
        self.assertIsNotNone(intent)
        self.assertEqual(location['zone_id'],'mother')

    def test_later_same_zone_touch_keeps_previously_authorized_setup(self):
        bars=copy.deepcopy(self.m5)
        bars[-1].update(h=101.,c=100.5)
        anchor,intent,location,_=self.select(m5=bars)
        self.assertIsNotNone(intent)
        self.assertEqual(location['touch_ts'],self.touch)

    def test_full_entry_pipeline_uses_real_lux_btc_structure_and_proximal(self):
        import json
        from pathlib import Path
        data=json.loads(Path('continuation_btc_fixture.json').read_text())
        created=1791324000000
        h1=[{'t':created+(i-4)*3600000,'o':85600.,'c':85600.,'h':85700.,'l':85580.} for i in range(3)]
        h1 += [{'t':created-3600000,'o':85600.,'c':85400.,'h':85610.,'l':85350.},
               {'t':created,'o':85400.,'c':85450.,'h':85565.,'l':85300.}]
        mother=e._kairos_fvg_states(h1)[-1]
        self.assertEqual((mother['bottom'],mother['top']),(85565.,85580.))
        data['H1']=h1+[{'t':t,'o':85000.,'c':84000.,'h':85500.,'l':83000.}
                       for t in range(created+3600000,1791374400000,3600000)]
        original_map=e._kairos_build_mtf_map
        def mapped(cs):
            result=original_map(cs)
            result['H1']={'zones':[mother]}
            return result
        def run(cutoff):
            candles={tf:[c for c in bars if c['t']+({'M15':900000,'M5':300000,'H1':3600000}[tf])<=cutoff]
                     for tf,bars in data.items()}
            with patch.object(e,'_kairos_build_mtf_map',side_effect=mapped), \
                 patch.object(e,'_kairos_select_structural_first_capture_sweep',return_value=(None,{})):
                return e.avaliar_vortex_decision_layer_v2(candles['M15'],candles['M5'],
                    candles_por_tf=candles,cutoff_ts=cutoff,audit_entries_only=True)
        armed=run(1791373200000)
        self.assertEqual(armed['authorization_path'],'HTF_FVG_TOUCH')
        self.assertEqual(armed['entry_setup']['level'],83684.9)
        self.assertIsNone(armed['entry_setup']['retest'])
        self.assertNotIn('_radar_capture',armed)
        self.assertIsNone(armed['liquidity_tf'])
        retested=run(1791374400000)
        self.assertEqual(retested['entry'],83684.9)
        self.assertEqual(retested['entry_tf'],'M5')
        self.assertEqual(retested['entry_setup']['retest']['t'],1791374100000)
        self.assertEqual(armed['entry_setup']['key'],retested['entry_setup']['key'])
        self.assertGreaterEqual(retested['entry_setup']['retest']['t'],
                                retested['m15_internal_confirmation_close_ts'])

    def test_fvg_with_missing_or_nonconsecutive_native_mother_is_rejected(self):
        for changes in ({'source_a':None},{'source_mid':{**self.zone['source_mid'],'t':-7200000}}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.select(maps={'H1':{'zones':[{**self.zone,**changes}]}})[1])

    def test_native_lifecycle_gap_and_stale_tf_fail_closed(self):
        native={'H1':[{'t':0},{'t':3600000}]}
        self.assertIsNotNone(self.select(native=native)[1])
        self.assertIsNone(self.select(native={'H1':[{'t':0}]})[1])
        self.assertIsNone(self.select(native={'H1':[{'t':3600000}]})[1])

    def test_successful_liquidity_setup_keeps_same_key_and_entry(self):
        import json
        from pathlib import Path
        data=json.loads(Path('continuation_btc_fixture.json').read_text())
        cutoff=1791374400000
        data={tf:[c for c in bars if c['t']+({'M15':900000,'M5':300000}[tf])<=cutoff]
              for tf,bars in data.items()}
        cap={'liquidity_tf':'H1','liquidity_type':'SWING_HIGH','nivel':85570.,
             'extremo':85580.,'liquidity_origin_ts':1791320000000,
             'first_capture_ts':1791330300000,'sweep_ts':1791330300000,
             'confirm_ts':1791331200000,'liquidity_side':'HIGH','post_capture_state':'REJECTION_RECLAIM'}
        args=(data['M15'],data['M5'])
        with patch.object(e,'_kairos_select_structural_first_capture_sweep',return_value=(cap,{})), \
             patch.object(e,'_kairos_fvg_touch_intent',side_effect=AssertionError('Existing liquidity must retain priority')):
            result=e.avaliar_vortex_decision_layer_v2(*args,candles_por_tf=data,cutoff_ts=cutoff,audit_entries_only=True)
        self.assertEqual(result['authorization_path'],'STRUCTURAL_LIQUIDITY_CAPTURE')
        self.assertEqual(result['entry'],83684.9)
        self.assertNotIn('HTF_FVG_TOUCH',result['entry_setup']['key'])
        self.assertNotIn('origin_kind',result['entry_setup'])

    def test_fvg_causal_proof_does_not_claim_unrelated_liquidity_capture(self):
        from kairos_causal_proof import _capture_payload
        result={'authorization_path':'HTF_FVG_TOUCH','structural_sweep_audit':{
            'selected_capture':{'liquidity_tf':'D1','liquidity_type':'PDH','nivel':100.}}}
        self.assertEqual(_capture_payload(result),{})

    def test_setup_presentation_identifies_fvg_without_claiming_capture(self):
        event={'direction':'SHORT','origin_kind':'HTF_FVG_TOUCH','htf_location':{
            'tf':'H4','tipo':'FVG_bearish','bottom':100.,'top':102.},'zone_type':'OB_bearish',
            'bottom':98.,'top':99.,'level':98.}
        msg=format_setup_message('SOLUSD',event)
        self.assertIn('FVG H4',msg)
        self.assertIn('100–102',msg)
        self.assertNotIn('💧',msg)
        self.assertNotIn('None',msg)


if __name__=='__main__': unittest.main()
