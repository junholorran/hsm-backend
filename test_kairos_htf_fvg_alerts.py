from kairos_htf_fvg_alerts import build_touch_events, format_fvg_touch_message


def test_only_relevant_htf_fvg_and_actual_m5_touch_are_alerted():
    maps = {
        'H4': {'zones': [
            {'id':'H4_B','tipo':'FVG_bearish','state':'ATIVA','bottom':84521.1,'top':85395.8,'created_ts':-14400000},
            {'id':'H4_OLD','tipo':'FVG_bullish','state':'INVALIDADA','bottom':81000,'top':82000,'created_ts':900},
        ]},
        'M15': {'zones': [
            {'id':'M15_X','tipo':'FVG_bearish','state':'ATIVA','bottom':84400,'top':84600,'created_ts':1000},
        ]},
    }
    m5 = {'t':2000,'h':84600.0,'l':84300.0,'c':84500.0}
    events = build_touch_events(maps, m5, touch_close_ts=2300)
    assert [e['key'] for e in events] == ['H4|H4_B']
    assert events[0]['tf'] == 'H4'
    assert events[0]['bottom'] == 84521.1
    assert events[0]['top'] == 85395.8


def test_message_is_context_only_not_trade_signal():
    msg = format_fvg_touch_message('BTCUSD', {
        'tf':'H4','tipo':'FVG_bearish','state':'ATIVA',
        'bottom':84521.1,'top':85395.8,'touch_close_ts':2300,
    })
    assert 'FVG H4' in msg
    assert '84521,10' in msg
    assert 'SEM TRADE' in msg
    assert 'M15' in msg
    assert 'M5' in msg
    assert 'LIQUIDEZ CAPTURADA' not in msg
