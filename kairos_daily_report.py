"""Experimental daily briefing. No scanner hooks, HTTP calls or order authority.

Consumes the existing structural registry unchanged. Its coverage is bounded by
the history and level limits of that registry; this is not an order-book feed.
"""
import copy
import json
import math
from datetime import datetime, timezone

DAY = 86_400_000
VERSION = 'DAILY_RANGE_V1_CANDIDATE'
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
    if report['status'] != 'READY_CANDIDATE':
        return []
    if max_chars < 1000 or max_chars > 3500:
        raise ValueError('max_chars must be between 1000 and 3500')
    d = report['daily']
    date = datetime.fromtimestamp(d['available_ts']/1000, timezone.utc).date().isoformat()
    header = f"KAIROS | VIÉS DIÁRIO EXPERIMENTAL\n{report['pair']} | {date} UTC | {d['bias']}\n"
    lines = [REASONS[d['reason']],
             f"Dia fechado: H {d['closed_day']['h']} | L {d['closed_day']['l']} | C {d['closed_day']['c']}",
             f"Dia anterior: H {d['previous_day']['h']} | L {d['previous_day']['l']}",
             'Hipótese inicial; sem entrada autorizada. Invalidação de preço ainda não validada.',
             'Observar reação no POI; confirmação M15 e refinamento M5.',
             f"Níveis H4/H1: {report['coverage_counts']}. Períodos ausentes: {', '.join(report['missing_period_types']) or 'nenhum'}.",
             'MAPA EXISTENTE H4/H1 + PERÍODOS | cobertura limitada ao histórico disponível:']
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
