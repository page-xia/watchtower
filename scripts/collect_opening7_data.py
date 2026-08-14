from __future__ import annotations

"""Collect minute bars + L1 transaction tape for the pool universe.

Data source: easy_tdx (TDX L1). Output: one pickle per (code, date) under
data/runtime/opening7/raw/ plus an index pickle with index minute bars.

- minute bars: get_history_minute_time_data(market, code, yyyymmdd)
- ticks:       get_history_transaction_data(market, code, yyyymmdd, start, 800)
               pages go from the END of the day backward; page until short.

Resume-safe: existing non-empty pickles are skipped.
"""

import argparse
import pickle
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "runtime" / "opening7" / "raw"

# Pool = themes.yaml members + watchlist codes.
POOL: dict[str, str] = {
    "300209": "SZ", "300308": "SZ", "300476": "SZ", "300502": "SZ",
    "300394": "SZ", "300620": "SZ", "002428": "SZ", "002463": "SZ",
    "002916": "SZ",
    "600206": "SH", "600183": "SH", "603228": "SH", "603986": "SH",
    "688981": "SH", "688041": "SH",
}

INDICES: dict[str, str] = {"999999": "SH", "399001": "SZ", "399006": "SZ"}


def trade_dates(start: str, end: str) -> list[str]:
    """Trading dates from local daily caches; extend with recent dates."""
    dates: set[str] = set()
    for path in (ROOT / "data" / "runtime").glob("daily_*.json"):
        dates.add(path.stem.split("_")[1])
    for path in (ROOT / "data" / "runtime").glob("easy_tdx_daily_*.json"):
        dates.add(path.stem.rsplit("_", 1)[1])
    # recent days not in daily cache but verified to have minute data
    dates.update({"20260810"})
    return sorted(d for d in dates if start <= d <= end)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20260515")
    parser.add_argument("--end", default="20260811")
    parser.add_argument("--ticks-after", default="20260601",
                        help="only fetch tick pages for dates >= this")
    args = parser.parse_args()

    from easy_tdx import TdxClient
    from easy_tdx.models.enums import Market

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dates = trade_dates(args.start, args.end)
    print(f"trade dates: {len(dates)}  {dates[0]}..{dates[-1]}", flush=True)

    client = TdxClient.from_best_host(timeout=5.0, ping_timeout=3.0)
    mk = {"SZ": Market.SZ, "SH": Market.SH}

    def reconnect() -> None:
        nonlocal client
        try:
            client.disconnect()
        except Exception:
            pass
        time.sleep(1.0)
        client = TdxClient.from_best_host(timeout=5.0, ping_timeout=3.0)

    stats = {"minute_ok": 0, "minute_empty": 0, "tick_ok": 0, "tick_empty": 0, "err": 0}
    t_begin = time.time()
    jobs: list[tuple[str, str]] = []
    for code in list(POOL) + list(INDICES):
        for d in dates:
            jobs.append((code, d))

    done = 0
    for code, d in jobs:
        done += 1
        is_index = code in INDICES
        path = OUT_DIR / f"{code}_{d}.pkl"
        if path.exists() and path.stat().st_size > 100:
            continue
        market = mk[POOL.get(code) or INDICES[code]]
        rec: dict = {"code": code, "date": d, "is_index": is_index}
        try:
            mdf = client.get_history_minute_time_data(market, code, int(d))
            rec["minute"] = mdf if mdf is not None else None
            if mdf is None or len(mdf) == 0:
                stats["minute_empty"] += 1
            else:
                stats["minute_ok"] += 1
        except Exception as exc:  # noqa: BLE001
            rec["minute"] = None
            rec["minute_err"] = f"{type(exc).__name__}: {exc}"
            stats["err"] += 1
            reconnect()

        if not is_index and d >= args.ticks_after:
            try:
                frames = []
                start = 0
                for _ in range(10):
                    tdf = client.get_history_transaction_data(market, code, int(d), start, 800)
                    if tdf is None or len(tdf) == 0:
                        break
                    frames.append(tdf)
                    start += 800
                    if len(tdf) < 800:
                        break
                if frames:
                    import pandas as pd
                    ticks = pd.concat(frames).sort_values("datetime").reset_index(drop=True)
                    rec["ticks"] = ticks
                    stats["tick_ok"] += 1
                else:
                    rec["ticks"] = None
                    stats["tick_empty"] += 1
            except Exception as exc:  # noqa: BLE001
                rec["ticks"] = None
                rec["tick_err"] = f"{type(exc).__name__}: {exc}"
                stats["err"] += 1
                reconnect()
        with path.open("wb") as fh:
            pickle.dump(rec, fh)
        if done % 200 == 0:
            el = time.time() - t_begin
            print(f"{done}/{len(jobs)}  elapsed {el:.0f}s  {stats}", flush=True)

    try:
        client.disconnect()
    except Exception:
        pass
    print(f"DONE {done}/{len(jobs)} in {time.time()-t_begin:.0f}s  {stats}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
