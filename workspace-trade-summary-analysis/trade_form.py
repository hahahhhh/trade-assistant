#!/usr/bin/env python3
"""Button-driven trade-plan form for WH6 (read-only helper).

The form only reads WH6 via screenshot OCR and computes a plan locally. It
never clicks the trading software, never submits orders, and never writes to
the WH6 window.

Run on Windows:
    python workspace-trade-summary-analysis\\trade_form.py
"""
from __future__ import annotations

import csv
import hashlib
import math
import json
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import wh6_screen as wh6

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "trade_form_state.json"
PLAN_DIR = ROOT / "data" / "plans"
SNAPSHOTS = ROOT / "data" / "trade_assistant" / "pre_trade_snapshots.jsonl"
CONFIG_PATH = ROOT / "data" / "trade_assistant_config.json"
RISK_FIELDS = (
    ("value_per_point", "每点价值（元）"),
    ("min_tick", "最小跳动（点）"),
    ("single_trade_limit_rmb", "单笔上限（元）"),
    ("round_trip_cost_per_lot", "手续费/手（元）"),
    ("slippage_buffer_per_lot", "滑点缓冲/手（元）"),
)
DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
FORM_VERSION = "3.0"

STEP_CHOICES = (1, 5, 10, 20)
DIRECTIONS = (("买多", "long"), ("卖空", "short"))
PRICE_MODES = (("对手价", "opponent"), ("排队价", "queue"), ("最新价", "last"), ("手工价", "manual"))
HTF_CHOICES = (("多头", "long"), ("空头", "short"), ("震荡", "ranging"), ("不清晰", "unclear"))


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def suggest_prices(entry: float, direction: str, tick: float, stop_ticks: int, take_ticks: int) -> dict[str, float]:
    """Stop sits behind entry and take-profit ahead of it, in whole ticks."""
    if direction not in ("long", "short"):
        raise ValueError("direction must be long or short")
    if tick <= 0:
        raise ValueError("tick must be positive")
    stop_sign = -1 if direction == "long" else 1
    take_sign = 1 if direction == "long" else -1
    stop = round(entry + stop_sign * stop_ticks * tick, 8)
    take = round(entry + take_sign * take_ticks * tick, 8)
    return {"stop_loss": stop, "take_profit": take}


def resolve_budget(limit: float | None, manual: str, use_limit: bool) -> float:
    """1R comes from the instrument cap unless the user opts to override it."""
    if use_limit:
        if limit is None or limit <= 0:
            raise ValueError("该品种未设置单笔上限，请先取消勾选并填写 1R")
        return float(limit)
    try:
        value = float(manual)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"1R 金额不是数字：{manual}") from exc
    if value <= 0:
        raise ValueError("1R 金额必须大于 0")
    return value


ACCOUNT_PATH = ROOT / "data" / "account.json"


def tf_account_equity() -> float | None:
    """Equity from data/account.json, or None when never set."""
    try:
        payload = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("equity_rmb")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def tf_save_equity(equity: float) -> None:
    if equity <= 0:
        raise ValueError("总资金必须大于 0")
    ACCOUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"equity_rmb": float(equity),
               "updated_at": datetime.now().isoformat(timespec="seconds")}
    ACCOUNT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def per_lot_cost(instrument: dict[str, Any]) -> float:
    """Round-trip fees plus slippage buffer for one lot."""
    return (float(instrument.get("round_trip_cost_per_lot", 0) or 0)
            + float(instrument.get("slippage_buffer_per_lot", 0) or 0))


def size_by_risk(budget: float, stop_ticks: int, value_per_tick: float,
                 cost_per_lot: float = 0.0) -> dict[str, Any]:
    """Lots affordable inside a 1R budget, rounded down.

    risk per lot = stop ticks x value per tick + fees/slippage; lots must not
    overshoot 1R, otherwise the shown risk would disagree with check_plan.
    """
    if budget <= 0:
        raise ValueError("1R 金额必须大于 0")
    if stop_ticks <= 0:
        raise ValueError("止损跳数必须大于 0")
    if value_per_tick <= 0:
        raise ValueError("每跳价值必须大于 0（请先在「品种配置」里填写）")
    per_lot = stop_ticks * value_per_tick + max(0.0, cost_per_lot)
    raw_lots = budget / per_lot
    lots = int(math.floor(raw_lots + 1e-9))
    return {
        "per_lot_risk": per_lot,
        "raw_lots": raw_lots,
        "lots": lots,
        "risk_rmb": lots * per_lot,
        "remainder": budget - lots * per_lot,
    }


def ticks_between(entry: float, price: float, tick: float) -> int:
    """Whole ticks between two prices, at least one."""
    if tick <= 0:
        raise ValueError("tick must be positive")
    return max(1, int(round(abs(price - entry) / tick)))


def linked_take_ticks(stop_ticks: int, ratio: float) -> int:
    """Take-profit distance as a multiple of the stop distance (R multiple)."""
    if ratio <= 0:
        raise ValueError("盈亏比必须大于 0")
    if stop_ticks <= 0:
        raise ValueError("止损跳数必须大于 0")
    return max(1, int(round(stop_ticks * ratio)))


def estimate_entry(recognized: dict[str, Any], price_mode: str, direction: str, tick: float) -> float | None:
    """Local estimate of the fill price so the form can suggest stop/target."""
    if price_mode == "manual":
        return None
    if direction not in ("long", "short"):
        return None
    try:
        if price_mode == "opponent":
            value = recognized["ask1"] if direction == "long" else recognized["bid1"]
        elif price_mode == "queue":
            value = recognized["bid1"] if direction == "long" else recognized["ask1"]
        elif price_mode == "last":
            value = recognized["last"]
        else:
            return None
        return float(value)
    except (KeyError, TypeError, ValueError):
        return None


def build_plan_from_form(fields: dict[str, Any], recognized: dict[str, Any],
                         config: dict[str, Any], equity: float | None = None) -> dict[str, Any]:
    """Single source of truth for the form's plan + risk check."""
    plan = wh6.build_plan_draft(
        recognized,
        direction=fields.get("direction"),
        size=int(fields.get("size") or 1),
        price_mode=fields.get("price_mode") or "opponent",
        entry_price=fields.get("entry_price"),
        stop_loss=fields.get("stop_loss"),
        take_profit=fields.get("take_profit"),
        smc_tag=fields.get("smc_tag"),
        htf_bias=fields.get("htf_bias"),
        is_add_on=bool(fields.get("is_add_on")),
    )
    result = wh6.check_plan(plan, config, equity)
    return {"plan": plan, "check": result}


def summarize(result: dict[str, Any]) -> str:
    """Plain-text summary shown in the form."""
    check = result["check"]
    lines = [f"状态：{check['status']}"]
    for item in check.get("hard_blocks", []):
        lines.append(f"  阻断：{item}")
    for item in check.get("warnings", []):
        lines.append(f"  警告：{item}")
    risk = check.get("risk")
    if risk:
        lines.append(
            f"  入场 {risk['entry_price']}｜止损距离 {risk['price_distance']:g} 点"
            f"（{risk['ticks']:g} 跳）｜风险 ¥{risk['total_risk_rmb']:.0f}"
            f"｜上限 ¥{risk['single_trade_limit_rmb']:.0f}"
        )
        lines.append(
            f"  当前手数下最多 {risk['max_ticks_at_size']:g} 跳"
            f"（{risk['max_stop_distance_at_size']:g} 点）"
        )
    return "\n".join(lines)


def market_summary(recognized: dict[str, Any]) -> str:
    if not recognized:
        return "尚未读取行情"
    parts = [
        recognized.get("contract_label") or recognized.get("contract") or "?",
        f"卖一 {recognized.get('ask1', '?')}",
        f"买一 {recognized.get('bid1', '?')}",
        f"最新 {recognized.get('last', '?')}",
    ]
    if recognized.get("ocr_confidence") is not None:
        parts.append(f"置信度 {recognized['ocr_confidence']:.3f}")
    if recognized.get("ocr_seconds") is not None:
        parts.append(f"{recognized['ocr_seconds']}s")
    return "｜".join(str(p) for p in parts)


def capture_and_recognize(tesseract_cmd: str | None, effort: str = "auto") -> dict[str, Any]:
    """Capture WH6 and OCR its quote panel. Windows-only; raises otherwise."""
    if tesseract_cmd:
        import pytesseract  # type: ignore
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    state = wh6.load_config()
    captures = wh6.capture_windows_process(process_name=wh6.PROCESS_NAME, capture_mode="app")
    best: dict[str, Any] | None = None
    for title, image in captures[0]["frames"]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            image.save(path)
            parsed = wh6.ocr_quote_image(path, effort=effort)
        parsed["window_title"] = title
        if best is None or wh6._quote_score(parsed) > wh6._quote_score(best):
            best = parsed
    if best is None:
        raise RuntimeError("WH6 没有可识别的窗口")
    best = wh6.validate_recognized_market(best, state)
    threshold = float(state.get("ocr_min_confidence", 0.85))
    if float(best.get("ocr_confidence") or 0) < threshold:
        best["validation"]["valid"] = False
        best["validation"]["errors"].append("OCR confidence is below threshold; verify every field manually")
    return best


