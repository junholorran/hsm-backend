workers = 1
worker_class = "gthread"
threads = 4


def post_worker_init(worker):
    """Install KAIROS causal-proof telemetry after the WSGI app is loaded.

    Observability only: no trading math, gates, entries, SL/TP or Telegram
    behavior is changed. Failure to install telemetry never kills the worker.
    """
    try:
        from kairos_causal_proof import install
        installed = install()
        worker.log.info("KAIROS causal-proof telemetry installed=%s", installed)
    except Exception:
        worker.log.exception("KAIROS causal-proof telemetry install failed")
