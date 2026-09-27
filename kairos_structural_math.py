"""Kairos structural math v1.
Pure helpers: no IO, no secrets, no side effects.
"""
def current_lux_dealing_range(swings, close, swing_size=50):
    ordered=sorted(
        (x for x in (swings or []) if x.get("tipo") in ("high","low") and x.get("t") is not None),
        key=lambda x:x["t"]
    )
    pair=None
    for i in range(len(ordered)-1,0,-1):
        if ordered[i]["tipo"] != ordered[i-1]["tipo"]:
            pair=(ordered[i-1],ordered[i])
            break
    if pair is None:
        return None
    a,b=pair
    high=a if a["tipo"]=="high" else b
    low=a if a["tipo"]=="low" else b
    hi=float(high["valor"]); lo=float(low["valor"])
    if hi<=lo:
        return None
    eq=lo+(hi-lo)*0.5
    px=float(close)
    location="EQUILIBRIUM" if abs(px-eq)<1e-12 else ("PREMIUM" if px>eq else "DISCOUNT")
    return {
        "high":hi,"low":lo,"equilibrium":eq,"location":location,
        "high_origin_ts":high.get("t"),"low_origin_ts":low.get("t"),
        "range_start_ts":min(a["t"],b["t"]),"range_end_ts":max(a["t"],b["t"]),
        "range_leg":a["tipo"].upper()+"_TO_"+b["tipo"].upper(),
        "swing_size":swing_size,"source":"LUX_LAST_CONFIRMED_ALTERNATING_LEG"
    }

def capture_is_fresh_for_m15(capture, now_ts, max_m15_bars=12):
    if not capture or capture.get("confirm_ts") is None:
        return False
    age=int(now_ts)-int(capture["confirm_ts"])
    return 0 <= age <= int(max_m15_bars)*15*60*1000
