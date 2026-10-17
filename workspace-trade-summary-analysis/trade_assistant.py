#!/usr/bin/env python3
"""Local, read-only trade planning and risk-check engine.

This tool never places orders or interacts with a broker. It accepts OCR JSON
or manually supplied market fields, validates them, computes risk, and records
immutable pre-trade snapshots.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "data" / "trade_assistant_config.json"
DEFAULT_SNAPSHOTS = ROOT / "data" / "trade_assistant" / "pre_trade_snapshots.jsonl"


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for code, item in data["instruments"].items():
        if not item.get("configured", False):
            continue
        expected = item["value_per_point"] * item["min_tick"]
        if not math.isclose(expected, item["value_per_tick"], rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"{code} config mismatch: value_per_point*min_tick != value_per_tick")
    return data


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("min_tick must be positive")
    return round(round(price / tick) * tick, 8)


def is_valid_tick(price: float, tick: float) -> bool:
    return math.isclose(price / tick, round(price / tick), rel_tol=0, abs_tol=1e-7)


def resolve_entry(plan: dict[str, Any], market: dict[str, Any], tick: float) -> float:
    mode = plan.get("price_mode", "manual")
    side = plan.get("direction")
    if mode == "manual":
        if plan.get("entry_price") is None:
            raise ValueError("manual price mode requires entry_price")
        price = float(plan["entry_price"])
    elif mode == "opponent":
        key = "ask1" if side == "long" else "bid1"
        price = float(market[key])
    elif mode == "queue":
        # For this tool's convention: join bid when buying, ask when selling.
        key = "bid1" if side == "long" else "ask1"
        price = float(market[key])
    elif mode == "last":
        price = float(market["last"])
    elif mode == "offset":
        base = plan.get("base_price")
        if base is None:
            key = "ask1" if side == "long" else "bid1"
            base = market[key]
        price = float(base) + int(plan.get("offset_ticks", 0)) * tick
    else:
        raise ValueError(f"Unsupported price_mode: {mode}")
    if not is_valid_tick(price, tick):
        raise ValueError(f"Entry price {price} is not on min-tick grid {tick}")
    return price


def calculate_risk(entry: float, stop: float, size: int, instrument: dict[str, Any]) -> dict[str, float]:
    tick = float(instrument["min_tick"])
    if size < 1:
        raise ValueError("size must be >= 1")
    raw_distance = abs(entry - stop)
    ticks = math.ceil(raw_distance / tick - 1e-10)
    distance = ticks * tick
    gross = ticks * float(instrument["value_per_tick"]) * size
    cost = float(instrument.get("round_trip_cost_per_lot", 0)) * size
    slippage = float(instrument.get("slippage_buffer_per_lot", 0)) * size
    return {
        "price_distance": distance,
        "ticks": float(ticks),
        "gross_risk_rmb": gross,
        "cost_buffer_rmb": cost + slippage,
        "total_risk_rmb": gross + cost + slippage,
    }


def validate_market(market: dict[str, Any], instrument: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("bid1", "ask1", "last"):
        if market.get(key) is None:
            errors.append(f"Missing market field: {key}")
            continue
        try:
            value = float(market[key])
        except (TypeError, ValueError):
            errors.append(f"Invalid market field: {key}")
            continue
        if not is_valid_tick(value, float(instrument["min_tick"])):
            errors.append(f"{key}={value} is not aligned to min_tick={instrument['min_tick']}")
    if market.get("bid1") is not None and market.get("ask1") is not None:
        if float(market["bid1"]) > float(market["ask1"]):
            errors.append("Invalid order book: bid1 is greater than ask1")
    return errors


def check_plan(plan: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    hard: list[str] = []
    warnings: list[str] = []
    code = str(plan.get("variety", "")).upper()
    instruments = config["instruments"]
    if code not in instruments:
        return {"status": "REJECT", "hard_blocks": [f"Risk config missing for variety {code or '(blank)'}"], "warnings": [], "risk": None}
    instrument = instruments[code]
    if plan.get("contract") and not str(plan["contract"]).upper().startswith(code):
        return {"status": "REJECT", "hard_blocks": [f"Contract {plan['contract']} does not match variety {code}"], "warnings": [], "risk": None}
    if not instrument.get("configured", False):
        return {"status": "REJECT", "hard_blocks": [f"Risk config for variety {code} is not configured"], "warnings": [], "risk": None}
    market = plan.get("market", {})
    hard.extend(validate_market(market, instrument))
    if plan.get("direction") not in ("long", "short"):
        hard.append("Direction must be long or short")
    if plan.get("smc_tag") in (None, ""):
        hard.append("Select a predefined SMC tag or enter a custom tag")
    if plan.get("htf_bias") not in ("long", "short", "ranging", "unclear"):
        hard.append("HTF bias must be long, short, ranging, or unclear")
    if plan.get("htf_bias") in ("ranging", "unclear"):
        warnings.append("HTF bias is ranging/unclear")
    if plan.get("stop_loss") is None:
        hard.append("Stop loss is required")
    if int(plan.get("size", 0) or 0) < 1:
        hard.append("Size must be at least 1 lot")
    entry = None
    risk = None
    try:
        if not hard:
            entry = resolve_entry(plan, market, float(instrument["min_tick"]))
            stop = float(plan["stop_loss"])
            if not is_valid_tick(stop, float(instrument["min_tick"])):
                hard.append("Stop loss is not aligned to the instrument minimum tick")
            elif plan["direction"] == "long" and stop >= entry:
                hard.append("Long position stop loss must be below entry")
            elif plan["direction"] == "short" and stop <= entry:
                hard.append("Short position stop loss must be above entry")
            else:
                risk = calculate_risk(entry, stop, int(plan["size"]), instrument)
                limit = float(instrument["single_trade_limit_rmb"])
                if risk["total_risk_rmb"] > limit + 1e-9:
                    hard.append(f"Plan risk ¥{risk['total_risk_rmb']:.2f} exceeds {code} limit ¥{limit:.2f}")
                max_ticks = math.floor(max(0, limit - risk["cost_buffer_rmb"]) / float(instrument["value_per_tick"]) / int(plan["size"]))
                risk["single_trade_limit_rmb"] = limit
                risk["max_ticks_at_size"] = float(max_ticks)
                risk["max_stop_distance_at_size"] = max_ticks * float(instrument["min_tick"])
                risk["entry_price"] = entry
    except (KeyError, TypeError, ValueError) as exc:
        hard.append(str(exc))
    if plan.get("is_add_on"):
        warnings.append("Add-on trade: verify combined position risk and structural confirmation")
    confidence = market.get("ocr_confidence")
    if confidence is not None and float(confidence) < float(config.get("ocr_min_confidence", 0.85)):
        warnings.append("OCR confidence below threshold; manually verify all recognized fields")
    status = "REJECT" if hard else ("WARN" if warnings else "PASS")
    return {"status": status, "hard_blocks": hard, "warnings": warnings, "risk": risk}


def parse_ocr_text(text: str) -> dict[str, Any]:
    """Best-effort parser for OCR text. Verify results before using them."""
    normalized = text.replace(",", " ")
    contract_match = re.search(r"\b(?:PVC|V|JD|LH|RU|FG|MA|JM|C)\s*\d{3,4}M?\b", normalized, re.I)
    result: dict[str, Any] = {}
    if contract_match:
        contract = re.sub(r"\s+", "", contract_match.group(0)).upper().removesuffix("M")
        result["contract"] = contract
        m = re.match(r"[A-Z]+", contract)
        if m:
            result["variety"] = m.group(0)
    # Common Chinese labels in the supplied quote panel.
    for label, key in (("买入", "bid1"), ("买一", "bid1"), ("卖出", "ask1"), ("卖一", "ask1"), ("最新", "last")):
        match = re.search(label + r"\s*[:：]?\s*(\d+(?:\.\d+)?)", normalized)
        if match and key not in result:
            result[key] = float(match.group(1))
    return result


def read_market_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "sha256" not in data:
        data["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return data


def save_snapshot(plan: dict[str, Any], result: dict[str, Any], path: Path = DEFAULT_SNAPSHOTS) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_id = str(uuid.uuid4())
    record = {
        "snapshot_id": snapshot_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rule_version": "rules-2026-10-initial",
        "plan": plan,
        "check": result,
        "model_ab_status": "not_implemented_archived_only",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return snapshot_id


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate a trade-plan JSON")
    check.add_argument("plan", type=Path)
    check.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    check.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    ocr = sub.add_parser("parse-ocr", help="best-effort parse OCR text from a text file")
    ocr.add_argument("text_file", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "parse-ocr":
        print(json.dumps(parse_ocr_text(args.text_file.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
        return 0
    config = load_config(args.config)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    result = check_plan(plan, config)
    snapshot_id = save_snapshot(plan, result, args.snapshots)
    result["snapshot_id"] = snapshot_id
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] != "REJECT" else 2


if __name__ == "__main__":
    sys.exit(main())
