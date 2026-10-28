from datetime import datetime,timezone
import pytest
from kairos_htf_fvg_alerts import build_touch_events,format_fvg_touch_message
from kairos_htf_fvg_watch import build_structure_watch_events
from test_htf_fvg_watch import fixture

def ts(year,month,day,hour=0):
    return int(datetime(year,month,day,hour,tzinfo=timezone.utc).timestamp()*1000)

def zone(born):
    return dict(id="monthly",tipo="FVG_bullish",ludo_fvg=True,state="ATIVA",
                direcao="alta",bottom=99,top=101,created_ts=born)

def bar(t):
    return dict(t=t,o=101,h=102,l=100,c=101)

@pytest.mark.parametrize("year,month,last_day,next_year,next_month",[
    (2026,1,31,2026,2),(2026,2,28,2026,3),(2024,2,29,2024,3),(2026,12,31,2027,1)])
def test_monthly_alert_uses_real_calendar_end(year,month,last_day,next_year,next_month):
    maps={"MN":{"zones":[zone(ts(year,month,1))]}}
    before=ts(year,month,last_day,12)
    assert build_touch_events(maps,bar(before),before+300000)==[]
    ready=ts(next_year,next_month,1)
    events=build_touch_events(maps,bar(ready),ready+300000)
    assert len(events)==1 and events[0]["tf"]=="MN"

def test_all_five_macro_timeframes_keep_touch_notices_without_liquidity_requirement():
    durations={"MN":None,"W1":604800000,"D1":86400000,"H4":14400000,"H1":3600000}
    now=ts(2026,3,1)
    maps={tf:{"zones":[zone(ts(2026,2,1) if tf=="MN" else now-d)]} for tf,d in durations.items()}
    events=build_touch_events(maps,bar(now),now+300000)
    assert {e["tf"] for e in events}==set(durations)
    assert all(e["liquidity_inside"]==[] for e in events)

def test_confluence_requires_available_confirmed_relevant_liquidity_inside_zone():
    now=ts(2026,3,1)
    maps={"H4":{"zones":[zone(now-14400000)]}}
    ref=dict(tf="H1",type="EQL",level=100.4,confirmed_ts=now-1,origin_ts=now-3600000,state="ACTIVE")
    levels=[ref,dict(ref),dict(ref,type="SWING_HIGH",level=100.8),
            dict(ref,type="SWING_LOW",level=99.4),dict(ref,level=102),
            dict(ref,level=100.5,confirmed_ts=now+1),dict(ref,level=100.6,state="CAPTURED"),
            dict(ref,level=100.7,tf="M5")]
    event=build_touch_events(maps,bar(now),now+300000,liquidity_levels=levels)[0]
    found=event["liquidity_inside"]
    assert {x["type"] for x in found}=={"EQL","SWING_HIGH","SWING_LOW"}
    assert len(found)==3
    msg=format_fvg_touch_message("BTCUSD",event)
    assert "EQL" in msg and "SWING_HIGH" in msg and "SWING_LOW" in msg
    assert "disponível" in msg and "SEM TRADE" in msg and "M15" in msg

@pytest.mark.parametrize("tf",["MN","W1","D1"])
def test_first_strong_m15_watch_also_accepts_month_week_and_day(tf):
    maps,rows,m5,cut=fixture()
    shift=ts(2026,3,1)
    for c in rows:c["t"]+=shift
    for c in m5:c["t"]+=shift
    z=maps["H4"]["zones"][0]
    z["created_ts"]=ts(2026,2,1) if tf=="MN" else shift-{"W1":604800000,"D1":86400000}[tf]
    output=build_structure_watch_events({tf:{"zones":[z]}},rows,m5,cut+shift)
    assert len(output)==1
    assert output[0]["origins"][0]["tf"]==tf

def test_macro_structure_notice_keeps_liquidity_context_without_entry_permission():
    maps,rows,m5,cut=fixture()
    level=dict(tf="H1",type="EQL",level=99.5,confirmed_ts=0,origin_ts=-3600000,state="ACTIVE")
    out=build_structure_watch_events(maps,rows,m5,cut,liquidity_levels=[level])
    assert out[0]["entry_authorized"] is False
    assert out[0]["origins"][0]["liquidity_inside"][0]["type"]=="EQL"
