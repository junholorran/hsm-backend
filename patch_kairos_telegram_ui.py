from pathlib import Path


IMPORT_LINE = (
    "from kairos_telegram_ui import (\n"
    "    format_capture_message, format_setup_message, format_retest_message,\n"
    ")\n"
)


def _function_slice(source, name, next_name):
    start_marker = f"def {name}("
    end_marker = f"def {next_name}("
    start = source.find(start_marker)
    end = source.find(end_marker, start + 1)
    if start < 0 or end < 0:
        raise RuntimeError(f"Telegram UI patch anchor missing: {name} -> {next_name}")
    return start, end


def patch_app_source(source):
    if not isinstance(source, str):
        raise TypeError('app source must be text')

    original_scanner = None
    scanner_start = source.find('def _kairos_live_scanner_loop():')
    scanner_end = source.find("if os.environ.get('RAILWAY_SERVICE_NAME') == 'kairos-poi-abc-sol'", scanner_start)
    if scanner_start >= 0 and scanner_end > scanner_start:
        original_scanner = source[scanner_start:scanner_end]

    if IMPORT_LINE not in source:
        anchor = 'import scalp_engine\n'
        if source.count(anchor) != 1:
            raise RuntimeError('Expected exactly one scalp_engine import anchor')
        source = source.replace(anchor, anchor + IMPORT_LINE, 1)

    capture_start, capture_end = _function_slice(
        source, '_kairos_send_capture_events', '_kairos_deliver_setup_once'
    )
    capture_block = source[capture_start:capture_end]
    if 'msg = format_capture_message(pair, event)' not in capture_block:
        msg_start = capture_block.find('        msg = (\n')
        send_anchor = capture_block.find('        if send_telegram(msg):', msg_start)
        if msg_start < 0 or send_anchor < 0:
            raise RuntimeError('Capture message block anchor missing')
        capture_block = (
            capture_block[:msg_start]
            + '        msg = format_capture_message(pair, event)\n'
            + capture_block[send_anchor:]
        )
        source = source[:capture_start] + capture_block + source[capture_end:]

    setup_start, setup_end = _function_slice(
        source, '_kairos_send_setup_events', '_kairos_format_entry_observation'
    )
    setup_block = source[setup_start:setup_end]
    if 'msg=format_setup_message(pair,event)' not in setup_block:
        msg_start = setup_block.find('        msg=(\n')
        deliver_anchor = setup_block.find('        if _kairos_deliver_setup_once(pair,event,msg):', msg_start)
        if msg_start < 0 or deliver_anchor < 0:
            raise RuntimeError('Setup message block anchor missing')
        setup_block = (
            setup_block[:msg_start]
            + '        msg=format_setup_message(pair,event)\n'
            + setup_block[deliver_anchor:]
        )
        source = source[:setup_start] + setup_block + source[setup_end:]

    obs_start, obs_end = _function_slice(
        source, '_kairos_format_entry_observation', '_kairos_replay_failure_audit'
    )
    obs_block = source[obs_start:obs_end]
    if 'return format_retest_message(pair, signal)' not in obs_block:
        replacement = (
            'def _kairos_format_entry_observation(pair, signal):\n'
            '    """Presentation only; causal/retest state remains owned by scalp_engine."""\n'
            '    return format_retest_message(pair, signal)\n\n\n'
        )
        source = source[:obs_start] + replacement + source[obs_end:]

    if original_scanner is not None:
        new_start = source.find('def _kairos_live_scanner_loop():')
        new_end = source.find("if os.environ.get('RAILWAY_SERVICE_NAME') == 'kairos-poi-abc-sol'", new_start)
        if source[new_start:new_end] != original_scanner:
            raise RuntimeError('Safety check failed: live scanner logic changed')

    compile(source, 'app.py', 'exec')
    required = (
        'msg = format_capture_message(pair, event)',
        'msg=format_setup_message(pair,event)',
        'return format_retest_message(pair, signal)',
        'jobs={pool.submit(scalp_engine._kairos_scan_latest_closed',
    )
    missing = [item for item in required if item not in source]
    if missing:
        raise RuntimeError(f'Telegram UI patch incomplete: {missing}')
    return source


def main():
    path = Path('app.py')
    source = path.read_text(encoding='utf-8')
    patched = patch_app_source(source)
    path.write_text(patched, encoding='utf-8')
    print('[KAIROS_TELEGRAM_UI_V1] capture/setup/retest presentation patch PASS; causal math unchanged')


if __name__ == '__main__':
    main()
