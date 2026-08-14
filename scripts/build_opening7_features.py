from __future__ import annotations

"""Build opening-window (first 7 minutes) features + forward labels.

Rows: (code, trade_date, k) with k = 1..7 meaning the decision is made at
clock 09:30+k using minute bars 0..k-1 (bar i covers clock 09:30+i) and all
L1 transaction prints stamped <= 09:30+k-1 (tick timestamps are minute
resolution; the 09:25 row is the opening auction print).

Labels use the full-day minute series of the same day (outcome window only,
never fed into features).
"""

import json
import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "runtime" / "opening7" / "raw"
OUT = ROOT / "data" / "runtime" / "opening7"

POOL = ["300209", "300308", "300476", "300502", "300394", "300620",
        "002428", "002463", "002916", "600206", "600183", "603228",
        "603986", "688981", "688041"]
CORE = {"300308", "300476", "002428", "002463", "600183", "300502"}
INDICES = ["999999", "399001", "399006"]

LARGE_TICK_AMOUNT = 500_000.0  # yuan; tick vol unit = 手 (100 shares)


def load(code: str, date: str) -> dict | None:
    path = RAW / f"{code}_{date}.pkl"
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return pickle.load(fh)


def minute_df(rec: dict) -> pd.DataFrame | None:
    df = rec.get("minute")
    if df is None or len(df) < 60:
        return None
    df = df.copy()
    df["hm"] = df["datetime"].dt.strftime("%H:%M")
    return df.reset_index(drop=True)


def daily_cache() -> dict[str, dict[str, dict]]:
    """date -> symbol -> daily row, from local daily snapshots."""
    out: dict[str, dict[str, dict]] = {}
    for path in sorted((ROOT / "data" / "runtime").glob("daily_*.json")):
        date = path.stem.split("_")[1]
        rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        out[date] = {str(r.get("symbol")): r for r in rows}
    path = ROOT / "data" / "runtime" / "easy_tdx_daily_20260811.json"
    if path.exists():
        rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        out["20260811"] = {str(r.get("symbol")): r for r in rows}
    return out


