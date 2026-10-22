# Independent capture alerts implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Notify each captured liquidity independently through M15 structure, causal POI, M5 refinement and prospective retest.

**Architecture:** An opt-in observer consumes the existing native closed candles and capture audit. Separate SQLite state and outbox preserve every capture and notification across restart. The existing entry engine and detectors remain untouched.

**Tech Stack:** Python, SQLite, pytest, existing Telegram sender.

**Spec:** User-approved sequence in this conversation: independent captures, both structural directions, M15 before M5, causal zones, no real orders.

## Global constraints

- Four existing pairs only. No new market fetches or credentials.
- Existing Ludo body-close break and existing FVG/OB detectors only.
- Retests must start after the plan was actually observed; never assume a fill or manual stop.
- Bootstrap suppresses historical notifications. Restart resumes persisted state.
- Opt-in flag defaults false. No changes to legacy entry decisions.

## Review focus

- Second capture after invalidation must use a later break, never the first capture's break.
- Same-bar invalidation and touch must not imply a valid entry.
- Telegram failures must retain a durable pending message.
- Rolling histories, data gaps and forming candles must fail closed.
- Direction changes must invalidate the previous observational plan before arming another.

### Task 1: Independent observer and durable delivery

**Files:** Create `kairos_capture_chains.py`, `test_capture_chains.py`.

**Interfaces:** `advance(state, pair, captures, tf_map, cutoff, engine)` returns copied state and notification records. `scan(...)` persists state and outbox. `submit(pair, sender, path)` drains pending messages in a background worker.

- [x] Write tests for independent captures, causal timing, opposite breaks, prospective retests, gaps, bootstrap, restart and delivery retry.
- [x] Run `python -m pytest -q test_capture_chains.py`; expect missing module failure.
- [x] Implement observer, selector adapters and SQLite outbox.
- [x] Run targeted tests; expect all pass.

### Task 2: Additive scanner integration and verification

**Files:** Modify `scalp_engine.py`, `app.py`; document operational behavior.

**Interfaces:** Scanner calls `scan` with existing candles/audit; app queues delivery after existing notifications.

- [x] Add disabled/enabled integration tests, then implement guarded hooks.
- [x] Run `python -m pytest -q --ignore=legacy-mute`; expect full suite green.
- [x] Request independent whole-branch review, fix important findings with regression tests.
- [x] Commit tested code; deploy only after verification and review.

Validation: 253 tests and 53 subtests passed. Independent review found lifecycle, chronological ordering, dead-parent and stale-refinement blockers; each corrected with red-to-green regressions. Native SOL replay confirms bearish M15 break at 20:15 Portugal / 107.87 and a prospective M5 retest. Deployment verification follows the reviewed commit.
