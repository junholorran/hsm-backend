"""Observe previously selected M5 POIs without changing any detector or selector.

An armed snapshot is evidence of a prospective level, never of a live fill.
Native removal and geometric contact on the same bar remain unresolved.
"""
from copy import deepcopy


def _thesis(setup):
    location=setup.get('htf_location') or {}
    return tuple(setup.get(k) for k in ('direction','capture_tf','capture_level','major_confirm_ts','internal_confirm_ts','origin_kind')) + tuple(location.get(k) for k in ('tf','bottom','top'))


def remember_armed(state, rec, result, setup, observed_ts):
    if rec.get('armed_snapshot'):
        return
    previous=[v for k,v in state.items() if k!=setup['key'] and v.get('armed_snapshot') and
              _thesis(v['armed_snapshot']['entry_setup'])==_thesis(setup)]
    if previous:
        prev=max(previous,key=lambda v:v.get('armed_observed_ts',0))
        old=prev['armed_snapshot']['entry_setup']
        reason='PRIOR_POI_REMOVED' if prev.get('retired_reason')=='NATIVE_POI_REMOVED' else 'CURRENT_SELECTOR_CHANGED'
        rec['selection_change']=dict(previous_level=old['level'],previous_bottom=old['bottom'],previous_top=old['top'],reason=reason)
    for prev in previous:
        prev['retired']=True
        prev.setdefault('retired_reason','REPLACED_BY_CURRENT_SELECTION')
    fields=('direction','choch_timestamp','entry_audit_only','m15_major_confirmation_close_ts',
            'm15_internal_confirmation_close_ts','m15_internal_confirmation_open_ts','authorization_path')
    snapshot={k:deepcopy(result.get(k)) for k in fields}
    snapshot.update(entry_setup=deepcopy(setup),entry=setup['level'],entry_tf=setup['tf'],
                    zone_type=setup.get('zone_type'),zone_bottom=setup['bottom'],zone_top=setup['top'],
                    zone_created_ts=(setup.get('poi') or {}).get('created_ts'),fill_assumed=False)
    rec.update(armed_snapshot=snapshot,armed_observed_ts=int(observed_ts))


def observe_armed(state, current, m5, cutoff, engine):
    """Preserve first contact of the frozen level before current selection changes.

    Only closed bars opening after the real observation of arming qualify.
    Removals before contact retire the snapshot. A different structural thesis
    retires it too. No newly discovered level becomes retrospectively armed.
    """
    zones=None
    for rec in list(state.values()):
        saved=rec.get('armed_snapshot')
        if not saved or not rec.get('armed') or rec.get('touched') or rec.get('retired'):
            continue
        setup=saved['entry_setup'];poi=setup.get('poi') or {}
        if setup.get('tf')!='M5':continue
        candidate=current.get('entry_setup') or {}
        thesis_changed=bool(candidate and _thesis(candidate)!=_thesis(setup))
        changed_at=max(candidate.get('major_confirm_ts') or 0,candidate.get('internal_confirm_ts') or 0) or cutoff
        start=max(setup['ready_ts'],rec['armed_observed_ts'])
        removed=poi.get('capacity_removed_ts') or poi.get('invalidated_ts')
        if str(setup.get('zone_type','')).startswith(('FVG_','IFVG_')):
            if zones is None:zones=engine._kairos_fvg_states(m5,lookback=len(m5))
            native=next((z for z in zones if z.get('id')==poi.get('id') and poi.get('id')),None)
            if native is None:
                rec.update(retired=True,retired_reason='NATIVE_LIFECYCLE_UNAVAILABLE');continue
            if native.get('invalidated_ts') is not None:removed=native['invalidated_ts']
        if str(setup.get('zone_type','')).startswith('OB_') and poi.get('refinement_basis')=='M15_AUTHORIZED_M5_CONTINUATION_BOS_OB':
            records=engine._kairos_order_blocks_map(m5,swing_size=5,lookback=len(m5))
            native=next((z for z in records if z.get('t')==poi.get('t') and z.get('break_ts')==poi.get('break_ts')),None)
            if native is None:
                rec.update(retired=True,retired_reason='NATIVE_LIFECYCLE_UNAVAILABLE');continue
            removed=native.get('removed_ts') or native.get('invalidated_ts') or removed
        for c in m5:
            if c['t']<start//300000*300000 or c['t']+300000>cutoff:continue
            if thesis_changed and c['t']>=changed_at:
                rec.update(retired=True,retired_reason='STRUCTURAL_THESIS_CHANGED');break
            if removed is not None and removed<c['t']:
                rec.update(retired=True,retired_reason='NATIVE_POI_REMOVED');break
            invalid=removed==c['t']
            if str(setup.get('zone_type','')).startswith('OB_'):
                from kairos_lux_ob import ob_invalidated
                invalid=invalid or ob_invalidated(poi,c)
            touch=c['t']>=start and c['h']>=setup['bottom'] and c['l']<=setup['top']
            if touch:
                signal=deepcopy(saved)
                signal.update(entry_audit_only=True,entry_observation_confirmed=True,armed_before_retest=True,
                              armed_observed_ts=rec['armed_observed_ts'],observed_retest_close=c['c'],
                              retest_open_ts=c['t'],retest_confirm_close_ts=c['t']+300000,
                              poi_invalidated_same_bar=bool(invalid),authorization_not_current=(not bool(candidate) or (thesis_changed and c['t']+300000>=changed_at)),
                              armed_alert_delivered_before_retest=bool(rec.get('armed_alert_delivered_ts') is not None and rec['armed_alert_delivered_ts']<=c['t']),
                              intrabar_sequence='UNRESOLVED' if invalid else 'CLOSED_BAR_CONTACT',fill_assumed=False)
                signal['entry_setup']['retest']=dict(c)
                rec.update(touched=True,signal_snapshot=signal)
                if invalid:rec['retired_reason']='NATIVE_POI_REMOVED'
                break
            if invalid:
                rec.update(retired=True,retired_reason='NATIVE_POI_REMOVED');break

print('[KAIROS_ARMED_POI_TRACKING] installed=true preserve_prearmed_contact=true no_fill_assumed=true detectors_unchanged=true', flush=True)
