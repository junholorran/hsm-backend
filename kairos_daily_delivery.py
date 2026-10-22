"""Opt-in daily context delivery through the existing Kairos Telegram sender."""
import copy
import html
import json
import os
import sqlite3
import threading

from kairos_daily_report import DAY, daily_hypothesis, build_report, notification_parts, VERSION

PAIRS = ('BTCUSD', 'ETHUSD', 'SOLUSD', 'BNBUSD')
_CACHE = {}
_CACHE_LOCK = threading.RLock()
_DELIVERY_LOCK = threading.RLock()


def enabled():
    return os.environ.get('KAIROS_DAILY_BRIEF_ENABLED', 'false').strip().lower() == 'true'


def scan_report(pair, tf_map, cutoff, engine, decision):
    """At most one current map per pair/closed M5, using already fetched bars.

    A first scan after restart may catch up the current day, never older days.
    Its map is explicitly as-of that first scan, not backdated to midnight.
    """
    if not enabled() or pair not in PAIRS:
        return None
    daily = daily_hypothesis(tf_map.get('D1') or [], cutoff)
    if daily['status'] != 'OK':
        return None
    with _CACHE_LOCK:
        previous = _CACHE.get(pair)
        if previous and previous['asof_ts'] == cutoff:
            return copy.deepcopy(previous)
        registry = (decision.get('structural_sweep_audit') or {}).get('levels')
        if registry is None:
            registry = engine._kairos_structural_registry(tf_map, cutoff)
        report = build_report(pair, tf_map['D1'], registry, cutoff, cutoff)
        report['structure_context'] = copy.deepcopy(decision.get('context_bias') or {})
        from kairos_context_map import enrich_report
        report = enrich_report(report, tf_map, decision)
        if report['status'] == 'READY_CANDIDATE':
            _CACHE[pair] = copy.deepcopy(report)
        return report


def deliver(report, sender, db_path):
    """Persist per-page receipts only after the existing sender returns True.

    Partially delivered payloads are frozen so a restart cannot change the
    remaining pages. A crash after Telegram accepts but before local commit
    can still duplicate that page: the Telegram API has no idempotency key.
    Failure never affects the legacy signal watermark.
    """
    if (not enabled() or not report or report.get('pair') not in PAIRS
            or report.get('status') != 'READY_CANDIDATE'):
        return False
    try:
        parts = notification_parts(report)
        pair = report['pair']; day = report['daily']['available_ts']
        version = VERSION + (':'+report['delivery_key'] if report.get('delivery_key') else '')
        # Local worker serialization; SQLite also serializes multiple processes.
        with _DELIVERY_LOCK, sqlite3.connect(db_path, timeout=2) as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_delivery '
                       '(pair TEXT, day INTEGER, version TEXT, parts TEXT, acknowledged INTEGER, '
                       'PRIMARY KEY(pair,day,version))')
            db.execute('CREATE TABLE IF NOT EXISTS context_current '
                       '(pair TEXT,day INTEGER,version TEXT,state_key TEXT,delivery_version TEXT,last_asof INTEGER,'
                       'PRIMARY KEY(pair,day,version))')
            db.execute('BEGIN IMMEDIATE')
            if report.get('delivery_key'):
                current=db.execute('SELECT state_key,delivery_version,last_asof FROM context_current '
                                   'WHERE pair=? AND day=? AND version=?',(pair,day,VERSION)).fetchone()
                if current and current[2]>report['asof_ts']:
                    db.commit();return True  # Ignore an obsolete queued snapshot.
                if current and current[0]==report['delivery_key']:
                    version=current[1]  # Resume the exact partially acknowledged occurrence.
                else:
                    version=VERSION+':'+report['delivery_key']+':'+str(report['asof_ts'])
                db.execute('INSERT OR REPLACE INTO context_current VALUES (?,?,?,?,?,?)',
                           (pair,day,VERSION,report['delivery_key'],version,report['asof_ts']))
            db.execute('INSERT OR IGNORE INTO daily_delivery VALUES (?,?,?,?,0)', 
                       (pair, day, version, json.dumps(parts, ensure_ascii=False)))
            db.commit()
            while True:
                db.execute('BEGIN IMMEDIATE')
                payload, acknowledged = db.execute(
                    'SELECT parts,acknowledged FROM daily_delivery WHERE pair=? AND day=? AND version=?',
                    (pair,day,version)).fetchone()
                frozen = json.loads(payload)
                if acknowledged >= len(frozen):
                    db.commit(); return True
                # Existing sender uses HTML parse mode; escape plain-text payloads.
                message = html.escape(frozen[acknowledged])
                if sender(message) is not True:
                    db.rollback(); return False
                db.execute('UPDATE daily_delivery SET acknowledged=? WHERE pair=? AND day=? AND version=?',
                           (acknowledged+1,pair,day,version))
                db.commit()
                if acknowledged+1==len(frozen):
                    print(f'[KAIROS_CONTEXT_MAP] delivered pair={pair} day={day} key={version} pages={len(frozen)} asof={report["asof_ts"]}',flush=True)
    except Exception as exc:
        print(f'[KAIROS_DAILY_BRIEF] delivery/storage error: {type(exc).__name__}', flush=True)
        return False


_PENDING = {}
_WORKER = None
_QUEUE_LOCK = threading.Lock()


def _drain(sender, db_path):
    global _WORKER
    while True:
        with _QUEUE_LOCK:
            if not _PENDING:
                _WORKER = None
                return
            _, report = _PENDING.popitem()
        deliver(report, sender, db_path)


def submit(report, sender, db_path):
    """Bounded background queue; no delay to capture/setup/entry delivery."""
    global _WORKER
    if not enabled() or not report or report.get('pair') not in PAIRS:
        return
    with _QUEUE_LOCK:
        _PENDING[report['pair']] = copy.deepcopy(report)
        if _WORKER is None:
            _WORKER = threading.Thread(target=_drain, args=(sender,db_path),daemon=True)
            _WORKER.start()
