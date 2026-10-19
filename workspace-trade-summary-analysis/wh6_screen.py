#!/usr/bin/env python3
"""Read-only screenshot capture and OCR adapter for WH6 (mytrader_spqh.exe).

No input injection, clicks, order submission, or window modification is performed.
Windows dependencies are optional: pywin32 + Pillow + pytesseract + Tesseract OCR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from trade_assistant import (load_config, check_plan, save_snapshot, calculate_risk,
                                 load_account, effective_trade_limit, daily_loss_limit)
except ModuleNotFoundError:  # direct file import from the test runner
    import importlib.util
    _spec = importlib.util.spec_from_file_location("trade_assistant", Path(__file__).with_name("trade_assistant.py"))
    _module = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(_module)
    load_config, check_plan, save_snapshot = _module.load_config, _module.check_plan, _module.save_snapshot
    calculate_risk = _module.calculate_risk
    load_account = _module.load_account
    effective_trade_limit = _module.effective_trade_limit
    daily_loss_limit = _module.daily_loss_limit

TOOL_VERSION = "2026-10.25"
PROCESS_NAME = "mytrader_spqh.exe"
APP_NAME = "wh6"


VARIETY_ALIASES = {"PVC": "V"}
# WH6 shows a Chinese name next to the code; the Chinese name is usually the
# most reliable signal because OCR mangles the short Latin code.
VARIETY_BY_NAME = {
    "螺纹钢": "RB",
    "聚氯乙烯": "V",
    "生猪": "LH",
    "玻璃": "FG",
    "甲醇": "MA",
    "焦煤": "JM",
    "玉米": "C",
    "橡胶": "RU",
    "鸡蛋": "JD",
    "铁矿石": "I",
}
VARIETY_CODES = ("PVC", "RB", "JD", "LH", "RU", "FG", "MA", "JM", "V", "C", "I")


def _contract_from_text(text: str) -> tuple[str, str, str, int] | None:
    """Return variety, canonical contract, WH6 label, and match end offset."""
    for name, code in VARIETY_BY_NAME.items():
        match = re.search(name + r"\s*(\d(?:[\s,.-]*\d){2,3})(?!\d)", text)
        if match:
            digits = re.sub(r"[^0-9]", "", match.group(1))
            return code, f"{code}{digits}", f"{code}{digits}", match.end()
    code_pattern = "|".join(r"\s*".join(ch for ch in code) for code in VARIETY_CODES)
    match = re.search(
        r"(?<![A-Za-z0-9])(" + code_pattern + r")\s*(\d(?:[\s,.-]*\d){2,3})(?!\d)",
        text,
        re.I,
    )
    if match:
        raw_code = re.sub(r"\s", "", match.group(1)).upper()
        canonical = VARIETY_ALIASES.get(raw_code, raw_code)
        digits = re.sub(r"[^0-9]", "", match.group(2))
        return canonical, f"{canonical}{digits}", f"{raw_code}{digits}", match.end()
    return None


def _prices_near(text: str, label: str) -> list[float]:
    return [float(value) for value in re.findall(label + r"\s*[:：]?\s*(\d{2,6}(?:\.\d+)?)", text)]


def parse_wh6_ocr(text: str, confidence: float | None = None) -> dict[str, Any]:
    """Parse WH6 right quote panel and return normalized, untrusted OCR data."""
    # Normalize common OCR confusions from WH6's compact contract code.
    text = re.sub(r"(?i)P\s*V\s*C", "PVC", text)
    text = re.sub(r"(?i)\bV\s*2\s*7\s*0\s*1\b", "V2701", text)
    # Tesseract often separates Chinese glyphs or replaces 买入 with a
    # Latin-looking token. Normalize only label-shaped fragments before prices.
    text = re.sub(r"卖\s*[出一]", "卖出", text)
    text = re.sub(r"买\s*[入一]", "买入", text)
    text = re.sub(r"最\s*新", "最新", text)
    result: dict[str, Any] = {}
    contract = _contract_from_text(text)
    panel_start = 0
    if contract:
        result["variety"], result["contract"], result["contract_label"], panel_start = contract
    # WH6 quote panel labels: 卖出 = ask1, 买入 = bid1, 最新 = last.
    for label, field in (("卖出", "ask1"), ("买入", "bid1"), ("最新", "last")):
        values = _prices_near(text, label)
        if not values:
            continue
        # 卖出/买入 belong to the best quote; 最新 is a single price.
        result.setdefault(field, min(values) if field == "ask1" else values[0])
    # Five-level book (螺纹钢/铁矿石 panels): 卖五..卖一 and 买一..买五.
    # The best ask is the lowest 卖 row; the best bid is the highest 买 row.
    asks = []
    bids = []
    for level in "一二三四五":
        asks.extend(_prices_near(text, r"卖\s*" + level))
        bids.extend(_prices_near(text, r"买\s*" + level))
    if asks:
        result.setdefault("ask1", min(asks))
    if bids:
        result.setdefault("bid1", max(bids))
    # OCR often drops one glyph from a label, e.g. `出 4903` / `入 4902` / `新 4903`.
    # Only use these forms after the contract so chart labels cannot leak in.
    panel = text[panel_start:]
    if "ask1" not in result:
        match = re.search(r"出\s*[:：]?\s*(\d{2,6}(?:\.\d+)?)", panel)
        if match:
            result["ask1"] = float(match.group(1))
    if "bid1" not in result:
        match = re.search(r"(?:入|EVN)\s*[:：]?\s*(\d{2,6}(?:\.\d+)?)", panel)
        if match:
            result["bid1"] = float(match.group(1))
    if "last" not in result:
        match = re.search(r"新\s*[:：]?\s*(\d{2,6}(?:\.\d+)?)", panel)
        if match:
            result["last"] = float(match.group(1))
    # Quote panel secondary values are optional; capture only when label-based.
    for label, field in (("涨跌", "change_text"), ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("昨收", "prev_close"), ("持仓", "open_interest"), ("总量", "volume")):
        match = re.search(label + r"\s*[:：]?\s*([+-]?[0-9,]+(?:\.[0-9]+)?%?)", text)
        if match:
            result[field] = match.group(1).replace(",", "")
    result["source"] = "wh6_ocr"
    result["tool_version"] = TOOL_VERSION
    if confidence is not None:
        result["ocr_confidence"] = round(float(confidence), 4)
    return result


def validate_recognized_market(data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    variety = data.get("variety")
    if not variety:
        errors.append("Could not identify contract/variety")
    if not data.get("contract"):
        errors.append("Could not identify contract code")
    for key in ("bid1", "ask1", "last"):
        value = data.get(key)
        if value is None:
            errors.append(f"Missing recognized field {key}")
    inst = config["instruments"].get(variety) if variety else None
    if not inst or not inst.get("configured", False):
        if variety:
            warnings.append(f"{variety} is recognized but its risk config is not enabled")
    else:
        tick = float(inst["min_tick"])
        for key in ("bid1", "ask1", "last"):
            value = data.get(key)
            if value is not None and abs(float(value) / tick - round(float(value) / tick)) > 1e-7:
                errors.append(f"{key}={value} does not match configured tick {tick}")
    if data.get("bid1") is not None and data.get("ask1") is not None:
        if float(data["bid1"]) > float(data["ask1"]):
            errors.append("Bid is greater than ask; OCR likely misread values")
    if data.get("ocr_confidence", 1) < float(config.get("ocr_min_confidence", .85)):
        warnings.append("OCR confidence is below configured threshold; manual confirmation required")
    data["validation"] = {"valid": not errors, "errors": errors, "warnings": warnings}
    return data


def filter_window_records(records: list[dict[str, Any]], title_filter: str | None = None) -> list[dict[str, Any]]:
    """Keep windows whose title contains the requested case-insensitive text."""
    if not title_filter:
        return records
    needle = title_filter.casefold()
    return [record for record in records if needle in str(record.get("title", "")).casefold()]


def enumerate_windows_process(process_name: str = PROCESS_NAME) -> list[dict[str, Any]]:
    """List visible top-level windows belonging to the requested process."""
    if sys.platform != "win32":
        raise RuntimeError("Process-window enumeration requires Windows")
    try:
        import ctypes
        import win32gui  # type: ignore
        import win32process  # type: ignore
        import psutil  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install optional Windows packages: pywin32, psutil") from exc
    try:
        # Use physical screen coordinates on Windows with display scaling.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    matching: list[dict[str, Any]] = []
    def enum(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name().casefold()
        except (psutil.Error, OSError):
            return
        if name == process_name.casefold():
            matching.append({"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "pid": pid, "rect": win32gui.GetWindowRect(hwnd)})
    win32gui.EnumWindows(enum, None)
    return matching


def _print_window(hwnd: int) -> Any | None:
    """Capture a covered/minimized Windows window without activating it."""
    try:
        import ctypes
        from PIL import Image, ImageGrab  # type: ignore
        import win32gui  # type: ignore
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        # PrintWindow renders the window into a compatible bitmap while the
        # user may be viewing another application; no click or focus change.
        image = None
        dc = win32gui.GetWindowDC(hwnd)
        try:
            import win32ui  # type: ignore
            src = win32ui.CreateDCFromHandle(dc)
            dst = src.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(src, width, height)
            dst.SelectObject(bitmap)
            result = ctypes.windll.user32.PrintWindow(hwnd, dst.GetSafeHdc(), 2)
            if result:
                bitmap_info = bitmap.GetInfo()
                bits = bitmap.GetBitmapBits(True)
                image = Image.frombuffer("RGB", (bitmap_info["bmWidth"], bitmap_info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
            else:
                image = None
            dst.DeleteDC()
            src.DeleteDC()
        finally:
            win32gui.ReleaseDC(hwnd, dc)
        return image
    except Exception:
        return None


def capture_windows_process(process_name: str = PROCESS_NAME, interval: float = 0.0, count: int = 1, capture_mode: str = "app") -> list[dict[str, Any]]:
    """Capture WH6 windows, their containing monitor, or the full desktop."""
    if sys.platform != "win32":
        raise RuntimeError("Direct process-window capture requires Windows; use --image on this platform")
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
        import psutil  # type: ignore
        from PIL import ImageGrab  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install optional Windows packages: pywin32, psutil, Pillow") from exc
    import time
    matching_records = enumerate_windows_process(process_name)
    if not matching_records:
        raise RuntimeError(f"No visible window found for process {process_name}")
    matching = [(record["hwnd"], record["title"]) for record in matching_records]
    captures = []
    if capture_mode not in ("app", "window", "monitor"):
        raise ValueError("capture_mode must be app, window, or monitor")
    for idx in range(max(1, count)):
        frames = []
        valid_rects: list[tuple[int, int, int, int, str]] = []
        for hwnd, title in matching:
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] > rect[0] and rect[3] > rect[1]:
                valid_rects.append((*rect, title))
        if not valid_rects:
            raise RuntimeError("Matching WH6 process has no capturable visible window")
        if capture_mode == "app":
            # Print each WH6 top-level window so OCR still works after the user
            # switches to another application or covers WH6.
            for hwnd, title in matching:
                image = _print_window(hwnd)
                if image is not None:
                    frames.append((title, image))
                else:
                    rect = win32gui.GetWindowRect(hwnd)
                    image = ImageGrab.grab(bbox=rect, all_screens=True)
                    frames.append((title + " (screen fallback)", image))
        else:
            for left, top, right, bottom, title in valid_rects:
                if capture_mode == "monitor":
                    try:
                        import win32api  # type: ignore
                        monitor = win32api.MonitorFromPoint((left, top), 2)
                        monitor_info = win32api.GetMonitorInfo(monitor)
                        bbox = monitor_info["Monitor"]
                    except ImportError:
                        bbox = (0, 0, 1920, 1080)
                    except Exception:
                        bbox = (0, 0, 1920, 1080)
                else:
                    bbox = (left, top, right, bottom)
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
                frames.append((title, image))
        if not frames:
            raise RuntimeError("Matching WH6 process has no capturable visible window")
        captures.append({"frames": frames, "captured_at": datetime.now(timezone.utc).isoformat(), "capture_mode": capture_mode})
        if idx + 1 < count:
            time.sleep(max(.25, interval))
    return captures


QUOTE_FIELDS = ("variety", "bid1", "ask1", "last")


def _quote_fields_found(parsed: dict[str, Any]) -> int:
    return sum(parsed.get(key) is not None for key in QUOTE_FIELDS)


def _quote_score(parsed: dict[str, Any]) -> tuple[int, float]:
    return _quote_fields_found(parsed), float(parsed.get("ocr_confidence", 0) or 0)


def _quote_is_complete(parsed: dict[str, Any]) -> bool:
    """All four quote fields with a sane book: no point retrying for confidence."""
    if _quote_fields_found(parsed) < len(QUOTE_FIELDS):
        return False
    try:
        return float(parsed["bid1"]) <= float(parsed["ask1"])
    except (KeyError, TypeError, ValueError):
        return False


def _render_variant(image: Any, variant_name: str) -> Any:
    from PIL import ImageOps, ImageEnhance, ImageFilter  # type: ignore
    if variant_name == "gray":
        variant = ImageOps.grayscale(image)
    elif variant_name == "contrast":
        variant = ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance(3.0)
    elif variant_name == "upscaled":
        gray = ImageOps.grayscale(image)
        variant = ImageEnhance.Contrast(gray.resize((gray.width * 2, gray.height * 2))).enhance(3.0)
    elif variant_name == "red_channel":
        variant = image.getchannel("R")
    elif variant_name == "green_channel":
        variant = image.getchannel("G")
    else:
        raise ValueError(f"Unknown OCR variant: {variant_name}")
    return variant.filter(ImageFilter.SHARPEN)


def _ocr_attempt(image: Any, lang: str, variant_name: str, psm: int) -> dict[str, Any]:
    """Run one Tesseract call and return parsed quote fields plus evidence."""
    import pytesseract  # type: ignore
    variant = _render_variant(image, variant_name)
    data = pytesseract.image_to_data(variant, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    words, confidences = [], []
    for word, conf in zip(data["text"], data["conf"]):
        word = word.strip()
        try:
            score = float(conf)
        except (TypeError, ValueError):
            score = -1
        if word:
            words.append(word)
            if score >= 0:
                confidences.append(score / 100)
    text = " ".join(words)
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    parsed = parse_wh6_ocr(text, confidence)
    parsed["ocr_text"] = text
    parsed["ocr_variant"] = variant_name
    parsed["ocr_psm"] = psm
    parsed["ocr_fields_found"] = _quote_fields_found(parsed)
    return parsed


def _quote_panel_box(width: int, height: int) -> tuple[int, int, int, int]:
    """Right-hand WH6 quote panel: labels, bid/ask, last, daily statistics."""
    return (int(width * 0.80), 0, width, max(1, int(height * 0.55)))


def _ocr_tiers(
    effort: str,
    region: tuple[int, int, int, int] | None,
    width: int = 2262,
    height: int = 1162,
) -> list[tuple[str, tuple[int, int, int, int] | None, str, tuple[int, ...]]]:
    """Ordered OCR passes: cheapest useful pass first, escalation last."""
    panel = region or _quote_panel_box(width, height)
    tiers: list[tuple[str, tuple[int, int, int, int] | None, str, tuple[int, ...]]] = [
        ("panel", panel, "gray", (11,)),
    ]
    if effort == "fast":
        return tiers
    tiers += [
        ("panel", panel, "gray", (6,)),
        ("panel", panel, "contrast", (11,)),
    ]
    if region is None:
        tiers.append(("full", None, "gray", (11, 6)))
    tiers += [
        ("panel", panel, "red_channel", (11,)),
        ("panel", panel, "green_channel", (11,)),
    ]
    if effort == "thorough":
        tiers += [("panel", panel, "upscaled", (6, 11)), ("full", None, "contrast", (6, 11))]
    return tiers


def ocr_quote_image(
    image_path: Path,
    lang: str = "chi_sim+eng",
    region: tuple[int, int, int, int] | None = None,
    effort: str = "auto",
) -> dict[str, Any]:
    """Tiered WH6 OCR: one cheap pass first, escalating only on failure.

    ``effort``:
      fast     - a single quote-panel grayscale pass (~1 Tesseract call)
      auto     - add psm/contrast/full-window passes only while fields are missing
      thorough - legacy behaviour: every variant and page mode
    """
    if effort not in ("fast", "auto", "thorough"):
        raise ValueError("effort must be fast, auto, or thorough")
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install Pillow and pytesseract, plus Tesseract OCR with chi_sim language data") from exc
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    panel = region or _quote_panel_box(width, height)
    tiers = _ocr_tiers(effort, region, width, height)

    best: dict[str, Any] | None = None
    best_score = (-1, -1.0)
    calls = 0
    started = time.perf_counter()
    for crop_name, box, variant_name, psms in tiers:
        working = image if box is None else image.crop(box)
        for psm in psms:
            calls += 1
            parsed = _ocr_attempt(working, lang, variant_name, psm)
            parsed["ocr_tier"] = crop_name
            parsed["ocr_calls"] = calls
            score = _quote_score(parsed)
            if score > best_score:
                best = parsed
                best_score = score
            if _quote_is_complete(parsed):
                break
        if best is not None and _quote_is_complete(best):
            break

    assert best is not None
    best["ocr_effort"] = effort
    best["ocr_calls"] = calls
    best["ocr_seconds"] = round(time.perf_counter() - started, 2)
    best["image_path"] = str(image_path)
    best["image_sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
    best["captured_at"] = datetime.now(timezone.utc).isoformat()
    best["ocr_region"] = list(region or panel)
    return best


def ocr_image_best(
    image_path: Path,
    lang: str,
    region: tuple[int, int, int, int] | None = None,
    effort: str = "auto",
) -> dict[str, Any]:
    """Backwards-compatible wrapper around :func:`ocr_quote_image`."""
    return ocr_quote_image(image_path, lang, region, effort)


def build_plan_draft(
    recognized: dict[str, Any],
    *,
    direction: str | None = None,
    size: int = 1,
    price_mode: str = "opponent",
    stop_loss: float | None = None,
    take_profit: float | None = None,
    smc_tag: str | None = None,
    htf_bias: str | None = None,
    is_add_on: bool = False,
    entry_price: float | None = None,
    offset_ticks: int | None = None,
) -> dict[str, Any]:
    """Turn recognized market data into a reviewable trade-plan draft."""
    market = {
        key: recognized.get(key)
        for key in ("bid1", "ask1", "last")
        if recognized.get(key) is not None
    }
    if recognized.get("ocr_confidence") is not None:
        market["ocr_confidence"] = recognized["ocr_confidence"]
    # WH6 renders PVC contracts as `PVC2701`; the risk engine matches on the
    # variety code, so canonicalize to V2701 and keep the label for traceability.
    variety = recognized.get("variety")
    label = recognized.get("contract")
    digits = re.sub(r"\D", "", str(label or ""))
    contract = f"{variety}{digits}" if variety and digits else label
    plan: dict[str, Any] = {
        "variety": variety,
        "contract": contract,
        "contract_label": label,
        "direction": direction,
        "size": int(size),
        "price_mode": price_mode,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "smc_tag": smc_tag,
        "htf_bias": htf_bias,
        "is_add_on": bool(is_add_on),
        "market": market,
        "market_provenance": {
            "source": recognized.get("source"),
            "captured_at": recognized.get("captured_at"),
            "ocr_confidence": recognized.get("ocr_confidence"),
            "ocr_variant": recognized.get("ocr_variant"),
            "ocr_psm": recognized.get("ocr_psm"),
            "image_sha256": recognized.get("image_sha256"),
            "window_title": recognized.get("window_title"),
            "validation": recognized.get("validation"),
        },
    }
    if offset_ticks is not None:
        plan["offset_ticks"] = int(offset_ticks)
    return plan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=f"wh6_screen {TOOL_VERSION}")
    p.add_argument("--image", type=Path, help="OCR a saved WH6 screenshot")
    p.add_argument("--list-windows", action="store_true", help="list visible windows for the process, then exit")
    p.add_argument("--window-title", help="only capture windows whose title contains this text (case-insensitive)")
    p.add_argument("--capture", action="store_true", help="capture visible WH6 process windows (Windows only)")
    p.add_argument("--capture-mode", choices=("app", "window", "monitor"), default="app", help="capture WH6 application windows, one window, or full monitor")
    p.add_argument("--process", default=PROCESS_NAME)
    p.add_argument("--interval", type=float, default=1.0, help="seconds between captures in watch mode")
    p.add_argument("--watch", action="store_true", help="repeat capture/OCR until Ctrl+C")
    p.add_argument("--region", help="crop x,y,width,height (relative to screenshot/window)")
    p.add_argument("--out", type=Path, help="append result JSON records here")
    p.add_argument("--save-capture", type=Path, help="save the captured WH6 frame for OCR debugging")
    p.add_argument("--tesseract-cmd", help="path to tesseract.exe if not on PATH")
    p.add_argument("--lang", default="chi_sim+eng")
    p.add_argument("--ocr-effort", choices=("fast", "auto", "thorough"), default="auto", help="OCR search depth: fast=1 call, auto=escalating, thorough=all variants")
    p.add_argument("--config", type=Path)
    p.add_argument("--plan-out", type=Path, help="write a trade-plan draft JSON built from the recognized market")
    p.add_argument("--direction", choices=("long", "short"))
    p.add_argument("--size", type=int, default=1)
    p.add_argument("--price-mode", choices=("manual", "opponent", "queue", "last", "offset"), default="opponent")
    p.add_argument("--entry-price", type=float)
    p.add_argument("--stop-loss", type=float)
    p.add_argument("--take-profit", type=float)
    p.add_argument("--smc-tag")
    p.add_argument("--htf-bias", choices=("long", "short", "ranging", "unclear"))
    p.add_argument("--is-add-on", action="store_true")
    p.add_argument("--plan-check", action="store_true", help="run the risk check on the generated plan and save a snapshot")
    p.add_argument("--equity", type=float, help="account equity for percent-based risk budgets (default: data/account.json)")
    p.add_argument("--snapshots", type=Path, help="snapshot JSONL path used with --plan-check")
    args = p.parse_args()
    if args.list_windows:
        records = enumerate_windows_process(args.process)
        if args.window_title:
            records = [record for record in records if args.window_title.casefold() in record["title"].casefold()]
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0 if records else 2
    config = load_config(args.config) if args.config else load_config()
    region = None
    if args.region:
        x, y, w, h = (int(v) for v in args.region.split(","))
        region = (x, y, x + w, y + h)
    if args.tesseract_cmd:
        try:
            import pytesseract  # type: ignore
            pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd
        except ImportError as exc:
            raise SystemExit("pytesseract is not installed") from exc
    results: list[dict[str, Any]] = []
    try:
        while True:
            if args.image:
                result = ocr_image_best(args.image, args.lang, region, args.ocr_effort)
            elif args.capture:
                captures = capture_windows_process(args.process, args.interval if args.watch else 0, 1, args.capture_mode)
                result = None
                fallback = None
                best_score = (-1, -1.0)
                frames = captures[0]["frames"]
                if args.window_title:
                    needle = args.window_title.casefold()
                    frames = [(title, image) for title, image in frames if needle in title.casefold()]
                if not frames:
                    raise RuntimeError(f"No captured WH6 window title contains {args.window_title!r}")
                for frame_index, (title, image) in enumerate(frames):
                    # Save a temporary frame so OCR provenance hashes actual image bytes.
                    temp = Path.cwd() / ".wh6_capture_tmp.png"
                    image.save(temp)
                    try:
                        parsed = ocr_image_best(temp, args.lang, region, args.ocr_effort)
                    finally:
                        temp.unlink(missing_ok=True)
                    parsed["window_title"] = title
                    fallback = parsed
                    score = (int(parsed.get("ocr_fields_found", 0)), float(parsed.get("ocr_confidence", 0)))
                    if score > best_score:
                        result = parsed
                        best_score = score
                        if args.save_capture:
                            args.save_capture.parent.mkdir(parents=True, exist_ok=True)
                            image.save(args.save_capture)
                            result["image_path"] = str(args.save_capture)
                            result["image_sha256"] = hashlib.sha256(args.save_capture.read_bytes()).hexdigest()
                # Preserve OCR text and image metadata when parsing fails. This
                # makes region/contrast/parser issues diagnosable from one run.
                if result is None:
                    result = fallback or {
                        "source": "wh6_capture",
                        "validation": {"valid": False, "errors": ["No capturable WH6 frame"], "warnings": []},
                    }
                    result.setdefault("source", "wh6_capture")
            else:
                p.error("Specify --image or --capture")
            result = validate_recognized_market(result, config)
            if result.get("ocr_confidence", 1) < float(config.get("ocr_min_confidence", .85)):
                result["validation"]["valid"] = False
                result["validation"]["errors"].append("OCR confidence is below threshold; manual confirmation required")
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(result, ensure_ascii=False) + "\n")
            results.append(result)
            check_result = None
            if args.plan_out:
                plan = build_plan_draft(
                    result,
                    direction=args.direction,
                    size=args.size,
                    price_mode=args.price_mode,
                    entry_price=args.entry_price,
                    stop_loss=args.stop_loss,
                    take_profit=args.take_profit,
                    smc_tag=args.smc_tag,
                    htf_bias=args.htf_bias,
                    is_add_on=args.is_add_on,
                )
                args.plan_out.parent.mkdir(parents=True, exist_ok=True)
                args.plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"plan draft written: {args.plan_out}", flush=True)
                if args.plan_check:
                    equity = args.equity if args.equity is not None else load_account().get("equity_rmb")
                    check_result = check_plan(plan, config, equity)
                    if args.snapshots:
                        check_result["snapshot_id"] = save_snapshot(plan, check_result, args.snapshots)
                    else:
                        check_result["snapshot_id"] = save_snapshot(plan, check_result)
                    print(json.dumps(check_result, ensure_ascii=False, indent=2), flush=True)
                    results.append({"plan_check": check_result})
            if not args.watch:
                break
            import time
            time.sleep(max(.25, args.interval))
    except KeyboardInterrupt:
        pass
    ocr_results = [item for item in results if "validation" in item]
    plan_results = [item["plan_check"] for item in results if "plan_check" in item]
    if plan_results:
        return 0 if plan_results[-1].get("status") in ("PASS", "WARN") else 2
    return 0 if ocr_results and ocr_results[-1]["validation"].get("valid") else 2

if __name__ == "__main__":
    raise SystemExit(main())
