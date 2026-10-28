import copy
from kairos_htf_fvg_alerts import build_touch_events
import kairos_htf_fvg_watch as watch
from test_m15_primary import inputs

M15=900000
M5=300000

def fixture(short=False,weak=False):
    rows,_,_,_=inputs()
    if weak: rows[21].update(o=101.04,h=101.5,l=101,c=101.1)
    if short:
        rows=[dict(r,o=200-r["o"],h=200-r["l"],l=200-r["h"],c=200-r["c"]) for r in rows]
    m5=[dict(c,t=c["t"]+j*M5) for c in rows for j in range(3)]
    z=dict(id="h4",tipo="FVG_bearish" if short else "FVG_bullish",ludo_fvg=True,
           direcao="baixa" if short else "alta",state="ATIVA",
           bottom=100.4 if short else 99,top=101 if short else 99.6,created_ts=-14400000)
    return {"H4":{"zones":[z]}},rows,m5,23*M15

def test_touch_then_first_strong_m15_is_observation_without_entry_authority():
    maps,rows,m5,cut=fixture()
    original=copy.deepcopy(maps)
    out=watch.build_structure_watch_events(maps,rows,m5,cut)
    assert len(out)==1
    event=out[0]
    assert event["notice_kind"]=="HTF_FVG_M15_STRUCTURE"
    assert event["direction"]=="LONG" and event["entry_authorized"] is False
    assert event["confirmation_close_ts"]==22*M15
    assert event["touch_close_ts"]==20*M15+M5
    assert event["m15_break"]["side"]=="UP"
    assert maps==original
    assert out==watch.build_structure_watch_events(maps,rows,m5,cut)

def test_bearish_watches_only_downward_strong_confirmation():
    maps,rows,m5,cut=fixture(short=True)
    out=watch.build_structure_watch_events(maps,rows,m5,cut)
    assert len(out)==1 and out[0]["direction"]=="SHORT"
    maps["H4"]["zones"][0].update(tipo="FVG_bullish",direcao="alta",bottom=98,top=102)
    assert watch.build_structure_watch_events(maps,rows,m5,cut)==[]

def test_weak_m15_break_and_unclosed_break_do_not_notify():
    maps,rows,m5,cut=fixture(weak=True)
    assert watch.build_structure_watch_events(maps,rows,m5,cut)==[]
    maps,rows,m5,cut=fixture()
    assert watch.build_structure_watch_events(maps,rows,m5,22*M15-1)==[]

def test_break_before_touch_is_not_reused():
    maps,rows,m5,cut=fixture()
    for c in m5:
        if c["t"]<22*M15:c.update(o=101,h=101.1,l=100.8,c=101)
    m5[-3].update(o=100,h=100.2,l=99.5,c=100)
    assert watch.build_structure_watch_events(maps,rows,m5,cut)==[]

def test_full_mitigation_in_closed_m5_cancels_watch_before_htf_close():
    maps,rows,m5,cut=fixture()
    m5[-1].update(o=100,h=101,l=98.9,c=100)
    assert watch.build_structure_watch_events(maps,rows,m5,cut)==[]

def test_overlapping_h4_h1_sources_make_one_confirmation_notice():
    maps,rows,m5,cut=fixture()
    maps["H1"]={"zones":[dict(maps["H4"]["zones"][0],id="h1",created_ts=-3600000)]}
    out=watch.build_structure_watch_events(maps,rows,m5,cut)
    assert len(out)==1
    assert {s["tf"] for s in out[0]["origins"]}=={"H4","H1"}

def test_data_gap_and_invalid_zone_do_not_notify():
    maps,rows,m5,cut=fixture()
    assert watch.build_structure_watch_events(maps,rows,m5[:60]+m5[61:],cut)==[]
    maps["H4"]["zones"][0]["state"]="INVALIDADA"
    assert watch.build_structure_watch_events(maps,rows,m5,cut)==[]

def test_h4_touch_that_fully_crosses_zone_is_not_announced_as_live():
    z=dict(id="gone",tipo="FVG_bullish",state="ATIVA",bottom=99,top=99.6,created_ts=-14400000)
    assert build_touch_events({"H4":{"zones":[z]}},dict(t=0,o=100,h=101,l=98,c=100),M5)==[]

def test_confirmation_message_names_origin_break_and_context_only():
    maps,rows,m5,cut=fixture()
    event=watch.build_structure_watch_events(maps,rows,m5,cut)[0]
    msg=watch.format_structure_watch("BTCUSD",event)
    assert "H4" in msg and "M15" in msg and "BTCUSD" in msg
    assert "SEM TRADE" in msg and "COMPRA" in msg


def test_same_confirmation_keeps_delivery_key_if_one_origin_disappears():
    maps,rows,m5,cut=fixture()
    maps["H1"]={"zones":[dict(maps["H4"]["zones"][0],id="h1",created_ts=-3600000)]}
    first=watch.build_structure_watch_events(maps,rows,m5,cut)[0]
    maps["H1"]["zones"][0]["state"]="INVALIDADA"
    later=watch.build_structure_watch_events(maps,rows,m5,cut)[0]
    assert first["key"]==later["key"]

def test_later_retouch_does_not_erase_prior_valid_confirmation():
    maps,rows,m5,cut=fixture()
    m5[-3].update(l=99.5)
    rows[-1].update(l=99.5)
    out=watch.build_structure_watch_events(maps,rows,m5,cut)
    assert any(e["confirmation_close_ts"]==22*M15 for e in out)

def test_touch_and_strong_break_can_be_confirmed_at_same_close():
    maps,rows,m5,cut=fixture()
    for c in rows:c["t"]+=11*M15
    for c in m5:c["t"]+=11*M15
    maps["H4"]["zones"][0].update(created_ts=16*M15,bottom=99.5,top=100.05)
    for c in m5:
        if 32*M15<=c["t"]<32*M15+2*M5:c.update(l=100.1)
    out=watch.build_structure_watch_events(maps,rows,m5,cut+11*M15)
    assert len(out)==1
    assert out[0]["touch_close_ts"]==out[0]["confirmation_close_ts"]==33*M15
