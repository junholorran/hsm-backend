"""Experimental daily briefing. No scanner hooks, HTTP calls or order authority.

Consumes the existing structural registry unchanged. Its coverage is bounded by
the history and level limits of that registry; this is not an order-book feed.
"""
import copy
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DAY = 86_400_000
VERSION = 'UNIFIED_CONTEXT_V2'
PERIOD_TYPES = {'PDH', 'PDL', 'PWH', 'PWL', 'PMH', 'PML'}
HIGH_TYPES = {'SWING_HIGH', 'EQH', 'PDH', 'PWH', 'PMH'}
LOW_TYPES = {'SWING_LOW', 'EQL', 'PDL', 'PWL', 'PML'}


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def daily_hypothesis(candles, cutoff):
    """Compare two consecutive CLOSED native UTC days, without future data.

    Both-side sweeps have priority and remain neutral even with an outside
    close. Exact boundary closes are neutral. This explicit hypothesis is not
    an empirically validated prediction or a complete ICT methodology.
    """
    closed = sorted((c for c in candles if c['t'] + DAY <= cutoff), key=lambda c: c['t'])
    if len(closed) < 2:
        return {'status': 'INSUFFICIENT_D1', 'bias': 'NEUTRO'}
    previous, current = closed[-2:]
    if current['t'] - previous['t'] != DAY:
        return {'status': 'D1_GAP', 'bias': 'NEUTRO'}
    for c in (previous, current):
        if (c['t'] % DAY or not all(_finite(c.get(k)) for k in ('o', 'h', 'l', 'c'))
                or not c['l'] <= min(c['o'], c['c']) <= max(c['o'], c['c']) <= c['h']):
            return {'status': 'INVALID_D1', 'bias': 'NEUTRO'}
    available = current['t'] + DAY
    if cutoff >= available + DAY:
        return {'status': 'STALE_D1', 'bias': 'NEUTRO'}
    high, low, close = previous['h'], previous['l'], current['c']
    swept_high, swept_low = current['h'] > high, current['l'] < low
    if swept_high and swept_low:
        bias, reason = 'NEUTRO', 'BOTH_SIDES_CAPTURED'
    elif close > high:
        bias, reason = 'BULLISH', 'CLOSE_ABOVE_PREVIOUS_HIGH'
    elif close < low:
        bias, reason = 'BEARISH', 'CLOSE_BELOW_PREVIOUS_LOW'
    elif low < close < high and swept_high:
        bias, reason = 'BEARISH', 'HIGH_CAPTURE_AND_CLOSE_INSIDE'
    elif low < close < high and swept_low:
        bias, reason = 'BULLISH', 'LOW_CAPTURE_AND_CLOSE_INSIDE'
    else:
        bias, reason = 'NEUTRO', 'NO_DIRECTIONAL_CONFIRMATION'
    return {'status': 'OK', 'bias': bias, 'reason': reason,
            'previous_day': copy.deepcopy(previous), 'closed_day': copy.deepcopy(current),
            'available_ts': available, 'valid_until_ts': available + DAY,
            'swept_high': swept_high, 'swept_low': swept_low,
            'interpretation': 'INITIAL_HYPOTHESIS_NOT_ENTRY',
            'invalidation': 'NOT_VALIDATED_NO_PRICE_INVENTED'}


