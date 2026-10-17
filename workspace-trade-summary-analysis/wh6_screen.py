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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from trade_assistant import load_config
except ModuleNotFoundError:  # direct file import from the test runner
    import importlib.util
    _spec = importlib.util.spec_from_file_location("trade_assistant", Path(__file__).with_name("trade_assistant.py"))
    _module = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(_module)
    load_config = _module.load_config

PROCESS_NAME = "mytrader_spqh.exe"
APP_NAME = "wh6"


def _normalize_contract(raw: str) -> tuple[str, str]:
    token = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    token = token.removesuffix("M")
    aliases = {"PVC": "V"}
    for prefix in sorted(("PVC", "LH", "JD", "RU", "FG", "MA", "JM", "V", "C"), key=len, reverse=True):
        if token.startswith(prefix) and token[len(prefix):].isdigit():
            return aliases.get(prefix, prefix), token
    raise ValueError(f"Unrecognized futures contract: {raw!r}")


def parse_wh6_ocr(text: str, confidence: float | None = None) -> dict[str, Any]:
    """Parse WH6 right quote panel and return normalized, untrusted OCR data."""
    # Normalize common OCR confusions from WH6's compact contract code.
    text = re.sub(r"(?i)P\s*V\s*C", "PVC", text)
    text = re.sub(r"(?i)\bV\s*2\s*7\s*0\s*1\b", "V2701", text)
    result: dict[str, Any] = {}
    # OCR frequently inserts spaces inside the contract (e.g. `PVC 2701 M`).
    # Match prefixes and digits separately, then normalize the complete token.
    contracts = re.findall(
        r"(?<![A-Za-z])(?:PVC|V|JD|LH|RU|FG|MA|JM|C)\s*\d(?:[\s,.-]*\d){3}\s*M?\b",
        text,
        re.I,
    )
    if contracts:
        # The first contract is typically chart tab/current contract.
        variety, contract = _normalize_contract(contracts[0])
        result["variety"] = variety
        result["contract"] = contract
    # WH6 quote panel labels: 卖出 = ask1, 买入 = bid1, 最新 = last.
    for label, field in (("卖出", "ask1"), ("卖一", "ask1"), ("买入", "bid1"), ("买一", "bid1"), ("最新", "last")):
        match = re.search(label + r"\s*[:：]?\s*([0-9]{2,7}(?:\.[0-9]+)?)", text)
        if match and field not in result:
            result[field] = float(match.group(1))
    # Quote panel secondary values are optional; capture only when label-based.
    for label, field in (("涨跌", "change_text"), ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("昨收", "prev_close"), ("持仓", "open_interest"), ("总量", "volume")):
        match = re.search(label + r"\s*[:：]?\s*([+-]?[0-9,]+(?:\.[0-9]+)?%?)", text)
        if match:
            result[field] = match.group(1).replace(",", "")
    result["source"] = "wh6_ocr"
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


