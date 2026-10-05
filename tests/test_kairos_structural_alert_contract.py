"""Contract tests for the KAIROS structural radar.

The radar alert is intentionally earlier than the full execution setup:
relevant HTF liquidity -> confirmed sweep -> confirmed M15 structure intent -> alert.
FVG/OB/retest/entry/RR are downstream analysis and must not veto this alert.
"""


def should_emit_structural_alert(*, liquidity_relevant, sweep_confirmed, m15_structure_confirmed):
    """Temporary specification helper; production must provide this contract."""
    raise NotImplementedError("production structural-alert gate not implemented yet")


def test_alert_requires_all_three_structural_facts():
    assert should_emit_structural_alert(
        liquidity_relevant=True,
        sweep_confirmed=True,
        m15_structure_confirmed=True,
    ) is True


def test_no_alert_without_relevant_liquidity():
    assert should_emit_structural_alert(
        liquidity_relevant=False,
        sweep_confirmed=True,
        m15_structure_confirmed=True,
    ) is False


def test_no_alert_without_confirmed_sweep():
    assert should_emit_structural_alert(
        liquidity_relevant=True,
        sweep_confirmed=False,
        m15_structure_confirmed=True,
    ) is False


def test_no_alert_without_confirmed_m15_structure():
    assert should_emit_structural_alert(
        liquidity_relevant=True,
        sweep_confirmed=True,
        m15_structure_confirmed=False,
    ) is False
