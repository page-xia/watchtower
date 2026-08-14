from __future__ import annotations

"""Phase 2: replay opening7 markers on cached packs; sector top-5 backtest."""

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "runtime" / "opening7"

from app.opening7 import opening_decision_markers  # noqa: E402

POOL = ["300209", "300308", "300476", "300502", "300394", "300620",
        "002428", "002463", "002916", "600206", "600183", "603228",
        "603986", "688981", "688041"]


def main() -> None:
    d3 = json.loads((ROOT / "data" / "runtime" / "easy_tdx_board_level_3.json").read_text(encoding="utf-8"))
    c2n = d3["code_to_name"]
    stock_board: dict[str, str] = {}
    for bc, members in d3["members_by_code"].items():
        for s in POOL:
            if s in members and s not in stock_board:
                stock_board[s] = bc
    print("stock->board:", {s: c2n.get(stock_board[s]) for s in POOL})

    board_matrix = pickle.load(open(OUT / "board_matrix.pkl", "rb"))
    pack = pickle.load(open(OUT / "stockday_pack.pkl", "rb"))
    dates = sorted(board_matrix.keys())

    # board strength per date/minute: chg at bar0 (09:31 decision) / bar2 (09:33)
    board_chg: dict[tuple[str, int], pd.Series] = {}
    for d, entry in board_matrix.items():
        board_chg[(d, 1)] = pd.Series({bc: (v[0] / v[2] - 1) * 100 for bc, v in entry.items()})
        board_chg[(d, 3)] = pd.Series({bc: (v[1] / v[2] - 1) * 100 for bc, v in entry.items()})
    board_rank = {key: s.rank(ascending=False, method="min") for key, s in board_chg.items()}

    events: list[dict] = []
    for di, d in enumerate(dates):
        idx_pack = pack.get(("300308", d))  # only used for existence; index rows separate
        idx_rec_path = ROOT / "data" / "runtime" / "opening7" / "raw" / f"999999_{d}.pkl"
        with idx_rec_path.open("rb") as fh:
            idx_rec = pickle.load(fh)
        idx_df = idx_rec.get("minute")
        if idx_df is None or not len(idx_df):
            continue
        if di:
            with (ROOT / "data" / "runtime" / "opening7" / "raw" / f"999999_{dates[di-1]}.pkl").open("rb") as fh:
                idx_prev_df = pickle.load(fh).get("minute")
            idx_prev_close = float(idx_prev_df["price"].iloc[-1]) if idx_prev_df is not None and len(idx_prev_df) else 0.0
        else:
            idx_prev_close = 0.0
        idx_rows = [{"time": t.strftime("%H:%M"), "price": float(p), "vol": float(v)}
                    for t, p, v in zip(idx_df["datetime"], idx_df["price"], idx_df["vol"])]
        for code in POOL:
            sd = pack.get((code, d))
            if not sd:
                continue
            markers = opening_decision_markers(
                minute_rows=sd["rows"],
                index_rows=idx_rows,
                index_prev_close=idx_prev_close,
                prev_close=sd["prev_close"],
                open_price=sd["open"],
                position=True,  # sell-gate backtest assumes the pool is held
                flow_points=sd["flow_points"],
            )
            for m in markers:
                k = 1 if m.time == "09:31" else 3
                bc = stock_board.get(code, "")
                rank = float(board_rank[(d, k)].get(bc, np.nan)) if (d, k) in board_rank else np.nan
                bchg = float(board_chg[(d, k)].get(bc, np.nan)) if (d, k) in board_chg else np.nan
                events.append({
                    "date": d, "code": code, "time": m.time, "side": m.side,
                    "rule": m.rule, "regime": m.regime, "k": k,
                    "board": c2n.get(bc, bc), "board_chg": bchg, "board_rank": rank,
                    "board_top5": bool(rank <= 5) if rank == rank else False,
                    "board_top20": bool(rank <= 20) if rank == rank else False,
                    "price": m.price, "prev_close": sd["prev_close"],
                })

    ev = pd.DataFrame(events)
    print(f"marker events: {len(ev)}  buy={len(ev[ev.side=='buy'])}  sell={len(ev[ev.side=='sell'])}")

    feat = pickle.load(open(OUT / "features.pkl", "rb"))
    lab = feat[feat["k"] == 3].set_index(["code", "date"])[
        ["ret30m", "ret60m", "ret_close", "mfe30", "mae30", "buy_tf", "sell_tf"]
    ]
    ev = ev.join(lab, on=["code", "date"])

    def report(g: pd.DataFrame, name: str) -> dict:
        n = len(g)
        if not n:
            return {}
        row = {
            "group": name, "n": n,
            "mean30": round(float(g["ret30m"].mean()), 2),
            "p30_pos": round(float((g["ret30m"] > 0).mean() * 100), 1),
            "mean_close": round(float(g["ret_close"].mean()), 2),
            "p_close_pos": round(float((g["ret_close"] > 0).mean() * 100), 1),
            "mean_mfe30": round(float(g["mfe30"].mean()), 2),
            "mean_mae30": round(float(g["mae30"].mean()), 2),
            "p_buy_tf": round(float((g["buy_tf"] == 1).mean() * 100), 1),
            "p_sell_tf": round(float((g["sell_tf"] == 1).mean() * 100), 1),
        }
        print(f"{name:32s} n={n:3d}  mean30={row['mean30']:6.2f}  P30+={row['p30_pos']:5.1f}%  "
              f"meanC={row['mean_close']:6.2f}  PC+={row['p_close_pos']:5.1f}%  "
              f"mfe={row['mean_mfe30']:5.2f}  mae={row['mean_mae30']:6.2f}  "
              f"buyTF={row['p_buy_tf']:5.1f}%  sellTF={row['p_sell_tf']:5.1f}%")
        return row

    out_rows = []
    buy = ev[ev["side"] == "buy"]
    sell = ev[ev["side"] == "sell"]
    print("\n== buy markers (09:33) ==")
    out_rows.append(report(buy, "buy all"))
    out_rows.append(report(buy[buy["board_top5"]], "buy & board top5"))
    out_rows.append(report(buy[~buy["board_top5"]], "buy & board NOT top5"))
    out_rows.append(report(buy[buy["board_top20"]], "buy & board top20"))
    out_rows.append(report(buy[~buy["board_top20"]], "buy & board NOT top20"))
    print("\n== sell markers (09:31, assumes held) ==")
    out_rows.append(report(sell, "sell all"))
    out_rows.append(report(sell[sell["board_top5"]], "sell & board top5"))
    out_rows.append(report(sell[~sell["board_top5"]], "sell & board NOT top5"))
    out_rows.append(report(sell[sell["board_top20"]], "sell & board top20"))
    out_rows.append(report(sell[~sell["board_top20"]], "sell & board NOT top20"))

    cols = ["date", "code", "board", "board_chg", "board_rank", "regime", "ret30m", "ret_close", "buy_tf"]
    top = buy[buy["board_top5"]].sort_values("date")
    print("\n== buy & top5 events ==")
    print(top[cols].round(2).to_string(index=False) if len(top) else "(none)")
    tsell = sell[sell["board_top5"]].sort_values("date")
    print("\n== sell & top5 events ==")
    print(tsell[cols].round(2).to_string(index=False) if len(tsell) else "(none)")

    print("\n== board rank distribution ==")
    print("buy :", buy["board_rank"].describe().round(1).to_dict())
    print("sell:", sell["board_rank"].describe().round(1).to_dict())

    if len(top):
        print("\n== buy&top5 per-date ret_close ==")
        print(top.groupby("date")["ret_close"].agg(["size", "mean"]).round(2).to_string())

    ev.to_csv(OUT / "marker_events_sector.csv", index=False, encoding="utf-8-sig")
    with (OUT / "sector_backtest.json").open("w", encoding="utf-8") as fh:
        json.dump([r for r in out_rows if r], fh, ensure_ascii=False, indent=2)
    print("\nsaved marker_events_sector.csv / sector_backtest.json")


if __name__ == "__main__":
    main()
