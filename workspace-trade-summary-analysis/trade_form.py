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
        warnings = data.get("validation", {}).get("warnings", [])
        lines = [f"合约：{data.get('contract', '?')}", f"卖一：{data.get('ask1', '?')}",
                 f"买一：{data.get('bid1', '?')}", f"最新：{data.get('last', '?')}"]
        if data.get("ocr_confidence") is not None:
            lines.append(f"OCR 置信度：{data['ocr_confidence']:.3f}")
        lines.append(f"来源：{data.get('source', '?')}")
        if data.get("window_title"):
            lines.append(f"窗口：{data['window_title']}")
        if problems:
            lines.extend(["", "校验问题："] + [f"  · {item}" for item in problems])
            form["status"].set("行情已读取，但存在校验问题，请核对后再检查计划")
        else:
            form["status"].set("行情已读取，选择方向后点「检查计划」")
        if warnings:
            lines.extend(["", "提示："] + [f"  · {item}" for item in warnings])
        write_output("\n".join(lines))
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
    # grid everywhere: pack(side="left") shrinks labels once the window is
    # narrower than the requested width, which truncated button text.
    pad = {"padx": 8, "pady": 5}
    root.minsize(940, 720)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(10, weight=1)

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
        ttk.Spinbox(nums, from_=start, to=end, width=width, textvariable=form[key],
                    command=fill_prices).grid(row=0, column=column * 2 + 1, sticky="w")

    prices = ttk.Frame(root)
    prices.grid(row=4, column=0, sticky="w", **pad)
    for column, (label, key) in enumerate((("入场", "entry_price"), ("止损", "stop_loss"), ("止盈", "take_profit"))):
        ttk.Label(prices, text=label).grid(row=0, column=column * 2, sticky="w", padx=(0 if column == 0 else 20, 4))
        ttk.Entry(prices, textvariable=form[key], width=12).grid(row=0, column=column * 2 + 1, sticky="w")

    smc = ttk.LabelFrame(root, text="SMC 标签")
    smc.grid(row=5, column=0, sticky="ew", **pad)
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
    htf.grid(row=6, column=0, sticky="ew", **pad)
    for column in range(4):
        htf.grid_columnconfigure(column, weight=1, uniform="htf")
    for index, (label, value) in enumerate(HTF_CHOICES):
        option(htf, label, value, form["htf_bias"]).grid(
            row=0, column=index, sticky="w", padx=6, pady=4)

    tess = ttk.Frame(root)
    tess.grid(row=7, column=0, sticky="ew", **pad)
    ttk.Label(tess, text="Tesseract").grid(row=0, column=0, sticky="w")
    ttk.Entry(tess, textvariable=form["tesseract_cmd"]).grid(row=0, column=1, sticky="ew", padx=6)
    tess.grid_columnconfigure(1, weight=1)

    actions = ttk.Frame(root)
    actions.grid(row=8, column=0, sticky="w", **pad)
    for column, (label, command) in enumerate((
        ("刷新行情", refresh), ("手工填价", manual_market),
        ("检查计划", check), ("保存计划", save),
    )):
        ttk.Button(actions, text=label, command=command, width=12).grid(row=0, column=column, padx=6)

    ttk.Label(root, textvariable=form["status"], foreground="#333").grid(row=9, column=0, sticky="w", **pad)
    output.grid(row=10, column=0, sticky="nsew", padx=8, pady=(0, 8))

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
