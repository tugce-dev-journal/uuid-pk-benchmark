#!/usr/bin/env python3
"""
SQLite reproduction of the article's *headline* chart — no dependencies, Python
stdlib only (sqlite3). This is the amplified, single-engine demonstration of the
mechanism; for the real-PostgreSQL version see ../benchmark.py.

Why SQLite + WITHOUT ROWID:
  WITHOUT ROWID makes the PRIMARY KEY the *clustering* key, so PK ordering drives
  the physical page layout — exactly like InnoDB (MySQL) and SQL Server. A
  deliberately tiny page cache (CACHE_MB) simulates the realistic case where the
  index no longer fits in RAM, which is what makes the random-key (UUIDv4)
  collapse show up clearly in a single run.

Three tables, identical except for the primary-key type:
  * UUIDv4  — random            (the one the article warns about)
  * UUIDv7  — time-ordered      (the fix)
  * BIGINT  — sequential        (the baseline)

Outputs (written next to this script):
  * sqlite_results.json            — summary metrics
  * sqlite_throughput_by_size.csv  — per-batch throughput (the degradation curve)
"""
import os
import sqlite3
import time
import uuid
import secrets
import json

# ---------------- configuration (override via env) ----------------
TOTAL    = int(os.getenv("ROWS", "1000000"))          # rows per table
CHUNK    = int(os.getenv("CHUNK", "100000"))          # rows per batch / commit
CACHE_MB = int(os.getenv("CACHE_MB", "8"))            # page cache; small = effect visible
PAYLOAD  = "x" * int(os.getenv("PAYLOAD_BYTES", "80"))
OUT_DIR  = os.path.dirname(os.path.abspath(__file__))

_counter = 0
def gen_uuidv7():
    """Strictly increasing, RFC-9562-shaped hex key (time-ordered representative)."""
    global _counter
    ms = int(time.time() * 1000)
    _counter += 1
    # 48-bit ms timestamp + 64-bit monotonic counter + 16 random bits => 32 hex chars
    return ms.to_bytes(6, "big").hex() + _counter.to_bytes(8, "big").hex() + secrets.token_bytes(2).hex()

def gen_uuidv4():
    return uuid.uuid4().hex

VARIANTS = [
    ("bigint", lambda i: i),
    ("uuidv7", lambda i: gen_uuidv7()),
    ("uuidv4", lambda i: gen_uuidv4()),
]

def make_db(path):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=MEMORY")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(f"PRAGMA cache_size=-{CACHE_MB * 1000}")  # negative => KiB
    return con

def run(kind, keygen, path):
    con = make_db(path)
    if kind == "bigint":
        con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, payload TEXT) WITHOUT ROWID")
    else:
        con.execute("CREATE TABLE t(id TEXT PRIMARY KEY, payload TEXT) WITHOUT ROWID")

    cur = con.cursor()
    series, done = [], 0
    t0 = time.perf_counter()
    while done < TOTAL:
        n = min(CHUNK, TOTAL - done)
        buf = [(keygen(done + i + 1), PAYLOAD) for i in range(n)]
        c0 = time.perf_counter()
        cur.executemany("INSERT INTO t VALUES(?,?)", buf)
        con.commit()
        dt = time.perf_counter() - c0
        done += n
        series.append({"rows": done, "rows_per_sec": n / dt})
    total = time.perf_counter() - t0

    pc = con.execute("PRAGMA page_count").fetchone()[0]
    ps = con.execute("PRAGMA page_size").fetchone()[0]
    size = pc * ps
    con.close()

    first = sum(p["rows_per_sec"] for p in series[:3]) / min(3, len(series))
    last  = sum(p["rows_per_sec"] for p in series[-3:]) / min(3, len(series))
    print(f"  {kind:7s} | {total:7.2f}s | {TOTAL/total:>9,.0f} rows/s avg | "
          f"start {first:>9,.0f} -> end {last:>9,.0f} ({first/last:.2f}x drop) | db {size/1e6:6.1f} MB")
    return {"kind": kind, "total_sec": total, "rows_per_sec": TOTAL / total,
            "db_bytes": size, "start_rps": first, "end_rps": last, "series": series}

def main():
    print(f"SQLite {sqlite3.sqlite_version} · WITHOUT ROWID (clustered key) · "
          f"cache={CACHE_MB}MB · {TOTAL:,} rows\n")
    results = {}
    for kind, kg in VARIANTS:
        results[kind] = run(kind, kg, os.path.join(OUT_DIR, f"_bench_{kind}.db"))
        os.remove(os.path.join(OUT_DIR, f"_bench_{kind}.db"))

    v4, v7, bi = results["uuidv4"], results["uuidv7"], results["bigint"]
    print("\n=== relative to UUIDv4 (at end of run) ===")
    print(f"  UUIDv7 end throughput : {v7['end_rps']/v4['end_rps']:.2f}x faster")
    print(f"  BIGINT end throughput : {bi['end_rps']/v4['end_rps']:.2f}x faster")
    print(f"  UUIDv4 self-slowdown  : {v4['start_rps']/v4['end_rps']:.2f}x slower at the end than the start")

    json.dump(results, open(os.path.join(OUT_DIR, "sqlite_results.json"), "w"), indent=2)
    with open(os.path.join(OUT_DIR, "sqlite_throughput_by_size.csv"), "w") as f:
        f.write("variant,rows,rows_per_sec\n")
        for kind in results:
            for p in results[kind]["series"]:
                f.write(f"{kind},{p['rows']},{p['rows_per_sec']:.0f}\n")
    print("\nSaved sqlite_results.json and sqlite_throughput_by_size.csv")

if __name__ == "__main__":
    main()
