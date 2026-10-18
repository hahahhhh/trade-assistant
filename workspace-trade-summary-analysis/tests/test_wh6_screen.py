import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wh6_screen', ROOT / 'wh6_screen.py')
wh6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wh6)
config = wh6.load_config()


class WH6Tests(unittest.TestCase):
    def test_parse_quote_panel(self):
        result = wh6.parse_wh6_ocr('PVC2701M DCE 卖出 4903 5 买入 4902 27 最新 4903 涨跌 55/1.13% 开盘 4870 最高 4938 最低 4860')
        self.assertEqual(result['variety'], 'V')
        self.assertEqual(result['contract'], 'PVC2701')
        self.assertEqual(result['ask1'], 4903)
        self.assertEqual(result['bid1'], 4902)
        self.assertEqual(result['last'], 4903)

    def test_parse_spaced_contract(self):
        result = wh6.parse_wh6_ocr('P V C 2 7 0 1 M 卖出 4903 买入 4902 最新 4903')
        self.assertEqual(result['variety'], 'V')
        self.assertEqual(result['contract'], 'PVC2701')

    def test_parse_spaced_quote_labels(self):
        result = wh6.parse_wh6_ocr('PVC2701 v2701 卖 出 4903 买 入 4902 最 新 4903')
        self.assertEqual(result['ask1'], 4903)
        self.assertEqual(result['bid1'], 4902)
        self.assertEqual(result['last'], 4903)

    def test_quote_price_fallback(self):
        result = wh6.parse_wh6_ocr('PVC2701 出 4903 EVN 4902 最 新 4903')
        self.assertEqual(result['ask1'], 4903)
        self.assertEqual(result['bid1'], 4902)
        self.assertEqual(result['last'], 4903)

    def test_quote_fallback_ignores_chart_prices_after_quote_rows(self):
        result = wh6.parse_wh6_ocr('PVC2701 v2701 出 4903 5 入 4902 27 新 4903 涨跌 55/1.13% 4950 4935')
        self.assertEqual(result['ask1'], 4903)
        self.assertEqual(result['bid1'], 4902)
        self.assertEqual(result['last'], 4903)

    def test_validate_configured_v_quote(self):
        result = wh6.validate_recognized_market(wh6.parse_wh6_ocr('PVC2701M 卖出 4903 买入 4902 最新 4903', .98), config)
        self.assertTrue(result['validation']['valid'])
        self.assertFalse(result['validation']['errors'])

    def test_unconfigured_recognized_variety_warns(self):
        result = wh6.validate_recognized_market(wh6.parse_wh6_ocr('JD2701 卖出 3000 买入 2999 最新 3000', .98), config)
        self.assertTrue(result['validation']['valid'])
        self.assertTrue(result['validation']['warnings'])

    def test_bad_order_book_fails(self):
        result = wh6.validate_recognized_market(wh6.parse_wh6_ocr('PVC2701 卖出 4902 买入 4903 最新 4903', .98), config)
        self.assertFalse(result['validation']['valid'])

    def test_parse_failure_is_diagnostic(self):
        result = wh6.parse_wh6_ocr('卖出 4903 买入 4902 最新 4903', .5)
        self.assertNotIn('variety', result)
        self.assertEqual(result['source'], 'wh6_ocr')
        self.assertEqual(result['ocr_confidence'], .5)

    def test_low_confidence_is_invalid(self):
        raw = wh6.parse_wh6_ocr('PVC2701 卖出 4903 买入 4902 最新 4903', .43)
        result = wh6.validate_recognized_market(raw, config)
        if raw.get('ocr_confidence', 1) < config['ocr_min_confidence']:
            result['validation']['valid'] = False
        self.assertFalse(result['validation']['valid'])

    def test_window_title_filter(self):
        records = [
            {"title": "文华交易客户端"},
            {"title": "WH6 Quote Panel"},
        ]
        self.assertEqual(wh6.filter_window_records(records, "quote"), [records[1]])
        self.assertEqual(wh6.filter_window_records(records, None), records)

    def test_process_constants(self):
        self.assertEqual(wh6.PROCESS_NAME, 'mytrader_spqh.exe')
        self.assertIn('app', ('app', 'window', 'monitor'))
        self.assertEqual(wh6.APP_NAME, 'wh6')

    def test_ocr_effort_budget(self):
        for effort in ('fast', 'auto', 'thorough'):
            tiers = wh6._ocr_tiers(effort, None)
            self.assertEqual(tiers[0][2:], ('gray', (11,)))
        self.assertEqual(sum(len(t[3]) for t in wh6._ocr_tiers('fast', None)), 1)
        self.assertLessEqual(sum(len(t[3]) for t in wh6._ocr_tiers('auto', None)), 7)
        with self.assertRaises(ValueError):
            wh6.ocr_quote_image(Path('missing.png'), effort='bogus')

    def test_plan_draft_canonicalizes_contract_and_passes(self):
        recognized = {
            'variety': 'V', 'contract': 'PVC2701',
            'bid1': 4902.0, 'ask1': 4903.0, 'last': 4903.0,
            'ocr_confidence': 0.86, 'source': 'wh6_ocr',
        }
        plan = wh6.build_plan_draft(
            recognized, direction='long', stop_loss=4873,
            smc_tag='FVG', htf_bias='long',
        )
        self.assertEqual(plan['contract'], 'V2701')
        self.assertEqual(plan['contract_label'], 'PVC2701')
        result = wh6.check_plan(plan, config)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['risk']['total_risk_rmb'], 150.0)

    def test_plan_draft_without_direction_is_rejected(self):
        recognized = {'variety': 'V', 'contract': 'PVC2701', 'bid1': 4902.0, 'ask1': 4903.0, 'last': 4903.0}
        plan = wh6.build_plan_draft(recognized, stop_loss=4863, smc_tag='FVG', htf_bias='long')
        self.assertEqual(wh6.check_plan(plan, config)['status'], 'REJECT')


if __name__ == '__main__':
    unittest.main()
