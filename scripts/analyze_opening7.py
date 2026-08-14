from __future__ import annotations

"""Opening-7-minute event study on the pool universe.

Answers:
1. Which features available at decision minute k predict forward returns?
2. How does signal quality evolve k=1..7 (earliest reliable decision)?
3. Transparent buy-T / sell-T rules and their per-k quality.
"""

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "data" / "runtime" / "opening7"

FEATURES = [
    "gap_pct", "chg", "from_open", "vwap_dev", "tick_vwap_dev", "slope1",
    "slope3", "hi_pos", "rebound", "pullback", "relvol", "tick_net_ratio",
    "tick_large_net", "last_min_net", "price_eff", "auction_vol_ratio",
    "999999_chg", "999999_slope3", "999999_rebound", "999999_relvol",
    "399006_chg", "399006_slope3", "pool_breadth", "pool_avg_chg",
    "core_avg_chg", "core_max_chg", "rel_pool", "prev_pct", "ret5", "pos20",
]
LABELS = ["ret5m", "ret15m", "ret30m", "ret60m", "ret_close", "mfe30", "mae30"]


def spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 30:
        return np.nan
    return float(a[m].rank().corr(b[m].rank()))


def bucket_stats(df: pd.DataFrame, feat: str, bins: list[float], label: str = "ret30m") -> pd.DataFrame:
    m = df[feat].notna() & df[label].notna()
    d = df[m].copy()
    d["bucket"] = pd.cut(d[feat], bins)
    g = d.groupby("bucket", observed=True)
    out = g.agg(n=(label, "size"), mean_ret=(label, "mean"),
                p_pos=(label, lambda s: (s > 0).mean() * 100),
                p_buy_tf=("buy_tf", lambda s: (s == 1).mean() * 100))
    return out.round(2)


