HTF_FVG_TFS = ('W1', 'D1', 'H4', 'H1')
ACTIVE_FVG_STATES = {'ATIVA', 'TOCADA', 'PARCIAL'}


def _price(value):
    return f"{float(value):.8f}".rstrip('0').rstrip('.')


def build_touch_events(maps, m5_candle, touch_close_ts):
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
            duration={'W1':604800000,'D1':86400000,'H4':14400000,'H1':3600000}[tf]
            born=zone.get('created_ts')
            if born is not None and m5_candle['t'] < born+duration:
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
                'observed_low': low,
                'observed_high': high,
            })
    return out


def format_fvg_touch_message(pair, event):
    if event.get('notice_kind')=='HTF_FVG_M15_STRUCTURE':
        from kairos_htf_fvg_watch import format_structure_watch
        return format_structure_watch(pair,event)
    from kairos_alert_presentation import format_htf_touch
    return format_htf_touch(pair,event)
