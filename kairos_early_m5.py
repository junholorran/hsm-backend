"""Prospective M5 structure observations; never authorizes or consumes M15 plans."""
import copy
import hashlib
import json

from kairos_ludo_structure import structure_snapshot
from kairos_reaction_episode import reaction_timeline, evidence_at, touch_episode

M5=300000
M15=900000


def observe(state,pair,tf_map,cutoff,observed_ts):
    from kairos_capture_chains import _closed,CAUSAL_POLICY_VERSION
    original=copy.deepcopy(state)
    m5=_closed(tf_map.get('M5',[]),M5,cutoff)
    m15=_closed(tf_map.get('M15',[]),M15,cutoff)
    if not m5 or not m15:return original,[]
    if 'early_m5' not in original:
        original['early_m5']=dict(since=cutoff,asof_ts=cutoff,records={})
        return original,[]
    tracking=original['early_m5']; previous=tracking['asof_ts']
    if cutoff<=previous:return original,[]
    snapshot=structure_snapshot(m5,M5,cutoff)
    if snapshot['status']!='OK':return original,[]
    events={e['close_ts']:e for e in snapshot['events']
            if e['close_ts']-e['open_ts']==M5 and e['level_known_ts']<=e['open_ts']}
    notices=[]

    def emit(cid,rec,record,stage,closed,reason=None):
        br=record['break']; direction='LONG' if br['side']=='UP' else 'SHORT'
        key=['EARLY_M5',pair,cid,record['episode_key'],br['close_ts'],stage]
        notice=dict(key=hashlib.sha256(json.dumps(key).encode()).hexdigest(),
            pair=pair,capture_id=cid,stage=stage,observed_ts=observed_ts,
            causal_policy_version=CAUSAL_POLICY_VERSION,capture=copy.deepcopy(rec['capture']),
            reaction=copy.deepcopy(record['reaction']),episode_key=record['episode_key'],
            break_ts=br['close_ts'],kind=br['kind'],level=br['level'],close=br['close'],
            direction=direction,m5_break=copy.deepcopy(br),confirmation_tf='M5',
            entry_authorized=False,fill_assumed=False,confirmation_ts=closed)
        if reason:
            level=br['opposite_level'] if reason=='STRUCTURAL_EXTREME_BREACHED' else rec['capture'].get('nivel')
            notice.update(reason=reason,invalidation_reasons=[dict(code=reason,confirmed_ts=closed,level=level)])
        notices.append(notice)

    for cid,rec in original.get('captures',{}).items():
        capture=rec['capture']; record=tracking['records'].get(cid)
        if rec.get('history_lost') or rec.get('origin_invalidated') or m5[0]['t']>min(previous,capture['first_capture_ts']):
            if record and not record.get('ended'):
                reason='HTF_ORIGIN_INVALIDATED' if rec.get('origin_invalidated') else 'HISTORY_LOST'
                record['ended']=reason
                emit(cid,rec,record,'M5_ANTECIPADO_INVALIDADO',cutoff,reason)
            continue
        timeline=reaction_timeline(m15,capture)
        touch=touch_episode(capture) if capture.get('origin_kind')=='HTF_FVG_TOUCH' else None
        for candle in m5:
            closed=candle['t']+M5
            if closed<=previous:continue
            reaction=touch if touch and touch['confirmed_ts']<=closed else evidence_at(timeline,closed)
            event=events.get(closed)
            plan=rec.get('plan') or {}
            if record and not record.get('ended'):
                br=record['break'];up=br['side']=='UP'
                reason=None
                if reaction and reaction['key']!=record['episode_key']:
                    reason='CONFIRMED_REACTION_CHANGED'
                elif (candle['l']<br['opposite_level'] if up else candle['h']>br['opposite_level']):
                    reason='STRUCTURAL_EXTREME_BREACHED'
                elif event and event['side']!=br['side']:
                    reason='OPPOSITE_M5_BREAK'
                if reason:
                    record['ended']=reason
                    emit(cid,rec,record,'M5_ANTECIPADO_INVALIDADO',closed,reason)
                # M15 takes over only its own qualified episode. A contrary
                # M5 extreme on this same bar must still close the old notice.
                elif (plan.get('episode_key')==record['episode_key']
                        and plan['break']['close_ts']<=closed):
                    record['ended']='M15_CONFIRMED'
            if not event or not reaction:continue
            if (closed<=tracking['since'] or event['open_ts']<capture['native_capture_confirm_ts']
                    or reaction['confirmed_ts']>closed):continue
            direction='LONG' if event['side']=='UP' else 'SHORT'
            if reaction['direction']!=direction:continue
            # Outside bars can break both structural sides. Without intrabar
            # ordering, do not announce a fresh valid early hypothesis.
            if (candle['l']<event['opposite_level'] if direction=='LONG'
                    else candle['h']>event['opposite_level']):continue
            if capture.get('origin_kind')!='HTF_FVG_TOUCH':
                if not (candle['c']>capture['nivel'] if direction=='LONG' else candle['c']<capture['nivel']):continue
            if record and record['episode_key']==reaction['key']:continue
            if plan.get('episode_key')==reaction['key'] and plan['break']['close_ts']<=closed:continue
            record={'episode_key':reaction['key'],'reaction':copy.deepcopy(reaction),'break':copy.deepcopy(event)}
            tracking['records'][cid]=record
            emit(cid,rec,record,'M5_ANTECIPADO',closed)
    tracking['asof_ts']=cutoff
    return original,notices
