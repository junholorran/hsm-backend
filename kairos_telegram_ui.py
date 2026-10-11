from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _price(value):
    if value is None:
        return '—'
    try:
        return f"{float(value):.8f}".rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(value)


def _arrow(value):
    return {'alta': '↑', 'baixa': '↓', 'neutro': '→'}.get(str(value or '').lower(), '→')


def _side(liquidity_type):
    typ = str(liquidity_type or '').upper()
    if typ in {'PDH', 'PWH', 'PMH', 'EQH'} or 'HIGH' in typ:
        return 'HIGH'
    if typ in {'PDL', 'PWL', 'PML', 'EQL'} or 'LOW' in typ:
        return 'LOW'
    return None


def _local_time(ts_ms):
    if not ts_ms:
        return 'não informado'
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo('Europe/Lisbon'))
        return dt.strftime('%d/%m/%Y %H:%M Portugal')
    except Exception:
        return dt.strftime('%d/%m/%Y %H:%M UTC')


def _utc_time(ts_ms):
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime('%d/%m/%Y %H:%M UTC')


def _context_line(event):
    context = event.get('context') or {}
    if event.get('liquidity_tf') == 'MN':
        return ' | '.join([
            f"MN: {_arrow(context.get('MN'))}",
            f"W1 {_arrow(context.get('W1'))}",
            f"D1 {_arrow(context.get('D1'))}",
            f"H4 {_arrow(context.get('H4'))}",
            f"H1 {_arrow(context.get('H1'))}",
        ])
    return ' | '.join(f"{tf} {_arrow(context.get(tf))}" for tf in ('D1', 'H4', 'H1'))


def format_capture_message(pair, event):
    tf = event.get('liquidity_tf') or 'HTF'
    typ = event.get('liquidity_type') or 'LIQUIDEZ'
    level = _price(event.get('level'))
    reaction = event.get('reaction') or 'UNRESOLVED_REACTION'
    side = _side(typ)

    if reaction == 'REJECTION_RECLAIM':
        reaction_label = 'REJEIÇÃO / RECUPERAÇÃO'
        expected = 'bearish' if side == 'HIGH' else 'bullish' if side == 'LOW' else 'a confirmar'
        reading = f"A reação favorece cenário {expected}, mas ainda precisa da autorização estrutural do M15."
        next_step = 'Aguardar M15 Major + Internal alinhados; depois M5 cria/refina o POI causal.'
    elif reaction == 'ACCEPTANCE_CONTINUATION':
        reaction_label = 'ACEITAÇÃO / CONTINUAÇÃO'
        expected = 'bullish' if side == 'HIGH' else 'bearish' if side == 'LOW' else 'a confirmar'
        reading = f"A reação favorece continuação {expected}, mas ainda precisa da autorização estrutural do M15."
        next_step = 'Aguardar M15 Major + Internal alinhados; depois M5 cria/refina o POI causal.'
    else:
        reaction_label = 'ainda indefinida'
        reading = f"O preço retirou liquidez {tf}. A captura sozinha não define direção."
        if side == 'HIGH':
            next_step = ('🔴 Rejeição / recuperação + M15 bearish → possível reversão para baixo\n'
                         '🟢 Aceitação acima + M15 bullish → possível continuação para cima')
        elif side == 'LOW':
            next_step = ('🟢 Rejeição / recuperação + M15 bullish → possível reversão para cima\n'
                         '🔴 Aceitação abaixo + M15 bearish → possível continuação para baixo')
        else:
            next_step = 'Aguardar reação e confirmação M15 antes de qualquer cenário operacional.'

    monthly_note = ('\n🟣 É contexto mensal HTF; não autoriza entrada sozinho.'
                    if tf == 'MN' else '')
    waiting = ('aguardando reação / confirmação M15'
               if reaction == 'UNRESOLVED_REACTION' else
               'aguardando confirmação M15 / construção M5')
    confirmed = _utc_time(event.get('timestamp'))
    confirmed_line = f'\n🕐 <b>Confirmação: {confirmed}</b>' if confirmed else ''

    return (
        '🔎 <b>KAIROS | LIQUIDEZ CAPTURADA</b>\n'
        f'<b>{pair}</b>\n\n'
        f'💧 <b>{tf} {typ} @ {level}</b>\n'
        f'🗺 <b>Contexto: {_context_line(event)}</b>\n'
        f'⚡ <b>Reação: {reaction_label}</b>'
        f'{confirmed_line}\n\n'
        '🧠 <b>LEITURA</b>\n'
        f'{reading}{monthly_note}\n\n'
        '👀 <b>O QUE ESPERAR</b>\n'
        f'{next_step}\n\n'
        f'⏳ <b>AGORA: {waiting}.</b>\n'
        '<b>SEM TRADE · DEMO / MANUAL</b>'
    )


