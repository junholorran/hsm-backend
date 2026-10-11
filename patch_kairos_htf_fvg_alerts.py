from pathlib import Path

SCALP_MARK = '# KAIROS_HTF_FVG_ALERTS_V1'
APP_MARK = '# KAIROS_HTF_FVG_TELEGRAM_V1'


def patch_scalp_source(source):
    if SCALP_MARK in source:
        return source
    anchor = "    signals=[value['signal_snapshot'] for value in phase_state.values() if value.get('signal_snapshot')]\n"
    if anchor not in source:
        raise RuntimeError('FVG scan anchor missing')
    block = """    # KAIROS_HTF_FVG_ALERTS_V1\n    # Presentation-only event export. Reuses the exact native Lux FVG detector\n    # on already-fetched CLOSED candles; no new detector, fetch, gate or signal math.\n    from kairos_htf_fvg_alerts import build_touch_events\n    _fvg_maps={}\n    _fvg_detector=globals().get('_kairos_fvg_states')\n    for _tf in ('W1','D1','H4','H1'):\n        _fvg_maps[_tf]={'zones':_fvg_detector(tf_map.get(_tf) or []) if callable(_fvg_detector) else []}\n    _m5_touch=(tf_map.get('M5') or [])[-1] if (tf_map.get('M5') or []) else None\n    _m5_touch_close=_kairos_candle_close_ts(_m5_touch['t'],'5') if _m5_touch else None\n    htf_fvg_touch_events=(build_touch_events(_fvg_maps,_m5_touch,_m5_touch_close)\n                          if _m5_touch and _m5_touch_close is not None else [])\n\n"""
    source = source.replace(anchor, block + anchor, 1)
    ret_anchor = "            'radar_captures':_kairos_capture_radar_events(r,tf_map,cutoff),\n"
    if ret_anchor not in source:
        raise RuntimeError('FVG return anchor missing')
    source = source.replace(ret_anchor, ret_anchor + "            'htf_fvg_touch_events':htf_fvg_touch_events,\n", 1)
    compile(source, 'scalp_engine.py', 'exec')
    return source


def patch_app_source(source):
    if APP_MARK in source:
        return source
    anchor = "_KAIROS_SETUP_SEEN={pair:set() for pair in _KAIROS_LIVE_PAIRS}\n"
    if anchor not in source:
        raise RuntimeError('FVG Telegram anchor missing')
    block = r'''# KAIROS_HTF_FVG_TELEGRAM_V1

def _kairos_deliver_fvg_once(pair, event, message):
    """Persist one alert per real HTF FVG zone; never changes trading state."""
    try:
        with sqlite3.connect(DB_FILE, timeout=15) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kairos_fvg_delivery (pair TEXT NOT NULL, event_key TEXT NOT NULL, touch_ts INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY (pair,event_key))")
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT status FROM kairos_fvg_delivery WHERE pair=? AND event_key=?', (pair,event['key'])).fetchone()
            if row and row[0] in ('DELIVERED','PRE_BOOT_BASELINE'):
                return True
            touch_ts=int(event['touch_close_ts'])
            if not row and touch_ts < _KAIROS_LIVE_STARTED_TS:
                conn.execute('INSERT INTO kairos_fvg_delivery VALUES (?,?,?,?)', (pair,event['key'],touch_ts,'PRE_BOOT_BASELINE'))
                status='PRE_BOOT_BASELINE'
            else:
                if not row:
                    conn.execute('INSERT INTO kairos_fvg_delivery VALUES (?,?,?,?)', (pair,event['key'],touch_ts,'PENDING'))
                if not send_telegram(message):
                    return False
                conn.execute("UPDATE kairos_fvg_delivery SET status='DELIVERED' WHERE pair=? AND event_key=?", (pair,event['key']))
                status='DELIVERED'
        print(f"[KAIROS_FVG] {status} pair={pair} tf={event.get('tf')} type={event.get('tipo')} zone={event.get('bottom')}-{event.get('top')} touch_ts={event.get('touch_close_ts')}", flush=True)
        return True
    except Exception as exc:
        print(f"[KAIROS_FVG] delivery/storage error pair={pair}: {exc}", flush=True)
        return False


def _kairos_send_fvg_events(pair, result, cutoff):
    """Separate FVG context stream. FVG is never treated as liquidity or entry."""
    from kairos_htf_fvg_alerts import format_fvg_touch_message
    delivered=True
    for event in result.get('htf_fvg_touch_events') or []:
        ts=event.get('touch_close_ts')
        if ts is None or ts > cutoff:
            continue
        msg=format_fvg_touch_message(pair,event)
        if not _kairos_deliver_fvg_once(pair,event,msg):
            delivered=False
    return delivered

'''
    source = source.replace(anchor, block + anchor, 1)
    scan_anchor = "                    delivered = _kairos_send_capture_events(pair, r, cycle_started_ms)\n"
    if scan_anchor not in source:
        raise RuntimeError('FVG scanner delivery anchor missing')
    source = source.replace(scan_anchor, scan_anchor + "                    delivered = _kairos_send_fvg_events(pair, r, cycle_started_ms) and delivered\n", 1)
    compile(source, 'app.py', 'exec')
    return source


def main():
    scalp=Path('scalp_engine.py')
    app=Path('app.py')
    scalp_source=patch_scalp_source(scalp.read_text(encoding='utf-8'))
    app_source=patch_app_source(app.read_text(encoding='utf-8'))
    scalp.write_text(scalp_source,encoding='utf-8')
    app.write_text(app_source,encoding='utf-8')
    print('[KAIROS_HTF_FVG_ALERTS_V1] W1/D1/H4/H1 touch alerts wired; causal math unchanged')


if __name__ == '__main__':
    main()
