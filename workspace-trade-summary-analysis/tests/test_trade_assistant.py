import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "trade_assistant.py"
spec = importlib.util.spec_from_file_location("trade_assistant", MODULE)
ta = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ta)
CONFIG = ta.load_config()


class RiskTests(unittest.TestCase):
    def test_v_40_points_is_200(self):
        risk = ta.calculate_risk(4900, 4860, 1, CONFIG["instruments"]["V"])
        self.assertEqual(risk["total_risk_rmb"], 200)

    def test_lh_two_ticks_is_160_three_is_240(self):
        lh = CONFIG["instruments"]["LH"]
        self.assertEqual(ta.calculate_risk(12000, 11990, 1, lh)["total_risk_rmb"], 160)
        self.assertEqual(ta.calculate_risk(12000, 11985, 1, lh)["total_risk_rmb"], 240)

    def test_fg_10_points_is_200(self):
        risk = ta.calculate_risk(1500, 1490, 1, CONFIG["instruments"]["FG"])
        self.assertEqual(risk["total_risk_rmb"], 200)

    def test_distance_rounds_up_to_ticks(self):
        risk = ta.calculate_risk(12000, 11991, 1, CONFIG["instruments"]["LH"])
        self.assertEqual(risk["ticks"], 2)
        self.assertEqual(risk["price_distance"], 10)


class PlanTests(unittest.TestCase):
    def plan(self, **overrides):
        plan = {
            "variety": "V", "contract": "V2701", "direction": "long", "size": 1,
            "price_mode": "opponent", "stop_loss": 4863, "smc_tag": "FVG",
            "htf_bias": "long", "market": {"bid1": 4902, "ask1": 4903, "last": 4903}
        }
        plan.update(overrides)
        return plan

    def test_valid_plan_passes(self):
        self.assertEqual(ta.check_plan(self.plan(), CONFIG)["status"], "PASS")

    def test_wrong_directional_stop_rejected(self):
        result = ta.check_plan(self.plan(stop_loss=4910), CONFIG)
        self.assertEqual(result["status"], "REJECT")

    def test_unconfigured_variety_rejected(self):
        result = ta.check_plan(self.plan(variety="RU"), CONFIG)
        self.assertEqual(result["status"], "REJECT")

    def test_ocr_parser_reads_screenshot_style_text(self):
        result = ta.parse_ocr_text("PVC2701M DCE 卖出 4903 买入 4902 最新 4903")
        self.assertEqual(result["variety"], "PVC")
        self.assertEqual(result["contract"], "PVC2701")
        self.assertEqual(result["ask1"], 4903)
        self.assertEqual(result["bid1"], 4902)
        self.assertEqual(result["last"], 4903)

    def test_save_snapshot_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshots.jsonl"
            sid = ta.save_snapshot(self.plan(), {"status": "PASS"}, path)
            record = json.loads(path.read_text().strip())
            self.assertEqual(sid, record["snapshot_id"])
            self.assertEqual(record["model_ab_status"], "not_implemented_archived_only")


if __name__ == "__main__":
    unittest.main()
