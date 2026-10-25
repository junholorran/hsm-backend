import copy
import unittest

from kairos_daily_report import notification_parts
import test_daily_report_candidate as fixtures


class ReadableDailyMapTests(unittest.TestCase):
    def report(self):
        r = fixtures.ReportTests().report()
        r.update(current_price={'value': 109, 'close_ts': r['asof_ts']},
                 structure_context={'D1': 'alta', 'H4': 'baixa', 'H1': 'alta'},
                 stage={'code': 'AGUARDANDO_RETESTE_M5', 'setup': {
                     'tf': 'M5', 'bottom': 108.5, 'top': 109.5,
                     'zone_type': 'IFVG_bullish'}},
                 zones=[{'tf': 'H4', 'type': 'FVG_bullish', 'bottom': 20, 'top': 25,
                         'state': 'ATIVA', 'role': 'ZONA_PARA_OBSERVAR_RECUO'},
                        {'tf': 'H4', 'type': 'IFVG_bullish', 'bottom': 106, 'top': 108,
                         'state': 'IFVG', 'role': 'ZONA_PARA_OBSERVAR_RECUO'}])
        return r

    def test_summary_first_exact_evidence_and_all_inventory_retained_without_mutation(self):
        r = self.report(); before = copy.deepcopy(r)
        parts = notification_parts(r)
        self.assertIn('VIÉS DIÁRIO: ALTA', parts[0])
        self.assertIn('Fechamento 112 > máxima anterior 110', parts[0])
        self.assertIn('D1 ↑ | H4 ↓ | H1 ↑', parts[0])
        self.assertIn('106–108', parts[0])
        self.assertNotIn('20–25', parts[0])
        self.assertIn('IFVG M5: 108.5–109.5', parts[0])
        self.assertIn('Sem ordem ou execução confirmada', parts[0])
        self.assertIn('DETALHES DO MAPA', parts[1])
        self.assertIn('20–25', '\n'.join(parts[1:]))
        self.assertEqual(r, before)

    def test_independent_observation_is_not_announced_as_causal_break(self):
        r = self.report()
        r['m15_observation'] = dict(kind='BOS', side='DOWN', level=90,
                                    close_ts=r['asof_ts'], close=89)
        self.assertNotIn('BOS', notification_parts(r)[0])
        r['stage']['m15_break'] = r['m15_observation']
        self.assertIn('BOS M15 ↓ abaixo de 90', notification_parts(r)[0])

    def test_historical_or_invalidated_setup_not_advertised_as_active_zone(self):
        for code in ['RETESTE_HISTORICO_SEM_NOVA_ENTRADA', 'SETUP_ANTERIOR_INVALIDADO']:
            r = self.report(); r['stage']['code'] = code
            self.assertNotIn('108.5–109.5', notification_parts(r)[0])

    def test_missing_zone_and_unknown_stage_are_explicit_without_crashing(self):
        r = self.report(); r['stage'].pop('setup')
        self.assertIn('Zona M5: não informada', notification_parts(r)[0])
        r['stage']['code'] = 'FUTURE_UNKNOWN_STAGE'
        self.assertIn('Estado não reconhecido', notification_parts(r)[0])

    def test_neutral_both_sides_is_not_called_confirmed_reversal(self):
        r = self.report(); r['daily'].update(bias='NEUTRO', reason='BOTH_SIDES_CAPTURED')
        first = notification_parts(r)[0]
        self.assertIn('VIÉS DIÁRIO: NEUTRO', first)
        self.assertIn('Capturou os dois lados', first)
        self.assertNotIn('Continuação de alta', first)

    def test_small_pages_retain_rows_and_limits(self):
        r = self.report()
        parts = notification_parts(r, max_chars=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual(sum(line.startswith('H1 SWING_HIGH @')
                             for p in parts for line in p.splitlines()), 1)

    def test_btc_prices_preserve_distinct_decimal_boundaries(self):
        r = self.report()
        r['daily']['closed_day']['c'] = 112345.2
        r['daily']['previous_day']['h'] = 112345.1
        r['stage']['setup'].update(bottom=112345.1, top=112345.2)
        first = notification_parts(r)[0]
        self.assertIn('112345.2 > máxima anterior 112345.1', first)
        self.assertIn('112345.1–112345.2', first)

    def test_neutral_shows_nearest_zone_on_each_side(self):
        r = self.report(); r['daily'].update(bias='NEUTRO', reason='BOTH_SIDES_CAPTURED')
        r['zones'].append(dict(tf='H4', type='FVG_bearish', direction='baixa',
                               bottom=112, top=114, state='ATIVA', role='CONTEXTO'))
        first = notification_parts(r)[0]
        self.assertIn('106–108', first)
        self.assertIn('112–114', first)

    def test_frozen_capture_reaction_is_not_presented_as_current(self):
        r = self.report()
        r['stage']['capture'] = dict(liquidity_tf='D1', liquidity_type='PDH',
                                    nivel=110, post_capture_state='UNRESOLVED_REACTION')
        first = notification_parts(r)[0]
        self.assertIn('CAPTURA DA SEQUÊNCIA', first)
        self.assertNotIn('reação por resolver', first)
