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


def notification_parts(report, max_chars=3500):
    """Plain-text Telegram-ready pages, including every supplied map level."""
    from kairos_alert_presentation import price
    if report['status'] != 'READY_CANDIDATE':
        return []
    if max_chars < 1000 or max_chars > 3500:
        raise ValueError('max_chars must be between 1000 and 3500')
    d = report['daily']
    date = datetime.fromtimestamp(d['available_ts']/1000, timezone.utc).date().isoformat()
    bias_text={'BULLISH':'ALTA','BEARISH':'BAIXA','NEUTRO':'NEUTRO'}[d['bias']]
    header = f"🗺 KAIROS | MAPA DO DIA — CONTEXTO\n{report['pair']} | {date} | Hipótese diária: {bias_text}\nSEM ENTRADA AUTORIZADA POR ESTE MAPA\n\n"
    lines = [REASONS[d['reason']],
             f"Dia fechado: H {price(d['closed_day']['h'])} | L {price(d['closed_day']['l'])} | C {price(d['closed_day']['c'])}",
             f"Dia anterior: H {price(d['previous_day']['h'])} | L {price(d['previous_day']['l'])}",
             'Leitura diária experimental; contexto, sem entrada autorizada.',
             'Observar reação no POI; confirmação M15 e refinamento M5.',
             f"Níveis H4/H1: {report['coverage_counts']}. Períodos ausentes: {', '.join(report['missing_period_types']) or 'nenhum'}.",
             'LIQUIDEZ MAPEADA H4/H1 + PERÍODOS | cobertura limitada ao histórico disponível:']
    context = report.get('structure_context') or {}
    if context:
        lines.insert(1, 'Contexto estrutural D1/H4/H1: ' + ' | '.join(f'{tf} {context.get(tf, "neutro")}' for tf in ('D1','H4','H1')))
    lines.insert(1, 'Mapa observado em ' + datetime.fromtimestamp(report['asof_ts']/1000, ZoneInfo('Europe/Lisbon')).isoformat()+' (Portugal)')
    if report.get('entry_structure_model') == 'LUDO':
        lines.insert(1, 'Estrutura ativa de entrada: SMC Ludo M15 → POI causal → refinamento M5 → reteste.')
    if report.get('current_price'):
        px=report['current_price']
        stage_labels={'SETUP_ANTERIOR_INVALIDADO':'setup anterior invalidado; aguardar uma nova cadeia','RETESTE_HISTORICO_SEM_NOVA_ENTRADA':'reteste histórico identificado; sem nova entrada autorizada','AGUARDANDO_ZONA_OU_CAPTURA':'aguardando zona relevante ou captura de liquidez','AGUARDANDO_CONFIRMACAO_M15':'aguardando confirmação M15 exigida pelo motor','AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5':'M15 confirmado; aguardando zona causal ou refinamento M5','AGUARDANDO_RETESTE_M5':'aguardando reteste da zona M5','RETESTE_M5_OBSERVADO':'reteste M5 observado pelo motor, DEMO/manual'}
        details=[f"Referência: último M5 fechado @ {price(px['value'])} (não é cotação ao vivo).",
                 f"AGORA: {stage_labels[report['stage']['code']]}"]
        if report.get('daily_review_required'):
            details.append('REAVALIAR hipótese diária: preço recuperou/perdeu a referência anterior; não é inversão automática.')
        for side,title in (('above','acima'),('below','abaixo')):
            lev=report['nearest_available'].get(side)
            details.append(f"Liquidez disponível mais próxima {title}: "+(f"{lev['tf']} {lev['type']} @ {price(lev['level'])}" if lev else 'não identificada no mapa disponível'))
        observation=report.get('m15_observation')
        if observation:
            stamp=datetime.fromtimestamp(observation['close_ts']/1000,ZoneInfo('Europe/Lisbon')).isoformat()
            details.append(f"Ludo M15 observado: {observation['kind']} {observation['side']} @ {observation['level']} | fechamento {stamp}. Observação não autoriza entrada.")
        cap=report.get('latest_capture')
        if cap:
            details.append(f"Última captura: {cap['liquidity_tf']} {cap['liquidity_type']} @ {cap['nivel']} | {cap['post_capture_state']}")
        details.append('ZONAS H4/H1 VÁLIDAS NO MAPA:')
        details.extend(f"{z['tf']} {z['type']} {price(z['bottom'])}–{price(z['top'])} | {z['state']} | {z['role'].lower().replace('_',' ')}" for z in report.get('zones',[]))
        if not report.get('zones'):
            details.append('Nenhuma zona válida identificada no histórico disponível.')
        lines=lines[:5]+details+lines[5:]
    lines += [f"{r['tf']} {r['type']} @ {price(r['level'])} | " + ('disponível' if r['state']=='ACTIVE' else 'capturada') for r in report['levels']]
    pages = []; page = header
    for line in lines:
        if len(page) + len(line) + 1 > max_chars:
            pages.append(page.rstrip()); page = header
        if len(page) + len(line) + 1 > max_chars:
            raise ValueError('A map row is too long')
        page += line + '\n'
    pages.append(page.rstrip())
    return pages


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