def build_report(pair, daily_candles, registry, registry_asof, cutoff):
    """Use a registry computed at exactly cutoff; never rewind today's states."""
    report = {'version': VERSION, 'pair': pair, 'asof_ts': cutoff,
              'entry_authorized': False, 'production_decision_changed': False,
              'coverage': 'EXISTING_REGISTRY_ONLY_HISTORY_AND_LIMITS_APPLY'}
    report['daily'] = daily_hypothesis(daily_candles, cutoff)
    if registry_asof != cutoff:
        report.update(status='REGISTRY_TIME_MISMATCH', levels=[])
        return report
    levels = []
    for original in registry:
        if original.get('tf') not in ('H4', 'H1') and original.get('type') not in PERIOD_TYPES:
            continue
        level = copy.deepcopy(original)
        known = level.get('confirmed_ts')
        capture_known = level.get('capture_confirm_ts')
        if (not _finite(level.get('level')) or level['level'] <= 0
                or not _finite(known) or known > cutoff
                or level.get('type') not in HIGH_TYPES | LOW_TYPES
                or level.get('state') not in ('ACTIVE', 'CAPTURED')
                or (capture_known is not None and capture_known > cutoff)
                or (level.get('captured_ts') is not None and level['captured_ts'] > cutoff)):
            report.update(status='INVALID_REGISTRY_EVIDENCE', levels=[])
            return report
        level['liquidity_side'] = 'HIGH' if level['type'] in HIGH_TYPES else 'LOW'
        levels.append(level)
    levels.sort(key=lambda r: (r['tf'], r['level'], r['type'], r.get('origin_ts') or 0))
    report['levels'] = levels
    report['coverage_counts'] = {tf: sum(r['tf'] == tf for r in levels) for tf in ('H4', 'H1')}
    report['missing_period_types'] = sorted(PERIOD_TYPES - {r['type'] for r in levels})
    report['status'] = 'READY_CANDIDATE' if levels and report['daily']['status'] == 'OK' else 'INCOMPLETE'
    px = (report['daily'].get('closed_day') or {}).get('c')
    active = [r for r in levels if r['state'] == 'ACTIVE']
    # Proximity is descriptive, not a forecast of which level is targeted next.
    report['nearest_available'] = {
        'above': min((r for r in active if px is not None and r['level'] > px), key=lambda r:r['level'], default=None),
        'below': max((r for r in active if px is not None and r['level'] < px), key=lambda r:r['level'], default=None),
        'reference': 'LAST_CLOSED_D1_CLOSE_NOT_LIVE_PRICE',
        'predicted_destination': None,
    }
    return report


REASONS = {
    'BOTH_SIDES_CAPTURED': 'Capturou os dois lados do dia anterior.',
    'CLOSE_ABOVE_PREVIOUS_HIGH': 'Fechou acima da máxima anterior.',
    'CLOSE_BELOW_PREVIOUS_LOW': 'Fechou abaixo da mínima anterior.',
    'HIGH_CAPTURE_AND_CLOSE_INSIDE': 'Capturou a máxima e fechou dentro do intervalo.',
    'LOW_CAPTURE_AND_CLOSE_INSIDE': 'Capturou a mínima e fechou dentro do intervalo.',
    'NO_DIRECTIONAL_CONFIRMATION': 'Sem confirmação direcional pela regra diária.',
}


