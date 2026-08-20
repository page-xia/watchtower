"""一次性迁移脚本:CloudBase MySQL(cynosdb 直连) -> 阿里云 RDS watchtower_msg。

断线重试 + 主键书签续传,INSERT IGNORE 幂等,可重复运行。
迁移完成后可删除。
"""
from __future__ import annotations

import re
import sys
import time

import pymysql
import yaml

TABLES = [
    ("message_topics", ["topic_id"]),
    ("message_events", ["event_id"]),
    ("message_event_links", ["event_id", "entity_type", "code", "name"]),
    ("message_sync_runs", ["run_id"]),
    ("message_evidence_cache", ["scope", "cache_key"]),
]
BATCH = 2000
MAX_RETRY = 8


def load_cfgs():
    cfg = yaml.safe_load(open("ts2db_config.yaml", encoding="utf-8"))
    m = re.match(r"mysql://([^:]+):(.+)@([^:/]+):(\d+)/(.+)", cfg["db_prod"])
    src = dict(host=m.group(3), port=int(m.group(4)), user=m.group(1), password=m.group(2),
               database=m.group(5), charset="utf8mb4", connect_timeout=15, read_timeout=60)
    import pathlib
    env = dict(
        line.split("=", 1) for line in pathlib.Path(".deploy/rds.env").read_text().splitlines() if "=" in line
    )
    dst = dict(host=env["WATCH_RDS_HOST"], port=int(env["WATCH_RDS_PORT"]), user=env["WATCH_RDS_USER"],
               password=env["WATCH_RDS_PWD"], database=env["WATCH_RDS_MSG_DB"], charset="utf8mb4",
               connect_timeout=15, read_timeout=120, write_timeout=120, autocommit=True)
    return src, dst


def connect(cfg):
    return pymysql.connect(**cfg)


def get_columns(src, table, db_name):
    with src.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ORDINAL_POSITION",
            (db_name, table),
        )
        return [r[0] for r in cur.fetchall()]


def copy_table(src_cfg, dst_cfg, table, pk_cols):
    src = connect(src_cfg)
    cols = get_columns(src, table, src_cfg["database"])
    col_sql = ",".join(f"`{c}`" for c in cols)
    ins_sql = (
        f"INSERT IGNORE INTO `{table}` ({col_sql}) VALUES "
        f"({','.join(['%s'] * len(cols))})"
    )
    where = ""
    # 断点续传:从目标库已有的最后一条主键继续(INSERT IGNORE 保证幂等)
    bookmark: tuple | None = None
    try:
        dst0 = connect(dst_cfg)
        with dst0.cursor() as cur:
            order_desc = ",".join(f"`{c}` DESC" for c in pk_cols)
            cur.execute(f"SELECT {','.join('`'+c+'`' for c in pk_cols)} FROM `{table}` ORDER BY {order_desc} LIMIT 1")
            row = cur.fetchone()
            if row:
                bookmark = tuple(str(v) for v in row)
                print(f"[{table}] resume from bookmark {bookmark}", flush=True)
        dst0.close()
    except Exception as exc:
        print(f"[{table}] resume probe failed, start from scratch: {exc!r}", flush=True)
    total = 0
    while True:
        params: list = []
        if bookmark is not None:
            ors = []
            for i in range(len(pk_cols)):
                eq = " AND ".join("`{}`=%s".format(pk_cols[j]) for j in range(i))
                gt = "`{}`>%s".format(pk_cols[i])
                cond = (eq + " AND " + gt) if eq else gt
                ors.append("(" + cond + ")")
                params.extend(bookmark[: i + 1])
            where = "WHERE " + " OR ".join(ors)
        order = ",".join(f"`{c}`" for c in pk_cols)
        sql = f"SELECT {col_sql} FROM `{table}` {where} ORDER BY {order} LIMIT {BATCH}"
        rows = None
        for attempt in range(MAX_RETRY):
            try:
                try:
                    src.ping(reconnect=True)
                except Exception:
                    src = connect(src_cfg)
                with src.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                break
            except Exception as exc:
                print(f"[{table}] read retry {attempt + 1}: {exc!r}", flush=True)
                time.sleep(min(2 ** attempt, 30))
                try:
                    src.close()
                except Exception:
                    pass
                src = connect(src_cfg)
        if rows is None:
            raise RuntimeError(f"{table}: read failed after {MAX_RETRY} retries")
        if not rows:
            break
        dst = connect(dst_cfg)
        for attempt in range(MAX_RETRY):
            try:
                with dst.cursor() as cur:
                    cur.executemany(ins_sql, [tuple(r) for r in rows])
                break
            except Exception as exc:
                print(f"[{table}] write retry {attempt + 1}: {exc!r}", flush=True)
                time.sleep(min(2 ** attempt, 30))
                try:
                    dst.close()
                except Exception:
                    pass
                dst = connect(dst_cfg)
        dst.close()
        total += len(rows)
        last = rows[-1]
        idx = [cols.index(c) for c in pk_cols]
        bookmark = tuple(str(last[i]) for i in idx)
        print(f"[{table}] copied {total}", flush=True)
        if len(rows) < BATCH:
            break
    src.close()
    print(f"[{table}] DONE total={total}", flush=True)


def main():
    src_cfg, dst_cfg = load_cfgs()
    for table, pk in TABLES:
        copy_table(src_cfg, dst_cfg, table, pk)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