def capture_windows_process(process_name: str = PROCESS_NAME, interval: float = 0.0, count: int = 1, capture_mode: str = "window") -> list[dict[str, Any]]:
    """Capture WH6 window or the full monitor containing it on Windows."""
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
    target = process_name.casefold()
    matching: list[tuple[int, str]] = []
    def enum(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name().casefold()
        except (psutil.Error, OSError):
            return
        if name == target:
            matching.append((hwnd, win32gui.GetWindowText(hwnd)))
    win32gui.EnumWindows(enum, None)
    if not matching:
        raise RuntimeError(f"No visible window found for process {process_name}")
    captures = []
    if capture_mode not in ("window", "monitor"):
        raise ValueError("capture_mode must be window or monitor")
    for idx in range(max(1, count)):
        frames = []
        for hwnd, title in matching:
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if capture_mode == "monitor":
                try:
                    import win32api  # type: ignore
                    monitor = win32api.MonitorFromPoint((rect[0], rect[1]), 2)
                    monitor_info = win32api.GetMonitorInfo(monitor)
                    bbox = monitor_info["Monitor"]
                except ImportError:
                    bbox = (0, 0, 1920, 1080)
                except Exception:
                    bbox = (0, 0, 1920, 1080)
            else:
                bbox = rect
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            frames.append((title, image))
        if not frames:
            raise RuntimeError("Matching WH6 process has no capturable visible window")
        captures.append({"frames": frames, "captured_at": datetime.now(timezone.utc).isoformat(), "capture_mode": capture_mode})
        if idx + 1 < count:
            time.sleep(max(.25, interval))
    return captures


def ocr_image(image_path: Path, lang: str = "chi_sim+eng", region: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    try:
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter  # type: ignore
        import pytesseract  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install Pillow and pytesseract, plus Tesseract OCR with chi_sim language data") from exc
    image = Image.open(image_path).convert("RGB")
    if region:
        image = image.crop(region)
    # WH6 uses colored text on black. Run OCR over several high-contrast
    # variants instead of assuming grayscale preserves the useful signal.
    gray = ImageOps.grayscale(image)
    variants = [
        ("gray", gray),
        ("contrast", ImageEnhance.Contrast(gray).enhance(3.0)),
        ("upscaled", ImageEnhance.Contrast(gray.resize((gray.width * 2, gray.height * 2))).enhance(3.0)),
        ("red_channel", image.getchannel("R")),
        ("green_channel", image.getchannel("G")),
    ]
    attempts = []
    for variant_name, variant in variants:
        variant = variant.filter(ImageFilter.SHARPEN)
        for psm in (6, 11, 12):
            data = pytesseract.image_to_data(variant, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
            words, confidences = [], []
            for word, conf in zip(data["text"], data["conf"]):
                word = word.strip()
                try: score = float(conf)
                except (TypeError, ValueError): score = -1
                if word:
                    words.append(word)
                    if score >= 0: confidences.append(score / 100)
            text = " ".join(words)
            confidence = sum(confidences) / len(confidences) if confidences else 0.0
            parsed = parse_wh6_ocr(text, confidence)
            field_count = sum(parsed.get(key) is not None for key in ("variety", "bid1", "ask1", "last"))
            # Prefer actual fields over generic OCR confidence: random window
            # text must not beat a weak but useful quote-panel read.
            attempts.append((field_count, confidence, variant_name, psm, text, parsed))
    field_count, confidence, variant_name, psm, text, result = max(attempts, key=lambda item: (item[0], item[1]))
    result["ocr_text"] = text
    result["ocr_variant"] = variant_name
    result["ocr_psm"] = psm
    result["ocr_fields_found"] = field_count
    result["image_path"] = str(image_path)
    result["image_sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
    result["captured_at"] = datetime.now(timezone.utc).isoformat()
    if region:
        result["ocr_region"] = list(region)
    return result


def ocr_image_best(image_path: Path, lang: str, region: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    """Try full image and WH6 right quote-panel crop; preserve best OCR evidence."""
    if region is not None:
        return ocr_image(image_path, lang, region)
    results = [ocr_image(image_path, lang)]
    try:
        from PIL import Image  # type: ignore
        with Image.open(image_path) as image:
            width, height = image.size
        # WH6 quote panel occupies the far-right top portion in the supplied layout.
        x0 = int(width * 0.82)
        y1 = int(height * 0.44)
        crop = (x0, 0, width, y1)
        if x0 > 0 and y1 > 0:
            results.append(ocr_image(image_path, lang, crop))
    except Exception:
        pass
    def score(item: dict[str, Any]) -> tuple[int, float]:
        fields = sum(item.get(key) is not None for key in ("variety", "bid1", "ask1", "last"))
        return fields, float(item.get("ocr_confidence", 0))
    return max(results, key=score)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", type=Path, help="OCR a saved WH6 screenshot")
    p.add_argument("--capture", action="store_true", help="capture visible WH6 process windows (Windows only)")
    p.add_argument("--capture-mode", choices=("window", "monitor"), default="window", help="capture WH6 window or full monitor containing it")
    p.add_argument("--process", default=PROCESS_NAME)
    p.add_argument("--interval", type=float, default=1.0, help="seconds between captures in watch mode")
    p.add_argument("--watch", action="store_true", help="repeat capture/OCR until Ctrl+C")
    p.add_argument("--region", help="crop x,y,width,height (relative to screenshot/window)")
    p.add_argument("--out", type=Path, help="append result JSON records here")
    p.add_argument("--save-capture", type=Path, help="save the captured WH6 frame for OCR debugging")
    p.add_argument("--tesseract-cmd", help="path to tesseract.exe if not on PATH")
    p.add_argument("--lang", default="chi_sim+eng")
    p.add_argument("--config", type=Path)
    args = p.parse_args()
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
                result = ocr_image_best(args.image, args.lang, region)
            elif args.capture:
                captures = capture_windows_process(args.process, args.interval if args.watch else 0, 1, args.capture_mode)
                result = None
                fallback = None
                for title, image in captures[0]["frames"]:
                    # Save a temporary frame so OCR provenance hashes actual image bytes.
                    temp = Path.cwd() / ".wh6_capture_tmp.png"
                    image.save(temp)
                    if args.save_capture:
                        args.save_capture.parent.mkdir(parents=True, exist_ok=True)
                        image.save(args.save_capture)
                    try:
                        parsed = ocr_image_best(temp, args.lang, region)
                    finally:
                        temp.unlink(missing_ok=True)
                    if args.save_capture:
                        parsed["image_path"] = str(args.save_capture)
                        parsed["image_sha256"] = hashlib.sha256(args.save_capture.read_bytes()).hexdigest()
                    parsed["window_title"] = title
                    fallback = parsed
                    if parsed.get("variety"):
                        result = parsed
                        break
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
            if not args.watch:
                break
            import time
            time.sleep(max(.25, args.interval))
    except KeyboardInterrupt:
        pass
    return 0 if results and results[-1].get("validation", {}).get("valid") else 2

if __name__ == "__main__":
    raise SystemExit(main())