def main() -> None:
    dates = sorted(p.stem.split("_")[1] for p in RAW.glob("999999_*.pkl"))
    dates = [d for d in dates if "20260515" <= d <= "20260811"]
    print(f"dates: {len(dates)}")
    daily = daily_cache()

    # Preload minute bars for everything.
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    ticks: dict[tuple[str, str], pd.DataFrame] = {}
    for code in POOL + INDICES:
        for d in dates:
            rec = load(code, d)
            if not rec:
                continue
            mdf = minute_df(rec)
            if mdf is not None:
                bars[(code, d)] = mdf
            tdf = rec.get("ticks")
            if tdf is not None and len(tdf):
                tdf = tdf.copy()
                tdf["hm"] = tdf["datetime"].dt.strftime("%H:%M")
                ticks[(code, d)] = tdf

    # prev close from prior date last minute price (15:00 close ~ 14:59 bar);
    # cross-check with daily cache when available.
    prev_close: dict[tuple[str, str], float] = {}
    for code in POOL + INDICES:
        for i, d in enumerate(dates):
            if i == 0:
                continue
            pdte = dates[i - 1]
            pb = bars.get((code, pdte))
            if pb is None:
                continue
            prev_close[(code, d)] = float(pb["price"].iloc[-1])

    # Prior-5-day same-window cumulative volume for relative volume.
    def rel_cumvol(code: str, d: str, k: int) -> float:
        """cum vol of bars 0..k-1 divided by prior-5-day mean."""
        i = dates.index(d)
        prior = []
        for j in range(max(0, i - 5), i):
            pb = bars.get((code, dates[j]))
            if pb is not None:
                prior.append(float(pb["vol"].iloc[:k].sum()))
        if not prior:
            return np.nan
        cur = bars[(code, d)]
        base = float(np.mean(prior))
        return float(cur["vol"].iloc[:k].sum()) / base if base > 0 else np.nan

    # Index feature frame per date/k.
    idx_feat: dict[tuple[str, int], dict[str, float]] = {}
    for d in dates:
        for k in range(1, 8):
            row: dict[str, float] = {}
            for idx in INDICES:
                b = bars.get((idx, d))
                pc = prev_close.get((idx, d))
                if b is None or pc is None:
                    continue
                p = b["price"].to_numpy(dtype=float)
                v = b["vol"].to_numpy(dtype=float)
                pk = p[k - 1]
                row[f"{idx}_chg"] = (pk / pc - 1) * 100
                row[f"{idx}_slope3"] = (pk / p[max(0, k - 3)] - 1) * 100 if k >= 2 else 0.0
                row[f"{idx}_rebound"] = (pk / p[:k].min() - 1) * 100
                row[f"{idx}_from_open"] = (pk / p[0] - 1) * 100
                rv = rel_cumvol(idx, d, k)
                row[f"{idx}_relvol"] = rv
            idx_feat[(d, k)] = row

    rows: list[dict] = []
    for d in dates:
        # pool context needs all members first
        member_chg: dict[str, float] = {}
        for code in POOL:
            b = bars.get((code, d))
            pc = prev_close.get((code, d))
            if b is None or pc is None:
                continue
        for code in POOL:
            b = bars.get((code, d))
            if b is None:
                continue
            pc = prev_close.get((code, d))
            if pc is None:
                # fall back to daily cache pre_close
                dc = daily.get(d, {}).get(code)
                pc = float(dc["pre_close"]) if dc and dc.get("pre_close") else None
            if pc is None:
                continue
            p = b["price"].to_numpy(dtype=float)
            v = b["vol"].to_numpy(dtype=float)
            n = len(p)
            tdf = ticks.get((code, d))
            # opening auction
            auction_price = np.nan
            auction_vol = np.nan
            if tdf is not None:
                auc = tdf[tdf["hm"] == "09:25"]
                if len(auc):
                    auction_price = float(auc["price"].iloc[-1])
                    auction_vol = float(auc["vol"].sum())
            open_price = auction_price if math.isfinite(auction_price) else p[0]

            # prior-day features from daily cache
            dc_prev = None
            i = dates.index(d)
            for j in range(i - 1, max(-1, i - 25), -1):
                dj = dates[j]
                if dj in daily and code in daily[dj]:
                    dc_prev = (dj, daily[dj][code])
                    break
            prev_pct = ret5 = pos20 = np.nan
            if dc_prev:
                dj, r0 = dc_prev
                prev_pct = float(r0.get("pct_chg") or np.nan)
                closes = []
                for j2 in range(max(0, dates.index(dj) - 19), dates.index(dj) + 1):
                    r2 = daily.get(dates[j2], {}).get(code)
                    if r2 and r2.get("close"):
                        closes.append(float(r2["close"]))
                if len(closes) >= 6:
                    ret5 = (closes[-1] / closes[-6] - 1) * 100
                if len(closes) >= 20:
                    lo, hi = min(closes[-20:]), max(closes[-20:])
                    pos20 = (closes[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0

            for k in range(1, 8):
                pk = p[k - 1]
                hi_sofar = p[:k].max()
                lo_sofar = p[:k].min()
                vol_sofar = v[:k].sum()
                vwap_min = float((p[:k] * v[:k]).sum() / vol_sofar) if vol_sofar > 0 else pk
                feat: dict = {
                    "code": code, "date": d, "k": k, "core": code in CORE,
                    "prev_close": pc, "open": open_price,
                    "price": pk,
                    "gap_pct": (open_price / pc - 1) * 100,
                    "chg": (pk / pc - 1) * 100,
                    "from_open": (pk / open_price - 1) * 100,
                    "vwap_dev": (pk / vwap_min - 1) * 100,
                    "above_vwap": pk >= vwap_min,
                    "slope1": (pk / p[k - 2] - 1) * 100 if k >= 2 else 0.0,
                    "slope3": (pk / p[max(0, k - 3)] - 1) * 100 if k >= 2 else 0.0,
                    "hi_pos": (pk - lo_sofar) / (hi_sofar - lo_sofar) if hi_sofar > lo_sofar else 0.5,
                    "rebound": (pk / lo_sofar - 1) * 100,
                    "pullback": (hi_sofar / pk - 1) * 100,
                    "relvol": rel_cumvol(code, d, k),
                    "last_bar_vol_share": float(v[k - 1] / vol_sofar) if vol_sofar > 0 else np.nan,
                    "prev_pct": prev_pct, "ret5": ret5, "pos20": pos20,
                    "has_ticks": tdf is not None,
                }
                feat.update(idx_feat.get((d, k), {}))
                # pool context (exclude self)
                chgs = []
                for other in POOL:
                    if other == code:
                        continue
                    ob = bars.get((other, d))
                    opc = prev_close.get((other, d))
                    if ob is None or opc is None:
                        continue
                    chgs.append((other, (float(ob["price"].iloc[k - 1]) / opc - 1) * 100))
                if chgs:
                    vals = [c for _, c in chgs]
                    feat["pool_breadth"] = sum(1 for c in vals if c > 0) / len(vals) * 100
                    feat["pool_avg_chg"] = float(np.mean(vals))
                    core_vals = [c for o, c in chgs if o in CORE]
                    feat["core_avg_chg"] = float(np.mean(core_vals)) if core_vals else np.nan
                    feat["core_max_chg"] = float(np.max(core_vals)) if core_vals else np.nan
                    feat["rank_in_pool"] = 1 + sum(1 for c in vals if c > feat["chg"])
                    feat["rel_pool"] = feat["chg"] - feat["pool_avg_chg"]
                # tick microstructure
                if tdf is not None:
                    upto = f"09:{29 + k:02d}"  # ticks stamped <= 09:30+k-1
                    tw = tdf[(tdf["hm"] >= "09:30") & (tdf["hm"] <= upto)]
                    if len(tw):
                        bvol = float(tw.loc[tw["buyorsell"] == 0, "vol"].sum())
                        svol = float(tw.loc[tw["buyorsell"] == 1, "vol"].sum())
                        amt = (tw["price"] * tw["vol"] * 100.0)
                        large = tw[amt >= LARGE_TICK_AMOUNT]
                        lb = float(large.loc[large["buyorsell"] == 0, "vol"].sum()) if len(large) else 0.0
                        ls = float(large.loc[large["buyorsell"] == 1, "vol"].sum()) if len(large) else 0.0
                        feat["tick_n"] = len(tw)
                        feat["tick_buy_vol"] = bvol
                        feat["tick_sell_vol"] = svol
                        feat["tick_net_ratio"] = (bvol - svol) / (bvol + svol) * 100 if bvol + svol > 0 else 0.0
                        feat["tick_large_net"] = (lb - ls) / (lb + ls) * 100 if lb + ls > 0 else 0.0
                        feat["tick_vwap"] = float((tw["price"] * tw["vol"]).sum() / tw["vol"].sum())
                        feat["tick_vwap_dev"] = (pk / feat["tick_vwap"] - 1) * 100
                        feat["tick_amt_yi"] = float(amt.sum() / 1e8)
                        feat["price_eff"] = feat["from_open"] / feat["tick_amt_yi"] if feat["tick_amt_yi"] > 0 else np.nan
                        # last-minute direction vs price response
                        lm = tw[tw["hm"] == upto]
                        if len(lm):
                            lbv = float(lm.loc[lm["buyorsell"] == 0, "vol"].sum())
                            lsv = float(lm.loc[lm["buyorsell"] == 1, "vol"].sum())
                            feat["last_min_net"] = (lbv - lsv) / (lbv + lsv) * 100 if lbv + lsv > 0 else 0.0
                            feat["absorb_flag"] = bool(lsv > lbv and feat["slope1"] >= 0) if k >= 2 else False
                            feat["sell_burst"] = bool(lsv >= 2 * max(lbv, 1) and feat["slope1"] < 0) if k >= 2 else False
                        feat["auction_vol_ratio"] = auction_vol / vol_sofar if vol_sofar > 0 else np.nan
                # labels (outcome window only)
                def fp(m: int) -> float:
                    j = min(n - 1, k - 1 + m)
                    return float(p[j])
                feat["ret5m"] = (fp(5) / pk - 1) * 100
                feat["ret15m"] = (fp(15) / pk - 1) * 100
                feat["ret30m"] = (fp(30) / pk - 1) * 100
                feat["ret60m"] = (fp(60) / pk - 1) * 100
                feat["ret_close"] = (float(p[-1]) / pk - 1) * 100
                win = p[k - 1:min(n, k - 1 + 31)]
                feat["mfe30"] = (win.max() / pk - 1) * 100
                feat["mae30"] = (win.min() / pk - 1) * 100
                # target-first: +0.8% before -0.5% (buy side) / symmetric sell side
                def first_hit(up: float, dn: float) -> int:
                    for q in win[1:]:
                        r = (q / pk - 1) * 100
                        if r >= up:
                            return 1
                        if r <= dn:
                            return -1
                    return 0
                feat["buy_tf"] = first_hit(0.8, -0.5)
                feat["sell_tf"] = first_hit(-0.8, 0.5) * -1  # 1 = down target first
                rows.append(feat)

    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "features.pkl").open("wb") as fh:
        pickle.dump(df, fh)
    print(f"rows: {len(df)}  stock-days: {df.groupby(['code','date']).ngroups}  "
          f"with ticks: {df[df['k']==7]['has_ticks'].mean()*100:.0f}%")
    print(df[df["k"] == 7][["chg", "relvol", "tick_net_ratio", "ret30m", "ret_close"]].describe().round(2).to_string())


if __name__ == "__main__":
    main()
