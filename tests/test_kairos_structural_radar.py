from kairos_structural_radar import should_emit_structural_alert


def test_alert_after_relevant_sweep_and_m15_structure():
    assert should_emit_structural_alert(True, True, True) is True


def test_no_alert_without_relevant_liquidity():
    assert should_emit_structural_alert(False, True, True) is False


def test_no_alert_without_sweep():
    assert should_emit_structural_alert(True, False, True) is False


def test_no_alert_without_m15_structure():
    assert should_emit_structural_alert(True, True, False) is False