def instrument_form_values(config: dict[str, Any], code: str) -> dict[str, Any]:
    """Editable values for one instrument, blanks instead of nulls."""
    inst = config["instruments"].get(code, {})
    values: dict[str, Any] = {}
    for key, _label in RISK_FIELDS:
        value = inst.get(key)
        values[key] = "" if value is None else f"{float(value):g}"
    values["configured"] = bool(inst.get("configured"))
    values["risk_budget_mode"] = inst.get("risk_budget_mode", "fixed")
    percent = inst.get("risk_percent")
    values["risk_percent"] = "" if percent is None else f"{float(percent):g}"
    return values


def build_instrument_config(config: dict[str, Any], code: str, values: dict[str, Any]) -> dict[str, Any]:
    """Validate edited risk parameters and return an updated config copy.

    ``value_per_tick`` is always derived, so the three numbers can never drift
    out of sync (load_config rejects mismatches at startup).
    """
    code = code.upper()
    instruments = config["instruments"]
    if code not in instruments:
        raise ValueError(f"未知品种：{code}")
    enabled = bool(values.get("configured"))

    def number(key: str, label: str, *, positive: bool, required: bool = True) -> float | None:
        raw = str(values.get(key, "")).strip()
        if not raw:
            if required and enabled:
                raise ValueError(f"启用 {code} 时必须填写「{label}」")
            return None
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"「{label}」不是数字：{raw}") from exc
        if positive and value <= 0:
            raise ValueError(f"「{label}」必须大于 0")
        if not positive and value < 0:
            raise ValueError(f"「{label}」不能为负")
        return value

    mode = str(values.get("risk_budget_mode") or "fixed")
    if mode not in ("fixed", "percent_of_equity"):
        raise ValueError(f"未知的风险预算模式：{mode}")
    percent = None
    if mode == "percent_of_equity":
        percent = number("risk_percent", "总资金比例（%）", positive=True)
        if percent is not None and percent > 100:
            raise ValueError("「总资金比例（%）」不能超过 100")
        if enabled and percent is None:
            raise ValueError("百分比模式必须填写总资金比例")

    value_per_point = number("value_per_point", "每点价值（元）", positive=True)
    min_tick = number("min_tick", "最小跳动（点）", positive=True)
    limit = number("single_trade_limit_rmb", "单笔上限（元）", positive=True,
                   required=(mode == "fixed"))
    cost = number("round_trip_cost_per_lot", "手续费/手（元）", positive=False, required=False) or 0.0
    slippage = number("slippage_buffer_per_lot", "滑点缓冲/手（元）", positive=False, required=False) or 0.0

    updated = dict(config)
    instruments = dict(instruments)
    instrument = dict(instruments[code])
    instrument.update({
        "value_per_point": value_per_point,
        "min_tick": min_tick,
        "value_per_tick": (value_per_point * min_tick) if value_per_point and min_tick else None,
        "single_trade_limit_rmb": limit,
        "round_trip_cost_per_lot": cost,
        "slippage_buffer_per_lot": slippage,
        "risk_budget_mode": mode,
        "risk_percent": percent,
        "configured": bool(enabled and value_per_point and min_tick
                           and (limit if mode == "fixed" else percent)),
    })
    if instrument["value_per_tick"] is not None:
        instrument["value_per_tick"] = round(instrument["value_per_tick"], 8)
    instruments[code] = instrument
    updated["instruments"] = instruments
    return updated


