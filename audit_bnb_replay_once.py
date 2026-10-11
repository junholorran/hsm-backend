import json
import kairos_causal_proof as kp
import scalp_engine as s

kp.install()
print('[BNB_REPLAY_AUDIT] START', flush=True)
r = s.replay_vortex_decision_layer_v2('BNBUSD', dias_historico=1, fim_ts_ms=1791730200000)
print('[BNB_REPLAY_AUDIT] SUMMARY ' + json.dumps({
    'erro': r.get('erro'),
    'signals': len(r.get('sinais_unicos_completos') or []),
    'entry_phase_events': len(r.get('entry_phase_events') or []),
    'failure_reason_counts': r.get('failure_reason_counts'),
}, ensure_ascii=False, default=str), flush=True)
for e in (r.get('entry_phase_events') or []):
    txt = json.dumps(e, ensure_ascii=False, default=str)
    if '748.2' in txt or '748.7' in txt or '1791729600000' in txt or '1791718200000' in txt:
        print('[BNB_REPLAY_MATCH] ' + txt, flush=True)
print('[BNB_REPLAY_AUDIT] DONE', flush=True)