def _detail_parts(report, max_chars=3500):
    """Plain-text Telegram-ready pages, including every supplied map level."""
    if report['status'] != 'READY_CANDIDATE':
        return []
    if max_chars < 1000 or max_chars > 3500:
        raise ValueError('max_chars must be between 1000 and 3500')
    d = report['daily']
    date = datetime.fromtimestamp(d['available_ts']/1000, timezone.utc).date().isoformat()
    bias_text={'BULLISH':'ALTA','BEARISH':'BAIXA','NEUTRO':'NEUTRO'}[d['bias']]
    header = f"📋 KAIROS | DETALHES DO MAPA\n{report['pair']} | {date} | Hipótese diária: {bias_text}\n"
    lines = [REASONS[d['reason']],
             f"Dia fechado: H {d['closed_day']['h']} | L {d['closed_day']['l']} | C {d['closed_day']['c']}",
             f"Dia anterior: H {d['previous_day']['h']} | L {d['previous_day']['l']}",
             'Leitura diária experimental; contexto, sem entrada autorizada.',
             'Observar reação no POI; confirmação M15 e refinamento M5.',
             f"Níveis H4/H1: {report['coverage_counts']}. Períodos ausentes: {', '.join(report['missing_period_types']) or 'nenhum'}.",
             'MAPA EXISTENTE H4/H1 + PERÍODOS | cobertura limitada ao histórico disponível:']
    context = report.get('structure_context') or {}
    if context:
        lines.insert(1, 'Contexto estrutural D1/H4/H1: ' + ' | '.join(f'{tf} {context.get(tf, "neutro")}' for tf in ('D1','H4','H1')))
    lines.insert(1, 'Mapa observado em ' + datetime.fromtimestamp(report['asof_ts']/1000, ZoneInfo('Europe/Lisbon')).isoformat()+' (Portugal)')
    if report.get('entry_structure_model') == 'LUDO':
        lines.insert(1, 'Estrutura ativa de entrada: SMC Ludo M15 → POI causal → refinamento M5 → reteste.')
    if report.get('current_price'):
        px=report['current_price']
        stage_labels={'SETUP_ANTERIOR_INVALIDADO':'setup anterior invalidado; aguardar uma nova cadeia','RETESTE_HISTORICO_SEM_NOVA_ENTRADA':'reteste histórico identificado; sem nova entrada autorizada','AGUARDANDO_ZONA_OU_CAPTURA':'aguardando zona relevante ou captura de liquidez','AGUARDANDO_CONFIRMACAO_M15':'aguardando confirmação M15 exigida pelo motor','AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5':'M15 confirmado; aguardando zona causal ou refinamento M5','AGUARDANDO_RETESTE_M5':'aguardando reteste da zona M5','RETESTE_M5_OBSERVADO':'reteste M5 observado pelo motor, DEMO/manual'}
        stage_labels['AGUARDANDO_REACAO_CONFIRMADA'] = 'aguardando reação confirmada'
        details=[f"Referência: último M5 fechado @ {px['value']} (não é cotação ao vivo).",
                 f"AGORA: {stage_labels.get(report['stage']['code'], 'Estado não reconhecido; conferir sequência')}"]
        if report.get('daily_review_required'):
            details.append('REAVALIAR hipótese diária: preço recuperou/perdeu a referência anterior; não é inversão automática.')
        for side,title in (('above','acima'),('below','abaixo')):
            lev=report['nearest_available'].get(side)
            details.append(f"Liquidez disponível mais próxima {title}: "+(f"{lev['tf']} {lev['type']} @ {lev['level']}" if lev else 'não identificada no mapa disponível'))
        observation=report.get('m15_observation')
        if observation:
            stamp=datetime.fromtimestamp(observation['close_ts']/1000,ZoneInfo('Europe/Lisbon')).isoformat()
            details.append(f"Ludo M15 observado: {observation['kind']} {observation['side']} @ {observation['level']} | fechamento {stamp}. Observação não autoriza entrada.")
        cap=report.get('latest_capture')
        if cap:
            details.append(f"Última captura: {cap['liquidity_tf']} {cap['liquidity_type']} @ {cap['nivel']} | {cap['post_capture_state']}")
        details.append('ZONAS H4/H1 VÁLIDAS NO MAPA:')
        details.extend(f"{z['tf']} {z['type']} {z['bottom']}–{z['top']} | {z['state']} | {z['role'].lower().replace('_',' ')}" for z in report.get('zones',[]))
        if not report.get('zones'):
            details.append('Nenhuma zona válida identificada no histórico disponível.')
        lines=lines[:5]+details+lines[5:]
    lines += [f"{r['tf']} {r['type']} @ {r['level']} | " + ('disponível' if r['state']=='ACTIVE' else 'capturada') for r in report['levels']]
    pages = []; page = header
    for line in lines:
        if len(page) + len(line) + 1 > max_chars:
            pages.append(page.rstrip()); page = header
        if len(page) + len(line) + 1 > max_chars:
            raise ValueError('A map row is too long')
        page += line + '\n'
    pages.append(page.rstrip())
    return pages


def _number(value):
    """Retain the supplied price precision; strip only an integer's .0 suffix."""
    return str(value).removesuffix('.0')


