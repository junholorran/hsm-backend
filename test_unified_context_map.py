import copy
import unittest
from kairos_daily_report import DAY, build_report, notification_parts
from test_daily_report_candidate import candle, level
from kairos_context_map import enrich_report

class UnifiedContextMapTests(unittest.TestCase):
    def inputs(self):
        report=build_report('SOLUSD',[candle(0),candle(DAY,115,95,112)],[level(value=120),level(typ='SWING_LOW',value=90),level(value=114,state='CAPTURED')],2*DAY,2*DAY)
        tm={'M5':[dict(t=2*DAY-300000,o=109,h=110,l=108,c=109)]}
        zone=dict(tipo='FVG_bullish',direcao='alta',bottom=100.,top=105.,state='ATIVA',created_ts=DAY)
        decision={'context_map_source':{'asof_ts':2*DAY,'zones':{'H4':[zone],'H1':[]}},
                  'failure_reason':'SEM_INTENCAO_M15_APOS_CAPTURA_ESTRUTURAL'}
        return report,tm,decision

    def test_uses_closed_price_current_zones_and_available_liquidity_without_mutation(self):
        r,tm,d=self.inputs();old=copy.deepcopy((r,tm,d));out=enrich_report(r,tm,d)
        self.assertEqual((r,tm,d),old)
        self.assertEqual(out['current_price']['value'],109)
        self.assertEqual(out['nearest_available']['above']['level'],120)
        self.assertEqual(out['nearest_available']['reference'],'LAST_CLOSED_M5_CLOSE')
        self.assertEqual(len(out['zones']),1)
        self.assertFalse(out['entry_authorized'])
        self.assertIn('FVG_bullish', '\n'.join(notification_parts(out)))
        self.assertIn('aguardando confirmação M15', '\n'.join(notification_parts(out)))

    def test_invalid_future_zone_and_invalidated_zone_cannot_be_presented_as_available(self):
        r,tm,d=self.inputs();z=d['context_map_source']['zones']['H4'][0]
        for changes in ({'created_ts':2*DAY},{'state':'INVALIDADA'},{'bottom':106,'top':105}):
            with self.subTest(changes=changes):
                q=copy.deepcopy(d);q['context_map_source']['zones']['H4'][0].update(changes)
                self.assertEqual(enrich_report(r,tm,q)['zones'],[])

    def test_closed_m15_capture_overrides_available_label_without_changing_registry(self):
        r,tm,d=self.inputs();original=copy.deepcopy(r['levels'])
        d['structural_sweep_audit']={'candidates':[dict(liquidity_tf='H1',liquidity_type='SWING_HIGH',
            nivel=120,liquidity_origin_ts=0,capture_confirm_ts=2*DAY,first_capture_ts=2*DAY-900000,
            post_capture_state='UNRESOLVED_REACTION')]}
        out=enrich_report(r,tm,d)
        self.assertIsNone(out['nearest_available']['above'])
        self.assertEqual(next(x for x in out['levels'] if x['level']==120)['state'],'CAPTURED')
        self.assertEqual(r['levels'],original)

    def test_stage_reflects_real_gate_not_a_bias_based_trade(self):
        r,tm,d=self.inputs()
        d.update(entry_setup={'key':'s','ready_ts':2*DAY,'tf':'M5','direction':'SHORT','bottom':110,'top':111},failure_reason='AGUARDANDO_RETESTE_ZONA')
        out=enrich_report(r,tm,d)
        self.assertEqual(out['stage']['code'],'AGUARDANDO_RETESTE_M5')
        self.assertFalse(out['entry_authorized'])
        d.update(entry_observation_confirmed=True,retest_confirm_close_ts=2*DAY)
        self.assertEqual(enrich_report(r,tm,d)['stage']['code'],'RETESTE_M5_OBSERVADO')

    def test_past_invalidated_or_retested_setup_is_not_advertised_as_waiting(self):
        r,tm,d=self.inputs()
        d['entry_setup']={'key':'old','ready_ts':DAY,'tf':'M5','direction':'LONG',
                          'bottom':110,'top':111,'poi':{'m15_invalidation_close_ts':2*DAY-900000}}
        out=enrich_report(r,tm,d)
        self.assertEqual(out['stage']['code'],'SETUP_ANTERIOR_INVALIDADO')
        self.assertIsNone(out['stage']['direction'])
        d['entry_setup']['poi']={}
        d['entry_setup']['retest']={'t':DAY+300000}
        self.assertEqual(enrich_report(r,tm,d)['stage']['code'],'RETESTE_HISTORICO_SEM_NOVA_ENTRADA')

    def test_forming_or_future_m5_cannot_change_price_or_daily_bias(self):
        r,tm,d=self.inputs();before=enrich_report(r,tm,d)
        tm['M5'].append(dict(t=2*DAY,o=900,h=999,l=1,c=999))
        after=enrich_report(r,tm,d)
        self.assertEqual(after['current_price'],before['current_price'])
        self.assertEqual(after['daily'],before['daily'])

    def test_same_stage_is_deduplicated_and_real_transition_changes_delivery_key(self):
        r,tm,d=self.inputs();first=enrich_report(r,tm,d)
        again=copy.deepcopy(r);again['asof_ts']+=300000;d['context_map_source']['asof_ts']+=300000
        tm['M5'][-1]['t']+=300000
        same=enrich_report(again,tm,d)
        self.assertEqual(first['delivery_key'],same['delivery_key'])
        d['entry_setup']={'key':'s','ready_ts':2*DAY,'tf':'M5','direction':'SHORT','bottom':110,'top':111}
        self.assertNotEqual(first['delivery_key'],enrich_report(again,tm,d)['delivery_key'])

if __name__=='__main__':unittest.main()
