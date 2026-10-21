import copy
import importlib.util
import json
from pathlib import Path

spec = importlib.util.find_spec('kairos_ludo_structure')
ludo = __import__('kairos_ludo_structure') if spec else None
DATA = json.loads(Path('tests/fixtures/ludo_bnb_20261021.json').read_text())
CUT = DATA['cutoff_ts']


def api(name):
    fn = getattr(ludo, name, None)
    assert callable(fn), f'Missing Ludo feature: {name}'
    return fn


def test_bnb_timeframes_select_the_levels_checked_on_tradingview():
    snapshot = api('structure_snapshot')
    for tf, duration, expected in [('M5',300000,744.91), ('M15',900000,742.84), ('H1',3600000,739.4)]:
        result = snapshot(DATA['candles'][tf], duration, CUT)
        assert result['state']['low'] == expected
        assert result['state']['high'] == 749.56


def test_m5_fund_is_updated_only_when_its_bos_closes():
    snapshot = api('structure_snapshot')
    before = snapshot(DATA['candles']['M5'],300000,1792574100000-1)
    after = snapshot(DATA['candles']['M5'],300000,1792574100000)
    assert before['state']['low'] != 744.91
    assert after['state']['low'] == 744.91
    assert after['events'][-1]['level'] == 747.54
    assert after['events'][-1]['close_ts'] == 1792574100000


def test_future_bars_do_not_rewrite_structure_and_inputs_are_unchanged():
    snapshot=api('structure_snapshot')
    source=copy.deepcopy(DATA['candles']['M5']); original=copy.deepcopy(source)
    cut=1792574100000
    assert snapshot(source,300000,cut)==snapshot([c for c in source if c['t']+300000<=cut],300000,cut)
    assert source==original


def test_a_wick_below_the_fund_does_not_confirm_a_close_break():
    snapshot=api('structure_snapshot')
    source=copy.deepcopy(DATA['candles']['M5'])
    source.append(dict(t=CUT,o=745.52,h=746.,l=744.,c=745.1))
    result=snapshot(source,300000,CUT+300000)
    assert result['state']['low']==744.91
    assert result['events'][-1]['side']=='UP'


def test_closed_loss_of_the_fund_is_a_choch_with_prior_level_evidence():
    snapshot=api('structure_snapshot')
    source=copy.deepcopy(DATA['candles']['M5'])
    source.append(dict(t=CUT,o=745.52,h=745.6,l=744.,c=744.5))
    result=snapshot(source,300000,CUT+300000)
    event=result['events'][-1]
    assert (event['kind'],event['side'],event['level'])==('CHoCH','DOWN',744.91)
    assert event['origin_ts']==1792572900000
    assert event['close_ts']==CUT+300000


def test_missing_bar_blocks_the_snapshot_instead_of_inventing_structure():
    snapshot=api('structure_snapshot')
    source=DATA['candles']['M5'][:100]+DATA['candles']['M5'][101:]
    result=snapshot(source,300000,CUT)
    assert result['status']=='DATA_GAP'
    assert result['state'] is None


def test_first_reversal_is_linked_to_capture_and_reclaim_not_to_old_breaks():
    observe=api('observe')
    source=copy.deepcopy(DATA['candles']); original=copy.deepcopy(source)
    capture=dict(liquidity_tf='H4',liquidity_type='EQH',nivel=746.65,
                 first_capture_ts=1792567800000,native_capture_confirm_ts=1792568700000,
                 liquidity_side='HIGH')
    decision={'structural_sweep_audit':{'candidates':[capture]},'entry_setup':{'direction':'LONG','key':'legacy'}}
    decision_before=copy.deepcopy(decision)
    result=observe(source,CUT,decision)
    assert result['mode']=='SHADOW_ONLY'
    assert result['candidate'] is None
    source['M5'].append(dict(t=CUT,o=745.52,h=745.6,l=744.,c=744.5))
    result=observe(source,CUT+300000,decision)
    assert result['candidate']['break']['level']==744.91
    assert result['candidate']['reaction']['close']<746.65
    assert result['candidate']['capture']['nivel']==746.65
    assert result['candidate']['status']=='BREAK_OBSERVED_NOT_AN_ENTRY'
    assert decision==decision_before
    assert original==DATA['candles']


def test_break_without_capture_cannot_create_a_shadow_entry():
    observe=api('observe')
    source=copy.deepcopy(DATA['candles'])
    source['M5'].append(dict(t=CUT,o=745.52,h=745.6,l=744.,c=744.5))
    result=observe(source,CUT+300000,{})
    assert result['candidate'] is None


def test_recording_is_deduplicated_per_pair_and_closed_cutoff(capsys):
    record=api('record_shadow')
    decision={'structural_sweep_audit':{'candidates':[]},'entry_setup':{'key':'old','direction':'LONG'}}
    original=copy.deepcopy(decision)
    a=record('BNB_TEST',DATA['candles'],CUT,decision)
    b=record('BNB_TEST',DATA['candles'],CUT,decision)
    assert a==b
    assert capsys.readouterr().out.count('[KAIROS_LUDO_SHADOW]')==1
    assert decision==original
    a['mode']='MUTATED_BY_CALLER'
    assert record('BNB_TEST',DATA['candles'],CUT,decision)['mode']=='SHADOW_ONLY'


def test_inside_closes_before_capture_confirmation_cannot_be_called_reclaim():
    observe=api('observe')
    source=copy.deepcopy(DATA['candles'])
    source['M5'][-1].update(c=747.,h=747.2)
    source['M5'].append(dict(t=CUT,o=747.,h=747.1,l=744.,c=744.5))
    capture=dict(liquidity_tf='H4',liquidity_type='EQH',nivel=746.65,
        first_capture_ts=CUT-900000,native_capture_confirm_ts=CUT,liquidity_side='HIGH')
    result=observe(source,CUT+300000,{'structural_sweep_audit':{'candidates':[capture]}})
    assert result['candidate'] is None
