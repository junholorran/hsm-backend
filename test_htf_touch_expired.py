from kairos_htf_fvg_alerts import build_touch_events

HOUR=3600000
M5=300000

def test_sol_fvg_fully_filled_before_later_return_is_not_alerted():
    ready=1793541600000
    zone=dict(id='SOL_H1',tipo='FVG_bearish',state='ATIVA',bottom=95.11,top=95.18,
              created_ts=ready-HOUR,ludo_fvg=True)
    rows=[dict(t=ready,h=94.93,l=94.78),dict(t=ready+M5,h=95.18,l=94.93),
          dict(t=ready+7*M5,h=95.17,l=95.03)]
    assert build_touch_events({'H1':{'zones':[zone]}},rows[-1],ready+8*M5,m5_candles=rows)==[]

def test_partial_touch_still_alerts_and_future_fill_does_not_leak():
    ready=1793541600000
    zone=dict(id='SOL_H1',tipo='FVG_bearish',state='ATIVA',bottom=95.11,top=95.18,
              created_ts=ready-HOUR,ludo_fvg=True)
    touch=dict(t=ready,h=95.17,l=95.03)
    future=dict(t=ready+M5,h=95.30,l=95.00)
    assert len(build_touch_events({'H1':{'zones':[zone]}},touch,ready+M5,
                                 m5_candles=[touch,future]))==1

def test_bullish_fvg_full_fill_before_later_return_is_not_alerted():
    ready=1793541600000
    zone=dict(id='BULL',tipo='FVG_bullish',state='ATIVA',bottom=95.11,top=95.18,
              created_ts=ready-HOUR,ludo_fvg=True)
    fill=dict(t=ready,l=95.11,h=95.30)
    touch=dict(t=ready+M5,l=95.12,h=95.30)
    assert build_touch_events({'H1':{'zones':[zone]}},touch,ready+2*M5,
                              m5_candles=[fill,touch])==[]
