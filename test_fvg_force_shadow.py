import copy
import pytest
from kairos_fvg_force_shadow import assess_fvg

D=300000

def case():
    bars=[dict(t=i*D,o=100,h=100.2,l=99.9,c=100.1) for i in range(20)]
    bars += [dict(t=20*D,o=100,h=100.2,l=99.9,c=100.1),
             dict(t=21*D,o=100.1,h=101.2,l=100,c=101.1),
             dict(t=22*D,o=101.1,h=101.3,l=100.8,c=100.9)]
    zone=dict(tipo='FVG_bullish',bottom=100.2,top=100.8,
              source_a=bars[-3],source_mid=bars[-2],source_c=bars[-1])
    event=dict(side='UP',level=100.5,close_ts=22*D,opposite_origin_ts=20*D)
    return bars,zone,event

def test_strong_break_and_red_third_candle_do_not_require_second_break():
    bars,z,e=case();before=copy.deepcopy((bars,z,e))
    r=assess_fvg(bars,z,e,D,23*D)
    assert r['status']=='OBSERVED' and r['geometry_valid']
    assert r['direct_break'] and r['inside_causal_leg']
    assert r['middle']['experimental_strong'] is True
    assert r['experimental_candidate'] is True
    assert r['changes_entry_decision'] is False
    assert (bars,z,e)==before

def test_unclosed_third_is_unknown_not_valid():
    bars,z,e=case()
    assert assess_fvg(bars,z,e,D,23*D-1)['status']=='NOT_YET_CLOSED'

def test_gap_requires_wicks_and_exact_native_sources():
    bars,z,e=case();bars[-1]['l']=100.1
    assert not assess_fvg(bars,z,e,D,23*D)['geometry_valid']
    bars,z,e=case();z=copy.deepcopy(z);z['source_mid']['c']=101.0
    assert assess_fvg(bars,z,e,D,23*D)['status']=='SOURCE_MISMATCH'

def test_missing_or_flat_baseline_is_unknown():
    bars,z,e=case()
    assert assess_fvg(bars[-3:],z,e,D,23*D)['middle']['experimental_strong'] is None
    for c in bars[:-3]:c['c']=c['o']
    assert assess_fvg(bars,z,e,D,23*D)['middle']['experimental_strong'] is None

def test_mirrored_short():
    bars,z,e=case()
    def mirror(c):return dict(t=c['t'],o=200-c['o'],h=200-c['l'],l=200-c['h'],c=200-c['c'])
    bars=list(map(mirror,bars));z=dict(tipo='FVG_bearish',bottom=99.2,top=99.8,
        source_a=bars[-3],source_mid=bars[-2],source_c=bars[-1])
    e.update(side='DOWN',level=99.5)
    r=assess_fvg(bars,z,e,D,23*D)
    assert r['experimental_candidate'] is True

def test_earlier_leg_fvg_is_not_direct_break_and_foreign_tf_not_associated():
    bars,z,e=case();e.update(close_ts=23*D)
    r=assess_fvg(bars,z,e,D,23*D)
    assert r['inside_causal_leg'] and not r['direct_break']
    assert r['experimental_candidate'] is False
    bars,z,e=case();e['close_ts']=22*D+1
    assert assess_fvg(bars,z,e,D,23*D)['status']=='BREAK_NOT_IN_NATIVE_BARS'

def test_missing_candle_and_nonfinite_fail_closed():
    bars,z,e=case();bars.pop(10)
    assert assess_fvg(bars,z,e,D,23*D)['middle']['experimental_strong'] is None
    bars,z,e=case();bars[-2]['h']=float('nan')
    assert assess_fvg(bars,z,e,D,23*D)['status']=='INVALID_CANDLES'

def test_snapshot_flag_keeps_all_existing_results_and_observer_failure_isolated(monkeypatch):
    from unittest.mock import patch
    import kairos_ludo_entry as provider
    import kairos_capture_chains as chains
    import kairos_daily_delivery as daily
    import kairos_fvg_force_shadow as shadow
    class Engine:
        _kairos_build_mtf_map=staticmethod(lambda tf:{})
        _kairos_context_bias=staticmethod(lambda tf:{})
        _kairos_select_structural_first_capture_sweep=staticmethod(lambda *a:(None,{'candidates':[]}))
        _kairos_capture_radar_events=staticmethod(lambda *a:[])
    tm=dict(M15=[],M5=[]);cutoff=900000
    view=chains.state_view({})
    with patch.object(chains,'scan',return_value=dict(asof_ts=cutoff,view=view)), \
         patch.object(daily,'scan_report',return_value=None):
        monkeypatch.delenv('KAIROS_FVG_FORCE_SHADOW',raising=False)
        baseline=provider.snapshot('SOLUSD',tm,cutoff,Engine)
        monkeypatch.setenv('KAIROS_FVG_FORCE_SHADOW','true')
        with patch.object(shadow,'observe_view',return_value=dict(status='TEST_DIAGNOSTIC')):
            observed=provider.snapshot('SOLUSD',tm,cutoff,Engine)
        assert observed.pop('fvg_force_shadow')==dict(status='TEST_DIAGNOSTIC')
        assert observed==baseline
        with patch.object(shadow,'observe_view',side_effect=RuntimeError('diagnostic only')):
            failed=provider.snapshot('SOLUSD',tm,cutoff,Engine)
        assert failed.pop('fvg_force_shadow')['status']=='OBSERVER_ERROR'
        assert failed==baseline

def test_selected_fvg_still_measured_without_direct_m5_break():
    from kairos_fvg_force_shadow import observe_view
    bars,z,e=case()
    # No native M5 Ludo event is expected on this flat synthetic prefix.
    r=observe_view({'M5':bars},{'m15_break':e,'setup':{'poi':z}},23*D)
    assert r['selected_fvg']['middle']['experimental_strong'] is True
    assert r['changes_entry_decision'] is False

def test_m15_break_measured_separately_from_m5_gap():
    from kairos_fvg_force_shadow import observe_view
    bars,z,e=case()
    m15=[dict(c,t=c['t']*3) for c in bars]
    e=dict(e,close_ts=22*D*3,open_ts=21*D*3,close=m15[-2]['c'])
    view={'m15_break':e,'setup':{'poi':z}}
    r=observe_view({'M5':bars,'M15':m15},view,23*D*3)
    assert r['parent_m15_break']['status']=='MEASURED'
    assert r['parent_m15_break']['candle']['body']==pytest.approx(1)
    assert len(r['selected_fvg']['sources'])==3
    assert r['selected_fvg']['geometry_valid'] is True


def test_selected_incomplete_third_cannot_be_measured_as_valid():
    from kairos_fvg_force_shadow import observe_view
    bars,z,e=case()
    r=observe_view({'M5':bars},{'m15_break':e,'setup':{'poi':z}},23*D-1)
    assert r['selected_fvg']['status']=='NOT_YET_CLOSED'
