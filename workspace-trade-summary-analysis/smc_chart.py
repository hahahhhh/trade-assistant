#!/usr/bin/env python3
"""Pure helpers for the SMC chart viewer (no tkinter, no matplotlib).

Kept separate from trade_form.py so the mapping/caching logic stays testable
without a display, an akshare install, or a trade-signal checkout.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PERIODS = (("M5", "5"), ("M15", "15"), ("H1", "60"), ("D1", "1440"))
INDICATORS = ("liquidity", "swing", "fvg", "ob", "breaker")
INDICATOR_LABELS = {
    "liquidity": "流动性",
    "swing": "波段",
    "fvg": "FVG",
    "ob": "OB",
    "breaker": "Breaker",
}
DEFAULT_INDICATORS = ("liquidity", "swing", "fvg")
DAILY_PERIODS = ("1440", "1d", "d1")
CACHE_TTL_SECONDS = 600
RUNNER_TIMEOUT_SECONDS = 120


def is_daily_period(period: str | int) -> bool:
    """The renderer only knows minute periods; D1 must use the daily API."""
    return str(period).strip().lower() in DAILY_PERIODS


def symbol_for(recognized: dict[str, Any]) -> str:
    """WH6 label -> exchange symbol, e.g. ('V','V2701') -> 'v2701'."""
    variety = str(recognized.get("variety") or "").strip().upper()
    label = str(recognized.get("contract") or recognized.get("contract_label") or "")
    digits = re.sub(r"\D", "", label)
    if not variety:
        raise ValueError("还没有识别到品种，请先读取行情或手工填价")
    if len(digits) < 3:
        raise ValueError(f"合约代码不完整：{label or '(空)'}")
    return f"{variety.lower()}{digits}"


def normalize_indicators(indicators: list[str] | tuple[str, ...]) -> list[str]:
    chosen = {str(item).strip().lower() for item in indicators if str(item).strip()}
    unknown = chosen - set(INDICATORS)
    if unknown:
        raise ValueError(f"未知指标：{', '.join(sorted(unknown))}")
    if not chosen:
        raise ValueError("至少选择一个指标")
    return [name for name in INDICATORS if name in chosen]


def chart_filename(symbol: str, period: str, indicators: list[str] | tuple[str, ...]) -> str:
    names = normalize_indicators(indicators)
    return f"{symbol}_{period}m_{'+'.join(names)}.png"


def cache_status(path: Path, ttl_seconds: int = CACHE_TTL_SECONDS,
                 now: datetime | None = None) -> dict[str, Any]:
    """Describe a cached chart: presence, age, and whether it is stale."""
    if not path.exists():
        return {"exists": False, "age_seconds": None, "stale": True, "label": "未生成"}
    current = now or datetime.now()
    age = max(0.0, (current - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds())
    stale = age > ttl_seconds
    if age < 60:
        label = f"生成于 {age:.0f} 秒前"
    elif age < 3600:
        label = f"生成于 {age / 60:.0f} 分钟前"
    else:
        label = f"生成于 {age / 3600:.1f} 小时前"
    return {"exists": True, "age_seconds": age, "stale": stale,
            "label": label + ("（已过期）" if stale else "")}


def build_runner_command(python: str, runner: Path, root: Path, symbol: str,
                         period: str, indicators: list[str] | tuple[str, ...],
                         candles: int, detect_length: int, outdir: Path) -> list[str]:
    names = normalize_indicators(indicators)
    return [
        python, str(runner),
        "--root", str(root),
        "--symbol", symbol,
        "--period", str(period),
        "--indicators", *names,
        "--candles", str(int(candles)),
        "--detect", str(int(detect_length)),
        "--outdir", str(outdir),
    ]


def parse_runner_output(stdout: str) -> dict[str, Any]:
    """The runner prints one JSON object; ignore any noise before it."""
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "ok" in payload:
            return payload
    raise ValueError("生成器没有返回可解析的结果")


def default_script_path() -> str:
    """Best guess: the trade-signal checkout used by this machine."""
    for candidate in (
        Path("/home/coding/trade-signal/examples/smc_comprehensive_chart.py"),
        Path.home() / "trade-signal/examples/smc_comprehensive_chart.py",
    ):
        if candidate.exists():
            return str(candidate)
    return ""


def script_root(script: str) -> Path:
    """trade-signal root derived from examples/<script>.py."""
    return Path(script).resolve().parent.parent
