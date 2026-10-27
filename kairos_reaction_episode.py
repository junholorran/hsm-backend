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


def entry_origin_allowed(capture):
    """Only the relevant higher-timeframe liquidity requested by the user."""
    if capture.get('origin_kind') == 'HTF_FVG_TOUCH':
        return False
    tf=capture.get('liquidity_tf');kind=capture.get('liquidity_type')
    if tf in ('H4','H1'):
        return kind in ('SWING_HIGH','SWING_LOW','EQH','EQL','LUDO_SWING_HIGH','LUDO_SWING_LOW')
    return kind in {'D1':('PDH','PDL'),'W1':('PWH','PWL'),'MN':('PMH','PML')}.get(tf,())


def entry_direction_allowed(capture, direction):
    if not entry_origin_allowed(capture):
        return False
    expected = {'HIGH': 'SHORT', 'LOW': 'LONG'}.get(capture.get('liquidity_side'))
    return expected is not None and direction == expected


def entry_reaction_allowed(capture, reaction, direction):
    return bool(reaction and entry_direction_allowed(capture, direction)
                and reaction.get('state') == 'REJECTION_RECLAIM'
                and reaction.get('direction') == direction)


def period_capture_origins(engine, tf_map, cutoff):
    """Reuse native calendar first capture; daily-only consumption stays context.

    No inferred intraday clock or fresh raid for an already consumed month.
    """
    result=[]
    for level in engine._kairos_previous_period_liquidity(tf_map,cutoff):
        opened=level.get('captured_ts');known=level.get('capture_confirm_ts')
        if (level.get('captured_tf')!='M15' or opened is None or known is None
                or known>cutoff or level.get('confirmed_ts',cutoff+1)>opened):
            continue
        bar=next((c for c in tf_map.get('M15',[]) if c['t']==opened and c['t']+M15<=cutoff),None)
        if bar is None:continue
        high=level['type'] in ('PDH','PWH','PMH')
        crossed=lambda c:c['h']>level['level'] if high else c['l']<level['level']
        if not crossed(bar) or known!=opened+M15:continue
        # Prove that the missing older part of an intraday tail was untouched.
        # Closed D1 candles may prove absence of a raid, never its intraday time.
        intervals=[]; prior_cross=False
        for tf,duration in (('M15',M15),('D1',86400000)):
            for c in tf_map.get(tf,[]):
                end=c['t']+duration
                if c['t']<level['confirmed_ts'] or end>opened:continue
                if crossed(c):
                    prior_cross=True
                else:intervals.append((c['t'],end))
        if prior_cross:continue
        covered=level['confirmed_ts']
        for left,right in sorted(intervals):
            if left>covered:break
            covered=max(covered,right)
        if covered<opened:continue
        result.append(dict(liquidity_tf=level['tf'],liquidity_type=level['type'],
            nivel=level['level'],liquidity_side='HIGH' if high else 'LOW',
            liquidity_origin_ts=level['origin_ts'],liquidity_confirm_ts=level['confirmed_ts'],
            first_capture_ts=opened,sweep_ts=opened,native_capture_confirm_ts=known,
            capture_confirm_ts=known,capture_evidence_tf='M15',
            extremo=bar['h'] if high else bar['l']))
    return result
