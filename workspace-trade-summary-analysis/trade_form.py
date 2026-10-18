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
FORM_VERSION = "1.11"

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


def build_plan_from_form(fields: dict[str, Any], recognized: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
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
    result = wh6.check_plan(plan, config)
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

    value_per_point = number("value_per_point", "每点价值（元）", positive=True)
    min_tick = number("min_tick", "最小跳动（点）", positive=True)
    limit = number("single_trade_limit_rmb", "单笔上限（元）", positive=True)
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
        "configured": bool(enabled and value_per_point and min_tick and limit),
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

    form: dict[str, Any] = {
        "direction": tk.StringVar(value="long"),
        "price_mode": tk.StringVar(value="opponent"),
        "size": tk.IntVar(value=1),
        "stop_ticks": tk.IntVar(value=30),
        "step_ticks": tk.IntVar(value=1),
        "link_take": tk.BooleanVar(value=True),
        "rr_ratio": tk.StringVar(value="2"),
        "take_ticks": tk.IntVar(value=60),
        "entry_price": tk.StringVar(),
        "stop_loss": tk.StringVar(),
        "take_profit": tk.StringVar(),
        "smc_tag": tk.StringVar(value=smc_tags[0] if smc_tags else ""),
        "smc_custom": tk.StringVar(),
        "htf_bias": tk.StringVar(value="long"),
        "is_add_on": tk.BooleanVar(value=False),
        "tesseract_cmd": tk.StringVar(value=state.get("tesseract_cmd", DEFAULT_TESSERACT)),
        "status": tk.StringVar(value=f"v{FORM_VERSION} 已就绪：点击“刷新行情”读取 WH6，或直接手工填价"),
    }
    recognized: dict[str, Any] = {}
    last_result: dict[str, Any] = {}
    price_spins: dict[str, Any] = {}
    ticks_spins: dict[str, Any] = {}
    rr_preview = tk.StringVar(value="止损 30 跳 → 止盈 60 跳")
    rr_state: dict[str, Any] = {}

    def invalidate(*_: Any) -> None:
        """Any edit after a check makes the old PASS/WARN/reject stale."""
        if last_result:
            last_result.clear()
            status = form["status"].get()
            if status.startswith(("检查完成", "已保存")):
                form["status"].set("参数已修改，需重新点「检查计划」")

    for key in ("direction", "price_mode", "size", "stop_ticks", "take_ticks",
                "entry_price", "stop_loss", "take_profit", "smc_tag", "smc_custom",
                "htf_bias", "is_add_on"):
        form[key].trace_add("write", invalidate)

    market_var = tk.StringVar(value="尚未读取行情")
    output = tk.Text(root, height=10, width=90, state="disabled", wrap="word")

    def write_output(text: str) -> None:
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("1.0", text)
        output.configure(state="disabled")

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
        market_var.set(market_summary(data))
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
        sync_increments()
        fill_prices()

    def refresh() -> None:
        form["status"].set("正在识别 WH6…")
        write_output("正在捕获并识别 WH6 行情…")

        def worker() -> None:
            try:
                data = capture_and_recognize(form["tesseract_cmd"].get() or None)
            except Exception as exc:  # surfaced in the UI, never silent
                root.after(0, lambda: fail(exc))
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
        refresh_ticks_from_prices()
        if form["link_take"].get():
            fill_prices()
        try:
            result = build_plan_from_form(collect(), recognized, config)
        except ValueError as exc:
            form["status"].set(str(exc))
            return
        last_result = result
        form["status"].set(f"检查完成：{result['check']['status']}")
        write_output(summarize(result))

    def save() -> None:
        if not last_result:
            check()
        if not last_result:
            return
        duplicate = find_duplicate_plan(last_result["plan"])
        if duplicate is not None:
            if not messagebox.askyesno("重复计划", f"相同内容的计划已存在：\n{duplicate.name}\n\n仍要再保存一份吗？"):
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

    def manual_market() -> None:
        """Type bid/ask by hand; prefill the last recognized quote."""
        win = tk.Toplevel(root)
        win.title("手工填价")
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
                messagebox.showerror("合约格式", "合约应形如 V2701 或 RB2701")
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
                    messagebox.showerror("数值格式", f"{key} 不是数字：{raw}")
                    return
            apply_market(data)
            win.destroy()

        ttk.Button(win, text="确定", command=accept).grid(row=4, column=0, columnspan=2, pady=8)

    def edit_config() -> None:
        """Edit per-instrument risk parameters without touching JSON by hand."""
        nonlocal config
        win = tk.Toplevel(root)
        win.title("品种配置（写入 trade_assistant_config.json）")
        codes = list(config["instruments"].keys())
        default = recognized.get("variety") if recognized.get("variety") in codes else codes[0]
        code_var = tk.StringVar(value=default)
        fields = {key: tk.StringVar() for key, _ in RISK_FIELDS}
        enabled = tk.BooleanVar(value=False)
        per_tick = tk.StringVar(value="每跳 -")
        name = tk.StringVar(value="")

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
            inst = config["instruments"].get(code, {})
            name.set(inst.get("name") or code)
            update_per_tick()

        ttk.Label(win, text="品种").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        combo = ttk.Combobox(win, textvariable=code_var, values=codes, width=10, state="readonly")
        combo.grid(row=0, column=1, sticky="w", padx=8, pady=6)
        combo.bind("<<ComboboxSelected>>", load_values)
        ttk.Label(win, textvariable=name, foreground="#555").grid(row=0, column=2, sticky="w", padx=8)

        for index, (key, label) in enumerate(RISK_FIELDS, start=1):
            ttk.Label(win, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=4)
            spin = ttk.Spinbox(win, textvariable=fields[key], width=14, from_=0, to=1000000, increment=1)
            spin.grid(row=index, column=1, sticky="w", padx=8, pady=4)
            if key in ("value_per_point", "min_tick"):
                fields[key].trace_add("write", update_per_tick)

        ttk.Label(win, textvariable=per_tick).grid(row=len(RISK_FIELDS) + 1, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(win, text="启用该品种（启用后才会参与风险检查）",
                        variable=enabled).grid(row=len(RISK_FIELDS) + 2, column=0, columnspan=3, sticky="w", padx=8, pady=6)

        def save() -> None:
            nonlocal config
            values = {key: fields[key].get() for key, _label in RISK_FIELDS}
            values["configured"] = bool(enabled.get())
            try:
                config = save_instrument_config(config, code_var.get(), values)
            except (ValueError, OSError) as exc:
                messagebox.showerror("配置有误", str(exc))
                return
            inst = config["instruments"][code_var.get()]
            form["status"].set(
                f"配置已保存：{code_var.get()} 每点 {inst['value_per_point']:g} 元 / "
                f"最小跳动 {inst['min_tick']:g} / 上限 {inst['single_trade_limit_rmb']:g} 元"
            )
            sync_increments()
            fill_prices()
            win.destroy()

        ttk.Button(win, text="保存", command=save, width=10).grid(
            row=len(RISK_FIELDS) + 3, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(win, text="取消", command=win.destroy, width=10).grid(
            row=len(RISK_FIELDS) + 3, column=2, pady=(4, 12))
        load_values()

    def open_history() -> None:
        """Browse saved plans, grouped by variety, with delete and CSV export."""
        win = tk.Toplevel(root)
        win.title("历史交易计划")
        win.geometry("1080x620")
        rows = load_history()
        variety_var = tk.StringVar(value="全部")
        summary = tk.StringVar(value="")

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="品种").pack(side="left")
        codes = ["全部"] + [code for code, _ in group_by_variety(rows)]
        variety_box = ttk.Combobox(top, textvariable=variety_var, values=codes,
                                   width=10, state="readonly")
        variety_box.pack(side="left", padx=6)

        table = ttk.Treeview(win, show="tree headings",
                             columns=[key for key, _ in HISTORY_COLUMNS])
        table.column("#0", width=90, anchor="w")
        table.heading("#0", text="品种")
        widths = {"timestamp": 150, "contract": 90, "direction": 60, "size": 50,
                  "entry": 80, "stop_loss": 80, "take_profit": 80, "risk_rmb": 80,
                  "status": 70, "smc_tag": 100, "htf_bias": 70}
        for key, label in HISTORY_COLUMNS:
            table.column(key, width=widths.get(key, 80), anchor="w")
            table.heading(key, text=label)
        scroll = ttk.Scrollbar(win, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side="left", fill="y", padx=(0, 8), pady=(0, 8))

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
            summary.set(f"共 {len(rows)} 条｜{counts}" if rows else "还没有保存过计划")

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
                messagebox.showinfo("删除", "请先选中要删除的计划")
                return
            if not messagebox.askyesno("删除", f"删除选中的 {len(chosen)} 条计划文件？\n快照记录会保留作为审计痕迹。"):
                return
            removed = 0
            for row in chosen:
                path = row.get("path")
                if path:
                    try:
                        Path(path).unlink()
                        removed += 1
                    except OSError as exc:
                        messagebox.showerror("删除失败", f"{path}\n{exc}")
            refresh()
            form["status"].set(f"已删除 {removed} 条计划文件（快照保留）")

        def export() -> None:
            chosen = selected_rows() or rows
            if not chosen:
                messagebox.showinfo("导出", "没有可导出的记录")
                return
            target = filedialog.asksaveasfilename(
                parent=win, title="导出历史计划", defaultextension=".csv",
                initialfile="trade_plans.csv", filetypes=[("CSV", "*.csv")])
            if not target:
                return
            count = export_history_csv(chosen, Path(target))
            messagebox.showinfo("导出", f"已导出 {count} 条到\n{target}")

        def open_folder() -> None:
            PLAN_DIR.mkdir(parents=True, exist_ok=True)
            try:
                import os
                os.startfile(str(PLAN_DIR))  # type: ignore[attr-defined]
            except (AttributeError, OSError) as exc:
                messagebox.showinfo("目录", f"{PLAN_DIR}\n{exc}")

        buttons = ttk.Frame(top)
        buttons.pack(side="right")
        for label, command in (("刷新", refresh), ("导出 CSV", export),
                               ("删除选中", delete_selected), ("打开目录", open_folder)):
            ttk.Button(buttons, text=label, command=command, width=10).pack(side="left", padx=4)

        ttk.Label(win, textvariable=summary, foreground="#555").pack(anchor="w", padx=10, pady=(0, 4))
        variety_box.bind("<<ComboboxSelected>>", render)
        try:
            render()
        except Exception as exc:  # keep the window usable and explain what broke
            messagebox.showerror("历史计划", f"列表渲染失败：{exc}")

    # ---- layout -----------------------------------------------------------
    # grid everywhere: pack(side="left") shrinks labels once the window is
    # narrower than the requested width, which truncated button text.
    pad = {"padx": 8, "pady": 5}
    root.minsize(940, 720)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(11, weight=1)

    ttk.Label(root, textvariable=market_var, font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w", **pad)

    direction_row = ttk.Frame(root)
    direction_row.grid(row=1, column=0, sticky="w", **pad)
    ttk.Label(direction_row, text="方向").grid(row=0, column=0, sticky="w")
    for index, (label, value) in enumerate(DIRECTIONS, start=1):
        option(direction_row, label, value, form["direction"], fill_prices).grid(
            row=0, column=index, sticky="w", padx=8)
    ttk.Checkbutton(direction_row, text="加仓", width=option_width("加仓"),
                    variable=form["is_add_on"]).grid(row=0, column=3, sticky="w", padx=20)

    modes = ttk.Frame(root)
    modes.grid(row=2, column=0, sticky="w", **pad)
    ttk.Label(modes, text="价格").grid(row=0, column=0, sticky="w")
    for index, (label, value) in enumerate(PRICE_MODES, start=1):
        option(modes, label, value, form["price_mode"], fill_prices).grid(
            row=0, column=index, sticky="w", padx=8)

    nums = ttk.Frame(root)
    nums.grid(row=3, column=0, sticky="w", **pad)
    for column, (label, key, start, end, width) in enumerate((
        ("手数", "size", 1, 50, 5),
        ("止损跳数", "stop_ticks", 1, 200, 6),
        ("止盈跳数", "take_ticks", 1, 400, 6),
    )):
        ttk.Label(nums, text=label).grid(row=0, column=column * 2, sticky="w", padx=(0 if column == 0 else 20, 4))
        tick_spin = ttk.Spinbox(nums, from_=start, to=end, width=width, textvariable=form[key],
                                command=fill_prices)
        tick_spin.grid(row=0, column=column * 2 + 1, sticky="w")
        ticks_spins[key] = tick_spin
    ttk.Label(nums, text="价格调整步长（跳）").grid(row=0, column=6, sticky="w", padx=(20, 4))
    step_box = ttk.Combobox(nums, textvariable=form["step_ticks"], values=STEP_CHOICES,
                            width=4, state="readonly")
    step_box.grid(row=0, column=7, sticky="w")
    step_box.bind("<<ComboboxSelected>>", lambda _event: (sync_increments(), fill_prices()))

    risk_row = ttk.Frame(root)
    risk_row.grid(row=4, column=0, sticky="w", **pad)
    ttk.Checkbutton(risk_row, text="止盈 = 止损 ×", variable=form["link_take"],
                    command=on_link_toggle).grid(row=0, column=0, sticky="w")
    rr_box = ttk.Combobox(risk_row, textvariable=form["rr_ratio"], values=("1", "1.5", "2", "3", "4"), width=5)
    rr_box.grid(row=0, column=1, sticky="w", padx=4)
    for event in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
        rr_box.bind(event, lambda _event: fill_prices())
    rr_state["box"] = rr_box
    ttk.Label(risk_row, text="倍（x > 0）").grid(row=0, column=2, sticky="w")
    ttk.Label(risk_row, textvariable=rr_preview, foreground="#555").grid(row=0, column=3, sticky="w", padx=(16, 0))

    prices = ttk.Frame(root)
    prices.grid(row=5, column=0, sticky="w", **pad)
    for column, (label, key) in enumerate((("入场", "entry_price"), ("止损", "stop_loss"), ("止盈", "take_profit"))):
        ttk.Label(prices, text=label).grid(row=0, column=column * 2, sticky="w", padx=(0 if column == 0 else 20, 4))
        spin = ttk.Spinbox(prices, textvariable=form[key], width=11, from_=0, to=1000000, increment=1)
        spin.grid(row=0, column=column * 2 + 1, sticky="w")
        price_spins[key] = spin

    smc = ttk.LabelFrame(root, text="SMC 标签")
    smc.grid(row=6, column=0, sticky="ew", **pad)
    for column in range(4):
        smc.grid_columnconfigure(column, weight=1, uniform="smc")
    for index, tag in enumerate(smc_tags):
        option(smc, tag, tag, form["smc_tag"]).grid(
            row=index // 4, column=index % 4, sticky="w", padx=6, pady=3)
    custom_row = index // 4 + 1 if smc_tags else 0
    ttk.Label(smc, text="自定义").grid(row=custom_row, column=0, sticky="w", padx=6, pady=(6, 4))
    ttk.Entry(smc, textvariable=form["smc_custom"], width=30).grid(
        row=custom_row, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 4))

    htf = ttk.LabelFrame(root, text="HTF Bias")
    htf.grid(row=7, column=0, sticky="ew", **pad)
    for column in range(4):
        htf.grid_columnconfigure(column, weight=1, uniform="htf")
    for index, (label, value) in enumerate(HTF_CHOICES):
        option(htf, label, value, form["htf_bias"]).grid(
            row=0, column=index, sticky="w", padx=6, pady=4)

    tess = ttk.Frame(root)
    tess.grid(row=8, column=0, sticky="ew", **pad)
    ttk.Label(tess, text="Tesseract").grid(row=0, column=0, sticky="w")
    ttk.Entry(tess, textvariable=form["tesseract_cmd"]).grid(row=0, column=1, sticky="ew", padx=6)
    tess.grid_columnconfigure(1, weight=1)

    actions = ttk.Frame(root)
    actions.grid(row=9, column=0, sticky="w", **pad)
    for column, (label, command) in enumerate((
        ("刷新行情", refresh), ("手工填价", manual_market),
        ("检查计划", check), ("保存计划", save),
        ("品种配置", edit_config), ("历史计划", open_history),
    )):
        ttk.Button(actions, text=label, command=command, width=12).grid(row=0, column=column, padx=6)

    ttk.Label(root, textvariable=form["status"], foreground="#333").grid(row=10, column=0, sticky="w", **pad)
    output.grid(row=11, column=0, sticky="nsew", padx=8, pady=(0, 8))

    def on_close() -> None:
        save_state({"tesseract_cmd": form["tesseract_cmd"].get()})
        root.destroy()

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
