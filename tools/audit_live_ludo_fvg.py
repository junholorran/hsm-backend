"""Read-only BTC FVG audit; no app import, delivery, order or production DB."""
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kairos_ludo_fvg import fvg_states

BASE="https://kairos-poi-abc-sol-production.up.railway.app/experiment/native_candles"
END=int(time.time()*1000)//300000*300000
def get(url):
    with urllib.request.urlopen(url,timeout=50) as response:
        return json.load(response)
query=urllib.parse.urlencode(dict(start=1,pair="BTCUSD",fim_ts_ms=END))
for attempt in range(45):
    try:
        response=get(BASE+"?"+query)
        if response.get("status") in ("STARTING","RUNNING"):
            break
    except urllib.error.HTTPError as exc:
        if exc.code!=409:
            raise
    time.sleep(3)
else:
    raise RuntimeError("Audit endpoint remained busy; no live process cancelled")
for attempt in range(80):
    status=get(BASE)
    if status.get("status")=="ERROR":
        raise RuntimeError(status.get("error"))
    if status.get("status")=="DONE":
        data=status["result"]
        assert data["pair"]=="BTCUSD" and data["cutoff_ts"]==END,"Unrelated export"
        break
    time.sleep(3)
else:
    raise RuntimeError("Export timed out")
raw=data["candles"]
assert hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(",",":")).encode()).hexdigest()==data["sha256"]
assert all(not p.get("error") and p.get("retCode")==0 for ps in data["fetch_audit"].values() for p in ps)
fmt=lambda ts:datetime.fromtimestamp(ts/1000,ZoneInfo("Europe/Lisbon")).isoformat() if ts is not None else None
print("KAIROS_AUDIT_META "+json.dumps(dict(pair=data["pair"],instrument=data["instrument"],cutoff=fmt(END),source_sha256=data["sha256"],code="7264cb05d6902fe16f4bd967ad4aee4bde7e2838",orders=False,telegram=False),sort_keys=True))
durations={"W1":604800000,"D1":86400000,"H4":14400000,"H1":3600000,"M15":900000}
for tf,duration in durations.items():
    rows=raw[tf] if tf=="W1" else raw[tf][-999:]
    assert all(c["t"]+duration<=END for c in rows)
    assert all(b["t"]-a["t"]==duration for a,b in zip(rows,rows[1:]))
    states=fvg_states(rows,lookback=len(rows))
    current_open=rows[-1]["t"]+duration
    intrabar=[c for c in raw["M5"] if current_open<=c["t"] and c["t"]+300000<=END]
    active=[z for z in states if z["state"]!="INVALIDADA"]
    print("KAIROS_AUDIT_TF "+json.dumps(dict(tf=tf,candles=len(rows),active_count=len(active),native_closed_through=fmt(current_open))))
    for z in active:
        a,b,c=[z[k] for k in ("source_a","source_mid","source_c")]
        expected=(a["h"],c["l"]) if z["direcao"]=="alta" else (c["h"],a["l"])
        assert (z["bottom"],z["top"])==expected
        full=next((bar for bar in intrabar if (bar["l"]<=z["bottom"] if z["direcao"]=="alta" else bar["h"]>=z["top"])),None)
        partial=next((bar for bar in intrabar if (bar["l"]<z["top"] if z["direcao"]=="alta" else bar["h"]>z["bottom"])),None)
        preview="FULL_MITIGATION_SEEN_IN_CLOSED_M5" if full else ("PARTIAL_SEEN_IN_CLOSED_M5" if partial else "NO_NEW_M5_MITIGATION_SEEN")
        sources={key:dict(open=fmt(bar["t"]),**{k:bar[k] for k in ("o","h","l","c")}) for key,bar in zip(("A","B","C"),(a,b,c))}
        out=dict(tf=tf,id=z["id"],type=z["tipo"],bottom=z["bottom"],top=z["top"],native_state=z["state"],formation_close=fmt(c["t"]+duration),first_native_touch=fmt(z["first_touch_ts"]),sources=sources,unclosed_native_preview=preview,preview_full_close=fmt(full["t"]+300000) if full else None)
        print("KAIROS_AUDIT_FVG "+json.dumps(out,sort_keys=True))
    for z in states:
        if tf=="H4" and ((z["bottom"],z["top"]) in [(81176.4,81208.6),(80142.4,80411.9)]):
            print("KAIROS_AUDIT_DISPUTED "+json.dumps({k:z.get(k) for k in ("id","tipo","bottom","top","state","invalidation_basis","invalidated_ts","source_a","source_mid","source_c")},sort_keys=True))
print("KAIROS_AUDIT_DONE")
