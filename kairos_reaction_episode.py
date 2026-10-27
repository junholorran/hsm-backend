"""Closed-M15 reaction evidence; no indicator, score or age expiry.

Matches the existing first-capture classifier: only adjacent closes strictly
on one side confirm acceptance/reclaim; mixed/equal closes do not create a
transition. Each actual transition has its own immutable confirmation clock.
"""
M15 = 900000


def reaction_timeline(candles, capture):
    opened = capture['first_capture_ts']
    level = capture.get('nivel')
    side = capture.get('liquidity_side')
    if level is None or side not in ('HIGH', 'LOW'):
        return []
    rows = [c for c in candles if c['t'] >= opened]
    if not rows or rows[0]['t'] != opened:
        return []
    result = []
    state = 'UNRESOLVED_REACTION'
    for previous, current in zip(rows, rows[1:]):
        if current['t'] - previous['t'] != M15:
            continue
        above = previous['c'] > level and current['c'] > level
        below = previous['c'] < level and current['c'] < level
        next_state = ('ACCEPTANCE_CONTINUATION' if (above if side == 'HIGH' else below)
                      else 'REJECTION_RECLAIM' if (below if side == 'HIGH' else above)
                      else None)
        if next_state is None or next_state == state:
            continue
        state = next_state
        up = (side == 'LOW') if state == 'REJECTION_RECLAIM' else (side == 'HIGH')
        closed = current['t'] + M15
        # The displacement may begin at the extreme BEFORE the two closes.
        # Keep the previous reaction's confirming bar as the interval boundary;
        # the native Ludo pivot still clamps the actual parent POI leg.
        leg_start = max(opened, result[-1]['confirmed_ts'] - M15) if result else opened
        result.append(dict(state=state, direction='LONG' if up else 'SHORT',
                           confirmed_ts=closed, leg_start_ts=leg_start,
                           key=f'{state}:{closed}', evidence_tf='M15'))
    return result


def evidence_at(timeline, closed):
    return next((r for r in reversed(timeline) if r['confirmed_ts'] <= closed), None)


def touch_episode(capture):
    location = capture.get('htf_location') or {}
    zone = capture.get('origin_zone') or {}
    kind = location.get('tipo') or zone.get('tipo') or ''
    direction = zone.get('direcao')
    if direction not in ('alta', 'baixa'):
        direction = 'alta' if kind == 'FVG_bullish' else 'baixa' if kind == 'FVG_bearish' else None
    if direction is None:
        return None
    return dict(state='HTF_FVG_TOUCH', direction='LONG' if direction == 'alta' else 'SHORT',
                confirmed_ts=capture['native_capture_confirm_ts'],
                leg_start_ts=capture['first_capture_ts'],
                key=f'TOUCH:{capture["native_capture_confirm_ts"]}', evidence_tf='M5')


def entry_direction_allowed(capture, direction):
    """Liquidity entries reverse a captured side; acceptance is context only.

    HTF FVG touch is an independently authorized origin, not a liquidity raid.
    Its native direction and structural checks remain with its own caller.
    """
    if capture.get('origin_kind') == 'HTF_FVG_TOUCH':
        return direction in ('LONG', 'SHORT')
    expected = {'HIGH': 'SHORT', 'LOW': 'LONG'}.get(capture.get('liquidity_side'))
    return expected is not None and direction == expected


def entry_reaction_allowed(capture, reaction, direction):
    if not reaction or not entry_direction_allowed(capture, direction):
        return False
    if capture.get('origin_kind') == 'HTF_FVG_TOUCH':
        return reaction.get('state') == 'HTF_FVG_TOUCH' and reaction.get('direction') == direction
    return reaction.get('state') == 'REJECTION_RECLAIM' and reaction.get('direction') == direction
