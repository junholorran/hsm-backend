"""Single Ludo structural provider for live manual entry observations.

Legacy Lux entry/replay remains offline reference. Live snapshots never call
its Major/Internal authorization or reuse its pending setup phase records.
"""
import copy
import os

import kairos_capture_chains as chains
from kairos_ludo_liquidity import captures as recent_ludo_captures


def enabled():
    policy = os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper()
    if policy not in ('', 'LUDO'):
        raise ValueError('Unsupported live entry structure policy')
    return policy == 'LUDO'


def htf_touch_locations(mapa, m15_candles, m5_candles, now_ts, candles_por_tf=None):
    """Existing HTF FVG interaction discovery, extracted without math changes."""
    m15=[c for c in m15_candles if c['t']+900000 <= now_ts]
    m5=[c for c in m5_candles if c['t']+300000 <= now_ts]
    durations={'W1':604800000,'D1':86400000,'H4':14400000,'H1':3600000}
    locations=[]
    for tf,duration in durations.items():
        for z in (mapa.get(tf) or {}).get('zones',[]):
            if (z.get('tipo') not in ('FVG_bullish','FVG_bearish') or
                    z.get('state') not in ('ATIVA','TOCADA','PARCIAL') or
                    not (z.get('ludo_fvg') is True or z.get('lux_fvg') is True)):
                continue
            born=z.get('created_ts'); top=z.get('top'); bottom=z.get('bottom')
            if born is None or top is None or bottom is None or top < bottom:
                continue
            sources=[z.get('source_a'),z.get('source_mid'),z.get('source_c')]
            if (not all(isinstance(c,dict) and c.get('t') is not None for c in sources) or
                    [c['t'] for c in sources] != [born-2*duration,born-duration,born]):
                continue
            ready=born+duration
            if ready > now_ts:
                continue
            if candles_por_tf is not None:
                native=[c for c in candles_por_tf.get(tf,[]) if born <= c['t'] and c['t']+duration <= now_ts]
                offset=345600000 if tf=='W1' else 0
                latest_open=((now_ts-offset)//duration)*duration+offset-duration
                if (not native or native[0]['t'] != born or native[-1]['t'] != latest_open or
                        any(b['t']-a['t'] != duration for a,b in zip(native,native[1:]))):
                    continue
            entries=[]
            for prev,c in zip(m5,m5[1:]):
                if c['t'] < ready:
                    continue
                inside=c['h'] >= bottom and c['l'] <= top
                prev_inside=prev['h'] >= bottom and prev['l'] <= top
                if inside and not prev_inside and c['t']-prev['t']==300000:
                    entries.append(c)
            zone_id=z.get('id') or f"{z['tipo']}:{born}:{bottom}:{top}"
            for entry in entries:
                # A later interaction cannot erase a previously authorized touch.
                covered=[c for c in m5 if c['t'] >= entry['t']]
                if any(b['t']-a['t'] != 300000 for a,b in zip(covered,covered[1:])):
                    continue
                locations.append({'tf':tf,'tipo':z['tipo'],'zone_id':zone_id,
                                  'top':top,'bottom':bottom,'origin_ts':born,
                                  'formation_close_ts':ready,'touch_ts':entry['t'],
                                  'touch_confirm_close_ts':entry['t']+300000,
                                  'touch_price':entry['c'],'source':'HTF_FVG_TOUCH'})
    return locations


def snapshot(pair, tf_map, cutoff, engine):
    """Sole live entry provider; the legacy phase/signal collections stay empty."""
    mapa = engine._kairos_build_mtf_map(tf_map)
    context = engine._kairos_context_bias(tf_map)
    _, audit = engine._kairos_select_structural_first_capture_sweep(tf_map, cutoff)
    decision = dict(valid=False, signal=False, entry_audit_only=True,
                    entry_structure_model='LUDO', structural_sweep_audit=audit,
                    causal_policy_version=chains.CAUSAL_POLICY_VERSION,
                    context_bias=context, bias=context.get('final'),
                    failure_reason='SEM_INTENCAO_M15_APOS_CAPTURA_ESTRUTURAL',
                    context_map_source=dict(asof_ts=cutoff, zones={tf:
                        list((mapa.get(tf) or {}).get('zones') or [])+
                        list((mapa.get(tf) or {}).get('order_blocks') or [])
                        for tf in ('H4', 'H1')}))
    locations = htf_touch_locations(mapa, tf_map['M15'], tf_map['M5'], cutoff, tf_map)
    origins = []
    for location in locations:
        zone = next((z for z in (mapa.get(location['tf']) or {}).get('zones', [])
                     if z.get('id') == location['zone_id']), None)
        if zone is None:
            continue
        origins.append(dict(origin_kind='HTF_FVG_TOUCH',
            origin_key=location['tf']+'|'+location['zone_id'],
            htf_location=location, origin_zone=copy.deepcopy(zone),
            first_capture_ts=location['touch_ts'], sweep_ts=location['touch_ts'],
            native_capture_confirm_ts=location['touch_confirm_close_ts']))
    # Standalone touches remain map context; they cannot originate an entry.
    decision['htf_entry_origins'] = []
    from kairos_reaction_episode import period_capture_origins
    decision['period_entry_origins'] = period_capture_origins(engine,tf_map,cutoff) if hasattr(engine,'_kairos_previous_period_liquidity') else []
    decision['ludo_liquidity_origins'] = recent_ludo_captures(tf_map,cutoff)
    try:
        radars = engine._kairos_capture_radar_events(decision, tf_map, cutoff)
    except Exception as exc:
        radars = []
        print(f'[KAIROS_CAPTURE_RADAR] Ludo presentation error pair={pair}: {type(exc).__name__}', flush=True)
    decision['radar_context'] = radars
    saved = chains.scan(pair, tf_map, cutoff, decision, engine)
    if not saved or saved.get('asof_ts') != cutoff:
        raise RuntimeError('Ludo durable provider did not confirm this cutoff')
    decision['ludo_entry_stage'] = saved['view']
    decision['direction'] = saved['view'].get('direction')
    decision['intent_m15_found'] = bool(saved['view'].get('m15_break'))
    daily = None
    from kairos_daily_delivery import scan_report
    try:
        daily = scan_report(pair, tf_map, cutoff, engine, decision)
    except Exception as exc:
        print(f'[KAIROS_DAILY_BRIEF] Ludo presentation error pair={pair}: {type(exc).__name__}', flush=True)
    for radar in radars:
        radar['entry_structure_model'] = 'LUDO'
    print(f'[KAIROS_ENTRY_MODEL] pair={pair} model=LUDO legacy_authorization=false '
          f'causal_policy_version={chains.CAUSAL_POLICY_VERSION} '
          f'm15_primary={str(chains.primary.enabled()).lower()} '
          f'cutoff={cutoff} stage={saved["view"]["code"]}', flush=True)
    from kairos_htf_fvg_alerts import build_touch_events
    last_m5=next((c for c in reversed(tf_map.get('M5',[])) if c['t']+300000<=cutoff),None)
    touch_events=build_touch_events(mapa,last_m5,last_m5['t']+300000) if last_m5 else []
    from kairos_htf_fvg_watch import build_structure_watch_events
    watch_events=build_structure_watch_events(mapa,tf_map.get('M15',[]),tf_map.get('M5',[]),cutoff)
    confirmed=(saved['view'].get('m15_break') or {}).get('close_ts')
    watch_events=[e for e in watch_events if e['confirmation_close_ts']!=confirmed]
    report = dict(pair=pair, scan_mode='LATEST_CLOSED_SNAPSHOT', evaluations=1,
        htf_fvg_structure_events=watch_events,
        htf_fvg_touch_events=touch_events,
        entry_structure_model='LUDO', daily_brief=daily, cutoff_ts=cutoff,
        causal_policy_version=chains.CAUSAL_POLICY_VERSION,
        sinais_unicos_completos=[], entry_phase_events=[], entry_setup_states={},
        pending_setup_keys=[], radar_captures=[], radar_context_events=radars,
        ludo_entry_stage=saved['view'],
        distribuicao_motivos_todos_ciclos={saved['view']['code']: 1})

    if os.environ.get('KAIROS_FVG_FORCE_SHADOW', '').strip().lower() == 'true':
        try:
            from kairos_fvg_force_shadow import observe_view
            report['fvg_force_shadow'] = observe_view(tf_map, saved['view'], cutoff)
        except Exception as exc:
            # Research output must never block the established entry provider.
            report['fvg_force_shadow'] = dict(mode='OBSERVATION_ONLY',
                status='OBSERVER_ERROR', error_type=type(exc).__name__,
                changes_entry_decision=False)
    return report
