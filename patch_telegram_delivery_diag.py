from pathlib import Path

MARK = '# KAIROS_TELEGRAM_DELIVERY_DIAG_V1'


def patch_app_source(source):
    if MARK in source:
        return source
    start = source.find('def send_telegram(message):')
    if start < 0:
        raise RuntimeError('send_telegram anchor missing')
    end = source.find('\ndef ', start + len('def send_telegram(message):'))
    if end < 0:
        raise RuntimeError('send_telegram end anchor missing')
    replacement = r'''# KAIROS_TELEGRAM_DELIVERY_DIAG_V1

def _telegram_safe_text(value):
    """Redact configured Telegram credentials before diagnostic logging."""
    text = str(value or '')
    for secret in (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID):
        if secret:
            text = text.replace(str(secret), '[REDACTED]')
    return text[:400].replace('\n', ' ')


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print('[KAIROS_TELEGRAM_DIAG] send_failed reason=MISSING_CONFIG', flush=True)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code == 200 and payload.get('ok') is True:
            return True
        description = payload.get('description')
        if not description:
            description = getattr(response, 'text', '') or 'NO_DESCRIPTION'
        print(
            '[KAIROS_TELEGRAM_DIAG] send_failed '
            f"http_status={response.status_code} "
            f"error_code={payload.get('error_code')} "
            f"description={_telegram_safe_text(description)}",
            flush=True,
        )
        return False
    except Exception as exc:
        print(
            '[KAIROS_TELEGRAM_DIAG] request_exception '
            f"type={type(exc).__name__} detail={_telegram_safe_text(exc)}",
            flush=True,
        )
        return False

'''
    patched = source[:start] + replacement + source[end + 1:]
    compile(patched, 'app.py', 'exec')
    return patched


def main():
    path = Path('app.py')
    source = path.read_text()
    patched = patch_app_source(source)
    path.write_text(patched)
    print('[KAIROS_TELEGRAM_DIAG] patch PASS; diagnostics only; causal math unchanged')


if __name__ == '__main__':
    main()
