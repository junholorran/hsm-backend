"""KAIROS structural radar overlay.

Alert contract: relevant HTF liquidity -> confirmed first capture/sweep ->
confirmed M15 BOS/CHoCH in the reaction direction -> Telegram alert.
FVG/IFVG/OB remain useful POIs shown when available, but they do not veto
the structural alert. No exchange order is sent.
"""

import sqlite3
from datetime import datetime, timezone


def should_emit_structural_alert(liquidity_relevant, sweep_confirmed, m15_structure_confirmed):
    return bool(liquidity_relevant and sweep_confirmed and m15_structure_confirmed)


def install(engine):
    if getattr(engine, "_KAIROS_STRUCTURAL_RADAR_INSTALLED", False):
        return

    original_eval = engine.avaliar_vortex_decision_layer_v2

    def direction_after_capture(candles, capture, swing_size=5):
        """Direction comes from post-sweep reaction + CLOSED M15 structure.

        Deliberately does not require momentum_z or body/ATR thresholds. Those
        are telemetry, not authorization for the radar notification.
        """
        if not candles or not capture:
            return None
        side = capture.get("liquidity_side")
        state = capture.get("post_capture_state")
        if side not in ("HIGH", "LOW") or state not in (
            "REJECTION_RECLAIM", "ACCEPTANCE_CONTINUATION"
        ):
            return None

        if state == "REJECTION_RECLAIM":
            expected = "baixa" if side == "HIGH" else "alta"
            mode = "REVERSAL"
        else:
            expected = "alta" if side == "HIGH" else "baixa"
            mode = "CONTINUATION"

        sweep_ts = capture.get("sweep_ts")
        if sweep_ts is None:
            return None
        idx = next((i for i, c in enumerate(candles) if c.get("t", 0) >= sweep_ts), None)
        if idx is None:
            return None

        sub = candles[max(0, idx - swing_size - 2):]
        events = engine.compute_lux_internal_structure(sub, swing_size=swing_size)
        for event in events:
            if event.get("t", 0) <= sweep_ts:
                continue
            if event.get("direcao") != expected:
                continue
            if event.get("tipo") not in ("CHoCH", "BOS"):
                continue
            full_idx = next((j for j, c in enumerate(candles) if c.get("t") == event.get("t")), None)
            if full_idx is None:
                continue
            causal = candles[:full_idx + 1]
            try:
                z = engine._kairos_momentum_z(causal)
            except Exception:
                z = None
            return {
                "direction": "LONG" if expected == "alta" else "SHORT",
                "direcao": expected,
                "mode": mode,
                "structure": {**event, "full_idx": full_idx},
                "momentum_z": z,
            }
        return None

    def radar_eval(*args, **kwargs):
        result = original_eval(*args, **kwargs)
        ready = should_emit_structural_alert(
            bool(result.get("liquidity_tf") and result.get("first_capture_ts")),
            bool(result.get("sweep_level") is not None),
            bool(result.get("direction") and result.get("choch_confirmed")),
        )
        result["structural_alert_ready"] = ready
        if ready and not result.get("valid"):
            previous = result.get("failure_reason")
            if previous != "AGUARDANDO_RETESTE_ZONA":
                result["execution_failure_reason"] = previous
                result["failure_reason"] = "AGUARDANDO_RETESTE_ZONA"
        return result

    def format_radar_message(pair, r):
        ts_ms = r.get("choch_timestamp") or r.get("sweep_confirm_ts")
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts_ms else "N/A"
        zone_type = r.get("zone_type")
        if zone_type:
            zone = f"{zone_type} [{r.get('zone_bottom')} — {r.get('zone_top')}]"
        else:
            zone = "ainda sem FVG/IFVG/OB causal confirmado"
        return (
            "🚨 <b>KAIROS — INTENÇÃO ESTRUTURAL</b>\n"
            f"Par: {pair}\n"
            f"Direção: <b>{r.get('direction')}</b>\n"
            f"Liquidez relevante: {r.get('liquidity_tf')} {r.get('liquidity_type')} @ {r.get('sweep_level')}\n"
            f"Sweep/first capture: CONFIRMADO | extremo {r.get('sweep_extreme')}\n"
            f"M15: quebra estrutural confirmada @ {r.get('choch_level')}\n"
            f"POI causal: {zone}\n"
            f"SL estrutural de referência: atrás do extremo do sweep {r.get('sweep_extreme')}\n"
            f"Horário da estrutura: {ts}\n"
            "👀 Radar estrutural: analisar manualmente FVG/OB e entrada.\n"
            "⚠️ Não é ordem automática; execução manual."
        )

    def radar_prealert(db_file, pair, r, agora_ts_ms):
        if not r.get("structural_alert_ready"):
            return False
        if not engine._garantir_tabela_prealerta_paper_v2(db_file):
            return False
        setup_key = engine._paper_v2_prealert_setup_key(pair, r)
        try:
            with sqlite3.connect(db_file) as conn:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO paper_trading_v2_prealertas (
                    setup_key, pair, direction, liquidity_tf, liquidity_type,
                    sweep_level, first_capture_ts, sweep_confirm_ts,
                    choch_timestamp, choch_level, zone_type, zone_top, zone_bottom,
                    limit_price, sl_ref, tp_ref, rr_ref, criado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        setup_key, pair, r.get("direction"), r.get("liquidity_tf"), r.get("liquidity_type"),
                        r.get("sweep_level"), r.get("first_capture_ts"), r.get("sweep_confirm_ts"),
                        r.get("choch_timestamp"), r.get("choch_level"), r.get("zone_type"),
                        r.get("zone_top"), r.get("zone_bottom"), r.get("prealert_limit"),
                        r.get("sweep_extreme"), None, None, agora_ts_ms,
                    ),
                )
                conn.commit()
                if cur.rowcount <= 0:
                    return False
            ok = engine._paper_trading_v2_enviar_telegram(format_radar_message(pair, r))
            print(f"[kairos_structural_radar] {'ENVIADO' if ok else 'FALHA_TELEGRAM'} {pair} key={setup_key[:12]}")
            return bool(ok)
        except Exception as exc:
            print(f"[kairos_structural_radar] erro {pair}: {exc}")
            return False

    engine._kairos_direction_after_first_capture = direction_after_capture
    engine.avaliar_vortex_decision_layer_v2 = radar_eval
    engine._paper_v2_tentar_prealerta = radar_prealert
    engine._KAIROS_STRUCTURAL_RADAR_INSTALLED = True
