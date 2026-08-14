from __future__ import annotations

"""Phase 1: compact caches for the opening7 x sector backtest.

Builds two pickles so the replay phase runs in seconds:
- board_matrix.pkl: per date, board -> [p_bar0, p_bar2, prev_close]
- stockday_pack.pkl: per (code,date) -> minute rows, flow points, prev close, open
"""

import json
import pickle
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "runtime" / "opening7" / "raw"
BOARDS = ROOT / "data" / "runtime" / "opening7" / "boards"
OUT = ROOT / "data" / "runtime" / "opening7"

POOL = ["300209", "300308", "300476", "300502", "300394", "300620",
        "002428", "002463", "002916", "600206", "600183", "603228",
        "603986", "688981", "688041"]


def load_pickle(path: Path):
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return pickle.load(fh)


def main() -> None:
    d3 = json.loads((ROOT / "data" / "runtime" / "easy_tdx_board_level_3.json").read_text(encoding="utf-8"))
    board_codes = sorted(d3["code_to_name"].keys())
    dates = sorted(p.stem.split("_")[1] for p in RAW.glob("999999_*.pkl"))
    dates = [d for d in dates if "20260515" <= d <= "20260811"]
    t0 = time.time()

    # ---- board matrix ----
    board_matrix: dict[str, dict[str, list[float]]] = {}
    for di, d in enumerate(dates):
        prev_d = dates[di - 1] if di else None
        entry: dict[str, list[float]] = {}
        for bc in board_codes:
            df = load_pickle(BOARDS / f"{bc}_{d}.pkl")
            if df is None or len(df) < 3:
                continue
            prev = load_pickle(BOARDS / f"{bc}_{prev_d}.pkl") if prev_d else None
            if prev is None or len(prev) == 0:
                continue
            pc = float(prev["price"].iloc[-1])
            if pc <= 0:
                continue
            p = df["price"].to_numpy(dtype=float)
            entry[bc] = [float(p[0]), float(p[2]), pc]
        board_matrix[d] = entry
        if di % 10 == 0:
            print(f"boards {di}/{len(dates)} {time.time()-t0:.0f}s", flush=True)
    with (OUT / "board_matrix.pkl").open("wb") as fh:
        pickle.dump(board_matrix, fh)
    print(f"board matrix done {time.time()-t0:.0f}s", flush=True)

    # ---- stock-day pack ----
    pack: dict[tuple[str, str], dict] = {}
    for di, d in enumerate(dates):
        prev_d = dates[di - 1] if di else None
        for code in POOL:
            rec = load_pickle(RAW / f"{code}_{d}.pkl")
            if not rec or rec.get("minute") is None or not len(rec["minute"]):
                continue
            prev_rec = load_pickle(RAW / f"{code}_{prev_d}.pkl") if prev_d else None
            if not prev_rec or prev_rec.get("minute") is None or not len(prev_rec["minute"]):
                continue
            mdf = rec["minute"]
            prev_close = float(prev_rec["minute"]["price"].iloc[-1])
            tdf = rec.get("ticks")
            open_price = 0.0
            flow_points: list[dict] = []
            if tdf is not None and len(tdf):
                hm = tdf["datetime"].dt.strftime("%H:%M")
                auc_mask = hm == "09:25"
                if auc_mask.any():
                    open_price = float(tdf.loc[auc_mask, "price"].iloc[-1])
                amt = tdf["price"].to_numpy(dtype=float) * tdf["vol"].to_numpy(dtype=float) * 100.0
                side = tdf["buyorsell"].to_numpy()
                reg = ~auc_mask
                import pandas as pd
                gdf = pd.DataFrame({"hm": hm[reg], "amt": amt[reg.values], "side": side[reg.values]})
                for h, g in gdf.groupby("hm"):
                    flow_points.append({
                        "time": h,
                        "buy_amount": float(g.loc[g["side"] == 0, "amt"].sum()),
                        "sell_amount": float(g.loc[g["side"] == 1, "amt"].sum()),
                    })
            rows = [{"time": t.strftime("%H:%M"), "price": float(p), "vol": float(v)}
                    for t, p, v in zip(mdf["datetime"], mdf["price"], mdf["vol"])]
            pack[(code, d)] = {
                "rows": rows, "prev_close": prev_close,
                "open": open_price, "flow_points": flow_points,
            }
        if di % 10 == 0:
            print(f"stocks {di}/{len(dates)} {time.time()-t0:.0f}s", flush=True)
    with (OUT / "stockday_pack.pkl").open("wb") as fh:
        pickle.dump(pack, fh)
    print(f"stock pack done ({len(pack)}) {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
