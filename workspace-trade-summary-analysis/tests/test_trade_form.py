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

    def test_summarize_and_market_summary(self):
        result = tf.build_plan_from_form(fields(), RECOGNIZED, config)
        text = tf.summarize(result)
        self.assertIn('PASS', text)
        self.assertIn('150', text)
        self.assertIn('卖一 4903', tf.market_summary(RECOGNIZED))
        self.assertIn('1.15', tf.market_summary(RECOGNIZED))


if __name__ == '__main__':
    unittest.main()