def notification_parts(report, max_chars=3500):
    """Concise reading first, followed by the unabridged existing inventory.

    Formatting only: no detectors, causal associations or delivery-key changes.
    A standalone M15 detector observation is kept in details, never promoted to
    confirmation of the capture/plan selected by the durable chain.
    """
    details = _detail_parts(report, max_chars)
    if not details:
        return []
    d = report['daily']; current = d['closed_day']; previous = d['previous_day']
    bias = {'BULLISH': 'ALTA', 'BEARISH': 'BAIXA', 'NEUTRO': 'NEUTRO'}[d['bias']]
    icon = {'BULLISH': '🔺', 'BEARISH': '🔻', 'NEUTRO': '◼️'}[d['bias']]
    stamp = datetime.fromtimestamp(report['asof_ts']/1000, ZoneInfo('Europe/Lisbon')).strftime('%d/%m/%Y · %H:%M')
    header = f"🗺 KAIROS | MAPA DO DIA\n{report['pair']} · {stamp} Portugal\n"
    reason = d['reason']
    if reason == 'CLOSE_ABOVE_PREVIOUS_HIGH':
        reading = f"Fechamento {_number(current['c'])} > máxima anterior {_number(previous['h'])}. Hipótese de continuação de alta."
    elif reason == 'CLOSE_BELOW_PREVIOUS_LOW':
        reading = f"Fechamento {_number(current['c'])} < mínima anterior {_number(previous['l'])}. Hipótese de continuação de baixa."
    elif reason in ('HIGH_CAPTURE_AND_CLOSE_INSIDE', 'LOW_CAPTURE_AND_CLOSE_INSIDE'):
        which = 'máxima' if reason == 'HIGH_CAPTURE_AND_CLOSE_INSIDE' else 'mínima'
        boundary = previous['h'] if which == 'máxima' else previous['l']
        reading = (f"Capturou a {which} anterior {_number(boundary)}; fechou em {_number(current['c'])} dentro do intervalo. "
                   'Hipótese de reversão, ainda sem confirmação estrutural.')
    else:
        reading = REASONS[reason] + ' Sem direção diária confirmada.'
    lines = ['', f'{icon} VIÉS DIÁRIO: {bias}', reading]
    if report.get('daily_review_required'):
        lines += ['⚠️ REAVALIAR: preço voltou além da referência diária; não é inversão automática.']
    context = report.get('structure_context') or {}
    if context:
        arrows = {'alta': '↑', 'baixa': '↓', 'neutro': '↔'}
        lines += ['', '🧭 ESTRUTURA', ' | '.join(f'{tf} {arrows.get(context.get(tf), "?")}' for tf in ('D1','H4','H1'))]
    stage = report.get('stage') or {}
    capture = stage.get('capture')
    # The latest map capture can belong to a different episode; label it so.
    cap = capture or report.get('latest_capture')
    if cap and cap.get('origin_kind') != 'HTF_FVG_TOUCH':
        reactions = {'REJECTION_RECLAIM': 'rejeição / recuperação',
                     'ACCEPTANCE_CONTINUATION': 'aceitação / continuação',
                     'UNRESOLVED_REACTION': 'reação por resolver'}
        title = '💧 CAPTURA DA SEQUÊNCIA' if capture else '💧 ÚLTIMA CAPTURA NO MAPA · vínculo ao plano não confirmado'
        lines += ['', title, f"{cap['liquidity_tf']} {cap['liquidity_type']}: {_number(cap['nivel'])}"]
        if not capture:
            reaction = cap.get('post_capture_state')
            lines += ['Classificação no mapa: ' + reactions.get(reaction, 'reação não informada')]
    event = stage.get('m15_break')
    if event:
        when = datetime.fromtimestamp(event['close_ts']/1000, ZoneInfo('Europe/Lisbon')).strftime('%d/%m %H:%M')
        arrow, relation = ('↑', 'acima de') if event['side'] == 'UP' else ('↓', 'abaixo de')
        lines += ['', f"📉 {event['kind']} M15 {arrow} {relation} {_number(event['level'])} · {when} Portugal"]
    px = (report.get('current_price') or {}).get('value', current['c'])
    wanted = {'BULLISH': 'alta', 'BEARISH': 'baixa'}.get(d['bias'])
    zones = report.get('zones') or []
    nearby = []
    for tf in ('H4', 'H1'):
        candidates = [z for z in zones if z['tf'] == tf and
                      (not wanted or z.get('role') == 'ZONA_PARA_OBSERVAR_RECUO')]
        groups = [candidates] if wanted else [
            [z for z in candidates if z['bottom'] <= px <= z['top']],
            [z for z in candidates if z['bottom'] > px],
            [z for z in candidates if z['top'] < px]]
        for group in groups:
            nearest = min(group, key=lambda z: max(z['bottom']-px, px-z['top'], 0), default=None)
            if nearest:
                nearby.append(nearest)
    lines += ['', '📍 ZONAS PRÓXIMAS PARA OBSERVAR']
    for z in nearby:
        typ = z['type'].split('_')[0]
        direction = {'alta': 'compra', 'baixa': 'venda'}.get(z.get('direction'), 'contexto')
        lines += [f"{z['tf']} {typ} {direction}: {_number(z['bottom'])}–{_number(z['top'])}"]
    if not nearby:
        lines += ['Nenhuma zona alinhada identificada no mapa.']
    lines += ['Zonas de contexto; não são ordens de entrada.', '', '💧 LIQUIDEZ DISPONÍVEL MAIS PRÓXIMA']
    for side, label in [('above', 'Acima'), ('below', 'Abaixo')]:
        row = report['nearest_available'].get(side)
        lines += [f"{label}: " + (f"{row['tf']} {row['type']} · {_number(row['level'])}" if row else 'não identificada')]
    labels = {
        'AGUARDANDO_ZONA_OU_CAPTURA': 'aguardando zona relevante ou captura de liquidez',
        'AGUARDANDO_REACAO_CONFIRMADA': 'aguardando reação confirmada',
        'AGUARDANDO_CONFIRMACAO_M15': 'aguardando confirmação M15 · CHoCH/BOS por fechamento alinhado',
        'AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5': 'M15 confirmado; aguardando POI causal ou refinamento M5',
        'AGUARDANDO_RETESTE_M5': 'aguardando reteste da zona M5',
        'RETESTE_M5_OBSERVADO': 'reteste M5 observado; execução não confirmada',
        'RETESTE_HISTORICO_SEM_NOVA_ENTRADA': 'reteste histórico; não é nova entrada',
        'SETUP_ANTERIOR_INVALIDADO': 'plano anterior invalidado; aguardar nova sequência',
    }
    code = stage.get('code')
    lines += ['', '⏳ AGORA: ' + labels.get(code, 'Estado não reconhecido; conferir sequência')]
    if code in ('AGUARDANDO_RETESTE_M5', 'RETESTE_M5_OBSERVADO'):
        setup = stage.get('setup') or {}
        if _finite(setup.get('bottom')) and _finite(setup.get('top')):
            typ = setup.get('zone_type', 'POI').split('_')[0]
            lines += [f"🎯 {typ} M5: {_number(setup['bottom'])}–{_number(setup['top'])}"]
        else:
            lines += ['Zona M5: não informada; conferir alerta da sequência.']
    lines += ['', f'Preço de referência: {_number(px)} · ' + ('último M5 fechado' if report.get('current_price') else 'fechamento D1') + '; não é cotação ao vivo.',
              'DEMO / MANUAL · Sem ordem ou execução confirmada.']
    # Preserve caller page limits even for deliberately small test/export pages.
    pages = []; page = header
    for line in lines:
        if len(page) + len(line) + 1 > max_chars:
            pages.append(page.rstrip()); page = header
        if len(page) + len(line) + 1 > max_chars:
            raise ValueError('A summary row is too long')
        page += line + '\n'
    pages.append(page.rstrip())
    return pages + details


def prepare_notification(connection, report):
    """Persist once per pair/native day/version in a caller-owned DEMO database.

    This only prepares payloads; it does not send or mark Telegram delivery.
    The caller owns the transaction. Never point at the production DB until
    scheduling, authorization and delivery retry behavior have been reviewed.
    """
    parts = notification_parts(report)
    if not parts:
        return False
    connection.execute('CREATE TABLE IF NOT EXISTS daily_brief_candidate '
                       '(pair TEXT, day INTEGER, version TEXT, payload TEXT, '
                       'PRIMARY KEY(pair, day, version))')
    cursor = connection.execute('INSERT OR IGNORE INTO daily_brief_candidate VALUES (?,?,?,?)',
        (report['pair'], report['daily']['available_ts'], VERSION,
         json.dumps({'report': report, 'parts': parts}, ensure_ascii=False)))
    return cursor.rowcount == 1
