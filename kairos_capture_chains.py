"""Durable Ludo observations qualified by closed reaction episodes.

Ludo supplies structure; existing engine functions supply causal FVG/OBs.
State/outbox is separate from legacy setup watermarks. Never submits orders.
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
from kairos_reaction_episode import reaction_timeline, evidence_at, touch_episode, entry_direction_allowed, entry_reaction_allowed

M5 = 300000
M15 = 900000
CAUSAL_POLICY_VERSION = 2
LIQUIDITY_ENTRY_POLICY = 'RECLAIM_ONLY_V1'
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
    if state and (cutoff < state['asof_ts'] or (cutoff == state['asof_ts']
            and state.get('causal_policy_version') == CAUSAL_POLICY_VERSION)):
        return original, []
    m15 = _closed(tf_map.get('M15', []), M15, cutoff)
    m5 = _closed(tf_map.get('M5', []), M5, cutoff)
    if not m15 or not m5:
        return original, []
    snapshots = {tf: structure_snapshot(cs, d, cutoff)
                 for tf, cs, d in (('M15', m15, M15), ('M5', m5, M5))}
    if any(s['status'] != 'OK' for s in snapshots.values()):
        return original, []
    if state and state.get('causal_policy_version') != CAUSAL_POLICY_VERSION:
        # Existing plans were not reaction-qualified. Rebase only on validated
        # closed data; never adopt their old plans or historical retests.
        return dict(start_ts=cutoff, asof_ts=cutoff, captures={},
                    causal_policy_version=CAUSAL_POLICY_VERSION,
                    migrated_from_unqualified_state=True,
                    ludo_only_since=cutoff), []
    if not state:
        return dict(start_ts=cutoff, asof_ts=cutoff, captures={},
                    causal_policy_version=CAUSAL_POLICY_VERSION), []
    state = original
    previous = state['asof_ts']
    alerts = []

    def emit(cid, rec, stage, **facts):
        event = dict(pair=pair, capture_id=cid, stage=stage, observed_ts=observed_ts,
                     causal_policy_version=CAUSAL_POLICY_VERSION,
                     capture=copy.deepcopy(rec['capture']), fill_assumed=False,
                     reaction=copy.deepcopy(rec.get('reaction')),
                     episode_key=(rec.get('episode') or {}).get('key'),
                     entry_authorized=False, **facts)
        # Presentation facts only: record the existing plan before any reset.
        plan=rec.get('plan')
        if plan and (stage!='M15_QUEBRA' or facts.get('break_ts')==plan['break']['close_ts']):
            def zone_view(z):
                return {k:z.get(k) for k in ('id','tipo','bottom','top','created_ts','flip_ts','t','break_ts')} if z else None
            event['plan_details']=dict(m15_break=copy.deepcopy(plan['break']),
                parent_zone=zone_view(plan.get('parent_zone')),m5_zone=zone_view(plan.get('m5_zone')),
                invalidation=plan.get('invalidation'),had_retest=bool(plan.get('touched')))
        event['key'] = hashlib.sha256(json.dumps(
            [cid, stage, facts.get('break_ts'), facts.get('candle_ts'), cutoff],
            separators=(',', ':')).encode()).hexdigest()
        alerts.append(event)

    for capture in captures:
        known = capture.get('native_capture_confirm_ts')
        poi_origin = capture.get('origin_kind') == 'HTF_FVG_TOUCH'
        ludo_origin = (capture.get('origin_kind') == 'LUDO_SWING_CAPTURE'
                       and capture.get('structural_source') == 'LUDO'
                       and capture.get('liquidity_tf') in ('H4','H1','M15')
                       and capture.get('liquidity_type') in ('LUDO_SWING_HIGH','LUDO_SWING_LOW')
                       and capture.get('liquidity_confirm_ts') is not None
                       and capture['liquidity_confirm_ts'] <= capture.get('first_capture_ts',-1)
                       and capture.get('liquidity_side') in ('HIGH','LOW'))
        valid_origin = (bool(capture.get('origin_key') and capture.get('htf_location')) if poi_origin
                        else ludo_origin if capture.get('origin_kind') == 'LUDO_SWING_CAPTURE'
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
        old_plan = rec.get('plan')
        if old_plan and not old_plan.get('invalidated'):
            old_direction = 'LONG' if old_plan['break']['side'] == 'UP' else 'SHORT'
            if not entry_direction_allowed(capture, old_direction):
                old_plan['invalidated'] = True
                emit(cid, rec, 'PLANO_INVALIDADO', break_ts=old_plan['break']['close_ts'],
                     candle_ts=cutoff-M5, direction=old_direction, reason='ENTRY_POLICY_CHANGED',
                     invalidation_reasons=[dict(code='ENTRY_POLICY_CHANGED', confirmed_ts=cutoff)])
        if rec.get('history_lost') or rec.get('origin_invalidated'):
            continue
        origin_zone = capture.get('origin_zone')
        origin_removed = None
        if origin_zone:
            tf = capture['htf_location']['tf']
            duration = {'H1': 3600000, 'H4': 14400000, 'D1': 86400000, 'W1': 604800000}[tf]
            origin_removed = _removed_at(origin_zone, tf_map.get(tf) or [], duration, engine)
        required_start = (capture['native_capture_confirm_ts'] if capture.get('origin_kind') == 'HTF_FVG_TOUCH'
                          else capture['first_capture_ts'])
        if m15[0]['t'] > min(previous, required_start):
            rec['history_lost'] = True
            emit(cid, rec, 'HISTORICO_INSUFICIENTE')
            continue
        fresh = [e for e in m15events if previous < e['close_ts'] <= cutoff
                 and e['open_ts'] >= capture['native_capture_confirm_ts']
                 and e['level_known_ts'] <= e['open_ts']]
        timeline = reaction_timeline(m15, capture)
        origin_reaction = touch_episode(capture) if capture.get('origin_kind') == 'HTF_FVG_TOUCH' else None
        removals = {}
        # Time-ordered closes: old-plan M5 observation, then native M15 break.
        # Plans discovered late are checked through the remaining closed bars
        # but cannot count historical contacts before actual arming at cutoff.
        for c in m5:
            if c['t'] < previous:
                continue
            closed = c['t']+M5
            reaction = (origin_reaction if origin_reaction and origin_reaction['confirmed_ts'] <= closed
                        else evidence_at(timeline, closed))
            if reaction and reaction['key'] != (rec.get('episode') or {}).get('key'):
                old_plan = rec.get('plan')
                if old_plan and not old_plan.get('invalidated'):
                    old_plan['invalidated'] = True
                    zone = old_plan.get('m5_zone')
                    ambiguous = bool(zone and not old_plan.get('touched') and c['t'] >= old_plan['armed_ts']
                                     and c['h'] >= zone['bottom'] and c['l'] <= zone['top'])
                    emit(cid, rec, 'TOQUE_E_INVALIDACAO' if ambiguous else 'PLANO_INVALIDADO',
                         break_ts=old_plan['break']['close_ts'], candle_ts=c['t'],
                         direction='LONG' if old_plan['break']['side'] == 'UP' else 'SHORT',
                         reason='CONFIRMED_REACTION_CHANGED',new_reaction=copy.deepcopy(reaction),
                         invalidation_reasons=[dict(code='CONFIRMED_REACTION_CHANGED',
                             level=capture.get('nivel'),confirmed_ts=reaction['confirmed_ts'])])
                rec['reaction'] = copy.deepcopy(reaction)
                rec['episode'] = dict(key=reaction['key'], consumed=False)
                rec['plan'] = None
            plan = rec.get('plan')
            if plan and not plan.get('invalidated'):
                zone = plan.get('m5_zone')
                up = plan['break']['side'] == 'UP'
                touch = bool(zone and c['t'] >= plan['armed_ts']
                             and c['h'] >= zone['bottom'] and c['l'] <= zone['top'])
                invalid = c['l'] < plan['invalidation'] if up else c['h'] > plan['invalidation']
                reasons=[]
                if invalid:reasons.append(dict(code='STRUCTURAL_EXTREME_BREACHED',
                    level=plan['invalidation'],confirmed_ts=closed))
                for name, rows, duration in (('parent_zone', m15, M15), ('m5_zone', m5, M5)):
                    key = (plan['break']['close_ts'], name)
                    if key not in removals:
                        removals[key] = _removed_at(plan.get(name), rows, duration, engine)
                    invalid |= removals[key] is not None and removals[key] <= closed
                    if removals[key] is not None and removals[key] <= closed:
                        reasons.append(dict(code='PARENT_POI_INVALIDATED' if name=='parent_zone' else 'M5_POI_INVALIDATED',
                                            confirmed_ts=removals[key]))
                contrary = next((e for e in fresh if e['close_ts'] == closed
                                 and e['side'] != plan['break']['side']), None)
                invalid |= contrary is not None
                invalid |= origin_removed is not None and origin_removed <= closed
                if contrary is not None:reasons.insert(0,dict(code='OPPOSITE_M15_BREAK',
                    kind=contrary['kind'],level=contrary['level'],close=contrary.get('close'),
                    confirmed_ts=contrary['close_ts']))
                if origin_removed is not None and origin_removed <= closed:
                    reasons.append(dict(code='HTF_ORIGIN_INVALIDATED',confirmed_ts=origin_removed))
                if invalid:
                    plan['invalidated'] = True
                    emit(cid, rec, 'TOQUE_E_INVALIDACAO' if touch and not plan.get('touched')
                         else 'PLANO_INVALIDADO', break_ts=plan['break']['close_ts'],
                         candle_ts=c['t'], direction='LONG' if up else 'SHORT',
                         reason=reasons[0]['code'],invalidation_reasons=reasons)
                elif touch and not plan.get('touched'):
                    plan['touched'] = True
                    plan['retest'] = dict(c)
                    emit(cid, rec, 'RETESTE_OBSERVADO', break_ts=plan['break']['close_ts'],
                         candle_ts=c['t'], zone=zone, direction='LONG' if up else 'SHORT')
            for event in (e for e in fresh if e['close_ts'] == closed):
                if origin_removed is not None and origin_removed <= closed:
                    continue
                direction = 'LONG' if event['side'] == 'UP' else 'SHORT'
                episode = rec.get('episode')
                reaction = rec.get('reaction')
                if not episode or episode.get('consumed') or not reaction:
                    continue
                if reaction['confirmed_ts'] > closed or not entry_reaction_allowed(capture, reaction, direction):
                    continue
                candle = next(row for row in m15 if row['t'] == event['open_ts'])
                if capture.get('origin_kind') != 'HTF_FVG_TOUCH':
                    if not (candle['c'] > capture['nivel'] if direction == 'LONG' else candle['c'] < capture['nivel']):
                        continue
                causal_capture = {**capture, 'first_capture_ts':reaction['leg_start_ts'],
                                  'sweep_ts':reaction['leg_start_ts']}
                zone = _parent_zone(engine, m15, causal_capture, event)
                episode.update(consumed=True, break_ts=event['close_ts'])
                emit(cid, rec, 'M15_QUEBRA', break_ts=event['close_ts'], direction=direction,
                     kind=event['kind'], level=event['level'],
                     parent_zone=zone if _zone_ok(zone) else None,invalidation=event['opposite_level'])
                plan = dict(break_ts=event['close_ts'], observed_ts=observed_ts, invalidated=False,
                            invalidation=event['opposite_level'],
                            episode_key=episode['key'], causal_start_ts=reaction['leg_start_ts'],
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
                 direction='LONG' if plan['break']['side'] == 'UP' else 'SHORT',
                 reason='PARENT_POI_INVALIDATED',invalidation_reasons=[dict(code='PARENT_POI_INVALIDATED',
                     confirmed_ts=_removed_at(parent,m15,M15,engine))])
            continue
        causal_capture = {**capture, 'first_capture_ts':plan['causal_start_ts'],
                          'sweep_ts':plan['causal_start_ts']}
        zone, ready = _refinement(engine, m5, causal_capture, plan['break'], parent, m5events)
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
    db.execute('CREATE TABLE IF NOT EXISTS chain_notice_context (pair TEXT PRIMARY KEY,payload TEXT)')
    return db


def scan(pair, tf_map, cutoff, decision, engine, path=DEFAULT_PATH):
    if not enabled() or pair not in PAIRS:
        return None
    with _LOCK, closing(_db(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM chain_state WHERE pair=?', (pair,)).fetchone()
        state = json.loads(row[0]) if row else {}
        captures = list((decision.get('structural_sweep_audit') or {}).get('candidates') or [])
        recent_since = state.get('ludo_liquidity_since',cutoff)
        if os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper() == 'LUDO':
            if state and 'ludo_only_since' not in state:
                state['ludo_only_since'] = cutoff
            baseline = state.get('ludo_only_since', cutoff)
            captures.extend(c for c in decision.get('htf_entry_origins', [])
                            if c['native_capture_confirm_ts'] > baseline)
            captures.extend(c for c in decision.get('ludo_liquidity_origins', [])
                            if c['native_capture_confirm_ts'] > recent_since)
        observed_now=int(time.time()*1000)
        updated, events = advance(state, pair, captures, tf_map, cutoff, engine,
                                  observed_ts=observed_now)
        if updated and updated.get('asof_ts')==cutoff:
            from kairos_early_m5 import observe
            updated,early_events=observe(updated,pair,tf_map,cutoff,observed_now)
            events.extend(early_events)
        if updated and os.environ.get('KAIROS_ENTRY_STRUCTURE', '').strip().upper() == 'LUDO':
            updated.setdefault('ludo_only_since', cutoff)
            # Detector rollout is prospective. Persist independently of the
            # older chain baseline; restarts must not resurrect old captures.
            updated.setdefault('ludo_liquidity_since', recent_since)
        if updated:
            if state.get('liquidity_entry_policy') != LIQUIDITY_ENTRY_POLICY:
                print(f'[KAIROS_CAPTURE_CHAIN] entry_policy pair={pair} policy={LIQUIDITY_ENTRY_POLICY} acceptance_context_only=true',flush=True)
            updated['liquidity_entry_policy'] = LIQUIDITY_ENTRY_POLICY
            db.execute('INSERT OR REPLACE INTO chain_state VALUES (?,?)', (pair, json.dumps(updated)))
        if updated and updated.get('asof_ts')==cutoff:
            from kairos_notice_context import context_snapshot,radar_notice,_valid_radar
            prior=db.execute('SELECT payload FROM chain_notice_context WHERE pair=?',(pair,)).fetchone()
            previous_context=json.loads(prior[0]) if prior else {}
            notice_since=previous_context.get('notice_since',cutoff)
            radars=decision.get('radar_context') or []
            context=context_snapshot(previous_context,updated,radars,cutoff)
            context.setdefault('notice_since',notice_since)
            db.execute('INSERT OR REPLACE INTO chain_notice_context VALUES(?,?)',(pair,json.dumps(context)))
            if os.environ.get('KAIROS_ENTRY_STRUCTURE','').strip().upper()=='LUDO':
                for r in radars:
                    if not (_valid_radar(r,cutoff) and r.get('key') and r['timestamp']>notice_since):continue
                    notice=radar_notice(pair,r,observed_now,CAUSAL_POLICY_VERSION)
                    if db.execute('SELECT 1 FROM chain_outbox WHERE key=?',(notice['key'],)).fetchone() is None:
                        events.append(notice)
        for event in events:
            db.execute('INSERT OR IGNORE INTO chain_outbox (key,pair,payload) VALUES (?,?,?)',
                       (event['key'], pair, json.dumps(event)))
        if updated and (not state or events):
            print(f'[KAIROS_CAPTURE_CHAIN] {"baseline" if not state else "advanced"} '
                  f'pair={pair} cutoff={cutoff} captures={len(updated["captures"])} events={len(events)}', flush=True)
        if state and state.get('causal_policy_version') != CAUSAL_POLICY_VERSION:
            print(f'[KAIROS_CAUSAL_POLICY] rebase pair={pair} version={CAUSAL_POLICY_VERSION} '
                  f'cutoff={cutoff} previous_unqualified_plans_not_adopted=true', flush=True)
        if not updated or updated.get('asof_ts') != cutoff:
            return None
        return dict(pair=pair, events=len(events), asof_ts=updated['asof_ts'], view=state_view(updated))


def state_view(state):
    """Current Ludo provider stage for presentation; never claims an exchange fill."""
    base = dict(model='LUDO', code='AGUARDANDO_ZONA_OU_CAPTURA', direction=None,
                entry_authorized=False, setup=None, capture=None, break_=None)
    if state and state.get('causal_policy_version') != CAUSAL_POLICY_VERSION:
        base['code'] = 'AGUARDANDO_REINICIALIZACAO_CAUSAL'
        return base
    records = [(cid, rec) for cid, rec in (state.get('captures') or {}).items()
               if not rec.get('history_lost')]
    if not records:
        return base
    def relevance(item):
        rec = item[1]
        plan = rec.get('plan') or {}
        active = not (rec.get('origin_invalidated') or plan.get('invalidated'))
        if plan and not entry_direction_allowed(rec['capture'], 'LONG' if plan['break']['side']=='UP' else 'SHORT'):
            active = False
        rank = (4 if active and plan.get('m5_zone') and not plan.get('touched') else
                3 if active and plan and not plan.get('touched') else
                2 if active and plan else 1 if active else 0)
        return rank, plan.get('break_ts', 0), rec['capture']['native_capture_confirm_ts']
    cid, rec = max(records, key=relevance)
    base['capture'] = copy.deepcopy(rec['capture'])
    if rec.get('origin_invalidated'):
        base['code'] = 'SETUP_ANTERIOR_INVALIDADO'
        return base
    plan = rec.get('plan')
    if not plan:
        base['code'] = ('AGUARDANDO_CONFIRMACAO_M15' if rec.get('reaction')
                        else 'AGUARDANDO_REACAO_CONFIRMADA')
        return base
    base['m15_break'] = copy.deepcopy(plan['break'])
    if plan.get('invalidated') or not entry_direction_allowed(rec['capture'], 'LONG' if plan['break']['side']=='UP' else 'SHORT'):
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
    from kairos_alert_presentation import format_chain_events
    return '\n\n'.join(format_chain_events([event]))


def _entry_notice_allowed(event):
    if event.get('stage') not in ('M15_QUEBRA', 'M5_ARMADO', 'RETESTE_OBSERVADO', 'M5_ANTECIPADO'):
        return True
    return entry_direction_allowed(event.get('capture') or {}, event.get('direction'))


def deliver(pair, sender, path=DEFAULT_PATH):
    if not enabled() or pair not in PAIRS:
        return False
    from kairos_alert_presentation import bundle_events
    from kairos_notice_context import update_groups,format_pair_update
    # Freeze pages before sending. Original events remain the audit record.
    # Receipts are per page; source events are acknowledged only at completion.
    with closing(_db(path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS chain_delivery_batches '
                   '(key TEXT PRIMARY KEY,pair TEXT,pages TEXT,seqs TEXT,acknowledged INTEGER DEFAULT 0,complete INTEGER DEFAULT 0,observed_ts INTEGER,policy_version INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        # A frozen page can mix origins. Stop its remaining pages if any source
        # is forbidden; keep receipts and audit, never replay delivered pages.
        for key, source in db.execute('SELECT key,seqs FROM chain_delivery_batches WHERE pair=? AND complete=0', (pair,)).fetchall():
            seqs=json.loads(source)
            sources=[db.execute('SELECT payload FROM chain_outbox WHERE seq=?',(seq,)).fetchone() for seq in seqs]
            if any(row and not _entry_notice_allowed(json.loads(row[0])) for row in sources):
                db.executemany('UPDATE chain_outbox SET ack=2 WHERE seq=? AND ack=0',[(seq,) for seq in seqs])
                db.execute('UPDATE chain_delivery_batches SET complete=1 WHERE key=?',(key,))
                print(f'[KAIROS_CAPTURE_CHAIN] suppressed_entry_policy pair={pair} batch={key[:8]}',flush=True)
        existing=db.execute('SELECT seqs FROM chain_delivery_batches WHERE pair=? AND complete=0',(pair,)).fetchall()
        assigned={seq for row in existing for seq in json.loads(row[0])}
        pending=[]
        for seq,payload in db.execute('SELECT seq,payload FROM chain_outbox WHERE pair=? AND ack=0 ORDER BY seq',(pair,)).fetchall():
            event=json.loads(payload)
            if event.get('causal_policy_version') != CAUSAL_POLICY_VERSION or not _entry_notice_allowed(event):
                db.execute('UPDATE chain_outbox SET ack=2 WHERE seq=?',(seq,))
                print(f'[KAIROS_CAPTURE_CHAIN] suppressed_unqualified pair={pair} seq={seq}',flush=True)
            elif seq not in assigned:
                pending.append((seq,event))
        context_row=db.execute('SELECT payload FROM chain_notice_context WHERE pair=?',(pair,)).fetchone()
        context=json.loads(context_row[0]) if context_row else {}
        for group in update_groups(bundle_events(pending)):
            key=hashlib.sha256(json.dumps([pair,group['seqs']],separators=(',',':')).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO chain_delivery_batches(key,pair,pages,seqs,observed_ts,policy_version) VALUES(?,?,?,?,?,?)',
                       (key,pair,json.dumps(format_pair_update(pair,group['groups'],context),ensure_ascii=False),
                        json.dumps(group['seqs']),max(e['observed_ts'] for g in group['groups'] for e in g['events']),CAUSAL_POLICY_VERSION))
        db.commit()
        batches=db.execute('SELECT key,pages,seqs,acknowledged,observed_ts,policy_version FROM chain_delivery_batches WHERE pair=? AND complete=0 ORDER BY rowid',(pair,)).fetchall()
        for key,payload,source,acknowledged,observed,policy in batches:
            pages=json.loads(payload);seqs=json.loads(source)
            if policy!=CAUSAL_POLICY_VERSION:
                db.executemany('UPDATE chain_outbox SET ack=2 WHERE seq=?',[(seq,) for seq in seqs])
                db.execute('UPDATE chain_delivery_batches SET complete=1 WHERE key=?',(key,))
                db.commit()
                print(f'[KAIROS_CAPTURE_CHAIN] suppressed_unqualified pair={pair} batch={key[:8]}',flush=True)
                continue
            while acknowledged < len(pages):
                page=pages[acknowledged]
                if observed is not None and int(time.time()*1000)-observed>M15 and 'Aviso histórico' not in page:
                    page='⚠️ Entrega atrasada — evento histórico; confirme o estado atual antes de agir.\n'+page
                if sender(page) is not True:
                    return False
                acknowledged+=1
                db.execute('UPDATE chain_delivery_batches SET acknowledged=? WHERE key=?',(acknowledged,key))
                if acknowledged==len(pages):
                    db.executemany('UPDATE chain_outbox SET ack=1 WHERE seq=?',[(seq,) for seq in seqs])
                    db.execute('UPDATE chain_delivery_batches SET complete=1 WHERE key=?',(key,))
                db.commit()
                print(f'[KAIROS_CAPTURE_CHAIN] delivered pair={pair} batch={key[:8]} page={acknowledged}/{len(pages)} events={len(seqs)}',flush=True)
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
