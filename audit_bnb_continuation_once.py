import json
import scalp_engine as s

PAIR = 'BNBUSD'
AUTH_TS = 1791719100000
CUTS = [1791729600000, 1791729900000]
END_TS = 1791730200000

print('[BNB_CONTINUATION_AUDIT] START', flush=True)
m15 = s._fetch_bybit_klines_historico(PAIR, '15', 1, fim_ts_ms=END_TS)
m5 = s._fetch_bybit_klines_historico(PAIR, '5', 1, fim_ts_ms=END_TS)
print('[BNB_CONTINUATION_DATA] ' + json.dumps({'m15': len(m15 or []), 'm5': len(m5 or []), 'authorization_ts': AUTH_TS}, default=str), flush=True)

for cutoff in CUTS:
    z = s._kairos_m5_continuation_zone(m5, m15, AUTH_TS, 'alta', cutoff)
    out = {'cutoff_ts': cutoff, 'zone': z}
    if z:
        ev = z.get('causal_structure_m5') or {}
        bc = z.get('causal_break_m5') or {}
        oc = z.get('origin_candle') or {}
        out['proof'] = {
            'type': z.get('tipo'),
            'bottom': z.get('bottom'),
            'top': z.get('top'),
            'ready_ts': z.get('ready_ts'),
            'origin_ts': z.get('origin_ts'),
            'created_ts': z.get('created_ts'),
            'refinement_basis': z.get('refinement_basis'),
            'm15_authorization_ts': z.get('m15_authorization_ts'),
            'm15_protected_boundary': z.get('m15_protected_boundary'),
            'm15_invalidation_close_ts': z.get('m15_invalidation_close_ts'),
            'm5_event_type': ev.get('tipo'),
            'm5_event_direction': ev.get('direcao'),
            'm5_break_level': ev.get('nivel'),
            'm5_broken_swing_origin_ts': ev.get('broken_swing_origin_ts'),
            'm5_protected_swing_type': ev.get('protected_swing_type'),
            'm5_protected_swing_level': ev.get('protected_swing_level'),
            'm5_protected_swing_origin_ts': ev.get('protected_swing_origin_ts'),
            'm5_break_open_ts': bc.get('t'),
            'm5_break_close_price': bc.get('c'),
            'origin_candle': oc,
            'retest': z.get('retest'),
            'checks': {
                'authorization_before_ready': AUTH_TS < (z.get('ready_ts') or 0),
                'broken_pivot_after_authorization': (ev.get('broken_swing_origin_ts') or 0) >= AUTH_TS,
                'ob_origin_after_authorization': (z.get('origin_ts') or 0) >= AUTH_TS,
                'bullish_bos': ev.get('tipo') == 'BOS' and ev.get('direcao') == 'alta',
                'zone_exact_748_2_748_7': z.get('bottom') == 748.2 and z.get('top') == 748.7,
                'ready_by_cutoff': (z.get('ready_ts') or 10**30) <= cutoff,
                'no_m15_invalidation_before_ready': z.get('m15_invalidation_close_ts') is None or z.get('m15_invalidation_close_ts') > (z.get('ready_ts') or 0),
            }
        }
    print('[BNB_CONTINUATION_PROOF] ' + json.dumps(out, ensure_ascii=False, default=str), flush=True)
print('[BNB_CONTINUATION_AUDIT] DONE', flush=True)
