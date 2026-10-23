"""Independent, opt-in observational chains. Never feeds the entry engine.

Ludo supplies structure; existing engine functions supply causal FVG/OBs.
SQLite state/outbox is separate from all legacy setup/delivery watermarks.
"""
import copy
import hashlib
import html
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from kairos_ludo_structure import structure_snapshot

M5 = 300000
M15 = 900000
PAIRS = ('BTCUSD', 'ETHUSD', 'SOLUSD', 'BNBUSD')
DEFAULT_PATH = '/data/kairos_capture_chains.db'
_LOCK = threading.RLock()
_QUEUE_LOCK = threading.Lock()
_PENDING = set()
_WORKER = None


def enabled():
    return (os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper() == 'LUDO'
            or os.environ.get('KAIROS_CAPTURE_CHAINS_ENABLED', 'false').strip().lower() == 'true')


def capture_id(pair, capture):
    if capture.get('origin_kind') == 'HTF_FVG_TOUCH':
        return hashlib.sha256(json.dumps([pair, 'HTF_FVG_TOUCH', capture['origin_key'],
            capture['first_capture_ts']], separators=(',', ':')).encode()).hexdigest()[:20]
    fields = [pair, capture.get('liquidity_tf'), capture.get('liquidity_type'),
              capture.get('liquidity_origin_ts'), capture.get('nivel'),
              capture.get('first_capture_ts')]
    return hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()[:20]


def _closed(rows, duration, cutoff):
    cs = [c for c in rows if c['t']+duration <= cutoff]
    if not cs or cs[-1]['t']+duration != cutoff//duration*duration:
        return None
    if any(b['t']-a['t'] != duration for a, b in zip(cs, cs[1:])):
        return None
    if any(not all(isinstance(c.get(k), (int, float)) and math.isfinite(c[k])
                   for k in ('t', 'o', 'h', 'l', 'c')) for c in cs):
        return None
    return cs


def _zone_ok(zone):
    return bool(zone and all(isinstance(zone.get(k), (int, float))
                            and math.isfinite(zone[k]) and zone[k] > 0
                            for k in ('top', 'bottom')) and zone['top'] > zone['bottom'])


def _structure(event, candles):
    index = next((i for i, c in enumerate(candles) if c['t'] == event['open_ts']), None)
    return dict(t=event['open_ts'], full_idx=index,
                leg_start_ts=event['opposite_origin_ts'],
                protected_swing_origin_ts=event['opposite_origin_ts'],
                broken_swing_origin_ts=event['origin_ts'])


def _parent_zone(engine, candles, capture, event):
    prefix = [c for c in candles if c['t']+M15 <= event['close_ts']]
    direction = 'alta' if event['side'] == 'UP' else 'baixa'
    return engine._kairos_select_entry_zone(
        prefix, {**capture, 'direcao': direction}, _structure(event, prefix), {})


def _removed_at(zone, candles, duration, engine):
    """Return native removal CLOSE time, never approximate FVG/IFVG rules."""
    if not zone:
        return None
    born = (zone.get('flip_ts') or zone.get('created_ts') or zone.get('break_ts') or 0)+duration
    if zone.get('tipo', '').startswith(('FVG_', 'IFVG_')):
        native = next((z for z in engine._kairos_fvg_states(candles, lookback=len(candles))
                       if z.get('id') == zone.get('id') and zone.get('id')), None)
        if native is None:
            return born  # Missing lifecycle evidence cannot authorize a zone.
        removed = native.get('invalidated_ts')
        return removed+duration if removed is not None else None
    from kairos_lux_ob import ob_invalidated
    removals = [zone[k]+duration for k in ('capacity_removed_ts', 'invalidated_ts')
                if zone.get(k) is not None]
    removals += [c['t']+duration for c in candles
                 if c['t']+duration >= born and ob_invalidated(zone, c)]
    return min(removals) if removals else None


