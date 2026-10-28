"""User-supplied Pine boundaries; deliberately distinguish Ludo from Lux."""
import scalp_engine as engine


def rows():
    return [dict(t=0,o=100,h=101,l=99,c=100),
            dict(t=900000,o=100,h=106,l=100,c=100),
            dict(t=1800000,o=105,h=107,l=104,c=106)]


def mother(cs, **kw):
    from kairos_ludo_fvg import fvg_states
    return next(z for z in fvg_states(cs, **kw) if z['source_mid']['t']==900000)


def test_live_provider_uses_ludo_geometry_without_lux_middle_filter():
    z=next((z for z in engine._kairos_fvg_states(rows()) if z.get('ludo_fvg')),None)
    assert z is not None
    assert (z['bottom'],z['top'],z['created_ts'])==(101,104,1800000)
    assert z.get('lux_fvg') is False


def test_near_edge_equality_untouched_far_edge_equality_removed():
    cs=rows()+[dict(t=2700000,o=105,h=106,l=104,c=105)]
    assert mother(cs)['state']=='ATIVA'
    cs.append(dict(t=3600000,o=104,h=105,l=103,c=104))
    assert mother(cs)['state']=='TOCADA'
    cs.append(dict(t=4500000,o=103,h=104,l=101,c=103))
    z=mother(cs)
    assert z['state']=='INVALIDADA'
    assert z['invalidated_ts']==4500000
    assert z['invalidation_basis']=='LUDO_FULL_MITIGATION'


def test_bearish_mirrors_geometry_and_full_boundary():
    cs=[dict(r,o=200-r['o'],h=200-r['l'],l=200-r['h'],c=200-r['c']) for r in rows()]
    cs.append(dict(t=2700000,o=97,h=99,l=96,c=98))
    z=mother(cs)
    assert (z['bottom'],z['top'],z['state'])==(96,99,'INVALIDADA')


def test_reduction_is_optional_and_preserves_original_boundaries():
    cs=rows()+[dict(t=2700000,o=105,h=106,l=103,c=105)]
    assert mother(cs)['top']==104
    z=mother(cs,reduce_mitigated=True)
    assert (z['top'],z['original_top'],z['bottom'])==(103,104,101)


def test_c_must_exist_and_history_literal_limit_is_six():
    from kairos_ludo_fvg import fvg_states
    assert fvg_states(rows()[:2])==[]
    cs=[dict(t=i*900000,o=100+3*i,h=101+3*i,l=99+3*i,c=100+3*i) for i in range(12)]
    zs=fvg_states(cs)
    assert sum(z['state']!='INVALIDADA' for z in zs)==6
    assert any(z.get('invalidation_basis')=='LUDO_HISTORY_EVICTION' for z in zs)


def test_live_h4_touch_is_context_alert_even_without_entry_origin(monkeypatch):
    from unittest.mock import patch
    import kairos_ludo_entry as provider
    import kairos_capture_chains as chains
    import kairos_daily_delivery as daily
    cut=14400000
    zone=dict(id='h4',tipo='FVG_bearish',ludo_fvg=True,state='ATIVA',
              bottom=103,top=109,created_ts=-14400000)
    class Engine:
        _kairos_build_mtf_map=staticmethod(lambda tf:{'H4':{'zones':[zone]}})
        _kairos_context_bias=staticmethod(lambda tf:{})
        _kairos_select_structural_first_capture_sweep=staticmethod(lambda *a:(None,{'candidates':[]}))
        _kairos_capture_radar_events=staticmethod(lambda *a:[])
    tf={'M15':[], 'M5':[dict(t=cut-300000,o=105,h=106,l=104,c=105)]}
    with patch.object(chains,'scan',return_value=dict(asof_ts=cut,view=chains.state_view({}))) as scan, \
         patch.object(daily,'scan_report',return_value={}):
        result=provider.snapshot('BTCUSD',tf,cut,Engine)
    assert result['htf_fvg_touch_events'][0]['key']=='H4|h4'
    assert scan.call_args.args[3]['htf_entry_origins']==[]


def test_htf_touch_cannot_predate_fvg_formation_close():
    from kairos_htf_fvg_alerts import build_touch_events
    z=dict(id='future',tipo='FVG_bearish',state='ATIVA',bottom=103,top=109,created_ts=0)
    c=dict(t=14100000,o=105,h=106,l=104,c=105)
    assert build_touch_events({'H4':{'zones':[z]}},c,14400000)==[]


def test_native_ludo_fvg_arms_strong_m15_break_with_original_force_gate():
    import kairos_m15_primary as primary
    from test_m15_primary import inputs
    cs,cap,event,_=inputs()
    assert primary.direct_zone(cs[:-1],event,22*900000,engine) is None
    zone=primary.direct_zone(cs,event,23*900000,engine)
    assert zone['ludo_fvg'] is True
    assert (zone['bottom'],zone['top'])==(100.2,100.8)
    assert primary.strong_break(cs,event) is True
    cs[21].update(o=101.04,h=101.5,l=101,c=101.1)
    assert primary.strong_break(cs,event) is False


def test_original_sol_triples_keep_both_fvgs_and_remove_only_upper():
    import json
    from pathlib import Path
    from kairos_ludo_fvg import fvg_states
    cs=json.loads(Path('tests/fixtures/ludo_sol_fvg_20261027.json').read_text())['candles']
    zs=fvg_states(cs,lookback=len(cs))
    lower=next(z for z in zs if (z['bottom'],z['top'])==(99.57,99.97))
    upper=next(z for z in zs if (z['bottom'],z['top'])==(100.10,100.38))
    assert lower['state']=='TOCADA' and lower['invalidated_ts'] is None
    assert upper['state']=='INVALIDADA'
    assert upper['invalidation_basis']=='LUDO_FULL_MITIGATION'
