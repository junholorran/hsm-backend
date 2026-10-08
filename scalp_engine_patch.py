# Compatibility/telemetry patch source used for audit traceability.
# The functional definitions are applied directly at the top of scalp_engine.py.
def _kairos_normalize_instrument(pair: str) -> str:
    p = str(pair).strip().upper().replace("PERP", "")
    if p.endswith("USDT"):
        p = p[:-4]
    elif p.endswith("USD"):
        p = p[:-3]
    return f"{p}USDT"

def _kairos_w1_bounds_utc(ts_ms: int):
    import datetime
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    start = dt - datetime.timedelta(days=dt.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=7)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

WHY_CODES = ["OK_ARMED","MISSING_M15_COVERAGE","REACTION_UNRESOLVED_GAP","ALREADY_CONSUMED_HISTORICAL","MISSING_RETEST_98_M5","M15_INTERNAL_INVERTS_MAJOR","LEVEL_NOT_CONFIRMED"]
