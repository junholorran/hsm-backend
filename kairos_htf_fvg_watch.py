"""Context-only observation: valid H4/H1 Ludo FVG touch -> first strong M15.
No capture origins, entry permission, order execution or delivery state.
"""
import hashlib
import html
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from kairos_ludo_structure import structure_snapshot
from kairos_m15_primary import strong_break
from kairos_htf_fvg_alerts import HTF_FVG_TFS,formation_close,liquidity_inside_zone,confluence_line

M5=300000
M15=900000
ACTIVE={"ATIVA","TOCADA","PARCIAL"}

def build_structure_watch_events(maps,m15_candles,m5_candles,cutoff,liquidity_levels=None):
    m15=[c for c in m15_candles if c["t"]+M15<=cutoff]
    m5=[c for c in m5_candles if c["t"]+M5<=cutoff]
    if not m15 or len(m5)<2:
        return []
    if any(b["t"]-a["t"]!=d for rows,d in ((m15,M15),(m5,M5)) for a,b in zip(rows,rows[1:])):
        return []
    snapshot=structure_snapshot(m15,M15,cutoff)
    if snapshot["status"]!="OK":
        return []
    events=sorted((e for e in snapshot["events"] if strong_break(m15,e)),key=lambda e:e["close_ts"])
    groups={}
    for tf in HTF_FVG_TFS:
        for z in ((maps or {}).get(tf) or {}).get("zones",[]):
            if z.get("ludo_fvg") is not True or z.get("state") not in ACTIVE:
                continue
            if z.get("tipo") not in ("FVG_bullish","FVG_bearish") or z.get("created_ts") is None:
                continue
            bottom,top=z.get("bottom"),z.get("top")
            if bottom is None or top is None or bottom>=top:
                continue
            bull=z["tipo"]=="FVG_bullish"
            ready=formation_close(z,tf)
            if ready is None:
                continue
            observed=[c for c in m5 if c["t"]>=ready]
            # A confirmed lower-timeframe breach already disproves continued
            # zone validity, even while its H4/H1 candle remains in progress.
            if any(c["l"]<=bottom if bull else c["h"]>=top for c in observed):
                continue
            inside=lambda c:c["h"]>=bottom and c["l"]<=top
            touches=[c for prev,c in zip(m5,m5[1:]) if c["t"]>=ready
                     and inside(c) and (prev["t"]<ready or not inside(prev))]
            if not touches:
                continue
            for index,touch in enumerate(touches):
                confirm=touch["t"]+M5
                next_open=touches[index+1]["t"] if index+1<len(touches) else cutoff+1
                event=next((e for e in events if e["side"]==("UP" if bull else "DOWN")
                            and confirm<=e["close_ts"]<=next_open),None)
                if event is None:
                    continue
                key=(event["close_ts"],event["side"])
                origin=dict(tf=tf,zone_id=z["id"],tipo=z["tipo"],bottom=bottom,top=top,
                            touch_open_ts=touch["t"],touch_close_ts=confirm,
                            liquidity_inside=liquidity_inside_zone(z,liquidity_levels,touch["t"]))
                if key not in groups:
                    groups[key]=dict(notice_kind="HTF_FVG_M15_STRUCTURE",direction="LONG" if bull else "SHORT",
                        confirmation_close_ts=event["close_ts"],m15_break=dict(event),
                        entry_authorized=False,fill_assumed=False,origins=[])
                groups[key]["origins"].append(origin)
    out=[]
    for key,notice in sorted(groups.items()):
        origins=sorted(notice["origins"],key=lambda z:(HTF_FVG_TFS.index(z["tf"]),z["zone_id"]))
        notice["origins"]=origins
        identity=key  # Pair is scoped by the persistent delivery table; origins can change.
        notice["key"]="M15_FVG_WATCH|"+hashlib.sha256(json.dumps(identity,separators=(",",":")).encode()).hexdigest()[:24]
        notice["touch_close_ts"]=min(z["touch_close_ts"] for z in origins)
        notice["tf"]="/".join(dict.fromkeys(z["tf"] for z in origins))
        out.append(notice)
    return out

def format_structure_watch(pair,event):
    price=lambda value:f"{float(value):,.2f}".replace(",","_").replace(".",",").replace("_",".")
    clock=lambda ts:datetime.fromtimestamp(ts/1000,ZoneInfo("Europe/Lisbon")).strftime("%d/%m %H:%M")
    direction="COMPRA" if event["direction"]=="LONG" else "VENDA"
    lines=["🔎 KAIROS | INTENÇÃO M15 APÓS FVG",html.escape(pair)+" · "+direction+" EM OBSERVAÇÃO"]
    for z in event["origins"]:
        lines.append("📦 "+z["tf"]+" FVG: "+price(z["bottom"])+"–"+price(z["top"]))
        lines.append("🕐 Toque confirmado: "+clock(z["touch_close_ts"])+" Portugal")
        confluence=confluence_line(z.get("liquidity_inside"))
        if confluence:
            lines.append(confluence)
    e=event["m15_break"]
    lines+=["✅ "+e["kind"]+" M15 por fechamento além de "+price(e["level"])+" · força confirmada",
            "🕐 Confirmação: "+clock(event["confirmation_close_ts"])+" Portugal",
            "👀 Aguardar plano causal de entrada; observar FVG M15 e reteste.",
            "CONTEXTO · SEM TRADE · DEMO / MANUAL"]
    return "\n".join(lines)
