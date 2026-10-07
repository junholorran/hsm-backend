import unittest
from datetime import datetime,timezone
from test_radar_regression import load_functions

class LiveSnapshotTests(unittest.TestCase):
    def ns(self,evaluate,stale=False):
        cutoff=1790895600001
        def fetch(symbol,iv,days,fim_ts_ms):
            duration={'5':300000,'15':900000,'1':60000,'30':1800000,'60':3600000,'240':14400000,'D':86400000,'W':604800000}.get(iv)
            if duration:
                end=cutoff//duration
                rows=[{'t':i*duration,'h':2.,'l':1.,'o':1.5,'c':1.5} for i in range(end-100,end)]
                if stale and iv=='5':rows.pop()
            else:
                cur=2026*12+9
                rows=[{'t':int(datetime(i//12,i%12+1,1,tzinfo=timezone.utc).timestamp()*1000),'h':2.,'l':1.,'o':1.5,'c':1.5} for i in range(cur-100,cur)]
            return rows+[{'t':cutoff+300000,'h':999.,'l':0.,'o':1.,'c':2.}]
        ns={'datetime':datetime,'timezone':timezone,'INTERVALO_MS_POR_LABEL':{'5':300000,'15':900000,'1':60000,'30':1800000,'60':3600000,'240':14400000,'D':86400000,'W':604800000},
            '_fetch_bybit_klines_historico':fetch,'_validar_e_limpar_candles':lambda cs,iv:(cs,{}),
            'avaliar_vortex_decision_layer_v2':evaluate,'_kairos_capture_radar_events':lambda *args:[],
            '_kairos_apply_entry_phase':lambda *args:None}
        load_functions('scalp_engine.py',['_kairos_candle_close_ts','_kairos_candles_fechados_ate','_kairos_snapshot_freshness','_kairos_scan_latest_closed'],ns)
        return ns,cutoff
    def test_evaluates_once_with_no_future_candles(self):
        seen=[]
        def evaluate(*a,**kw):seen.append(kw);return {'valid':False,'failure_reason':'SEM_SWEEP_FIRST_CAPTURE'}
        ns,cutoff=self.ns(evaluate);r=ns['_kairos_scan_latest_closed']('BTCUSD',cutoff,{})
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['cutoff_ts'],1790895600000)
        self.assertTrue(all(c['h']!=999. for cs in seen[0]['candles_por_tf'].values() for c in cs))
        self.assertEqual(r['sinais_unicos_completos'],[])
    def test_missing_latest_m5_rejects_without_mutating_state(self):
        seen=[];ns,cutoff=self.ns(lambda *a,**kw:seen.append(kw),stale=True);state={'A':{'armed':True}}
        r=ns['_kairos_scan_latest_closed']('BTCUSD',cutoff,state)
        self.assertEqual(r['erro'],'STALE_CLOSED_CANDLES');self.assertEqual(seen,[])
        self.assertEqual(state,{'A':{'armed':True}})
    def test_failed_signal_is_retained_when_current_setup_changes(self):
        ns,cutoff=self.ns(lambda *a,**kw:{'valid':False,'failure_reason':'NO_CURRENT_SETUP'})
        signal={'timestamp':cutoff-300000,'entry':100.,'retest_confirm_close_ts':cutoff-1}
        state={'A':{'signal_snapshot':signal}}
        r=ns['_kairos_scan_latest_closed']('BTCUSD',cutoff,state)
        self.assertEqual(r['sinais_unicos_completos'],[signal])
