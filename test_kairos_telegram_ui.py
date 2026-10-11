import unittest

from kairos_telegram_ui import (
    format_capture_message,
    format_setup_message,
    format_retest_message,
)


class KairosTelegramUiTests(unittest.TestCase):
    def test_monthly_capture_keeps_mn_explicit_and_explains_next_step(self):
        msg = format_capture_message('BTCUSD', {
            'liquidity_tf': 'MN', 'liquidity_type': 'PMH', 'level': 92500.0,
            'reaction': 'UNRESOLVED_REACTION',
            'context': {'MN': 'alta', 'W1': 'baixa', 'D1': 'baixa', 'H4': 'alta', 'H1': 'alta'},
        })
        self.assertIn('MN PMH @ 92500', msg)
        self.assertIn('W1 ↓ | D1 ↓ | H4 ↑ | H1 ↑', msg)
        self.assertIn('captura sozinha não define direção', msg)
        self.assertIn('aguardando reação / confirmação M15', msg)
        self.assertIn('SEM TRADE', msg)

    def test_weekly_low_capture_explains_both_neutral_scenarios(self):
        msg = format_capture_message('BTCUSD', {
            'liquidity_tf': 'W1', 'liquidity_type': 'SWING_LOW', 'level': 80250.0,
            'reaction': 'UNRESOLVED_REACTION',
            'context': {'MN': 'neutro', 'W1': 'baixa', 'D1': 'baixa', 'H4': 'baixa', 'H1': 'alta'},
        })
        self.assertIn('W1 SWING_LOW @ 80250', msg)
        self.assertIn('Rejeição / recuperação + M15 bullish', msg)
        self.assertIn('Aceitação abaixo + M15 bearish', msg)

    def test_resolved_high_acceptance_tells_user_what_is_expected_next(self):
        msg = format_capture_message('BNBUSD', {
            'liquidity_tf': 'H1', 'liquidity_type': 'EQH', 'level': 751.0,
            'reaction': 'ACCEPTANCE_CONTINUATION',
            'context': {'MN': 'neutro', 'W1': 'neutro', 'D1': 'alta', 'H4': 'alta', 'H1': 'alta'},
        })
        self.assertIn('ACEITAÇÃO / CONTINUAÇÃO', msg)
        self.assertIn('favorece continuação bullish', msg)
        self.assertIn('M15 Major + Internal', msg)
        self.assertNotIn('entrada autorizada', msg.lower())

    def test_setup_message_reads_as_complete_chain_without_claiming_fill(self):
        msg = format_setup_message('PENDLEUSD', {
            'direction': 'LONG', 'capture_tf': 'D1', 'capture_level': 2.209,
            'major_confirm_ts': 1791729000000, 'internal_confirm_ts': 1791729900000,
            'zone_type': 'OB_bullish', 'bottom': 2.202, 'top': 2.21, 'level': 2.21,
            'poi': {'refinement_basis': 'M15_AUTHORIZED_M5_CONTINUATION_BOS_OB'},
        })
        self.assertIn('SETUP M5 ARMADO', msg)
        self.assertIn('PENDLEUSD | LONG', msg)
        self.assertIn('Origem: D1 @ 2.209', msg)
        self.assertIn('M15 Major: bullish', msg)
        self.assertIn('M15 Internal: bullish', msg)
        self.assertIn('M5 BOS + POI causal', msg)
        self.assertIn('OB_bullish M5: 2.202–2.21', msg)
        self.assertIn('Proximal / entrada planeada: 2.21', msg)
        self.assertIn('CADEIA CAUSAL COMPLETA', msg)
        self.assertIn('aguardar retorno ao poi', msg.lower())
        self.assertNotIn('preenchido', msg.lower())

    def test_retest_message_preserves_observation_not_fill_and_uses_portugal_time(self):
        msg = format_retest_message('PENDLEUSD', {
            'direction': 'LONG', 'zone_type': 'OB_bullish', 'zone_bottom': 2.202, 'zone_top': 2.21,
            'entry': 2.21, 'observed_retest_close': 2.205,
            'm15_internal_confirmation_close_ts': 1791729900000,
            'retest_confirm_close_ts': 1791741900000,
        })
        self.assertIn('RETESTE M5', msg)
        self.assertIn('PENDLEUSD | LONG', msg)
        self.assertIn('POI M5 retestado', msg)
        self.assertIn('Proximal planeada: 2.21', msg)
        self.assertIn('Fecho observado: 2.205', msg)
        self.assertIn('Portugal', msg)
        self.assertIn('preenchimento não comprovado', msg.lower())


if __name__ == '__main__':
    unittest.main()
