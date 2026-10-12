import functools
import json
import threading

_LOCK = threading.Lock()
_SEEN = set()
_INSTALLED = False


def _event_payload(event, candles, close_ms):
    if not event:
        return None
    candle = next((c for c in candles if c.get("t") == event.get("t")), None)
    return {
        "event_type": event.get("tipo"),
        "direction": event.get("direcao"),
        "break_level": event.get("nivel"),
        "broken_swing_origin_ts": event.get("broken_swing_origin_ts"),
        "break_open_ts": event.get("t"),
        "break_close_ts": (event.get("t") + close_ms) if event.get("t") is not None else None,
        "break_close_price": candle.get("c") if candle else None,
        "protected_swing_type": event.get("protected_swing_type"),
        "protected_swing_level": event.get("protected_swing_level"),
        "protected_swing_origin_ts": event.get("protected_swing_origin_ts"),
        "leg_start_ts": event.get("leg_start_ts"),
        "leg_end_ts": event.get("leg_end_ts"),
    }


def _capture_payload(result):
    if result.get("authorization_path") == "HTF_FVG_TOUCH":
        return {}
    audit = result.get("structural_sweep_audit") or {}
    selected = audit.get("selected_capture") or {}
    candidates = audit.get("candidates") or []
    match = next((x for x in candidates if
        x.get("liquidity_tf") == selected.get("liquidity_tf") and
        x.get("liquidity_type") == selected.get("liquidity_type") and
        x.get("sweep_ts") == selected.get("sweep_ts") and
        x.get("nivel") == selected.get("nivel")), None)
    x = match or selected
    return {
        "liquidity_tf": x.get("liquidity_tf") or result.get("liquidity_tf"),
        "liquidity_type": x.get("liquidity_type") or result.get("liquidity_type"),
        "level": x.get("nivel") if x.get("nivel") is not None else result.get("sweep_level"),
        "liquidity_origin_ts": x.get("liquidity_origin_ts"),
        "liquidity_confirm_ts": x.get("liquidity_confirm_ts"),
        "capture_open_ts": x.get("first_capture_ts") or x.get("sweep_ts") or result.get("first_capture_ts"),
        "capture_confirm_ts": x.get("capture_confirm_ts") or result.get("sweep_confirm_ts"),
        "capture_evidence_tf": x.get("capture_evidence_tf") or result.get("capture_tf"),
        "side": x.get("liquidity_side"),
        "reaction": x.get("post_capture_state"),
        "extreme": x.get("extremo") if x.get("extremo") is not None else result.get("sweep_extreme"),
    }


def _emit(se, result, pair, cutoff_ts, m15):
    setup = result.get("entry_setup") or {}
    if not setup or result.get("failure_reason") != "AGUARDANDO_RETESTE_ZONA":
        return
    key = (pair or "NA", setup.get("key"))
    with _LOCK:
        if key in _SEEN:
            return
        _SEEN.add(key)
        if len(_SEEN) > 5000:
            _SEEN.clear()
            _SEEN.add(key)

    major_close = result.get("m15_major_confirmation_close_ts")
    major_open = major_close - 900000 if major_close is not None else None
    internal_open = result.get("m15_internal_confirmation_open_ts")
    major = next((e for e in se.compute_lux_structure_events(m15, swing_size=50) if e.get("t") == major_open), None)
    internal = next((e for e in se.compute_lux_internal_structure(m15, swing_size=5) if e.get("t") == internal_open), None)
    cap = _capture_payload(result)
    origin = result.get("causal_origin") or {}
    poi = setup.get("poi") or {}
    ready_ts = setup.get("ready_ts")
    internal_close = result.get("m15_internal_confirmation_close_ts")
    capture_ts = cap.get("capture_open_ts")
    checks = {
        "capture_level_confirmed_before_capture": cap.get("liquidity_confirm_ts") is None or capture_ts is None or cap.get("liquidity_confirm_ts") <= capture_ts,
        "major_event_found": major is not None,
        "internal_event_found": internal is not None,
        "major_known_by_cutoff": major_close is not None and cutoff_ts is not None and major_close <= cutoff_ts,
        "internal_known_by_cutoff": internal_close is not None and cutoff_ts is not None and internal_close <= cutoff_ts,
        "poi_ready_by_cutoff": ready_ts is not None and cutoff_ts is not None and ready_ts <= cutoff_ts,
        "capture_before_internal": capture_ts is not None and internal_open is not None and capture_ts < internal_open,
    }
    if origin.get("kind") == "HTF_FVG_TOUCH":
        checks.pop("capture_level_confirmed_before_capture", None)
        checks.pop("capture_before_internal", None)
        checks["fvg_formed_before_touch"] = origin.get("formation_close_ts") <= origin.get("touch_ts")
        checks["fvg_touch_before_major"] = origin.get("touch_confirm_close_ts") <= major_open
    proof = {
        "pair": pair,
        "cutoff_ts": cutoff_ts,
        "setup_key": setup.get("key"),
        "direction": setup.get("direction") or result.get("direction"),
        **({"origin": origin} if origin else {"capture": cap}),
        "m15_major": _event_payload(major, m15, 900000),
        "m15_internal": _event_payload(internal, m15, 900000),
        "m5_poi": {
            "type": setup.get("zone_type") or poi.get("tipo"),
            "bottom": setup.get("bottom"),
            "top": setup.get("top"),
            "proximal": setup.get("level"),
            "ready_ts": ready_ts,
            "origin_ts": poi.get("origin_ts") or poi.get("t"),
            "created_ts": poi.get("created_ts"),
            "refinement_basis": poi.get("refinement_basis"),
            "causal_break_m5": poi.get("causal_break_m5"),
            "causal_structure_m5": poi.get("causal_structure_m5"),
        },
        "checks": checks,
        "all_causal_proof_pass": all(checks.values()),
        "telemetry_only": True,
    }
    print("[KAIROS_CAUSAL_PROOF] " + json.dumps(proof, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)


def install():
    global _INSTALLED
    if _INSTALLED:
        return False
    import scalp_engine as se
    original = se.avaliar_vortex_decision_layer_v2
    if getattr(original, "_kairos_causal_proof_wrapped", False):
        _INSTALLED = True
        return False

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            m15 = args[0] if args else kwargs.get("m15_ate_agora") or []
            pair = kwargs.get("audit_pair")
            cutoff_ts = kwargs.get("cutoff_ts")
            _emit(se, result, pair, cutoff_ts, m15)
        except Exception as exc:
            print("[KAIROS_CAUSAL_PROOF_ERR] " + repr(exc), flush=True)
        return result

    wrapped._kairos_causal_proof_wrapped = True
    se.avaliar_vortex_decision_layer_v2 = wrapped
    _INSTALLED = True
    print("[KAIROS_CAUSAL_PROOF] telemetry_installed=true math_changed=false", flush=True)
    return True

