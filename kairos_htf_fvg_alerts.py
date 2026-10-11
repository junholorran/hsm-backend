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
            bottom = zone.get('bottom')
            top = zone.get('top')
            if bottom is None or top is None:
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
    tf = event.get('tf') or 'HTF'
    typ = str(event.get('tipo') or 'FVG')
    side = 'BULLISH' if typ.endswith('bullish') else 'BEARISH' if typ.endswith('bearish') else typ.upper()
    return (
        f'📦 <b>KAIROS | FVG {tf}</b>\n'
        f'<b>{pair} | {side}</b>\n\n'
        f'📍 <b>Zona: {_price(event.get("bottom"))}–{_price(event.get("top"))}</b>\n'
        f'⚡ <b>Preço tocou / entrou no FVG {tf}</b>\n\n'
        '🧠 <b>LEITURA</b>\n'
        'FVG HTF relevante em contacto com o preço. Isto é contexto/atenção; não define direção sozinho.\n\n'
        '👀 <b>AGORA</b>\n'
        'M15 precisa confirmar a intenção estrutural. M5 apenas refina/executa depois da autorização.\n\n'
        '<b>⚠️ ATENÇÃO — SEM TRADE · DEMO / MANUAL</b>'
    )
