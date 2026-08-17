"""Probe tushare pro F10-relevant endpoints with the configured token.

Usage: .\.venv\Scripts\python.exe scripts\probe_tushare_f10.py [--code 300476.SZ]
"""
from __future__ import annotations

import argparse
import sys
import traceback

import yaml

ENDPOINTS = [
    ("stock_basic", {"ts_code": "{code}", "fields": "ts_code,name,industry,market,list_date,fullname,enname,cnspell,exchange,curr_type,list_status,delist_date,is_hs,act_name,act_ent_type"}),
    ("stock_company", {"ts_code": "{code}"}),
    ("fina_indicator", {"ts_code": "{code}", "limit": 4}),
    ("income", {"ts_code": "{code}", "limit": 2}),
    ("balancesheet", {"ts_code": "{code}", "limit": 2}),
    ("cashflow", {"ts_code": "{code}", "limit": 2}),
    ("forecast", {"ts_code": "{code}", "limit": 4}),
    ("express", {"ts_code": "{code}", "limit": 4}),
    ("dividend", {"ts_code": "{code}", "limit": 6}),
    ("fina_mainbz", {"ts_code": "{code}", "limit": 12}),
    ("top10_holders", {"ts_code": "{code}", "limit": 10}),
    ("top10_floatholders", {"ts_code": "{code}", "limit": 10}),
    ("stk_holdernumber", {"ts_code": "{code}", "limit": 4}),
    ("share_float", {"ts_code": "{code}", "limit": 6}),
    ("fina_audit", {"ts_code": "{code}", "limit": 3}),
    ("stk_managers", {"ts_code": "{code}", "limit": 10}),
    ("stk_rewards", {"ts_code": "{code}", "limit": 10}),
    ("report_rc", {"ts_code": "{code}", "limit": 5}),
    ("pledge_stat", {"ts_code": "{code}", "limit": 4}),
    ("repurchase", {"ts_code": "{code}", "limit": 5}),
    ("concept_detail", {"code": "{code6}", "limit": 10}),
    ("stk_factor_pro", {"ts_code": "{code}", "limit": 2}),
    ("moneyflow_ind_dc", {"ts_code": "{code}", "limit": 2}),
    ("cyq_perf", {"ts_code": "{code}", "limit": 2}),
    ("ccass_hold", {"ts_code": "{code6}", "limit": 2}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="300476.SZ")
    parser.add_argument("--config", default="ts2db_config.yaml")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        token = (yaml.safe_load(fh) or {}).get("tushare_token", "")
    if not token:
        print("no tushare token in config")
        return 1

    import tushare as ts

    pro = ts.pro_api(token)
    code = args.code
    code6 = code.split(".")[0]
    ok, fail = 0, 0
    for name, kwargs in ENDPOINTS:
        kw = {k: (v.format(code=code, code6=code6) if isinstance(v, str) else v) for k, v in kwargs.items()}
        try:
            api = getattr(pro, name)
        except AttributeError:
            print(f"[MISSING] {name}: pro_api has no such method")
            fail += 1
            continue
        try:
            df = api(**kw)
            rows, cols = df.shape
            col_list = ",".join(list(df.columns)[:14])
            print(f"[OK] {name}: rows={rows} cols={cols} cols=[{col_list}...]")
            ok += 1
        except Exception as exc:
            msg = str(exc).replace("\n", " ")[:140]
            print(f"[FAIL] {name}: {msg}")
            fail += 1
    print(f"\nsummary: ok={ok} fail={fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
