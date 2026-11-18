"""Context-only range models built from consecutive closed feed candles."""
M15 = 900_000
WINDOWS = {'H4': (14_400_000, 4), 'D1': (86_400_000, 16)}


def model_events(tf_map, since, cutoff):
    if since is None or since >= cutoff:
        return
    m15 = {row['t']: row for row in tf_map.get('M15', [])
           if row['t'] + M15 <= cutoff}
    for tf, (duration, initial_count) in WINDOWS.items():
        bars = sorted(tf_map.get(tf, []), key=lambda row: row['t'])
        for previous, current in zip(bars, bars[1:]):
            start = current['t']
            end = start + duration
            if end <= since or start > cutoff or previous['t'] + duration != start:
                continue
            lo, hi = previous['l'], previous['h']
            if end <= cutoff:
                low_swept, high_swept = current['l'] < lo, current['h'] > hi
                if lo < current['c'] < hi and (low_swept or high_swept):
                    direction = None if low_swept and high_swept else ('SHORT' if high_swept else 'LONG')
                    yield dict(model='CRT', tf=tf, window=start, confirmed_ts=end,
                               direction=direction, low=lo, high=hi, close=current['c'],
                               target=lo if direction == 'SHORT' else hi if direction == 'LONG' else None)
            initial_end = start + initial_count * M15
            initial = [m15.get(t) for t in range(start, initial_end, M15)]
            if any(row is None for row in initial):
                continue
            opening = initial[0]['o']
            low, high = min(row['l'] for row in initial), max(row['h'] for row in initial)
            swept = None
            for t in range(initial_end, min(end, cutoff), M15):
                row = m15.get(t)
                if row is None:
                    break
                up, down = row['h'] > high, row['l'] < low
                if (up and down) or (swept == 'SHORT' and down and row['c'] >= opening) or (swept == 'LONG' and up and row['c'] <= opening):
                    break
                if swept and (row['c'] < opening if swept == 'SHORT' else row['c'] > opening):
                    if t + M15 > since:
                        yield dict(model='PO3', tf=tf, window=start, confirmed_ts=t + M15,
                                   direction=swept, low=low, high=high, close=row['c'],
                                   opening=opening, sweep_ts=sweep_ts)
                    break
                if swept is None and (up or down):
                    swept, sweep_ts = ('SHORT' if up else 'LONG'), t + M15
