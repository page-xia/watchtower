from __future__ import annotations

"""Follow-up analysis: interactions, per-date stability, sell-side form."""

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "data" / "runtime" / "opening7"


def main() -> None:
    with (OUT / "features.pkl").open("rb") as fh:
        df: pd.DataFrame = pickle.load(fh)

    # ---- A) index-regime interaction at k=3 ----
    print("== A) stock features inside index regimes @09:33 (ret30m) ==")
    d = df[df["k"] == 3].copy()
    d["idx_regime"] = pd.cut(d["999999_chg"], [-99, -0.8, -0.3, 0.0, 0.3, 99],
                             labels=["idx<=-0.8", "-0.8~-0.3", "-0.3~0", "0~0.3", ">0.3"])
    for reg, g in d.groupby("idx_regime", observed=True):
        print(f"\n-- {reg}  n={len(g)}  mean30={g['ret30m'].mean():.2f}  "
              f"P>0={100*(g['ret30m']>0).mean():.0f}%  p_buy_tf={100*(g['buy_tf']==1).mean():.0f}%")
        for feat, bins in [("tick_net_ratio", [-100, -10, 0, 10, 100]),
                           ("tick_vwap_dev", [-99, 0, 99]),
                           ("relvol", [0, 1.0, 1.5, 99]),
                           ("gap_pct", [-99, -1, 1, 3, 99])]:
            g2 = g[g[feat].notna()]
            if len(g2) < 20:
                continue
            b = g2.groupby(pd.cut(g2[feat], bins), observed=True)["ret30m"].agg(["size", "mean"])
            line = " | ".join(f"{iv}:{r['mean']:.2f}({int(r['size'])})" for iv, r in b.iterrows())
            print(f"    {feat:>16}: {line}")

    # ---- B) per-date stability of index-reversal effect ----
    print("\n== B) per-date: mean ret30m when idx<=-0.3 vs idx>0 @k=3 ==")
    rows = []
    for date, g in d.groupby("date"):
        weak = g[g["999999_chg"] <= -0.3]
        strong = g[g["999999_chg"] > 0]
        if len(weak) >= 3 and len(strong) >= 3:
            rows.append((date, weak["ret30m"].mean(), strong["ret30m"].mean()))
    sdf = pd.DataFrame(rows, columns=["date", "weak_idx", "strong_idx"])
    sdf["diff"] = sdf["weak_idx"] - sdf["strong_idx"]
    print(f"dates with both groups: {len(sdf)};  weak>strong on "
          f"{(sdf['diff'] > 0).sum()} dates ({100*(sdf['diff'] > 0).mean():.0f}%)")
    print(f"mean weak={sdf['weak_idx'].mean():.2f}  strong={sdf['strong_idx'].mean():.2f}")

    # ---- C) sell-side: fade gap-up exhaustion ----
    print("\n== C) SELL-first candidates @09:33: gap-up + tick selling ==")
    for gap_min in (1.0, 2.0, 3.0):
        sel = d[(d["gap_pct"] >= gap_min) & (d["tick_net_ratio"] <= 0) & (d["slope1"] < 0)]
        print(f"gap>={gap_min} & net<=0 & slope1<0: n={len(sel)}  "
              f"mean30={sel['ret30m'].mean():.2f}  P(sell_tf=1)={100*(sel['sell_tf']==1).mean():.0f}%  "
              f"mean_close={sel['ret_close'].mean():.2f}")
    sel2 = d[(d["gap_pct"] >= 2) & (d["tick_net_ratio"] <= -10)]
    print(f"gap>=2 & net<=-10: n={len(sel2)}  mean30={sel2['ret30m'].mean():.2f}  "
          f"P(sell_tf=1)={100*(sel2['sell_tf']==1).mean():.0f}%")
    sel3 = d[(d["chg"] >= 3) & (d["tick_vwap_dev"] < 0)]
    print(f"chg>=3 & below vwap: n={len(sel3)}  mean30={sel3['ret30m'].mean():.2f}  "
          f"P(sell_tf=1)={100*(sel3['sell_tf']==1).mean():.0f}%")
    sel4 = d[(d["rebound"] >= 4) & (d["pullback"] >= 1)]
    print(f"rebound>=4 & pullback>=1 (冲高回落): n={len(sel4)}  mean30={sel4['ret30m'].mean():.2f}  "
          f"P(sell_tf=1)={100*(sel4['sell_tf']==1).mean():.0f}%")

    # ---- D) speed value: price paid at 09:33 vs 09:37 ----
    print("\n== D) cost of waiting: price change from k=3 to k=7 ==")
    p3 = df[df["k"] == 3].set_index(["code", "date"])["price"]
    p7 = df[df["k"] == 7].set_index(["code", "date"])["price"]
    common = p3.index.intersection(p7.index)
    drift = (p7[common] / p3[common] - 1) * 100
    print(f"all: mean={drift.mean():.3f}%  median={drift.median():.3f}%  std={drift.std():.2f}")
    # for the eventual winners (ret30m@k7 > 1%), how much more expensive at 09:37?
    r30_7 = df[df["k"] == 7].set_index(["code", "date"])["ret30m"]
    win = r30_7[common] > 1.0
    print(f"eventual winners (ret30m@k7>1%): extra cost of waiting = {drift[win].mean():.3f}%  (n={win.sum()})")
    lose = r30_7[common] < -1.0
    print(f"eventual losers (ret30m@k7<-1%): entry saving of waiting = {drift[lose].mean():.3f}%  (n={lose.sum()})")

    # ---- E) best simple buy rule search @k=3 (transparent, small) ----
    print("\n== E) buy-rule grid @09:33 (n>=25, sort by p_buy_tf) ==")
    best = []
    for idx_lo in (-0.3, -0.8, -99):
        for net_lo in (-100, 0, 5, 10):
            for vwap_req in (False, True):
                m = (d["999999_chg"] > idx_lo) & (d["tick_net_ratio"] >= net_lo)
                if vwap_req:
                    m &= d["tick_vwap_dev"] >= 0
                g = d[m]
                if len(g) >= 25:
                    best.append((f"idx>{idx_lo} net>={net_lo} vwap={vwap_req}", len(g),
                                 g["ret30m"].mean(), 100 * (g["buy_tf"] == 1).mean(),
                                 100 * (g["ret30m"] > 0).mean()))
    for name, n, m30, ptf, ppos in sorted(best, key=lambda x: -x[3]):
        print(f"  {name:34s} n={n:3d}  mean30={m30:6.2f}  p_buy_tf={ptf:5.1f}%  P>0={ppos:5.1f}%")

    print("\n== E2) contrarian buy: idx<=-0.8 grid @09:33 ==")
    w = d[d["999999_chg"] <= -0.8]
    print(f"weak-idx universe: n={len(w)} mean30={w['ret30m'].mean():.2f}")
    for feat, bins in [("tick_net_ratio", [-100, -10, 0, 100]), ("gap_pct", [-99, -3, -1, 99]),
                       ("relvol", [0, 1.0, 99]), ("tick_vwap_dev", [-99, 0, 99])]:
        w2 = w[w[feat].notna()]
        b = w2.groupby(pd.cut(w2[feat], bins), observed=True)["ret30m"].agg(["size", "mean"])
        line = " | ".join(f"{iv}:{r['mean']:.2f}({int(r['size'])})" for iv, r in b.iterrows())
        print(f"    {feat:>16}: {line}")


if __name__ == "__main__":
    main()
