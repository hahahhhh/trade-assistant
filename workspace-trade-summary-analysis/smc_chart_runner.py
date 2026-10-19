#!/usr/bin/env python3
"""Generate one SMC annotated chart by calling the trade-signal renderer.

Run with an interpreter that has akshare / matplotlib / pandas installed:

    python smc_chart_runner.py --root D:\\trade-signal --symbol jd2612 \
        --period 5 --indicators liquidity swing fvg --outdir <cache>

Prints exactly one JSON line on stdout so the GUI can parse it safely.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smc_chart import chart_filename, is_daily_period, normalize_indicators  # noqa: E402


def _fail(message: str, tail: str = "") -> int:
    print(json.dumps({"ok": False, "error": message, "stdout_tail": tail[-2000:]},
                     ensure_ascii=False))
    return 2


def _load_renderer(root: Path):
    script = root / "examples" / "smc_comprehensive_chart.py"
    if not script.exists():
        raise FileNotFoundError(f"找不到渲染脚本：{script}")
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("smc_comprehensive_chart", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _patch_daily_period() -> None:
    """The renderer only knows minute periods; serve D1 from the daily API."""
    from data.akshare_client import AkShareClient  # type: ignore

    original = AkShareClient.get_futures_minute

    def get_futures_minute(self, symbol, period="15", limit=1000):  # type: ignore[no-untyped-def]
        if is_daily_period(period):
            return self.get_futures_daily(symbol)
        return original(self, symbol, period=period, limit=limit)

    AkShareClient.get_futures_minute = get_futures_minute  # type: ignore[assignment]


def main() -> int:
    parser = argparse.ArgumentParser(description="SMC 标注图生成器")
    parser.add_argument("--root", required=True, help="trade-signal 仓库根目录")
    parser.add_argument("--symbol", required=True, help="交易所代码，如 jd2612")
    parser.add_argument("--period", required=True, help="5 / 15 / 60 / 1440")
    parser.add_argument("--indicators", nargs="+", default=["liquidity", "swing", "fvg"])
    parser.add_argument("--candles", type=int, default=200)
    parser.add_argument("--detect", type=int, default=500)
    parser.add_argument("--outdir", required=True, help="PNG 输出目录（也是缓存目录）")
    args = parser.parse_args()

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "output").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLBACKEND", "Agg")
    # numba's cache=True needs a writable directory even when the checkout is read-only.
    os.environ.setdefault("NUMBA_CACHE_DIR", str(outdir / "numba"))

    try:
        indicators = normalize_indicators(args.indicators)
        module = _load_renderer(Path(args.root).resolve())
        _patch_daily_period()
    except Exception as exc:  # import-time failures are the common setup mistake
        return _fail(f"{type(exc).__name__}: {exc}", traceback.format_exc())

    # The renderer writes to ./output relative to the working directory.
    os.chdir(outdir)
    before = {item.name for item in (outdir / "output").glob("*.png")}
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            module.plot_smc_comprehensive(
                symbol=args.symbol,
                timeframe=str(args.period),
                num_candles=int(args.candles),
                detect_length=int(args.detect),
                indicators=indicators,
                output_name="+".join(indicators),
            )
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", buffer.getvalue() + traceback.format_exc())

    produced = [item for item in (outdir / "output").glob("*.png") if item.name not in before]
    if not produced:
        return _fail("渲染完成但没有找到输出图片", buffer.getvalue())
    newest = max(produced, key=lambda item: item.stat().st_mtime)

    target = outdir / chart_filename(args.symbol, str(args.period), indicators)
    if target.exists():
        target.unlink()
    newest.replace(target)

    print(json.dumps({
        "ok": True,
        "png": str(target),
        "symbol": args.symbol,
        "period": str(args.period),
        "indicators": indicators,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
