#!/usr/bin/env python3
"""Button-driven trade-plan form for WH6 (read-only helper).

The form only reads WH6 via screenshot OCR and computes a plan locally. It
never clicks the trading software, never submits orders, and never writes to
the WH6 window.

Run on Windows:
    python workspace-trade-summary-analysis\\trade_form.py
"""
from __future__ import annotations

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
DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

DIRECTIONS = (("买多 / long", "long"), ("卖空 / short", "short"))
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
    from tkinter import messagebox, ttk

    config = wh6.load_config()
    state = load_state()
    smc_tags = config.get("smc_tags", [])

    root = tk.Tk()
    root.title("WH6 交易计划（只读，不下单）")
    root.geometry("760x720")

    form: dict[str, Any] = {
        "direction": tk.StringVar(value="long"),
        "price_mode": tk.StringVar(value="opponent"),
        "size": tk.IntVar(value=1),
        "stop_ticks": tk.IntVar(value=30),
        "take_ticks": tk.IntVar(value=60),
        "entry_price": tk.StringVar(),
        "stop_loss": tk.StringVar(),
        "take_profit": tk.StringVar(),
        "smc_tag": tk.StringVar(value=smc_tags[0] if smc_tags else ""),
        "smc_custom": tk.StringVar(),
        "htf_bias": tk.StringVar(value="long"),
        "is_add_on": tk.BooleanVar(value=False),
        "tesseract_cmd": tk.StringVar(value=state.get("tesseract_cmd", DEFAULT_TESSERACT)),
        "status": tk.StringVar(value="点击“刷新行情”读取 WH6，或直接手动填价"),
    }
    recognized: dict[str, Any] = {}
    last_result: dict[str, Any] = {}

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

    def fill_prices() -> None:
        entry = estimate_entry(recognized, form["price_mode"].get(), form["direction"].get(), current_tick())
        if entry is None:
            form["entry_price"].set("")
            return
        form["entry_price"].set(f"{entry:g}")
        prices = suggest_prices(entry, form["direction"].get(), current_tick(),
                                _safe_int(form["stop_ticks"], 30), _safe_int(form["take_ticks"], 60))
        form["stop_loss"].set(f"{prices['stop_loss']:g}")
        form["take_profit"].set(f"{prices['take_profit']:g}")

    def apply_market(data: dict[str, Any]) -> None:
        nonlocal recognized
        recognized = data
        recognized["contract_label"] = data.get("contract")
        market_var.set(market_summary(data))
        problems = data.get("validation", {}).get("errors", [])
        form["status"].set("行情已读取" if not problems else "行情已读取，但有校验问题：" + "；".join(problems))
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

    def check() -> None:
        nonlocal last_result
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
        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        plan_path = PLAN_DIR / f"plan-{stamp}.json"
        plan_path.write_text(json.dumps(last_result["plan"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check_result = dict(last_result["check"])
        check_result["snapshot_id"] = wh6.save_snapshot(last_result["plan"], check_result, SNAPSHOTS)
        write_output(summarize(last_result) + f"\n\n计划已保存：{plan_path}\n快照：{check_result['snapshot_id']}")
        form["status"].set(f"已保存（{last_result['check']['status']}）")

    def manual_market() -> None:
        """Type bid/ask by hand when OCR is unavailable."""
        win = tk.Toplevel(root)
        win.title("手工填价")
        entries: dict[str, tk.StringVar] = {k: tk.StringVar() for k in ("contract", "bid1", "ask1", "last")}
        for row, (key, label) in enumerate((("contract", "合约（如 V2701）"), ("bid1", "买一"), ("ask1", "卖一"), ("last", "最新"))):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(win, textvariable=entries[key], width=20).grid(row=row, column=1, padx=8, pady=4)

        def accept() -> None:
            contract = entries["contract"].get().strip().upper()
            match = re.match(r"([A-Z]+)(\d+)$", contract)
            if not match:
                messagebox.showerror("合约格式", "合约应形如 V2701")
                return
            data: dict[str, Any] = {
                "variety": "V" if match.group(1) == "PVC" else match.group(1),
                "contract": contract,
                "source": "manual_entry",
                "validation": {"valid": True, "errors": [], "warnings": ["manual market entry"]},
            }
            for key in ("bid1", "ask1", "last"):
                raw = entries[key].get().strip()
                if raw:
                    data[key] = float(raw)
            apply_market(data)
            win.destroy()

        ttk.Button(win, text="确定", command=accept).grid(row=4, column=0, columnspan=2, pady=8)

    # ---- layout -----------------------------------------------------------
    pad = {"padx": 6, "pady": 4}
    ttk.Label(root, textvariable=market_var, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", **pad)

    top = ttk.Frame(root); top.pack(anchor="w", fill="x", **pad)
    ttk.Label(top, text="方向").pack(side="left")
    for label, value in DIRECTIONS:
        ttk.Radiobutton(top, text=label, value=value, variable=form["direction"], command=fill_prices).pack(side="left", padx=4)
    ttk.Checkbutton(top, text="加仓", variable=form["is_add_on"]).pack(side="left", padx=12)

    modes = ttk.Frame(root); modes.pack(anchor="w", fill="x", **pad)
    ttk.Label(modes, text="价格").pack(side="left")
    for label, value in PRICE_MODES:
        ttk.Radiobutton(modes, text=label, value=value, variable=form["price_mode"], command=fill_prices).pack(side="left", padx=4)

    nums = ttk.Frame(root); nums.pack(anchor="w", fill="x", **pad)
    ttk.Label(nums, text="手数").pack(side="left")
    ttk.Spinbox(nums, from_=1, to=50, width=5, textvariable=form["size"]).pack(side="left", padx=4)
    ttk.Label(nums, text="止损跳数").pack(side="left", padx=(12, 0))
    ttk.Spinbox(nums, from_=1, to=200, width=6, textvariable=form["stop_ticks"], command=fill_prices).pack(side="left", padx=4)
    ttk.Label(nums, text="止盈跳数").pack(side="left", padx=(12, 0))
    ttk.Spinbox(nums, from_=1, to=400, width=6, textvariable=form["take_ticks"], command=fill_prices).pack(side="left", padx=4)

    prices = ttk.Frame(root); prices.pack(anchor="w", fill="x", **pad)
    for label, key in (("入场", "entry_price"), ("止损", "stop_loss"), ("止盈", "take_profit")):
        ttk.Label(prices, text=label).pack(side="left")
        ttk.Entry(prices, textvariable=form[key], width=10).pack(side="left", padx=4)

    smc = ttk.LabelFrame(root, text="SMC 标签")
    smc.pack(anchor="w", fill="x", **pad)
    for index, tag in enumerate(smc_tags):
        ttk.Radiobutton(smc, text=tag, value=tag, variable=form["smc_tag"]).grid(row=index // 4, column=index % 4, sticky="w", padx=4, pady=2)
    ttk.Label(smc, text="自定义").grid(row=2, column=0, sticky="w", padx=4)
    ttk.Entry(smc, textvariable=form["smc_custom"], width=28).grid(row=2, column=1, columnspan=3, sticky="w", padx=4, pady=4)

    htf = ttk.LabelFrame(root, text="HTF Bias")
    htf.pack(anchor="w", fill="x", **pad)
    for label, value in HTF_CHOICES:
        ttk.Radiobutton(htf, text=label, value=value, variable=form["htf_bias"]).pack(side="left", padx=6)

    tess = ttk.Frame(root); tess.pack(anchor="w", fill="x", **pad)
    ttk.Label(tess, text="Tesseract").pack(side="left")
    ttk.Entry(tess, textvariable=form["tesseract_cmd"], width=52).pack(side="left", padx=4)

    actions = ttk.Frame(root); actions.pack(anchor="w", fill="x", **pad)
    ttk.Button(actions, text="刷新行情", command=refresh).pack(side="left", padx=4)
    ttk.Button(actions, text="手工填价", command=manual_market).pack(side="left", padx=4)
    ttk.Button(actions, text="检查计划", command=check).pack(side="left", padx=4)
    ttk.Button(actions, text="保存计划", command=save).pack(side="left", padx=4)

    ttk.Label(root, textvariable=form["status"], foreground="#333").pack(anchor="w", **pad)
    output.pack(fill="both", expand=True, padx=6, pady=6)

    def on_close() -> None:
        save_state({"tesseract_cmd": form["tesseract_cmd"].get()})
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


def main() -> int:
    try:
        return run_form()
    except ImportError as exc:
        print(f"tkinter 不可用：{exc}\n请使用带 tkinter 的 Python，或改用命令行参数。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
