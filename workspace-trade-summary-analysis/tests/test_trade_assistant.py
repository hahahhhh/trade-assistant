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
        # 30 ticks x ¥5 = ¥150, comfortably below the ¥200 cap.
        self.assertEqual(ta.check_plan(self.plan(stop_loss=4873), CONFIG)["status"], "PASS")

    def test_plan_at_cap_warns(self):
        # Exactly ¥200 gross with a zero fee buffer is not a clean pass.
        result = ta.check_plan(self.plan(stop_loss=4863), CONFIG)
        self.assertEqual(result["status"], "WARN")
        self.assertIn("per-trade limit", " ".join(result["warnings"]))

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

    def test_effective_limit_fixed_mode(self):
        limit, budget = ta.effective_trade_limit(CONFIG["instruments"]["V"], 50000)
        self.assertEqual(limit, 200)
        self.assertEqual(budget["mode"], "fixed")

    def test_effective_limit_percent_mode(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument.update({"risk_budget_mode": "percent_of_equity", "risk_percent": 2})
        limit, budget = ta.effective_trade_limit(instrument, 50000)
        self.assertEqual(limit, 1000)
        self.assertEqual(budget["equity_rmb"], 50000)
        self.assertEqual(budget["percent"], 2)

    def test_effective_limit_percent_requires_equity(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument.update({"risk_budget_mode": "percent_of_equity", "risk_percent": 2})
        with self.assertRaises(ValueError):
            ta.effective_trade_limit(instrument, None)
        with self.assertRaises(ValueError):
            ta.effective_trade_limit(instrument, 0)

    def test_effective_limit_rejects_bad_percent(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument.update({"risk_budget_mode": "percent_of_equity"})
        for bad in (0, -1, 101, None):
            instrument["risk_percent"] = bad
            with self.assertRaises(ValueError):
                ta.effective_trade_limit(instrument, 50000)

    def test_effective_limit_rejects_unknown_mode(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument["risk_budget_mode"] = "whatever"
        with self.assertRaises(ValueError):
            ta.effective_trade_limit(instrument, 50000)

    def test_percent_plan_uses_equity_limit(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument.update({"risk_budget_mode": "percent_of_equity", "risk_percent": 2})
        cfg = dict(CONFIG)
        cfg["instruments"] = dict(CONFIG["instruments"])
        cfg["instruments"]["V"] = instrument
        plan = self.plan(stop_loss=4873, size=3)
        result = ta.check_plan(plan, cfg, equity=50000)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["risk"]["single_trade_limit_rmb"], 1000)
        self.assertEqual(result["budget"]["mode"], "percent_of_equity")

    def test_percent_plan_rejected_without_equity(self):
        instrument = dict(CONFIG["instruments"]["V"])
        instrument.update({"risk_budget_mode": "percent_of_equity", "risk_percent": 2})
        cfg = dict(CONFIG)
        cfg["instruments"] = dict(CONFIG["instruments"])
        cfg["instruments"]["V"] = instrument
        result = ta.check_plan(self.plan(), cfg)
        self.assertEqual(result["status"], "REJECT")
        self.assertIn("总资金", " ".join(result["hard_blocks"]))

    def test_daily_loss_limit_modes(self):
        value, budget = ta.daily_loss_limit(CONFIG, 50000)
        self.assertEqual(value, 400.0)
        self.assertEqual(budget["mode"], "fixed")
        cfg = dict(CONFIG)
        cfg["global_limits"] = dict(CONFIG["global_limits"])
        cfg["global_limits"].update({"daily_loss_mode": "percent_of_equity", "daily_loss_percent": 5})
        value, budget = ta.daily_loss_limit(cfg, 50000)
        self.assertEqual(value, 2500)
        self.assertEqual(budget["percent"], 5)

    def test_account_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "account.json"
            self.assertEqual(ta.load_account(path), {})
            ta.save_account(50000, path)
            self.assertEqual(ta.load_account(path)["equity_rmb"], 50000)

    def test_save_snapshot_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshots.jsonl"
            sid = ta.save_snapshot(self.plan(), {"status": "PASS"}, path)
            record = json.loads(path.read_text().strip())
            self.assertEqual(sid, record["snapshot_id"])
            self.assertEqual(record["model_ab_status"], "not_implemented_archived_only")


if __name__ == "__main__":
    unittest.main()