def _causal_m5(zone, start, ready):
    if zone.get('tipo', '').startswith(('FVG_', 'IFVG_')):
        sources = [zone.get(k) or {} for k in ('source_a', 'source_mid', 'source_c')]
        return all(s.get('t') is not None and start <= s['t'] and s['t']+M5 <= ready
                   for s in sources)
    origin = zone.get('t')
    return origin is not None and start <= origin < ready


def _refinement(engine, m5, capture, event, parent, snapshots):
    # Freeze initial refinement on the exact closed M15 confirmation prefix.
    prefix = [c for c in m5 if c['t']+M5 <= event['close_ts']]
    direction = 'alta' if event['side'] == 'UP' else 'baixa'
    zone = engine._kairos_m5_refine_zone(
        prefix, parent, capture['first_capture_ts'], event['close_ts'], direction,
        structure_level=event['level'], structure_origin_ts=event['origin_ts'])
    if (_zone_ok(zone) and zone.get('direcao') == direction
            and zone['top'] >= parent['bottom'] and zone['bottom'] <= parent['top']
            and _causal_m5(zone, capture['first_capture_ts'], event['close_ts'])
            and _removed_at(zone, m5, M5, engine) is None):
        return zone, event['close_ts']
    # Later M5 structure may produce a new causal refinement, never an old POI.
    for e in reversed(snapshots):
        if e['open_ts'] < event['close_ts'] or e['side'] != event['side']:
            continue
        if e['level_known_ts'] > e['open_ts']:
            continue
        prefix = [c for c in m5 if c['t']+M5 <= e['close_ts']]
        z = engine._kairos_select_entry_zone(
            prefix, {**capture, 'sweep_ts': event['close_ts'], 'direcao': direction},
            _structure(e, prefix), {})
        if (_zone_ok(z) and z.get('direcao') == direction
                and z['top'] >= parent['bottom'] and z['bottom'] <= parent['top']
                and _causal_m5(z, event['close_ts'], e['close_ts'])
                and _removed_at(z, m5, M5, engine) is None):
            return z, e['close_ts']
    return None, None