def main() -> None:
    with (OUT / "features.pkl").open("rb") as fh:
        df: pd.DataFrame = pickle.load(fh)
    print(f"rows={len(df)} stock-days={df.groupby(['code','date']).ngroups} "
          f"dates={df['date'].nunique()} codes={df['code'].nunique()}")
    base = df[df["k"] == 7]
    print(f"\nbaseline @09:37  P(ret30m>0)={100*(base['ret30m']>0).mean():.1f}%  "
          f"mean={base['ret30m'].mean():.2f}%  P(buy_tf=1)={100*(base['buy_tf']==1).mean():.1f}%  "
          f"P(sell_tf=1)={100*(base['sell_tf']==1).mean():.1f}%")

    # ---- 1) univariate predictive power by decision minute ----
    print("\n== Spearman IC vs ret30m, by decision minute k ==")
    hdr = "feature".ljust(17) + "".join(f"k={k}".rjust(8) for k in range(1, 8))
    print(hdr)
    ic_table: dict[str, list[float]] = {}
    for f in FEATURES:
        ics = [spearman(df[df["k"] == k][f], df[df["k"] == k]["ret30m"]) for k in range(1, 8)]
        ic_table[f] = ics
        print(f.ljust(17) + "".join((f"{v:8.2f}" if np.isfinite(v) else "     --") for v in ics))

    print("\n== Spearman IC vs ret_close (k=3,5,7) ==")
    for f in FEATURES:
        ics = [spearman(df[df["k"] == k][f], df[df["k"] == k]["ret_close"]) for k in (3, 5, 7)]
        print(f.ljust(17) + "".join((f"{v:8.2f}" if np.isfinite(v) else "     --") for v in ics))

    # ---- 2) key buckets at k=3 (09:33) and k=7 (09:37) ----
    for k in (3, 7):
        d = df[df["k"] == k]
        print(f"\n== buckets @09:3{k} ==")
        print("-- tick_net_ratio (net buy % of opening ticks)")
        print(bucket_stats(d, "tick_net_ratio", [-100, -25, -10, 0, 10, 25, 100]).to_string())
        print("-- relvol (vs prior-5d same window)")
        print(bucket_stats(d, "relvol", [0, 0.6, 0.9, 1.2, 1.6, 2.5, 99]).to_string())
        print("-- above tick_vwap x tick_net sign")
        d2 = d.copy()
        d2["combo"] = np.where(d2["tick_vwap_dev"] >= 0, np.where(d2["tick_net_ratio"] >= 0, "up+buy", "up+sell"),
                               np.where(d2["tick_net_ratio"] >= 0, "dn+buy", "dn+sell"))
        g = d2.groupby("combo")
        print(g.agg(n=("ret30m", "size"), mean30=("ret30m", "mean"),
                    p_buy_tf=("buy_tf", lambda s: (s == 1).mean() * 100),
                    mean_close=("ret_close", "mean")).round(2).to_string())
        print("-- 999999_chg (index level at decision)")
        print(bucket_stats(d, "999999_chg", [-99, -0.8, -0.3, 0, 0.3, 0.8, 99]).to_string())
        print("-- gap_pct")
        print(bucket_stats(d, "gap_pct", [-99, -3, -1, 0, 1, 3, 6, 99]).to_string())

    # ---- 3) rules per decision minute ----
    print("\n== rule evaluation by decision minute ==")
    print("BUY-T: idx_chg>-0.4 & idx_slope3>=-0.15 & above tick_vwap & relvol>=1.2 "
          "& tick_net>=5 & slope1>=-0.1 & gap<5 & rel_pool>=-1.5")
    print("SELL: tick_net<=-12 & below tick_vwap & slope1<0 & (idx_chg<0 or sell_burst)")
    print(f"{'k':>2} {'nBUY':>5} {'pT+F':>6} {'m30':>6} {'mClose':>7} {'pT+S':>6} "
          f"{'nSELL':>6} {'pT-S':>6} {'m30s':>6} {'mCloses':>7}")
    results = []
    for k in range(1, 8):
        d = df[df["k"] == k].copy()
        idx_ok = (d["999999_chg"] > -0.4) & (d["999999_slope3"] >= -0.15)
        buy = (idx_ok & (d["tick_vwap_dev"] >= 0) & (d["relvol"] >= 1.2)
               & (d["tick_net_ratio"] >= 5) & (d["slope1"] >= -0.1)
               & (d["gap_pct"] < 5) & (d["rel_pool"] >= -1.5))
        sell = ((d["tick_net_ratio"] <= -12) & (d["tick_vwap_dev"] < 0)
                & (d["slope1"] < 0) & ((d["999999_chg"] < 0) | d["sell_burst"].fillna(False)))
        b = d[buy]
        s = d[sell]
        row = {
            "k": k, "n_buy": len(b),
            "buy_p_tf": float((b["buy_tf"] == 1).mean() * 100) if len(b) else np.nan,
            "buy_m30": float(b["ret30m"].mean()) if len(b) else np.nan,
            "buy_mclose": float(b["ret_close"].mean()) if len(b) else np.nan,
            "sell_n": len(s),
            "sell_p_tf": float((s["sell_tf"] == 1).mean() * 100) if len(s) else np.nan,
            "sell_m30": float(s["ret30m"].mean()) if len(s) else np.nan,
            "sell_mclose": float(s["ret_close"].mean()) if len(s) else np.nan,
        }
        results.append(row)
        print(f"{k:>2} {len(b):>5} {row['buy_p_tf']:>6.1f} {row['buy_m30']:>6.2f} "
              f"{row['buy_mclose']:>7.2f} {100*(b['sell_tf']==1).mean() if len(b) else float('nan'):>6.1f} "
              f"{len(s):>6} {row['sell_p_tf']:>6.1f} {row['sell_m30']:>6.2f} {row['sell_mclose']:>7.2f}")

    # ---- 4) decision stability: rule at k vs rule at 7 ----
    print("\n== decision consistency: rule@k vs rule@7 (same stock-day) ==")
    d7 = df[df["k"] == 7].set_index(["code", "date"])
    idx7 = (d7["999999_chg"] > -0.4) & (d7["999999_slope3"] >= -0.15)
    buy7 = (idx7 & (d7["tick_vwap_dev"] >= 0) & (d7["relvol"] >= 1.2)
            & (d7["tick_net_ratio"] >= 5) & (d7["slope1"] >= -0.1)
            & (d7["gap_pct"] < 5) & (d7["rel_pool"] >= -1.5))
    for k in (2, 3, 4, 5):
        dk = df[df["k"] == k].set_index(["code", "date"])
        idxk = (dk["999999_chg"] > -0.4) & (dk["999999_slope3"] >= -0.15)
        buyk = (idxk & (dk["tick_vwap_dev"] >= 0) & (dk["relvol"] >= 1.2)
                & (dk["tick_net_ratio"] >= 5) & (dk["slope1"] >= -0.1)
                & (dk["gap_pct"] < 5) & (dk["rel_pool"] >= -1.5))
        both = buyk.index.intersection(buy7.index)
        agree = (buyk[both] == buy7[both]).mean() * 100
        print(f"k={k}: agreement with 09:37 decision = {agree:.1f}%  (n={len(both)})")

    with (OUT / "rule_eval.json").open("w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print("\nsaved rule_eval.json")


if __name__ == "__main__":
    main()
