#!/usr/bin/env python3
"""
UUIDv4 vs UUIDv7 vs BIGINT primary-key insert benchmark for PostgreSQL.

It creates three identical tables that differ ONLY in primary-key type, then
inserts ROWS rows into each in CHUNK-sized batches, measuring:

  * insert throughput (rows/sec), overall and per chunk (to expose degradation
    as the table grows past cache),
  * WAL bytes generated  -> write amplification (the random-key tax on Postgres),
  * final heap + primary-key index size -> index bloat.

The keys are generated SERVER-SIDE via column DEFAULTs, exactly like the line the
article warns about:  id uuid DEFAULT gen_random_uuid()

Postgres note: unlike MySQL/InnoDB, a Postgres table is a heap (rows are NOT
clustered by PK). So the random-UUID penalty here shows up in the PK *index*
(page splits, cache misses) and in WAL full-page-write amplification rather than
in the heap order. The mechanism is the same; the cost just lives in the index.
"""
import os
import json
import time
import sys
import psycopg

# ---------------- configuration (override via env) ----------------
ROWS         = int(os.getenv("ROWS", "2000000"))      # rows per table
CHUNK        = int(os.getenv("CHUNK", "50000"))       # rows per batch / commit
PAYLOAD_BYTES= int(os.getenv("PAYLOAD_BYTES", "80"))  # fixed payload so row size is equal
PAYLOAD      = "x" * PAYLOAD_BYTES

CONNINFO = os.getenv(
    "DATABASE_URL",
    "host={} port={} dbname={} user={} password={}".format(
        os.getenv("PGHOST", "localhost"),
        os.getenv("PGPORT", "5433"),
        os.getenv("PGDATABASE", "uuidbench"),
        os.getenv("PGUSER", "postgres"),
        os.getenv("PGPASSWORD", "benchmark"),
    ),
)

VARIANTS = [
    # name        table             create-table DDL (payload column identical everywhere)
    ("uuidv4", "bench_uuidv4", "id uuid PRIMARY KEY DEFAULT gen_random_uuid()"),
    ("uuidv7", "bench_uuidv7", "id uuid PRIMARY KEY DEFAULT uuidv7()"),
    ("bigint", "bench_bigint", "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY"),
]

# Dependency-free UUIDv7 used ONLY if the server has no native uuidv7()
# (PostgreSQL < 18). On PG 18+ the built-in pg_catalog.uuidv7() is used instead.
UUIDV7_FALLBACK = r"""
CREATE OR REPLACE FUNCTION public.uuidv7() RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
  ts_ms bigint := (extract(epoch from clock_timestamp()) * 1000)::bigint;
  b bytea := uuid_send(gen_random_uuid());          -- 16 random bytes
BEGIN
  b := overlay(b placing substring(int8send(ts_ms) from 3 for 6) from 1 for 6);  -- 48-bit ts
  b := set_byte(b, 6, (get_byte(b, 6) & 15) | 112); -- version 7  (0x70)
  b := set_byte(b, 8, (get_byte(b, 8) & 63) | 128); -- variant 10xx (0x80)
  RETURN encode(b, 'hex')::uuid;
END $$;
"""


def ensure_uuidv7(conn):
    native = conn.execute(
        "SELECT count(*) FROM pg_proc "
        "WHERE proname = 'uuidv7' AND pronamespace = 'pg_catalog'::regnamespace"
    ).fetchone()[0]
    if native:
        print("· native uuidv7() found (PostgreSQL 18+) — using it")
    else:
        conn.execute(UUIDV7_FALLBACK)
        print("· no native uuidv7() — installed a SQL fallback (PostgreSQL < 18).")
        print("  NOTE: the fallback is plpgsql and costs more per row than the native")
        print("  C function, so on PG<18 the v7 *generation* overhead can mask the win")
        print("  at small scale. PostgreSQL 18 gives the clean comparison.")


def lsn(conn):
    return conn.execute("SELECT pg_current_wal_lsn()").fetchone()[0]


def run_variant(conn, name, table, pk_ddl):
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({pk_ddl}, payload text NOT NULL)")
    conn.execute("CHECKPOINT")

    wal_start = lsn(conn)
    series = []
    inserted = 0
    t0 = time.perf_counter()
    while inserted < ROWS:
        n = min(CHUNK, ROWS - inserted)
        c0 = time.perf_counter()
        conn.execute(
            f"INSERT INTO {table} (payload) SELECT %s FROM generate_series(1, %s)",
            (PAYLOAD, n),
        )
        dt = time.perf_counter() - c0
        inserted += n
        series.append({"rows": inserted, "rows_per_sec": n / dt})
    total = time.perf_counter() - t0
    wal_end = lsn(conn)

    conn.execute("CHECKPOINT")
    wal_bytes = conn.execute(
        "SELECT pg_wal_lsn_diff(%s, %s)::bigint", (wal_end, wal_start)
    ).fetchone()[0]
    heap = conn.execute(f"SELECT pg_relation_size('{table}')").fetchone()[0]
    idx  = conn.execute(f"SELECT pg_indexes_size('{table}')").fetchone()[0]

    print(
        f"  {name:7s} | {total:7.2f}s | {ROWS/total:>9,.0f} rows/s | "
        f"WAL {wal_bytes/1e6:7.1f} MB | index {idx/1e6:6.1f} MB | heap {heap/1e6:6.1f} MB"
    )
    return {
        "name": name,
        "total_sec": total,
        "rows_per_sec": ROWS / total,
        "wal_bytes": int(wal_bytes),
        "index_bytes": int(idx),
        "heap_bytes": int(heap),
        "series": series,
    }


def main():
    print(f"Connecting … (ROWS={ROWS:,}, CHUNK={CHUNK:,}, payload={PAYLOAD_BYTES}B)")
    try:
        conn = psycopg.connect(CONNINFO, autocommit=True)
    except Exception as e:
        print(f"\n[!] Could not connect to Postgres: {e}", file=sys.stderr)
        print("    Is the container up?  ->  docker compose up -d", file=sys.stderr)
        sys.exit(1)

    ver = conn.execute("SHOW server_version").fetchone()[0]
    sb  = conn.execute("SHOW shared_buffers").fetchone()[0]
    print(f"· PostgreSQL {ver} · shared_buffers={sb}\n")
    ensure_uuidv7(conn)
    print("\nInserting … this may take a few minutes.\n")

    results = {}
    for name, table, ddl in VARIANTS:
        results[name] = run_variant(conn, name, table, ddl)

    v4, v7, bi = results["uuidv4"], results["uuidv7"], results["bigint"]
    print("\n=== relative to UUIDv4 ===")
    print(f"  UUIDv7 insert throughput : {v7['rows_per_sec']/v4['rows_per_sec']:.2f}x faster")
    print(f"  BIGINT insert throughput : {bi['rows_per_sec']/v4['rows_per_sec']:.2f}x faster")
    print(f"  UUIDv4 WAL generated     : {v4['wal_bytes']/max(v7['wal_bytes'],1):.2f}x more than UUIDv7")
    print(f"  UUIDv4 index size        : {v4['index_bytes']/max(v7['index_bytes'],1):.2f}x UUIDv7")

    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    # per-chunk CSV so you can plot the degradation curve yourself
    with open("throughput_by_size.csv", "w") as f:
        f.write("variant,rows,rows_per_sec\n")
        for name in results:
            for p in results[name]["series"]:
                f.write(f"{name},{p['rows']},{p['rows_per_sec']:.0f}\n")
    print("\nSaved results.json and throughput_by_size.csv")
    conn.close()


if __name__ == "__main__":
    main()
