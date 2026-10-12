from patch_telegram_delivery_diag import patch_app_source

OLD = '''TELEGRAM_TOKEN = "secret-token"\nTELEGRAM_CHAT_ID = "123456"\n\ndef send_telegram(message):\n    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:\n        return False\n    try:\n        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"\n        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)\n        return response.status_code == 200 and response.json().get('ok') is True\n    except Exception as e:\n        print(f"Telegram erro: {e}")\n        return False\n\n\ndef next_function():\n    pass\n'''


def test_patch_adds_safe_failure_diagnostics_and_is_idempotent():
    patched = patch_app_source(OLD)
    assert '[KAIROS_TELEGRAM_DIAG]' in patched
    assert 'http_status=' in patched
    assert 'error_code=' in patched
    assert 'description=' in patched
    assert '_telegram_safe_text' in patched
    assert patch_app_source(patched) == patched
    compile(patched, 'app.py', 'exec')


class FakeResponse:
    status_code = 400
    text = '{"ok":false}'

    def json(self):
        return {'ok': False, 'error_code': 400, 'description': 'Bad Request secret-token chat 123456'}


class FakeRequests:
    def post(self, *args, **kwargs):
        return FakeResponse()


def test_runtime_logs_status_and_redacts_credentials(capsys):
    patched = patch_app_source(OLD)
    ns = {'requests': FakeRequests()}
    exec(patched, ns)
    assert ns['send_telegram']('<b>x</b>') is False
    out = capsys.readouterr().out
    assert 'http_status=400' in out
    assert 'error_code=400' in out
    assert 'secret-token' not in out
    assert '123456' not in out
    assert '[REDACTED]' in out
