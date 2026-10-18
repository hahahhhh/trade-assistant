import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('trade_form', ROOT / 'trade_form.py')
tf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tf)
config = tf.wh6.load_config()

RECOGNIZED = {
    'variety': 'V', 'contract': 'PVC2701',
    'bid1': 4902.0, 'ask1': 4903.0, 'last': 4903.0,
    'ocr_confidence': 0.86, 'source': 'wh6_ocr', 'ocr_seconds': 1.15,
}


def fields(**overrides):
    base = {
        'direction': 'long', 'price_mode': 'opponent', 'size': 1,
        'stop_loss': 4873.0, 'take_profit': 4963.0,
        'smc_tag': 'FVG', 'htf_bias': 'long', 'is_add_on': False,
    }
    base.update(overrides)
    return base


class FormLogicTests(unittest.TestCase):
    def test_module_imports_without_tkinter(self):
        # GUI toolkit must be imported lazily so logic stays testable headless.
        self.assertTrue(hasattr(tf, 'run_form'))
        self.assertNotIn('tkinter', sys.modules)

    def test_suggest_prices_long(self):
        prices = tf.suggest_prices(4903, 'long', 1, 30, 60)
        self.assertEqual(prices['stop_loss'], 4873)
        self.assertEqual(prices['take_profit'], 4963)

    def test_suggest_prices_short(self):
        prices = tf.suggest_prices(4902, 'short', 1, 30, 60)
        self.assertEqual(prices['stop_loss'], 4932)
        self.assertEqual(prices['take_profit'], 4842)

    def test_suggest_prices_rejects_bad_direction(self):
        with self.assertRaises(ValueError):
            tf.suggest_prices(4903, 'sideways', 1, 30, 60)

    def test_ticks_between(self):
        self.assertEqual(tf.ticks_between(19890, 19790, 5), 20)
        self.assertEqual(tf.ticks_between(19890, 19890, 5), 1)
        with self.assertRaises(ValueError):
            tf.ticks_between(1, 2, 0)

    def test_estimate_entry_by_mode(self):
        self.assertEqual(tf.estimate_entry(RECOGNIZED, 'opponent', 'long', 1), 4903)
        self.assertEqual(tf.estimate_entry(RECOGNIZED, 'opponent', 'short', 1), 4902)
        self.assertEqual(tf.estimate_entry(RECOGNIZED, 'queue', 'long', 1), 4902)
        self.assertEqual(tf.estimate_entry(RECOGNIZED, 'last', 'long', 1), 4903)
        self.assertIsNone(tf.estimate_entry(RECOGNIZED, 'manual', 'long', 1))

    def test_form_plan_passes_below_cap(self):
        result = tf.build_plan_from_form(fields(), RECOGNIZED, config)
        self.assertEqual(result['check']['status'], 'PASS')
        self.assertEqual(result['plan']['contract'], 'V2701')
        self.assertEqual(result['check']['risk']['total_risk_rmb'], 150.0)

    def test_form_plan_warns_at_cap(self):
        result = tf.build_plan_from_form(fields(stop_loss=4863.0), RECOGNIZED, config)
        self.assertEqual(result['check']['status'], 'WARN')

    def test_form_plan_rejects_over_cap(self):
        result = tf.build_plan_from_form(fields(stop_loss=4862.0), RECOGNIZED, config)
        self.assertEqual(result['check']['status'], 'REJECT')

    def test_form_version_is_exposed(self):
        self.assertRegex(tf.FORM_VERSION, r"^\d+\.\d+$")

    def test_manual_prefill_uses_last_quote(self):
        prefill = tf.manual_prefill(RECOGNIZED)
        self.assertEqual(prefill['contract'], 'PVC2701')
        self.assertEqual(prefill['bid1'], '4902')
        self.assertEqual(prefill['ask1'], '4903')
        self.assertEqual(prefill['last'], '4903')

    def test_manual_prefill_blank_without_quote(self):
        prefill = tf.manual_prefill({})
        self.assertEqual(prefill, {'contract': '', 'bid1': '', 'ask1': '', 'last': ''})

    def test_config_values_for_configured_instrument(self):
        values = tf.instrument_form_values(config, 'V')
        self.assertEqual(values['value_per_point'], '5')
        self.assertEqual(values['min_tick'], '1')
        self.assertTrue(values['configured'])

    def test_config_values_for_unconfigured_instrument_are_blank(self):
        values = tf.instrument_form_values(config, 'RB')
        self.assertEqual(values['value_per_point'], '')
        self.assertFalse(values['configured'])

    def test_build_config_derives_value_per_tick(self):
        values = {'value_per_point': '10', 'min_tick': '5',
                  'single_trade_limit_rmb': '300', 'configured': True}
        updated = tf.build_instrument_config(config, 'RB', values)
        instrument = updated['instruments']['RB']
        self.assertEqual(instrument['value_per_tick'], 50)
        self.assertTrue(instrument['configured'])
        self.assertTrue(config['instruments']['RB']['configured'] is False)

    def test_build_config_rejects_bad_numbers(self):
        for bad in (
            {'value_per_point': '0', 'min_tick': '1', 'single_trade_limit_rmb': '200'},
            {'value_per_point': 'abc', 'min_tick': '1', 'single_trade_limit_rmb': '200'},
            {'value_per_point': '10', 'min_tick': '', 'single_trade_limit_rmb': '200'},
            {'value_per_point': '10', 'min_tick': '1', 'single_trade_limit_rmb': '-5'},
        ):
            with self.assertRaises(ValueError):
                tf.build_instrument_config(config, 'RB', {**bad, 'configured': True})

    def test_save_config_roundtrips_through_loader(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cfg.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            values = {'value_per_point': '10', 'min_tick': '1', 'single_trade_limit_rmb': '300',
                      'round_trip_cost_per_lot': '3', 'slippage_buffer_per_lot': '2', 'configured': True}
            reloaded = tf.save_instrument_config(config, 'RB', values, path)
            self.assertEqual(reloaded['instruments']['RB']['value_per_tick'], 10)
            self.assertTrue(path.with_suffix('.json.bak').exists())

    def test_summarize_and_market_summary(self):
        result = tf.build_plan_from_form(fields(), RECOGNIZED, config)
        text = tf.summarize(result)
        self.assertIn('PASS', text)
        self.assertIn('150', text)
        self.assertIn('卖一 4903', tf.market_summary(RECOGNIZED))
        self.assertIn('1.15', tf.market_summary(RECOGNIZED))


if __name__ == '__main__':
    unittest.main()
