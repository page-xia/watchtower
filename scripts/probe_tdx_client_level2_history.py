from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if value != value else round(value, 6)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "tolist"):
        try:
            return value.tolist()
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            pass
    return str(value)


def _shape(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "NoneType", "empty": True}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": sorted(str(key) for key in value.keys()),
            "size": len(value),
            "sample": _small_sample(value),
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": type(value).__name__,
            "size": len(value),
            "sample": [_small_sample(item) for item in list(value)[:3]],
        }
    if hasattr(value, "dtype") and hasattr(value, "shape"):
        names = list(getattr(getattr(value, "dtype", None), "names", None) or [])
        return {
            "type": type(value).__name__,
            "shape": tuple(int(item) for item in value.shape),
            "dtype_names": names,
            "sample": [_small_sample(row) for row in value[:3]],
        }
    if hasattr(value, "columns") and hasattr(value, "shape"):
        return {
            "type": type(value).__name__,
            "shape": tuple(int(item) for item in value.shape),
            "columns": [str(item) for item in value.columns],
            "sample": value.head(3).to_dict(orient="records"),
        }
    return {"type": type(value).__name__, "repr": repr(value)[:500]}


def _small_sample(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in list(value.items())[:20]}
    if hasattr(value, "dtype") and getattr(getattr(value, "dtype", None), "names", None):
        return {name: _jsonable(value[name]) for name in value.dtype.names or []}
    return _jsonable(value)


def _market_suffix(code: str) -> str:
    raw = str(code).strip().upper()
    if "." in raw:
        return raw
    raw = raw.zfill(6)
    if raw.startswith(("6", "5", "9")):
        return f"{raw}.SH"
    if raw.startswith(("0", "2", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    return raw


def _safe_call(fn) -> dict[str, Any]:
    try:
        value = fn()
        return {"ok": True, "result": _shape(value)}
    except SystemExit as exc:
        return {"ok": False, "error": f"SystemExit: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _raw_get_tdx_data(tqcenter: Any, tq: Any, request: dict[str, Any], timeout_ms: int) -> Any:
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    ptr = tqcenter.dll.GetTdxDataStr(tq._get_run_id(), payload, timeout_ms)
    if ptr is None or len(ptr) == 0:
        return {"ErrorId": "empty", "Error": "GetTdxDataStr returned empty pointer"}
    text = ptr.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:2000]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe local TDX quant client historical tick/Level2 APIs.")
    parser.add_argument("--tdx-dir", default=r"G:\tdx")
    parser.add_argument("--codes", default="300476,300308,000001")
    parser.add_argument("--date", default="20260807", help="Historical trade date YYYYMMDD")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--startxh", type=int, default=0)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tdx_dir = Path(args.tdx_dir).resolve()
    pyplugins_dir = tdx_dir / "PYPlugins"
    user_dir = pyplugins_dir / "user"
    codes = [_market_suffix(code) for code in args.codes.split(",") if str(code).strip()]
    if not codes:
        print(json.dumps({"ok": False, "error": "codes is empty"}, ensure_ascii=False, indent=2))
        return 2

    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(pyplugins_dir))
        os.add_dll_directory(str(tdx_dir))
    os.environ["PATH"] = f"{pyplugins_dir};{tdx_dir};{os.environ.get('PATH', '')}"
    sys.path.insert(0, str(user_dir))

    payload: dict[str, Any] = {
        "ok": True,
        "observed_at": datetime.now().isoformat(timespec="seconds"),
        "tdx_dir": str(tdx_dir),
        "codes": codes,
        "date": args.date,
        "count": args.count,
        "probes": {},
    }

    tqcenter = importlib.import_module("tqcenter")
    tq = tqcenter.tq
    payload["tqcenter_file"] = str(Path(tqcenter.__file__).resolve())
    payload["tqcenter_version"] = getattr(tqcenter, "__version__", "")

    init_result = _safe_call(lambda: tq.initialize(str(Path(__file__).resolve())))
    payload["probes"]["initialize"] = init_result
    if not init_result["ok"]:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable))
        return 1

    try:
        payload["run_id"] = getattr(tq, "run_id", None)
        payload["run_mode"] = getattr(tq, "run_mode", None)

        payload["probes"]["market_snapshot"] = _safe_call(lambda: tq.get_market_snapshot(codes[0], field_list=[]))

        payload["probes"]["get_market_data_tick_public"] = _safe_call(
            lambda: tq.get_market_data(
                stock_list=[codes[0]],
                period="tick",
                start_time=args.date,
                end_time=args.date,
                count=max(1, args.count),
                dividend_type="none",
                fill_data=False,
            )
        )

        start_time_fmt = datetime.strptime(args.date, "%Y%m%d").strftime("%Y-%m-%d 00:00:00")
        end_time_fmt = datetime.strptime(args.date, "%Y%m%d").strftime("%Y-%m-%d 23:59:59")
        payload["probes"]["fetch_market_data_tick_private"] = _safe_call(
            lambda: tq._fetch_market_data_batch(
                [codes[0]],
                "tick",
                start_time_fmt,
                end_time_fmt,
                "none",
                max(1, args.count),
                timeout_ms=args.timeout_ms,
            )
        )

        raw_tick_request = {
            "id": tq._get_run_id(),
            "type": 1,
            "stock_list": [codes[0]],
            "start_time": start_time_fmt,
            "end_time": end_time_fmt,
            "period": "tick",
            "dividend_type": "none",
            "count": max(1, args.count),
            "stock_page_index": 0,
            "stock_page_size": 100,
        }
        payload["probes"]["raw_GetTdxDataStr_period_tick"] = _safe_call(
            lambda: _raw_get_tdx_data(tqcenter, tq, raw_tick_request, args.timeout_ms)
        )

        raw_docs_like_request = {
            "id": tq._get_run_id(),
            "type": 27,
            "stock_code": codes[0],
            "date": args.date,
            "startxh": max(0, args.startxh),
            "wantnum": max(1, min(args.count, 2000)),
        }
        payload["probes"]["raw_GetTdxDataStr_docs_like_type_27"] = _safe_call(
            lambda: _raw_get_tdx_data(tqcenter, tq, raw_docs_like_request, args.timeout_ms)
        )

        try:
            pylambda_core = importlib.import_module("pylambda.core")
            get_tick_data = getattr(pylambda_core, "get_tick_data")
            payload["probes"]["pylambda_core_get_tick_data"] = _safe_call(
                lambda: get_tick_data(
                    codes[0],
                    args.date,
                    startxh=max(0, args.startxh),
                    wantnum=max(1, min(args.count, 2000)),
                    field_list=None,
                )
            )
        except Exception as exc:
            payload["probes"]["pylambda_core_get_tick_data"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    finally:
        payload["probes"]["close"] = _safe_call(lambda: tq.close())

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable))
    return 0


if __name__ == "__main__":
    sys.exit(main())