def advance(state, pair, captures, tf_map, cutoff, engine, observed_ts=None):
    """Pure causal transition, returning copied state and immutable alert facts.

    First activation establishes a baseline. New captures are admitted using
    their first known confirmation, independent of the selected legacy setup.
    A late-observed plan is armed NOW; no earlier touch becomes a live entry.
    """
    original = copy.deepcopy(state)
    observed_ts = max(cutoff, observed_ts or cutoff)
    if state and cutoff <= state['asof_ts']:
        return original, []
    m15 = _closed(tf_map.get('M15', []), M15, cutoff)
    m5 = _closed(tf_map.get('M5', []), M5, cutoff)
    if not m15 or not m5:
        return original, []
    snapshots = {tf: structure_snapshot(cs, d, cutoff)
                 for tf, cs, d in (('M15', m15, M15), ('M5', m5, M5))}
    if any(s['status'] != 'OK' for s in snapshots.values()):
        return original, []
    if not state:
        return dict(start_ts=cutoff, asof_ts=cutoff, captures={}), []
    state = original
    previous = state['asof_ts']
    alerts = []

    def emit(cid, rec, stage, **facts):
        event = dict(pair=pair, capture_id=cid, stage=stage, observed_ts=observed_ts,
                     capture=copy.deepcopy(rec['capture']), fill_assumed=False,
                     entry_authorized=False, **facts)
        event['key'] = hashlib.sha256(json.dumps(
            [cid, stage, facts.get('break_ts'), facts.get('candle_ts'), cutoff],
            separators=(',', ':')).encode()).hexdigest()
        alerts.append(event)

    for capture in captures:
        known = capture.get('native_capture_confirm_ts')
        poi_origin = capture.get('origin_kind') == 'HTF_FVG_TOUCH'
        valid_origin = (bool(capture.get('origin_key') and capture.get('htf_location')) if poi_origin
                        else capture.get('liquidity_tf') in ('W1', 'D1', 'H4', 'H1')
                        and capture.get('liquidity_side') in ('HIGH', 'LOW'))
        if not valid_origin or known is None or not state['start_ts'] < known <= cutoff:
            continue
        cid = capture_id(pair, capture)
        if cid not in state['captures']:
            state['captures'][cid] = dict(capture=copy.deepcopy(capture), plan=None)
            emit(cid, state['captures'][cid], 'POI_HTF_TOCADO' if poi_origin else 'CAPTURA', known_ts=known)

    m15events = snapshots['M15']['events']
    m5events = snapshots['M5']['events']
    for cid, rec in state['captures'].items():
        capture = rec['capture']
        if rec.get('history_lost') or rec.get('origin_invalidated'):
            continue
        origin_zone = capture.get('origin_zone')
        origin_removed = None
        if origin_zone:
            tf = capture['htf_location']['tf']
            duration = {'H1': 3600000, 'H4': 14400000, 'D1': 86400000, 'W1': 604800000}[tf]
            origin_removed = _removed_at(origin_zone, tf_map.get(tf) or [], duration, engine)
        if m15[0]['t'] > min(previous, capture['native_capture_confirm_ts']):
            rec['history_lost'] = True
            emit(cid, rec, 'HISTORICO_INSUFICIENTE')
            continue
        fresh = [e for e in m15events if previous < e['close_ts'] <= cutoff
                 and e['open_ts'] >= capture['native_capture_confirm_ts']
                 and e['level_known_ts'] <= e['open_ts']]
        removals = {}
        # Time-ordered closes: old-plan M5 observation, then native M15 break.
        # Plans discovered late are checked through the remaining closed bars
        # but cannot count historical contacts before actual arming at cutoff.
        for c in m5:
            if c['t'] < previous:
                continue
            closed = c['t']+M5
            plan = rec.get('plan')
            if plan and not plan.get('invalidated'):
                zone = plan.get('m5_zone')
                up = plan['break']['side'] == 'UP'
                touch = bool(zone and c['t'] >= plan['armed_ts']
                             and c['h'] >= zone['bottom'] and c['l'] <= zone['top'])
                invalid = c['l'] < plan['invalidation'] if up else c['h'] > plan['invalidation']
                for name, rows, duration in (('parent_zone', m15, M15), ('m5_zone', m5, M5)):
                    key = (plan['break']['close_ts'], name)
                    if key not in removals:
                        removals[key] = _removed_at(plan.get(name), rows, duration, engine)
                    invalid |= removals[key] is not None and removals[key] <= closed
                contrary = next((e for e in fresh if e['close_ts'] == closed
                                 and e['side'] != plan['break']['side']), None)
                invalid |= contrary is not None
                invalid |= origin_removed is not None and origin_removed <= closed
                if invalid:
                    plan['invalidated'] = True
                    emit(cid, rec, 'TOQUE_E_INVALIDACAO' if touch and not plan.get('touched')
                         else 'PLANO_INVALIDADO', break_ts=plan['break']['close_ts'],
                         candle_ts=c['t'], direction='LONG' if up else 'SHORT')
                elif touch and not plan.get('touched'):
                    plan['touched'] = True
                    plan['retest'] = dict(c)
                    emit(cid, rec, 'RETESTE_OBSERVADO', break_ts=plan['break']['close_ts'],
                         candle_ts=c['t'], zone=zone, direction='LONG' if up else 'SHORT')
            for event in (e for e in fresh if e['close_ts'] == closed):
                if origin_removed is not None and origin_removed <= closed:
                    continue
                direction = 'LONG' if event['side'] == 'UP' else 'SHORT'
                plan = rec.get('plan')
                zone = (plan.get('parent_zone') if plan and not plan.get('invalidated')
                        and plan.get('m5_zone') else _parent_zone(engine, m15, capture, event))
                emit(cid, rec, 'M15_QUEBRA', break_ts=event['close_ts'], direction=direction,
                     kind=event['kind'], level=event['level'],
                     parent_zone=zone if _zone_ok(zone) else None)
                if plan and not plan.get('invalidated') and plan.get('m5_zone'):
                    continue
                plan = dict(break_ts=event['close_ts'], observed_ts=observed_ts, invalidated=False,
                            invalidation=event['opposite_level'],
                            parent_zone=zone if _zone_ok(zone) else None)
                plan['break'] = copy.deepcopy(event)
                rec['plan'] = plan
        if origin_removed is not None and origin_removed <= cutoff:
            rec['origin_invalidated'] = True
            emit(cid, rec, 'ORIGEM_INVALIDADA', candle_ts=origin_removed)
        plan = rec.get('plan')
        if rec.get('origin_invalidated'):
            if plan:
                plan['invalidated'] = True
            continue
        if not plan or plan.get('invalidated') or plan.get('m5_zone'):
            continue
        parent = plan.get('parent_zone')
        if not parent:
            continue
        if _removed_at(parent, m15, M15, engine) is not None:
            plan['invalidated'] = True
            emit(cid, rec, 'PLANO_INVALIDADO', break_ts=plan['break']['close_ts'],
                 direction='LONG' if plan['break']['side'] == 'UP' else 'SHORT')
            continue
        zone, ready = _refinement(engine, m5, capture, plan['break'], parent, m5events)
        if _zone_ok(zone):
            up = plan['break']['side'] == 'UP'
            plan.update(m5_zone=copy.deepcopy(zone), armed_ts=observed_ts, zone_known_ts=ready)
            emit(cid, rec, 'M5_ARMADO', break_ts=plan['break']['close_ts'],
                 direction='LONG' if up else 'SHORT', zone=zone,
                 parent_zone=parent, invalidation=plan['invalidation'])
    state['asof_ts'] = cutoff
    return state, alerts