def save_instrument_config(config: dict[str, Any], code: str, values: dict[str, Any],
                           path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Persist edited parameters and return the reloaded config."""
    updated = build_instrument_config(config, code, values)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return wh6.load_config(path)


HISTORY_COLUMNS = (
    ("timestamp", "时间"),
    ("contract", "合约"),
    ("direction", "方向"),
    ("size", "手数"),
    ("entry", "入场"),
    ("stop_loss", "止损"),
    ("take_profit", "止盈"),
    ("risk_rmb", "风险¥"),
    ("status", "结果"),
    ("smc_tag", "SMC"),
    ("htf_bias", "HTF"),
)


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """Stable id so a saved plan file can be matched to its snapshot record."""
    payload = json.dumps(plan, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _history_row(plan: dict[str, Any], snapshot: dict[str, Any] | None,
                 path: Path | None) -> dict[str, Any]:
    check = (snapshot or {}).get("check") or {}
    risk = check.get("risk") or {}
    timestamp = (snapshot or {}).get("created_at_utc")
    if not timestamp and path is not None:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "timestamp": timestamp or "",
        "variety": plan.get("variety") or "?",
        "contract": plan.get("contract_label") or plan.get("contract") or "?",
        "direction": plan.get("direction") or "",
        "size": plan.get("size"),
        "entry": risk.get("entry_price", plan.get("entry_price")),
        "stop_loss": plan.get("stop_loss"),
        "take_profit": plan.get("take_profit"),
        "risk_rmb": risk.get("total_risk_rmb"),
        "status": check.get("status") or "未检查",
        "smc_tag": plan.get("smc_tag") or "",
        "htf_bias": plan.get("htf_bias") or "",
        "path": str(path) if path is not None else "",
        "snapshot_id": (snapshot or {}).get("snapshot_id", ""),
        "fingerprint": plan_fingerprint(plan),
    }


def _read_snapshots(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def load_history(snapshots_path: Path = SNAPSHOTS, plans_dir: Path = PLAN_DIR) -> list[dict[str, Any]]:
    """Join saved plan files with their risk-check snapshots, newest first."""
    snapshots = {}
    for record in _read_snapshots(snapshots_path):
        plan = record.get("plan")
        if isinstance(plan, dict):
            snapshots[plan_fingerprint(plan)] = record
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if plans_dir.exists():
        for path in sorted(plans_dir.glob("plan-*.json")):
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            fingerprint = plan_fingerprint(plan)
            seen.add(fingerprint)
            rows.append(_history_row(plan, snapshots.get(fingerprint), path))
    for fingerprint, record in snapshots.items():
        if fingerprint in seen:
            continue
        plan = record.get("plan") or {}
        rows.append(_history_row(plan, record, None))
    rows.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    return rows


def unique_row_ids(rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Treeview iids must be unique; identical plans can share a fingerprint."""
    used: set[str] = set()
    result: list[tuple[str, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        base = str(row.get("fingerprint") or f"row{index}")
        candidate = base
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{base}-{suffix}"
        used.add(candidate)
        result.append((candidate, row))
    return result


def group_by_variety(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group history rows by variety, keeping the newest-first order inside."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("variety") or "?"), []).append(row)
    return sorted(groups.items(), key=lambda item: item[0])


def export_history_csv(rows: list[dict[str, Any]], path: Path) -> int:
    """Write rows as UTF-8-BOM CSV so Excel shows Chinese correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _key, label in HISTORY_COLUMNS] + ["品种", "计划文件", "快照 ID"])
        for row in rows:
            writer.writerow([
                "" if row.get(key) is None else row.get(key) for key, _label in HISTORY_COLUMNS
            ] + [row.get("variety"), row.get("path"), row.get("snapshot_id")])
    return len(rows)


def remove_snapshot_records(path: Path, snapshot_ids: set[str]) -> int:
    """Drop snapshot lines by id, keeping a .bak copy. Returns the count removed."""
    ids = {sid for sid in snapshot_ids if sid}
    if not path.exists() or not ids:
        return 0
    original = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0
    for line in original:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if record.get("snapshot_id") in ids:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    path.with_suffix(path.suffix + ".bak").write_text("\n".join(original) + "\n", encoding="utf-8")
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return removed


def find_duplicate_plan(plan: dict[str, Any], plans_dir: Path = PLAN_DIR) -> Path | None:
    """Return an existing plan file with identical content, if any."""
    if not plans_dir.exists():
        return None
    target = plan_fingerprint(plan)
    for path in sorted(plans_dir.glob("plan-*.json")):
        try:
            other = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan_fingerprint(other) == target:
            return path
    return None


THEME = {
    "ok": "#1a7f37",
    "warn": "#b26a00",
    "danger": "#c62828",
    "muted": "#666666",
    "track": "#e0e0e0",
    "stale_bg": "#eeeeee",
    "band_low": "#fff7d6",
    "font_body": ("Microsoft YaHei UI", 10),
    "font_title": ("Microsoft YaHei UI", 12, "bold"),
    "font_price": ("Consolas", 14, "bold"),
    "font_badge": ("Microsoft YaHei UI", 16, "bold"),
    "pad": 8,
}

BADGE_STYLES = {
    "未检查": ("未检查", THEME["muted"], "#f2f2f2"),
    "已过期": ("已过期，需重新检查", THEME["muted"], THEME["stale_bg"]),
    "PASS": ("PASS 通过", THEME["ok"], "#e8f5e9"),
    "WARN": ("WARN 有警告", THEME["warn"], "#fff4e0"),
    "REJECT": ("REJECT 阻断", THEME["danger"], "#fdecea"),
}


def risk_ratio(total: float, limit: float) -> float:
    """Share of the per-trade cap consumed; 0 when unknown."""
    if limit <= 0:
        return 0.0
    return max(0.0, float(total) / float(limit))


def risk_level(ratio: float) -> str:
    if ratio > 1:
        return "over"
    if ratio >= 0.95:
        return "warn"
    return "ok"


def status_badge(status: str | None, stale: bool) -> tuple[str, str, str]:
    """(text, foreground, background); staleness outranks any old verdict."""
    if stale:
        return BADGE_STYLES["已过期"]
    if status in BADGE_STYLES:
        return BADGE_STYLES[status]
    return BADGE_STYLES["未检查"]


def risk_preview(fields: dict[str, Any], recognized: dict[str, Any],
                 config: dict[str, Any], equity: float | None = None) -> dict[str, Any]:
    """Live risk numbers without running the full rule check."""
    variety = recognized.get("variety")
    instrument = config["instruments"].get(variety) if variety else None
    if not instrument or not instrument.get("configured"):
        return {"error": "该品种尚未启用风险配置"}
    try:
        limit, budget = wh6.effective_trade_limit(instrument, equity)
    except ValueError as exc:
        return {"error": str(exc)}
    tick = float(instrument["min_tick"])
    direction = fields.get("direction")
    entry = estimate_entry(recognized, fields.get("price_mode") or "opponent", direction, tick)
    if entry is None:
        raw = str(fields.get("entry_price") or "").strip()
        try:
            entry = float(raw)
        except ValueError:
            return {"error": "缺少入场价"}
    try:
        stop = float(fields.get("stop_loss"))
    except (TypeError, ValueError):
        return {"error": "缺少止损价"}
    size = int(fields.get("size") or 1)
    try:
        risk = wh6.calculate_risk(entry, stop, size, instrument)
    except (ValueError, KeyError) as exc:
        return {"error": str(exc)}
    value_per_tick = float(instrument["value_per_tick"])
    cost = per_lot_cost(instrument)
    ticks = risk["ticks"]
    total = risk["total_risk_rmb"]
    return {
        "entry": entry,
        "stop": stop,
        "size": size,
        "ticks": ticks,
        "per_lot_risk": total / size if size else 0.0,
        "total_risk": total,
        "limit": limit,
        "ratio": risk_ratio(total, limit),
        "level": risk_level(risk_ratio(total, limit)),
        "max_ticks_at_size": (
            int(max(0.0, limit - cost * size) // (value_per_tick * size)) if size else 0
        ),
        "budget": budget,
        "limit": limit,
    }


def manual_prefill(recognized: dict[str, Any]) -> dict[str, str]:
    """Initial values for the manual-price dialog, taken from the last quote."""
    label = recognized.get("contract_label") or recognized.get("contract") or ""
    prefill = {"contract": str(label)}
    for key in ("bid1", "ask1", "last"):
        value = recognized.get(key)
        prefill[key] = "" if value is None else f"{float(value):g}"
    return prefill


def blank_recognized(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Manual fallback when OCR is unavailable; fields are typed by hand."""
    return {"source": "manual_entry", "validation": {"valid": False, "errors": ["manual market entry"], "warnings": []}}


def _safe_int(var: Any, default: int) -> int:
    """Tk variables raise TclError when the user clears a spinbox."""
    try:
        return int(var.get())
    except Exception:
        return default


def run_form() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    config = wh6.load_config()
    state = load_state()
    smc_tags = config.get("smc_tags", [])

    root = tk.Tk()
    root.title(f"WH6 交易计划 v{FORM_VERSION}（只读，不下单）")
    try:
        root.state("zoomed")  # Windows: start maximized so no label gets clipped
    except tk.TclError:
        root.geometry("980x820")

    # Tk can measure CJK text with a narrower font than it renders, so button
    # labels get clipped. Give every option explicit character width, inner
    # padding and trailing blanks; all three are invisible slack.
    def option_width(text: str) -> int:
        return max(10, len(text) * 2 + 4)

    def option(parent: Any, text: str, value: str, variable: Any, command: Any = None) -> Any:
        return ttk.Radiobutton(parent, text=text + "   ", value=value, variable=variable,
                               command=command, width=option_width(text), padding=(2, 2, 8, 2))

    account_equity = tf_account_equity()
    form: dict[str, Any] = {
        "direction": tk.StringVar(value="long"),
        "price_mode": tk.StringVar(value="opponent"),
        "size": tk.IntVar(value=1),
        "stop_ticks": tk.IntVar(value=30),
        "step_ticks": tk.IntVar(value=1),
        "link_take": tk.BooleanVar(value=True),
        "rr_ratio": tk.StringVar(value="2"),
        "risk_budget": tk.StringVar(value=str(state.get("risk_budget", "400"))),
        "budget_from_limit": tk.BooleanVar(value=bool(state.get("budget_from_limit", True))),
        "take_ticks": tk.IntVar(value=60),
        "entry_price": tk.StringVar(),
        "stop_loss": tk.StringVar(),
        "take_profit": tk.StringVar(),
        "smc_tag": tk.StringVar(value=smc_tags[0] if smc_tags else ""),
        "smc_custom": tk.StringVar(),
        "htf_bias": tk.StringVar(value="long"),
        "is_add_on": tk.BooleanVar(value=False),
        "equity": tk.StringVar(value="" if account_equity is None else f"{account_equity:g}"),
        "tesseract_cmd": tk.StringVar(value=state.get("tesseract_cmd", DEFAULT_TESSERACT)),
        "status": tk.StringVar(value=f"v{FORM_VERSION} 已就绪：点击“刷新行情”读取 WH6，或直接手工填价"),
    }
    recognized: dict[str, Any] = {}
    last_result: dict[str, Any] = {}
    price_spins: dict[str, Any] = {}
    ticks_spins: dict[str, Any] = {}
    rr_preview = tk.StringVar(value="止损 30 跳 → 止盈 60 跳")
    rr_state: dict[str, Any] = {}
    sizing_text = tk.StringVar(value="1R 计算：等待行情")
    budget_spin: dict[str, Any] = {}
    widgets: dict[str, Any] = {}
    header = {
        "contract": tk.StringVar(value="尚未读取行情"),
        "ask": tk.StringVar(value="—"),
        "bid": tk.StringVar(value="—"),
        "last": tk.StringVar(value="—"),
        "meta": tk.StringVar(value="点击「刷新行情」或手工填价"),
    }
    badge_text = tk.StringVar(value="未检查")
    metrics = {key: tk.StringVar(value="—") for key in ("per_lot", "total", "limit", "remain", "cap", "daily")}
    ratio_var = tk.DoubleVar(value=0.0)
    ratio_note = tk.StringVar(value="")
    checked_at = tk.StringVar(value="尚未检查")
    apply_text = tk.StringVar(value="套用建议手数")
    version_info = tk.StringVar(value=f"v{FORM_VERSION} · 尚未检查")

    def set_badge(status: str | None, stale: bool) -> None:
        text, fg, bg = status_badge(status, stale)
        badge_text.set(text)
        badge = widgets.get("badge")
        if badge is not None:
            badge.configure(foreground=fg, background=bg)
        save_button = widgets.get("save")
        if save_button is not None:
            usable = (not stale) and status in ("PASS", "WARN")
            save_button.configure(state="normal" if usable else "disabled")

    def update_metrics() -> None:
        """Live risk numbers; does not decide PASS/REJECT."""
        try:
            fields = collect()
        except ValueError:
            for var in metrics.values():
                var.set("—")
            ratio_var.set(0)
            ratio_note.set("")
            return
        preview = risk_preview(fields, recognized, config, current_equity())
        if "error" in preview:
            for key in ("per_lot", "total", "remain", "cap", "limit", "daily"):
                metrics[key].set("—")
            ratio_var.set(0)
            ratio_note.set(preview["error"])
            return
        budget = preview.get("budget") or {}
        metrics["per_lot"].set(f"¥{preview['per_lot_risk']:g}")
        metrics["total"].set(f"¥{preview['total_risk']:g}")
        if budget.get("mode") == "percent_of_equity":
            metrics["limit"].set(f"¥{preview['limit']:g}（{budget['percent']:g}%）")
        else:
            metrics["limit"].set(f"¥{preview['limit']:g}")
        try:
            daily, daily_budget = wh6.daily_loss_limit(config, current_equity())
        except ValueError as exc:
            metrics["daily"].set(str(exc))
        else:
            if daily is None:
                metrics["daily"].set("—")
            elif daily_budget.get("mode") == "percent_of_equity":
                metrics["daily"].set(f"¥{daily:g}（{daily_budget['percent']:g}%）")
            else:
                metrics["daily"].set(f"¥{daily:g}")
        remain = preview["limit"] - preview["total_risk"]
        metrics["remain"].set(f"¥{remain:g}")
        metrics["cap"].set(f"{preview['max_ticks_at_size']} 跳（{preview['max_ticks_at_size'] * current_tick():g} 点）")
        ratio_var.set(min(preview["ratio"], 1.5) * 100)
        note = widgets.get("ratio_note")
        if preview["level"] == "over":
            ratio_note.set(f"超出上限 ¥{preview['total_risk'] - preview['limit']:g}")
            colour = THEME["danger"]
        elif preview["level"] == "warn":
            ratio_note.set("接近上限，注意成本")
            colour = THEME["warn"]
        else:
            ratio_note.set(f"占上限 {preview['ratio']:.0%}")
            colour = THEME["muted"]
        if note is not None:
            note.configure(foreground=colour)

    def invalidate(*_: Any) -> None:
        """Any edit after a check makes the old PASS/WARN/reject stale."""
        if last_result:
            last_result.clear()
            status = form["status"].get()
            if status.startswith(("检查完成", "已保存", "已按")):
                form["status"].set("参数已修改，需重新点「检查计划」")
        set_badge(None, True)
        update_metrics()

    for key in ("direction", "price_mode", "size", "stop_ticks", "take_ticks",
                "entry_price", "stop_loss", "take_profit", "smc_tag", "smc_custom",
                "htf_bias", "is_add_on"):
        form[key].trace_add("write", invalidate)

    def _fill_text(key: str, text: str) -> None:
        target = widgets.get(key)
        if target is None:
            return
        target.configure(state="normal")
        target.delete("1.0", "end")
        target.insert("1.0", text)
        target.configure(state="disabled")

    def write_output(text: str) -> None:
        _fill_text("detail", text)

    def write_ocr(text: str) -> None:
        _fill_text("ocr", text)

    def append_log(line: str) -> None:
        target = widgets.get("log")
        if target is None:
            return
        target.configure(state="normal")
        target.insert("end", line + "\n")
        target.configure(state="disabled")
        target.see("end")

    def current_tick() -> float:
        variety = recognized.get("variety")
        inst = config["instruments"].get(variety) if variety else None
        return float(inst["min_tick"]) if inst and inst.get("configured") else 1.0

    def sync_increments() -> None:
        """Price spin buttons step by tick × chosen step, so large price bases stay quick."""
        step = _safe_int(form["step_ticks"], 1) or 1
        increment = current_tick() * step
        for spin in price_spins.values():
            spin.configure(increment=increment)

    def fill_prices() -> None:
        entry = estimate_entry(recognized, form["price_mode"].get(), form["direction"].get(), current_tick())
        if entry is None:
            form["entry_price"].set("")
            return
        stop_ticks = _safe_int(form["stop_ticks"], 30) or 1
        if form["link_take"].get():
            try:
                take_ticks = linked_take_ticks(stop_ticks, float(form["rr_ratio"].get()))
            except ValueError as exc:
                form["status"].set(f"止盈联动参数有误：{exc}")
                take_ticks = _safe_int(form["take_ticks"], 60) or 1
            else:
                form["take_ticks"].set(take_ticks)
                rr_preview.set(f"止损 {stop_ticks} 跳 → 止盈 {take_ticks} 跳"
                               f"（{take_ticks * current_tick():g} 点）")
        else:
            take_ticks = _safe_int(form["take_ticks"], 60) or 1
            rr_preview.set("止盈独立设置")
        form["entry_price"].set(f"{entry:g}")
        prices = suggest_prices(entry, form["direction"].get(), current_tick(), stop_ticks, take_ticks)
        form["stop_loss"].set(f"{prices['stop_loss']:g}")
        form["take_profit"].set(f"{prices['take_profit']:g}")
        sync_increments()
        update_sizing()
        update_metrics()

    def on_equity_change(_event: Any = None) -> None:
        """Persist equity, then recompute every limit that depends on it."""
        equity = current_equity()
        if equity is not None:
            try:
                tf_save_equity(equity)
            except (ValueError, OSError):
                pass
        last_result.clear()
        set_badge(None, True)
        sync_budget()
        update_sizing()
        update_metrics()

    def current_equity() -> float | None:
        raw = form["equity"].get().strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def effective_limit_for(instrument: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        return wh6.effective_trade_limit(instrument, current_equity())

    def sync_budget() -> None:
        """Keep 1R equal to the instrument cap when the checkbox is on."""
        variety = recognized.get("variety")
        instrument = config["instruments"].get(variety) if variety else None
        spin = budget_spin.get("widget")
        if form["budget_from_limit"].get() and instrument and instrument.get("configured"):
            try:
                limit, _budget = effective_limit_for(instrument)
            except ValueError:
                limit = None
            if limit is not None:
                form["risk_budget"].set(f"{limit:g}")
        if spin is not None:
            spin.configure(state="disabled" if form["budget_from_limit"].get() else "normal")

    def current_budget() -> float:
        variety = recognized.get("variety")
        instrument = config["instruments"].get(variety) if variety else None
        limit = None
        if instrument:
            try:
                limit, _budget = effective_limit_for(instrument)
            except ValueError:
                limit = None
        return resolve_budget(limit, form["risk_budget"].get(), bool(form["budget_from_limit"].get()))

    def update_sizing() -> None:
        """Show lots affordable within 1R, and whether they exceed the cap."""
        variety = recognized.get("variety")
        instrument = config["instruments"].get(variety) if variety else None
        if not instrument or not instrument.get("configured"):
            sizing_text.set("1R 计算：该品种尚未启用风险配置")
            return
        stop_ticks = _safe_int(form["stop_ticks"], 30) or 1
        value_per_tick = float(instrument["value_per_tick"])
        try:
            budget = current_budget()
            sizing = size_by_risk(budget, stop_ticks, value_per_tick, per_lot_cost(instrument))
        except ValueError as exc:
            sizing_text.set(f"1R 计算：{exc}")
            return
        limit = float(instrument["single_trade_limit_rmb"])
        suffix = "（= 单笔上限）" if form["budget_from_limit"].get() else ""
        cost = per_lot_cost(instrument)
        cost_part = f"+¥{cost:g} 成本" if cost else ""
        text = (f"1R ¥{budget:g}{suffix} ÷（{stop_ticks} 跳 × ¥{value_per_tick:g}{cost_part}）"
                f"= ¥{sizing['per_lot_risk']:g}/手 → {sizing['raw_lots']:.2f} 手"
                f" → {sizing['lots']} 手（风险 ¥{sizing['risk_rmb']:g}，余 ¥{sizing['remainder']:g}）")
        if sizing["lots"] < 1:
            text += "｜1R 不足 1 手"
            apply_text.set("1R 不足 1 手")
        else:
            if sizing["risk_rmb"] > limit:
                text += f"｜⚠ 超过单笔上限 ¥{limit:g}"
            apply_text.set(f"套用 {sizing['lots']} 手")
        sizing_text.set(text)

    def apply_sizing() -> None:
        variety = recognized.get("variety")
        instrument = config["instruments"].get(variety) if variety else None
        if not instrument or not instrument.get("configured"):
            messagebox.showinfo("按 R 下单", "该品种尚未启用风险配置，无法计算手数", parent=root)
            return
        try:
            sizing = size_by_risk(current_budget(),
                                  _safe_int(form["stop_ticks"], 30) or 1,
                                  float(instrument["value_per_tick"]),
                                  per_lot_cost(instrument))
        except ValueError as exc:
            messagebox.showerror("按 R 下单", str(exc), parent=root)
            return
        if sizing["lots"] < 1:
            messagebox.showinfo("按 R 下单", "1R 预算不足以开 1 手，请放宽止损或提高预算", parent=root)
            return
        form["size"].set(int(sizing["lots"]))
        check()
        form["status"].set(f"已按 1R 套用 {sizing['lots']} 手（风险 ¥{sizing['risk_rmb']:g}）")

    def on_link_toggle() -> None:
        """Linked mode derives take-profit from the stop; unlock to edit it."""
        linked = bool(form["link_take"].get())
        if "take_ticks" in ticks_spins:
            ticks_spins["take_ticks"].configure(state="disabled" if linked else "normal")
        box = rr_state.get("box")
        if box is not None:
            box.configure(state="readonly" if linked else "disabled")
        fill_prices()

    def apply_market(data: dict[str, Any]) -> None:
        nonlocal recognized
        recognized = data
        recognized["contract_label"] = data.get("contract")
        header["contract"].set(data.get("contract_label") or data.get("contract") or "?")
        header["ask"].set(str(data.get("ask1", "—")))
        header["bid"].set(str(data.get("bid1", "—")))
        header["last"].set(str(data.get("last", "—")))
        meta = []
        if data.get("ocr_confidence") is not None:
            meta.append(f"置信度 {data['ocr_confidence']:.3f}")
        if data.get("ocr_seconds") is not None:
            meta.append(f"{data['ocr_seconds']}s")
        header["meta"].set(" · ".join(meta) or "手工录入")
        problems = data.get("validation", {}).get("errors", [])
        warnings = data.get("validation", {}).get("warnings", [])
        lines = [f"合约：{data.get('contract', '?')}", f"卖一：{data.get('ask1', '?')}",
                 f"买一：{data.get('bid1', '?')}", f"最新：{data.get('last', '?')}"]
        if data.get("ocr_confidence") is not None:
            lines.append(f"OCR 置信度：{data['ocr_confidence']:.3f}")
        lines.append(f"来源：{data.get('source', '?')}")
        if data.get("window_title"):
            lines.append(f"窗口：{data['window_title']}")
        if data.get("ocr_text"):
            raw = str(data["ocr_text"])
            lines.extend(["", "OCR 原文（截断）：", f"  {raw[:300]}{'…' if len(raw) > 300 else ''}"])
        if problems:
            lines.extend(["", "校验问题："] + [f"  · {item}" for item in problems])
            form["status"].set("行情已读取，但存在校验问题，请核对后再检查计划")
        else:
            form["status"].set("行情已读取，选择方向后点「检查计划」")
        if warnings:
            lines.extend(["", "提示："] + [f"  · {item}" for item in warnings])
        write_output("\n".join(lines))
        write_ocr(str(data.get("ocr_text") or "（本次没有 OCR 原文，可能是手工录入）"))
        low_conf = float(data.get("ocr_confidence") or 1) < float(config.get("ocr_min_confidence", 0.85))
        banner = widgets.get("banner")
        if banner is not None:
            banner.configure(background=THEME["band_low"] if (low_conf or problems) else "#ffffff")
        sync_increments()
        sync_budget()
        fill_prices()

    def refresh() -> None:
        form["status"].set("正在识别 WH6…")
        write_output("正在捕获并识别 WH6 行情…")

        def worker() -> None:
            try:
                data = capture_and_recognize(form["tesseract_cmd"].get() or None)
            except Exception as exc:  # surfaced in the UI, never silent
                root.after(0, lambda error=exc: fail(error))
                return
            root.after(0, lambda: apply_market(data))

        def fail(exc: Exception) -> None:
            form["status"].set(f"识别失败：{exc}")
            write_output(f"识别失败：{exc}\n\n可以改用「手工填价」，直接输入买一/卖一。")

        threading.Thread(target=worker, daemon=True).start()

    def collect() -> dict[str, Any]:
        def number(var: tk.StringVar, name: str) -> float | None:
            raw = var.get().strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} 不是数字：{raw}") from exc

        smc = form["smc_custom"].get().strip() or form["smc_tag"].get().strip()
        return {
            "direction": form["direction"].get(),
            "price_mode": form["price_mode"].get(),
            "size": _safe_int(form["size"], 1),
            "entry_price": number(form["entry_price"], "入场价") if form["price_mode"].get() == "manual" else None,
            "stop_loss": number(form["stop_loss"], "止损价"),
            "take_profit": number(form["take_profit"], "止盈价"),
            "smc_tag": smc,
            "htf_bias": form["htf_bias"].get(),
            "is_add_on": bool(form["is_add_on"].get()),
        }

    def on_price_commit(_event: Any = None) -> None:
        """Typing a price then leaving the box updates the tick count."""
        refresh_ticks_from_prices()
        fill_prices()

    def on_ticks_commit(_event: Any = None) -> None:
        """Typing a tick count then leaving the box updates the prices."""
        fill_prices()

    def refresh_ticks_from_prices() -> None:
        """Keep the tick boxes truthful after the price boxes are edited directly."""
        try:
            entry = float(form["entry_price"].get())
        except ValueError:
            return
        tick = current_tick()
        for price_key, ticks_key in (("stop_loss", "stop_ticks"), ("take_profit", "take_ticks")):
            raw = form[price_key].get().strip()
            if not raw:
                continue
            try:
                form[ticks_key].set(ticks_between(entry, float(raw), tick))
            except ValueError:
                continue

    def check() -> None:
        nonlocal last_result
        if form["link_take"].get():
            fill_prices()
        try:
            result = build_plan_from_form(collect(), recognized, config, current_equity())
        except ValueError as exc:
            form["status"].set(str(exc))
            return
        last_result = result
        status = result["check"]["status"]
        form["status"].set(f"检查完成：{status}")
        set_badge(status, False)
        checked_at.set(datetime.now().strftime("%H:%M:%S"))
        version_info.set(f"v{FORM_VERSION} · 上次检查 {checked_at.get()}")
        reasons = []
        for item in result["check"].get("hard_blocks", []):
            reasons.append(f"阻断：{item}")
        for item in result["check"].get("warnings", []):
            reasons.append(f"警告：{item}")
        _fill_text("reasons", "\n".join(reasons) if reasons else "无阻断项，无警告")
        update_metrics()
        write_output(summarize(result))
        append_log(f"{checked_at.get()}  {status}  {summarize(result).replace(chr(10), ' | ')}")

    def save() -> None:
        if not last_result:
            check()
        if not last_result:
            return
        duplicate = find_duplicate_plan(last_result["plan"])
        if duplicate is not None:
            if not messagebox.askyesno("重复计划", f"相同内容的计划已存在：\n{duplicate.name}\n\n仍要再保存一份吗？", parent=root):
                form["status"].set("已取消保存（内容相同的计划已存在）")
                return
        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        plan_path = PLAN_DIR / f"plan-{stamp}.json"
        plan_path.write_text(json.dumps(last_result["plan"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check_result = dict(last_result["check"])
        check_result["snapshot_id"] = wh6.save_snapshot(last_result["plan"], check_result, SNAPSHOTS)
        write_output(summarize(last_result) + f"\n\n计划已保存：{plan_path}\n快照：{check_result['snapshot_id']}")
        form["status"].set(f"已保存（{last_result['check']['status']}）")
        set_badge(last_result["check"]["status"], False)
        if widgets.get("save") is not None:
            widgets["save"].configure(state="disabled")
        append_log(f"{datetime.now().strftime('%H:%M:%S')}  已保存  {plan_path.name}  {check_result['snapshot_id'][:8]}")

    def manual_market() -> None:
        """Type bid/ask by hand; prefill the last recognized quote."""
        win = tk.Toplevel(root)
        win.title("手工填价")
        win.transient(root)
        prefill = manual_prefill(recognized)
        entries: dict[str, tk.StringVar] = {key: tk.StringVar(value=value) for key, value in prefill.items()}

        for row, (key, label) in enumerate((("contract", "合约（如 V2701）"), ("bid1", "买一"), ("ask1", "卖一"), ("last", "最新"))):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            if key == "contract":
                ttk.Entry(win, textvariable=entries[key], width=20).grid(row=row, column=1, padx=8, pady=4)
            else:
                ttk.Spinbox(win, textvariable=entries[key], width=18, from_=0, to=1000000,
                            increment=1).grid(row=row, column=1, padx=8, pady=4)

        def accept() -> None:
            contract = entries["contract"].get().strip().upper()
            match = re.match(r"([A-Z]+)(\d+)$", contract)
            if not match:
                messagebox.showerror("合约格式", "合约应形如 V2701 或 RB2701", parent=win)
                return
            data: dict[str, Any] = {
                "variety": "V" if match.group(1) == "PVC" else match.group(1),
                "contract": f"{'V' if match.group(1) == 'PVC' else match.group(1)}{match.group(2)}",
                "contract_label": contract,
                "source": "manual_entry",
                "validation": {"valid": True, "errors": [], "warnings": ["manual market entry"]},
            }
            for key in ("bid1", "ask1", "last"):
                raw = entries[key].get().strip()
                if not raw:
                    continue
                try:
                    data[key] = float(raw)
                except ValueError:
                    messagebox.showerror("数值格式", f"{key} 不是数字：{raw}", parent=win)
                    return
            apply_market(data)
            win.destroy()

        ttk.Button(win, text="确定", command=accept).grid(row=4, column=0, columnspan=2, pady=8)

    def edit_config() -> None:
        """Edit per-instrument risk parameters without touching JSON by hand."""
        win = tk.Toplevel(root)
        win.title("品种配置（写入 trade_assistant_config.json）")
        win.transient(root)
        codes = list(config["instruments"].keys())
        default = recognized.get("variety") if recognized.get("variety") in codes else codes[0]
        code_var = tk.StringVar(value=default)
        fields = {key: tk.StringVar() for key, _ in RISK_FIELDS}
        enabled = tk.BooleanVar(value=False)
        per_tick = tk.StringVar(value="每跳 -")
        name = tk.StringVar(value="")
        mode_var = tk.StringVar(value="fixed")
        percent_var = tk.StringVar(value="")
        spins: dict[str, Any] = {}

        def update_mode_state(*_args: Any) -> None:
            percent_mode = mode_var.get() == "percent_of_equity"
            limit_spin = spins.get("single_trade_limit_rmb")
            if limit_spin is not None:
                limit_spin.configure(state="disabled" if percent_mode else "normal")
            percent_spin = spins.get("percent")
            if percent_spin is not None:
                percent_spin.configure(state="normal" if percent_mode else "disabled")

        def update_per_tick(*_args: Any) -> None:
            try:
                value = float(fields["value_per_point"].get()) * float(fields["min_tick"].get())
            except ValueError:
                per_tick.set("每跳 -")
                return
            per_tick.set(f"每跳 {value:g} 元（自动计算）")

        def load_values(*_args: Any) -> None:
            code = code_var.get()
            values = instrument_form_values(config, code)
            for key, _label in RISK_FIELDS:
                fields[key].set(values[key])
            enabled.set(values["configured"])
            mode_var.set(values.get("risk_budget_mode", "fixed"))
            percent_var.set(values.get("risk_percent", ""))
            inst = config["instruments"].get(code, {})
            name.set(inst.get("name") or code)
            update_per_tick()
            update_mode_state()

        ttk.Label(win, text="品种").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        combo = ttk.Combobox(win, textvariable=code_var, values=codes, width=10, state="readonly")
        combo.grid(row=0, column=1, sticky="w", padx=8, pady=6)
        combo.bind("<<ComboboxSelected>>", load_values)
        ttk.Label(win, textvariable=name, foreground="#555").grid(row=0, column=2, sticky="w", padx=8)

        mode_frame = ttk.LabelFrame(win, text="风险预算方式（二选一）", padding=6)
        mode_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 2))
        ttk.Radiobutton(mode_frame, text="固定金额", value="fixed", variable=mode_var,
                        command=update_mode_state).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Radiobutton(mode_frame, text="按总资金百分比", value="percent_of_equity",
                        variable=mode_var, command=update_mode_state).grid(
            row=0, column=1, sticky="w", padx=12)
        ttk.Label(mode_frame, text="比例%").grid(row=0, column=2, sticky="e", padx=(12, 4))
        percent_spin = ttk.Spinbox(mode_frame, textvariable=percent_var, width=8,
                                   from_=0.1, to=100, increment=0.1)
        percent_spin.grid(row=0, column=3, sticky="w")
        spins["percent"] = percent_spin

        for index, (key, label) in enumerate(RISK_FIELDS, start=2):
            ttk.Label(win, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=4)
            spin = ttk.Spinbox(win, textvariable=fields[key], width=14, from_=0, to=1000000, increment=1)
            spin.grid(row=index, column=1, sticky="w", padx=8, pady=4)
            spins[key] = spin
            if key in ("value_per_point", "min_tick"):
                fields[key].trace_add("write", update_per_tick)

        base = 2 + len(RISK_FIELDS)
        ttk.Label(win, textvariable=per_tick).grid(row=base, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(win, text="启用该品种（启用后才会参与风险检查）",
                        variable=enabled).grid(row=base + 1, column=0, columnspan=3,
                                               sticky="w", padx=8, pady=6)

        def save() -> None:
            nonlocal config
            values = {key: fields[key].get() for key, _label in RISK_FIELDS}
            values["configured"] = bool(enabled.get())
            values["risk_budget_mode"] = mode_var.get()
            values["risk_percent"] = percent_var.get()
            try:
                config = save_instrument_config(config, code_var.get(), values)
            except (ValueError, OSError) as exc:
                messagebox.showerror("配置有误", str(exc), parent=win)
                return
            inst = config["instruments"][code_var.get()]
            if inst.get("risk_budget_mode") == "percent_of_equity":
                summary = (f"配置已保存：{code_var.get()} 每点 {inst['value_per_point']:g} 元 / "
                           f"最小跳动 {inst['min_tick']:g} / 上限按总资金 {inst['risk_percent']:g}%")
            else:
                summary = (f"配置已保存：{code_var.get()} 每点 {inst['value_per_point']:g} 元 / "
                           f"最小跳动 {inst['min_tick']:g} / 上限 {inst['single_trade_limit_rmb']:g} 元")
            form["status"].set(summary)
            sync_increments()
            sync_budget()
            fill_prices()
            win.destroy()

        ttk.Button(win, text="保存", command=save, width=10).grid(
            row=base + 2, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(win, text="取消", command=win.destroy, width=10).grid(
            row=base + 2, column=2, pady=(4, 12))
        load_values()

    def open_history() -> None:
        """Browse saved plans, grouped by variety, with delete and CSV export."""
        win = tk.Toplevel(root)
        win.title("历史交易计划")
        win.geometry("1080x620")
        # Without an owner the dialogs attach to the maximized main window and
        # it covers this list, which looks like the window "minimised".
        win.transient(root)
        rows = load_history()
        variety_var = tk.StringVar(value="全部")
        summary = tk.StringVar(value="")

        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        top = ttk.Frame(win, padding=(8, 8, 8, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        ttk.Label(top, text="品种").grid(row=0, column=0, sticky="w")
        codes = ["全部"] + [code for code, _ in group_by_variety(rows)]
        variety_box = ttk.Combobox(top, textvariable=variety_var, values=codes,
                                   width=10, state="readonly")
        variety_box.grid(row=0, column=1, sticky="w", padx=6)

        toolbar_buttons = ttk.Frame(top)
        toolbar_buttons.grid(row=0, column=2, sticky="e")

        table_frame = ttk.Frame(win, padding=(8, 0, 8, 0))
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        table = ttk.Treeview(table_frame, show="tree headings",
                             columns=[key for key, _ in HISTORY_COLUMNS])
        table.column("#0", width=90, anchor="w")
        table.heading("#0", text="品种")
        widths = {"timestamp": 150, "contract": 90, "direction": 60, "size": 50,
                  "entry": 80, "stop_loss": 80, "take_profit": 80, "risk_rmb": 80,
                  "status": 70, "smc_tag": 100, "htf_bias": 70}
        for key, label in HISTORY_COLUMNS:
            table.column(key, width=widths.get(key, 80), anchor="w")
            table.heading(key, text=label)
        table.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scroll.set)

        footer = ttk.Frame(win, padding=(8, 4, 8, 8))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, textvariable=summary, foreground="#555").pack(anchor="w")

        rows_by_iid: dict[str, dict[str, Any]] = {}

        def render(*_args: Any) -> None:
            table.delete(*table.get_children())
            rows_by_iid.clear()
            selected = variety_var.get()
            shown = [row for row in rows if selected in ("全部", row.get("variety"))]
            for variety, group in group_by_variety(shown):
                parent = table.insert("", "end", text=f"{variety}（{len(group)}）", open=True)
                for iid, row in unique_row_ids(group):
                    rows_by_iid[iid] = row
                    direction = "多" if row.get("direction") == "long" else ("空" if row.get("direction") == "short" else "")
                    table.insert(parent, "end", iid=iid, text="", values=(
                        (row.get("timestamp") or "")[:19].replace("T", " "),
                        row.get("contract"), direction, row.get("size"),
                        row.get("entry"), row.get("stop_loss"), row.get("take_profit"),
                        row.get("risk_rmb"), row.get("status"), row.get("smc_tag"),
                        row.get("htf_bias"),
                    ))
            counts = "，".join(f"{code} {len(group)} 条" for code, group in group_by_variety(rows))
            orphan = sum(1 for row in rows if not row.get("path"))
            extra = f"｜其中 {orphan} 条仅在快照中" if orphan else ""
            summary.set(f"共 {len(rows)} 条｜{counts}{extra}" if rows else "还没有保存过计划")

        def refresh(*_args: Any) -> None:
            nonlocal rows
            rows = load_history()
            variety_box.configure(values=["全部"] + [code for code, _ in group_by_variety(rows)])
            render()

        def selected_rows() -> list[dict[str, Any]]:
            return [rows_by_iid[iid] for iid in table.selection() if iid in rows_by_iid]

        def delete_selected() -> None:
            chosen = selected_rows()
            if not chosen:
                messagebox.showinfo("删除", "请先选中要删除的计划", parent=win)
                return
            with_file = [row for row in chosen if row.get("path")]
            snapshot_only = [row for row in chosen if not row.get("path")]
            question = (f"选中 {len(chosen)} 条：\n"
                        f"· 有计划文件 {len(with_file)} 条\n"
                        f"· 仅存在于快照 {len(snapshot_only)} 条\n\n继续删除？")
            if not messagebox.askyesno("删除", question, parent=win):
                return

            removed_files = 0
            failures: list[str] = []
            for row in with_file:
                target = Path(row["path"])
                try:
                    target.unlink()
                    removed_files += 1
                    continue
                except FileNotFoundError:
                    removed_files += 1          # already gone; treat as done
                    continue
                except OSError:
                    pass
                try:                            # Windows: clear read-only then retry
                    target.chmod(0o666)
                    target.unlink()
                    removed_files += 1
                except OSError as exc:
                    failures.append(f"{target.name}: {exc}")

            removed_snaps = 0
            ids = {row.get("snapshot_id") for row in snapshot_only if row.get("snapshot_id")}
            if ids:
                if messagebox.askyesno(
                        "删除快照",
                        f"这 {len(ids)} 条只存在于快照文件，没有计划文件。\n"
                        f"要从快照中永久删除吗？（会先备份 .bak）",
                        parent=win):
                    removed_snaps = remove_snapshot_records(SNAPSHOTS, ids)

            refresh()
            win.lift()
            win.focus_force()
            report = f"已删除：计划文件 {removed_files} 个，快照记录 {removed_snaps} 条"
            form["status"].set(report)
            if failures:
                messagebox.showerror("部分文件删除失败", "\n".join(failures[:8]), parent=win)
            if not removed_files and not removed_snaps:
                messagebox.showinfo("没有变化",
                                    "选中的记录没有可删除的内容（可能已被删除或文件被占用）。",
                                    parent=win)
            else:
                deleted = {row.get("fingerprint") for row in chosen}
                twins = [row for row in rows if row.get("fingerprint") in deleted]
                if twins:
                    messagebox.showinfo(
                        "提示",
                        f"列表里还有 {len(twins)} 条内容完全相同的记录（可能是重复保存）。\n"
                        f"如需一并清理，请再选中删除。",
                        parent=win)

        def export() -> None:
            chosen = selected_rows() or rows
            if not chosen:
                messagebox.showinfo("导出", "没有可导出的记录", parent=win)
                return
            target = filedialog.asksaveasfilename(
                parent=win, title="导出历史计划", defaultextension=".csv",
                initialfile="trade_plans.csv", filetypes=[("CSV", "*.csv")])
            if not target:
                return
            count = export_history_csv(chosen, Path(target))
            messagebox.showinfo("导出", f"已导出 {count} 条到\n{target}", parent=win)
            win.lift()

        def open_folder() -> None:
            PLAN_DIR.mkdir(parents=True, exist_ok=True)
            try:
                import os
                os.startfile(str(PLAN_DIR))  # type: ignore[attr-defined]
            except (AttributeError, OSError) as exc:
                messagebox.showinfo("目录", f"{PLAN_DIR}\n{exc}", parent=win)

        for label, command in (("刷新", refresh), ("导出 CSV", export),
                               ("删除选中", delete_selected), ("打开目录", open_folder)):
            ttk.Button(toolbar_buttons, text=label, command=command, width=10).pack(side="left", padx=4)

        variety_box.bind("<<ComboboxSelected>>", render)
        try:
            render()
        except Exception as exc:  # keep the window usable and explain what broke
            messagebox.showerror("历史计划", f"列表渲染失败：{exc}", parent=win)

    # ---- layout -----------------------------------------------------------
    # Two columns: an order ticket that follows the decision order on the left,
    # a always-visible risk panel on the right.
    root.minsize(1040, 780)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(1, weight=1)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    style.configure("Segment.Toolbutton", font=THEME["font_title"], padding=(16, 8))
    style.configure("Contract.TLabel", font=THEME["font_title"])
    style.configure("Price.TLabel", font=THEME["font_price"])
    style.configure("Section.TLabelframe.Label", font=THEME["font_body"])

    # --- header (fixed) ---------------------------------------------------
    banner = tk.Frame(root, background="#ffffff")
    banner.grid(row=0, column=0, sticky="ew")
    widgets["banner"] = banner
    banner.grid_columnconfigure(3, weight=1)

    ttk.Label(banner, textvariable=header["contract"], style="Contract.TLabel").grid(
        row=0, column=0, sticky="w", padx=(12, 16), pady=10)

    quote = ttk.Frame(banner)
    quote.grid(row=0, column=1, sticky="w")
    for column, (label, key) in enumerate((("卖一", "ask"), ("买一", "bid"), ("最新", "last"))):
        ttk.Label(quote, text=label, foreground=THEME["muted"]).grid(
            row=0, column=column * 2, sticky="e", padx=(0, 4))
        ttk.Label(quote, textvariable=header[key], style="Price.TLabel").grid(
            row=0, column=column * 2 + 1, sticky="w", padx=(0, 18))

    ttk.Label(banner, textvariable=header["meta"], foreground=THEME["muted"]).grid(
        row=0, column=3, sticky="e", padx=(0, 12))

    header_buttons = ttk.Frame(banner)
    header_buttons.grid(row=0, column=4, sticky="e", padx=(0, 12))
    for index, (label, command) in enumerate((
        ("刷新行情 F5", refresh), ("手工填价", manual_market),
        ("品种配置", edit_config), ("历史计划", open_history),
    )):
        ttk.Button(header_buttons, text=label, command=command).grid(row=0, column=index, padx=(6, 0))

    # --- body: ticket + risk panel ---------------------------------------
    body = ttk.Frame(root, padding=(12, 0))
    body.grid(row=1, column=0, sticky="nsew")
    body.grid_columnconfigure(0, weight=3, uniform="cols")
    body.grid_columnconfigure(1, weight=2, uniform="cols")
    body.grid_rowconfigure(0, weight=1)

    ticket = ttk.Frame(body)
    ticket.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    ticket.grid_columnconfigure(0, weight=1)

    # ① 方向
    sec_dir = ttk.LabelFrame(ticket, text="① 方向", padding=8)
    sec_dir.grid(row=0, column=0, sticky="ew", pady=(0, 6))
    for index, (label, value) in enumerate(DIRECTIONS):
        ttk.Radiobutton(sec_dir, text=label, value=value, variable=form["direction"],
                        command=fill_prices, style="Segment.Toolbutton", width=6).grid(
            row=0, column=index, sticky="w", padx=(0, 10))
    ttk.Checkbutton(sec_dir, text="加仓", variable=form["is_add_on"]).grid(
        row=0, column=2, sticky="w", padx=(18, 0))

    # ② 入场
    sec_entry = ttk.LabelFrame(ticket, text="② 入场", padding=8)
    sec_entry.grid(row=1, column=0, sticky="ew", pady=(0, 6))
    for index, (label, value) in enumerate(PRICE_MODES):
        option(sec_entry, label, value, form["price_mode"], fill_prices).grid(
            row=0, column=index, sticky="w", padx=(0, 6))
    entry_row = ttk.Frame(sec_entry)
    entry_row.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
    ttk.Label(entry_row, text="入场价").pack(side="left")
    entry_spin = ttk.Spinbox(entry_row, textvariable=form["entry_price"], width=12,
                             from_=0, to=1000000, increment=1)
    entry_spin.pack(side="left", padx=(6, 18))
    price_spins["entry_price"] = entry_spin
    ttk.Label(entry_row, text="调整步长（跳）").pack(side="left")
    step_box = ttk.Combobox(entry_row, textvariable=form["step_ticks"], values=STEP_CHOICES,
                            width=4, state="readonly")
    step_box.pack(side="left", padx=6)
    step_box.bind("<<ComboboxSelected>>", lambda _event: (sync_increments(), fill_prices()))

    # ③ 止损
    sec_stop = ttk.LabelFrame(ticket, text="③ 止损（决定风险）", padding=8)
    sec_stop.grid(row=2, column=0, sticky="ew", pady=(0, 6))
    ttk.Label(sec_stop, text="跳数").grid(row=0, column=0, sticky="w")
    stop_ticks_spin = ttk.Spinbox(sec_stop, from_=1, to=200, width=6, textvariable=form["stop_ticks"],
                                  command=fill_prices)
    stop_ticks_spin.grid(row=0, column=1, sticky="w", padx=(6, 20))
    ticks_spins["stop_ticks"] = stop_ticks_spin
    ttk.Label(sec_stop, text="价格").grid(row=0, column=2, sticky="w")
    stop_price_spin = ttk.Spinbox(sec_stop, textvariable=form["stop_loss"], width=12,
                                  from_=0, to=1000000, increment=1)
    stop_price_spin.grid(row=0, column=3, sticky="w", padx=(6, 0))
    price_spins["stop_loss"] = stop_price_spin

    # ④ 手数
    sec_size = ttk.LabelFrame(ticket, text="④ 手数（按 1R 推导）", padding=8)
    sec_size.grid(row=3, column=0, sticky="ew", pady=(0, 6))
    budget_row = ttk.Frame(sec_size)
    budget_row.grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(budget_row, text="1R = 单笔上限", variable=form["budget_from_limit"],
                    command=lambda: (sync_budget(), update_sizing())).pack(side="left")
    ttk.Label(budget_row, text="1R 金额").pack(side="left", padx=(14, 4))
    budget_widget = ttk.Spinbox(budget_row, textvariable=form["risk_budget"], width=8,
                                from_=0, to=1000000, increment=50, command=update_sizing)
    budget_widget.pack(side="left")
    budget_spin["widget"] = budget_widget
    form["risk_budget"].trace_add("write", lambda *_args: update_sizing())
    ttk.Button(budget_row, textvariable=apply_text, command=apply_sizing).pack(side="left", padx=(12, 0))

    ttk.Label(sec_size, textvariable=sizing_text, foreground=THEME["ok"]).grid(
        row=1, column=0, sticky="w", pady=(6, 4))
    size_row = ttk.Frame(sec_size)
    size_row.grid(row=2, column=0, sticky="w")
    ttk.Label(size_row, text="手数").pack(side="left")
    size_spin = ttk.Spinbox(size_row, from_=1, to=50, width=6, textvariable=form["size"],
                            command=fill_prices)
    size_spin.pack(side="left", padx=(6, 0))
    ticks_spins["size"] = size_spin

    # ⑤ 止盈
    sec_take = ttk.LabelFrame(ticket, text="⑤ 止盈", padding=8)
    sec_take.grid(row=4, column=0, sticky="ew", pady=(0, 6))
    take_row = ttk.Frame(sec_take)
    take_row.grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(take_row, text="止盈 = 止损 ×", variable=form["link_take"],
                    command=on_link_toggle).pack(side="left")
    rr_box = ttk.Combobox(take_row, textvariable=form["rr_ratio"], values=("1", "1.5", "2", "3", "4"),
                          width=5)
    rr_box.pack(side="left", padx=4)
    for event in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
        rr_box.bind(event, lambda _event: fill_prices())
    rr_state["box"] = rr_box
    ttk.Label(take_row, text="倍（x > 0）").pack(side="left", padx=(4, 12))
    ttk.Label(take_row, textvariable=rr_preview, foreground=THEME["muted"]).pack(side="left")

    take_fields = ttk.Frame(sec_take)
    take_fields.grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Label(take_fields, text="跳数").pack(side="left")
    take_ticks_spin = ttk.Spinbox(take_fields, from_=1, to=400, width=6, textvariable=form["take_ticks"],
                                  command=fill_prices)
    take_ticks_spin.pack(side="left", padx=(6, 20))
    ticks_spins["take_ticks"] = take_ticks_spin
    ttk.Label(take_fields, text="价格").pack(side="left")
    take_price_spin = ttk.Spinbox(take_fields, textvariable=form["take_profit"], width=12,
                                  from_=0, to=1000000, increment=1)
    take_price_spin.pack(side="left", padx=(6, 0))
    price_spins["take_profit"] = take_price_spin

    # ⑥ 标签
    sec_tags = ttk.LabelFrame(ticket, text="⑥ 标签", padding=8)
    sec_tags.grid(row=5, column=0, sticky="ew")
    for column in range(4):
        sec_tags.grid_columnconfigure(column, weight=1, uniform="smc")
    for index, tag in enumerate(smc_tags):
        option(sec_tags, tag, tag, form["smc_tag"]).grid(
            row=index // 4, column=index % 4, sticky="w", padx=4, pady=2)
    tag_row = index // 4 + 1 if smc_tags else 0
    ttk.Label(sec_tags, text="自定义").grid(row=tag_row, column=0, sticky="w", padx=4, pady=(6, 2))
    ttk.Entry(sec_tags, textvariable=form["smc_custom"], width=26).grid(
        row=tag_row, column=1, columnspan=3, sticky="w", padx=4, pady=(6, 2))
    htf_row = tag_row + 1
    ttk.Label(sec_tags, text="HTF").grid(row=htf_row, column=0, sticky="w", padx=4, pady=(4, 0))
    htf_frame = ttk.Frame(sec_tags)
    htf_frame.grid(row=htf_row, column=1, columnspan=3, sticky="w", pady=(4, 0))
    for label, value in HTF_CHOICES:
        option(htf_frame, label, value, form["htf_bias"]).pack(side="left", padx=(0, 6))

    # --- right: risk panel ------------------------------------------------
    panel = ttk.Frame(body)
    panel.grid(row=0, column=1, sticky="nsew")
    panel.grid_columnconfigure(0, weight=1)
    panel.grid_rowconfigure(4, weight=1)

    badge = tk.Label(panel, textvariable=badge_text, font=THEME["font_badge"],
                     foreground=THEME["muted"], background="#f2f2f2", padx=10, pady=12, anchor="center")
    badge.grid(row=0, column=0, sticky="ew")
    widgets["badge"] = badge

    equity_row = ttk.Frame(panel)
    equity_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    ttk.Label(equity_row, text="总资金（元）").pack(side="left")
    equity_spin = ttk.Spinbox(equity_row, textvariable=form["equity"], width=12,
                              from_=0, to=100000000, increment=1000, command=on_equity_change)
    equity_spin.pack(side="left", padx=6)
    ttk.Label(equity_row, text="百分比模式使用", foreground=THEME["muted"]).pack(side="left")
    form["equity"].trace_add("write", lambda *_args: on_equity_change())

    metric_frame = ttk.LabelFrame(panel, text="风险", padding=8)
    metric_frame.grid(row=2, column=0, sticky="ew", pady=8)
    metric_frame.grid_columnconfigure(1, weight=1)
    metric_rows = (("每手风险", "per_lot"), ("计划风险", "total"),
                   ("单笔上限", "limit"), ("剩余预算", "remain"),
                   ("日亏损上限", "daily"))
    for row, (label, key) in enumerate(metric_rows):
        ttk.Label(metric_frame, text=label, foreground=THEME["muted"]).grid(
            row=row, column=0, sticky="w", pady=1)
        ttk.Label(metric_frame, textvariable=metrics[key], font=("Consolas", 11, "bold")).grid(
            row=row, column=1, sticky="e", pady=1)
    ttk.Label(metric_frame, text="最多可承受", foreground=THEME["muted"]).grid(
        row=len(metric_rows), column=0, sticky="w", pady=(6, 0))
    ttk.Label(metric_frame, textvariable=metrics["cap"]).grid(
        row=len(metric_rows), column=1, sticky="e", pady=(6, 0))

    bar_frame = ttk.Frame(panel)
    bar_frame.grid(row=3, column=0, sticky="ew")
    ttk.Progressbar(bar_frame, maximum=100, variable=ratio_var).pack(fill="x")
    note_label = ttk.Label(bar_frame, textvariable=ratio_note, foreground=THEME["muted"])
    note_label.pack(anchor="w", pady=(2, 0))
    widgets["ratio_note"] = note_label

    reasons_frame = ttk.LabelFrame(panel, text="检查发现", padding=6)
    reasons_frame.grid(row=4, column=0, sticky="nsew", pady=8)
    reasons_text = tk.Text(reasons_frame, height=6, wrap="word", state="disabled", relief="flat")
    reasons_text.pack(fill="both", expand=True)
    widgets["reasons"] = reasons_text
    _fill_text("reasons", "尚未检查")

    action_frame = ttk.Frame(panel)
    action_frame.grid(row=5, column=0, sticky="ew")
    ttk.Button(action_frame, text="检查计划  Ctrl+Enter", command=check).pack(fill="x", pady=(0, 6))
    save_button = ttk.Button(action_frame, text="保存计划  Ctrl+S", command=save, state="disabled")
    save_button.pack(fill="x")
    widgets["save"] = save_button

    # --- status bar + log tabs -------------------------------------------
    status_bar = ttk.Frame(root, padding=(12, 4))
    status_bar.grid(row=2, column=0, sticky="ew")
    status_bar.grid_columnconfigure(0, weight=1)
    ttk.Label(status_bar, textvariable=form["status"]).grid(row=0, column=0, sticky="w")
    ttk.Label(status_bar, textvariable=version_info, foreground=THEME["muted"]).grid(
        row=0, column=1, sticky="e")

    book = ttk.Notebook(root)
    book.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
    for title, key in (("行情详情", "detail"), ("OCR 原文", "ocr"), ("本次检查", "log")):
        tab = ttk.Frame(book)
        book.add(tab, text=title)
        view = tk.Text(tab, height=7, wrap="word", state="disabled", relief="flat")
        view.pack(fill="both", expand=True)
        widgets[key] = view

    # --- keyboard shortcuts ----------------------------------------------
    root.bind("<F5>", lambda _event: refresh())
    root.bind("<Control-Return>", lambda _event: check())
    root.bind("<Control-s>", lambda _event: save() if str(widgets["save"].cget("state")) == "normal" else None)
    root.bind("<Alt-Key-1>", lambda _event: (form["direction"].set("long"), fill_prices()))
    root.bind("<Alt-Key-2>", lambda _event: (form["direction"].set("short"), fill_prices()))

    def on_close() -> None:
        save_state({"tesseract_cmd": form["tesseract_cmd"].get(),
                    "risk_budget": form["risk_budget"].get(),
                    "budget_from_limit": bool(form["budget_from_limit"].get())})
        root.destroy()

    for key in ("stop_loss", "take_profit"):
        price_spins[key].bind("<FocusOut>", on_price_commit)
        price_spins[key].bind("<Return>", on_price_commit)
    for key in ("stop_ticks", "take_ticks"):
        ticks_spins[key].bind("<FocusOut>", on_ticks_commit)
        ticks_spins[key].bind("<Return>", on_ticks_commit)

    set_badge(None, True)
    update_metrics()
    on_link_toggle()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


def main() -> int:
    # Printed on every launch so a stale copy is obvious immediately.
    print(f"trade_form v{FORM_VERSION}  ({Path(__file__).resolve()})", flush=True)
    try:
        return run_form()
    except ImportError as exc:
        print(f"tkinter 不可用：{exc}\n请使用带 tkinter 的 Python，或改用命令行参数。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
