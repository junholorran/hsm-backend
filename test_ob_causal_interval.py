import json
import unittest
from unittest.mock import patch
from pathlib import Path
import scalp_engine as e


class ObCausalIntervalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(Path('sol_ob_tie_fixture.json').read_text())

    def test_sol_lux_tied_high_origin_remains_eligible(self):
        d = self.fixture
        zone = e._kairos_select_entry_zone(d['M15'], dict(d['sweep']), dict(d['structure']), {})
        self.assertIsNotNone(zone)
        self.assertEqual(zone['tipo'], 'OB_bearish')
        self.assertEqual((zone['bottom'], zone['top']), (118.53, 119.03))
        self.assertEqual(zone['t'], 1791354600000)

    def test_ob_origin_before_capture_remains_ineligible(self):
        d = self.fixture
        sweep = {**d['sweep'], 'sweep_ts':1791355500000}
        self.assertIsNone(e._kairos_select_entry_zone(d['M15'], sweep, d['structure'], {}))

    def test_mirrored_long_uses_native_lux_ob_interval(self):
        d = self.fixture
        candles = [{**c, 'o':200-c['o'], 'c':200-c['c'], 'h':200-c['l'], 'l':200-c['h']} for c in d['M15']]
        structure = {**d['structure'], 'direcao':'alta', 'nivel':200-d['structure']['nivel']}
        zone = e._kairos_select_entry_zone(candles, {**d['sweep'],'direcao':'alta'}, structure, {})
        self.assertIsNotNone(zone)
        self.assertEqual(zone['tipo'], 'OB_bullish')
        self.assertAlmostEqual(zone['bottom'], 80.97)
        self.assertAlmostEqual(zone['top'], 81.47)

    def test_sol_authorized_ob_refines_using_its_native_leg(self):
        d = self.fixture
        # Isolate already audited HTF capture; all M15/M5 selection is real.
        with patch.object(e, '_kairos_select_structural_first_capture_sweep',
                          return_value=(dict(d['sweep']), {})):
            result = e.avaliar_vortex_decision_layer_v2(
                d['M15'], d['M5'], [],
                candles_por_tf={'M15':d['M15'], 'M5':d['M5']},
                cutoff_ts=1791366300000, audit_entries_only=True)
        self.assertEqual(result['failure_reason'], 'AGUARDANDO_RETESTE_ZONA')
        self.assertEqual(result['entry_setup']['ready_ts'], 1791366300000)
        self.assertAlmostEqual(result['entry_setup']['level'], 118.83)