def _db(path):
    db = sqlite3.connect(path, timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS chain_state (pair TEXT PRIMARY KEY, payload TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS chain_outbox '
               '(seq INTEGER PRIMARY KEY, key TEXT UNIQUE, pair TEXT, payload TEXT, ack INTEGER DEFAULT 0)')
    return db


def scan(pair, tf_map, cutoff, decision, engine, path=DEFAULT_PATH):
    if not enabled() or pair not in PAIRS:
        return None
    with _LOCK, closing(_db(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM chain_state WHERE pair=?', (pair,)).fetchone()
        state = json.loads(row[0]) if row else {}
        captures = list((decision.get('structural_sweep_audit') or {}).get('candidates') or [])
        if os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper() == 'LUDO':
            if state and 'ludo_only_since' not in state:
                state['ludo_only_since'] = cutoff
            baseline = state.get('ludo_only_since', cutoff)
            captures.extend(c for c in decision.get('htf_entry_origins', [])
                            if c['native_capture_confirm_ts'] > baseline)
        updated, events = advance(state, pair, captures, tf_map, cutoff, engine,
                                  observed_ts=int(time.time()*1000))
        if updated and os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper() == 'LUDO':
            updated.setdefault('ludo_only_since', cutoff)
        if updated:
            db.execute('INSERT OR REPLACE INTO chain_state VALUES (?,?)', (pair, json.dumps(updated)))
        for event in events:
            db.execute('INSERT OR IGNORE INTO chain_outbox (key,pair,payload) VALUES (?,?,?)',
                       (event['key'], pair, json.dumps(event)))
        if updated and (not state or events):
            print(f'[KAIROS_CAPTURE_CHAIN] {"baseline" if not state else "advanced"} '
                  f'pair={pair} cutoff={cutoff} captures={len(updated["captures"])} events={len(events)}', flush=True)
        if not updated or updated.get('asof_ts') != cutoff:
            return None
        return dict(pair=pair, events=len(events), asof_ts=updated['asof_ts'], view=state_view(updated))


def state_view(state):
    """Current Ludo provider stage for presentation; never claims an exchange fill."""
    base = dict(model='LUDO', code='AGUARDANDO_ZONA_OU_CAPTURA', direction=None,
                entry_authorized=False, setup=None, capture=None, break_=None)
    records = [(cid, rec) for cid, rec in (state.get('captures') or {}).items()
               if not rec.get('history_lost')]
    if not records:
        return base
    cid, rec = max(records, key=lambda x: x[1]['capture']['native_capture_confirm_ts'])
    base['capture'] = copy.deepcopy(rec['capture'])
    if rec.get('origin_invalidated'):
        base['code'] = 'SETUP_ANTERIOR_INVALIDADO'
        return base
    plan = rec.get('plan')
    if not plan:
        base['code'] = 'AGUARDANDO_CONFIRMACAO_M15'
        return base
    base['m15_break'] = copy.deepcopy(plan['break'])
    if plan.get('invalidated'):
        base['code'] = 'SETUP_ANTERIOR_INVALIDADO'
        return base
    base['direction'] = 'LONG' if plan['break']['side'] == 'UP' else 'SHORT'
    zone = plan.get('m5_zone')
    if not zone:
        base['code'] = 'AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5'
        return base
    base['code'] = 'RETESTE_M5_OBSERVADO' if plan.get('touched') else 'AGUARDANDO_RETESTE_M5'
    if plan.get('touched') and (not plan.get('retest')
            or plan['retest']['t']+M5 < state['asof_ts']):
        base.update(code='RETESTE_HISTORICO_SEM_NOVA_ENTRADA', direction=None)
        return base
    base['setup'] = dict(key=f'LUDO:{cid}:{plan["break"]["close_ts"]}',
        tf='M5', direction=base['direction'], ready_ts=plan['armed_ts'],
        bottom=zone['bottom'], top=zone['top'], zone_type=zone['tipo'],
        level=zone['top'] if base['direction'] == 'LONG' else zone['bottom'],
        poi=copy.deepcopy(zone), retest=copy.deepcopy(plan.get('retest')))
    return base


def message(event):
    c = event['capture']
    stamp = datetime.fromtimestamp(event['observed_ts']/1000, ZoneInfo('Europe/Lisbon')).strftime('%d/%m %H:%M')
    titles = {'CAPTURA': 'Liquidez capturada', 'POI_HTF_TOCADO': 'Interação com FVG HTF',
              'M15_QUEBRA': 'Quebra M15 confirmada',
              'M5_ARMADO': 'Plano M5 observado: aguardar reteste',
              'RETESTE_OBSERVADO': 'Reteste M5 observado', 'PLANO_INVALIDADO': 'Plano invalidado',
              'TOQUE_E_INVALIDACAO': 'Toque e invalidação no mesmo candle',
              'ORIGEM_INVALIDADA': 'FVG HTF de origem invalidado: sequência encerrada',
              'HISTORICO_INSUFICIENTE': 'Histórico insuficiente: sequência encerrada'}
    location = c.get('htf_location') or {}
    origin_line = (f'FVG {location["tf"]} {location["bottom"]:g}–{location["top"]:g}'
                   if c.get('origin_kind') == 'HTF_FVG_TOUCH' else
                   f'{c["liquidity_tf"]} {c["liquidity_type"]} @ {c["nivel"]:g}')
    lines = [f'KAIROS | SEQUÊNCIA CAUSAL LUDO | {event["pair"]}',
             f'{origin_line} | evento {event["capture_id"][:8]}',
             f'{titles[event["stage"]]} | observado {stamp} Portugal']
    if int(time.time()*1000)-event['observed_ts'] > M15:
        lines.append('Entrega atrasada: evento histórico; confirme o estado atual antes de agir.')
    if event.get('direction'):
        lines.append(f'Sentido observado: {event["direction"]}')
    if event['stage'] in ('RETESTE_OBSERVADO', 'TOQUE_E_INVALIDACAO'):
        closed = event['candle_ts']+M5
        when = datetime.fromtimestamp(closed/1000, ZoneInfo('Europe/Lisbon')).strftime('%d/%m %H:%M')
        lines.append(f'Candle M5 confirmado: {when} Portugal.')
        if event['observed_ts']-closed >= M5:
            lines.append('Observação recuperada do histórico; sem nova entrada presumida agora.')
    if event['stage'] == 'CAPTURA':
        lines.append('Aguardar quebra M15 confirmada; captura não confirma reversão.')
    if event['stage'] == 'POI_HTF_TOCADO':
        lines.append('Toque em POI, sem captura de liquidez presumida. Aguardar quebra M15 Ludo.')
    if event['stage'] == 'M15_QUEBRA':
        lines.append(f'{event["kind"]} M15 por fechamento além de {event["level"]:g}; procurar POI causal e refinamento M5.')
        when = datetime.fromtimestamp(event['break_ts']/1000, ZoneInfo('Europe/Lisbon')).strftime('%d/%m %H:%M')
        lines.append(f'Confirmação da quebra: {when} Portugal | estrutura Ludo.')
        if not event.get('parent_zone'):
            lines.append('Ainda sem POI causal M15: nenhum plano M5 armado por esta quebra.')
    if event.get('parent_zone'):
        z = event['parent_zone']
        lines.append(f'POI M15 {z["tipo"]}: {z["bottom"]:g}–{z["top"]:g}')
    if event.get('zone'):
        z = event['zone']
        lines.append(f'M5 {z["tipo"]}: {z["bottom"]:g}–{z["top"]:g}')
    if event['stage'] == 'M5_ARMADO':
        lines.append(f'Aguardar reteste futuro. Extremo estrutural: {event["invalidation"]:g}; invalidação do POI também encerra o plano.')
    if event['stage'] == 'TOQUE_E_INVALIDACAO':
        lines.append('Toque e invalidação no mesmo candle: sequência intrabar desconhecida.')
    lines.append('Observação DEMO/manual. Sem ordem automática ou execução presumida.')
    return html.escape('\n'.join(lines))


def deliver(pair, sender, path=DEFAULT_PATH):
    if not enabled() or pair not in PAIRS:
        return False
    # One delivery worker owns acknowledgements; scanner uses short separate transactions.
    with closing(_db(path)) as db:
        for seq, payload in db.execute('SELECT seq,payload FROM chain_outbox WHERE pair=? AND ack=0 ORDER BY seq', (pair,)).fetchall():
            if sender(message(json.loads(payload))) is not True:
                return False
            db.execute('UPDATE chain_outbox SET ack=1 WHERE seq=?', (seq,))
            db.commit()
            print(f'[KAIROS_CAPTURE_CHAIN] delivered pair={pair} seq={seq}', flush=True)
    return True


def _drain(sender, path):
    global _WORKER
    while True:
        with _QUEUE_LOCK:
            if not _PENDING:
                _WORKER = None
                return
            pair = _PENDING.pop()
        try:
            deliver(pair, sender, path)
        except Exception as exc:
            print(f'[KAIROS_CAPTURE_CHAIN] delivery error pair={pair}: {type(exc).__name__}', flush=True)


def submit(pair, sender, path=DEFAULT_PATH):
    global _WORKER
    if not enabled() or pair not in PAIRS:
        return
    with _QUEUE_LOCK:
        _PENDING.add(pair)
        if _WORKER is None:
            _WORKER = threading.Thread(target=_drain, args=(sender, path), daemon=True)
            _WORKER.start()