def format_setup_message(pair, event):
    direction = str(event.get('direction') or '').upper()
    icon = '🟢' if direction == 'LONG' else '🔴' if direction == 'SHORT' else '⚪'
    structural = 'bullish' if direction == 'LONG' else 'bearish' if direction == 'SHORT' else 'confirmado'
    poi = event.get('poi') or {}
    continuation = poi.get('refinement_basis') == 'M15_AUTHORIZED_M5_CONTINUATION_BOS_OB'
    m5_line = ('M5 BOS + POI causal: confirmado'
               if continuation else 'M5 POI causal: confirmado')

    return (
        '⏳ <b>KAIROS | SETUP M5 ARMADO</b>\n'
        f'{icon} <b>{pair} | {direction}</b>\n\n'
        f"💧 <b>Origem: {event.get('capture_tf')} @ {_price(event.get('capture_level'))}</b>\n"
        f'✅ <b>M15 Major: {structural}</b>\n'
        f'✅ <b>M15 Internal: {structural}</b>\n'
        f'✅ <b>{m5_line}</b>\n\n'
        f"📦 <b>{event.get('zone_type')} M5: {_price(event.get('bottom'))}–{_price(event.get('top'))}</b>\n"
        f"📍 <b>Proximal / entrada planeada: {_price(event.get('level'))}</b>\n\n"
        '🧬 <b>CADEIA CAUSAL COMPLETA ✅</b>\n\n'
        '🧠 <b>LEITURA</b>\n'
        'Estrutura de execução formada. Não perseguir preço; aguardar retorno ao POI.\n\n'
        '⏳ <b>AGUARDANDO RETESTE</b>\n'
        '<b>DEMO / MANUAL · sem fill assumido</b>'
    )


def format_retest_message(pair, signal):
    direction = str(signal.get('direction') or '').upper()
    icon = '🟢' if direction == 'LONG' else '🔴' if direction == 'SHORT' else '⚪'
    return (
        '🎯 <b>KAIROS | RETESTE M5</b>\n'
        f'{icon} <b>{pair} | {direction}</b>\n\n'
        f"📦 <b>POI M5 retestado: {signal.get('zone_type')} "
        f"[{_price(signal.get('zone_bottom'))}–{_price(signal.get('zone_top'))}]</b>\n"
        f"📍 <b>Proximal planeada: {_price(signal.get('entry'))}</b>\n"
        f"👁 <b>Fecho observado: {_price(signal.get('observed_retest_close'))}</b>\n\n"
        '🧬 <b>MESMO SETUP CAUSAL</b> — sem nova tese.\n'
        f"🕐 M15 confirmado: {_local_time(signal.get('m15_internal_confirmation_close_ts'))}\n"
        f"🕐 Reteste observado: {_local_time(signal.get('retest_confirm_close_ts'))}\n\n"
        '🧠 O preço voltou ao POI que já estava armado.\n'
        '<b>DEMO / MANUAL · preenchimento não comprovado</b>'
    )
