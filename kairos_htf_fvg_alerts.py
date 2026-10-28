from datetime import datetime,timezone
import html
import math

HTF_FVG_TFS = ('MN', 'W1', 'D1', 'H4', 'H1')
ACTIVE_FVG_STATES = {'ATIVA', 'TOCADA', 'PARCIAL'}

def formation_close(zone,tf):
    born=zone.get("created_ts")
    if not isinstance(born,(int,float)) or not math.isfinite(born):
        return None
    if tf=="MN":
        current=datetime.fromtimestamp(born/1000,timezone.utc)
        if current.day!=1 or any((current.hour,current.minute,current.second,current.microsecond)):
            return None
        year=current.year+(current.month==12)
        month=1 if current.month==12 else current.month+1
        return int(datetime(year,month,1,tzinfo=timezone.utc).timestamp()*1000)
    duration={"W1":604800000,"D1":86400000,"H4":14400000,"H1":3600000}.get(tf)
    return born+duration if duration is not None else None

def liquidity_inside_zone(zone,levels,known_before):
    result=[];seen=set()
    allowed={"EQH","EQL","SWING_HIGH","SWING_LOW","LUDO_SWING_HIGH","LUDO_SWING_LOW",
             "PDH","PDL","PWH","PWL","PMH","PML"}
    for ref in levels or []:
        value=ref.get("level");confirmed=ref.get("confirmed_ts")
        if (ref.get("tf") not in HTF_FVG_TFS or ref.get("type") not in allowed
                or ref.get("state")!="ACTIVE" or ref.get("captured_ts") is not None
                or not isinstance(value,(int,float)) or not math.isfinite(value)
                or not isinstance(confirmed,(int,float)) or not math.isfinite(confirmed)
                or confirmed>known_before or not zone["bottom"]<=value<=zone["top"]):
            continue
        key=(ref["tf"],ref["type"],value,ref.get("origin_ts"))
        if key in seen:
            continue
        seen.add(key)
        result.append({k:ref.get(k) for k in ("tf","type","level","origin_ts","confirmed_ts","state")})
    return sorted(result,key=lambda ref:(HTF_FVG_TFS.index(ref["tf"]),ref["type"],ref["level"]))

def confluence_line(levels):
    if not levels:
        return None
    from kairos_alert_presentation import price
    visible=levels[:3]
    text="💧 Liquidez disponível no mapa: "+" | ".join(
        ref["tf"]+" "+ref["type"]+" @ "+price(ref["level"]) for ref in visible)
    if len(levels)>3:
        text+=" | +"+str(len(levels)-3)+" referências"
    return text

def _price(value):
    return f"{float(value):.8f}".rstrip('0').rstrip('.')


def build_touch_events(maps, m5_candle, touch_close_ts, liquidity_levels=None):
    if not m5_candle:
        return []
    low = float(m5_candle['l'])
    high = float(m5_candle['h'])
    out = []
    for tf in HTF_FVG_TFS:
        zones = ((maps or {}).get(tf) or {}).get('zones') or []
        for zone in zones:
            typ = str(zone.get('tipo') or '')
            if typ not in ('FVG_bullish', 'FVG_bearish'):
                continue
            if zone.get('state') not in ACTIVE_FVG_STATES:
                continue
            ready=formation_close(zone,tf)
            if ready is None or m5_candle['t'] < ready:
                continue
            bottom = zone.get('bottom')
            top = zone.get('top')
            if bottom is None or top is None:
                continue
            if (low <= float(bottom) if typ=='FVG_bullish' else high >= float(top)):
                continue
            if high < float(bottom) or low > float(top):
                continue
            zone_id = zone.get('id') or f"{typ}:{zone.get('origin_ts')}:{bottom}:{top}"
            out.append({
                'key': f'{tf}|{zone_id}',
                'tf': tf,
                'tipo': typ,
                'state': zone.get('state'),
                'bottom': float(bottom),
                'top': float(top),
                'origin_ts': zone.get('origin_ts'),
                'created_ts': zone.get('created_ts'),
                'touch_close_ts': int(touch_close_ts),
                'liquidity_inside': liquidity_inside_zone(zone,liquidity_levels,m5_candle['t']),
                'observed_low': low,
                'observed_high': high,
            })
    return out


def format_fvg_touch_message(pair,event):
    if event.get("notice_kind")=="HTF_FVG_M15_STRUCTURE":
        from kairos_htf_fvg_watch import format_structure_watch
        return format_structure_watch(pair,event)
    from kairos_alert_presentation import price,stamp
    bull=event.get("tipo")=="FVG_bullish"
    lines=["📦 KAIROS | FVG "+str(event.get("tf"))+" TOCADO",
           html.escape(pair)+" · "+("COMPRADOR" if bull else "VENDEDOR"),
           "📍 Zona: "+price(event.get("bottom"))+"–"+price(event.get("top")),
           "🕐 "+stamp(event.get("touch_close_ts"))+" Portugal"]
    confluence=confluence_line(event.get("liquidity_inside"))
    if confluence:
        lines.append(confluence)
    lines+=["👀 Observar quebra M15 de "+("ALTA" if bull else "BAIXA")+" com força.",
            "M5: aviso antecipado / refinamento; aguardar plano causal.",
            "CONTEXTO · SEM TRADE · DEMO / MANUAL"]
    return "\n".join(lines)
