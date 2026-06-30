# SQLite reproduction (the article's headline chart)

This is the **amplified, zero-dependency** version of the benchmark — the one
behind the article's main throughput chart. It needs nothing but Python 3:

```bash
python sqlite_bench.py
```

## Why this one is so dramatic

It uses **SQLite `WITHOUT ROWID`** tables, which makes the primary key the
*clustering* key — so the random vs ordered key difference hits the **physical
row layout** directly (exactly like InnoDB/MySQL and SQL Server). Combined with a
deliberately tiny page cache (`CACHE_MB`, default 8 MB), the random-key index
spills out of cache almost immediately and the UUIDv4 collapse is huge.

That makes it a clean teaching demo of the *mechanism*. For what it looks like on
a real heap-based engine with normal caches, run the PostgreSQL benchmark one level
up (`../benchmark.py`) — the same physics, a gentler curve, and index bloat + WAL
as the clearest signals.

## What it does

Three tables, identical except for the primary-key type (UUIDv4 / UUIDv7 / BIGINT).
Inserts `ROWS` rows into each in `CHUNK`-sized batches, recording per-batch
throughput so you can see UUIDv4 degrade as the table grows while the others stay flat.

## Configuration (env vars)

| Variable        | Default     | Meaning                                   |
|-----------------|-------------|-------------------------------------------|
| `ROWS`          | `1000000`   | Rows per table                            |
| `CHUNK`         | `100000`    | Rows per batch / commit                   |
| `CACHE_MB`      | `8`         | SQLite page cache. Smaller = bigger effect|
| `PAYLOAD_BYTES` | `80`        | Size of the payload column                |

```bash
ROWS=2000000 CACHE_MB=8 python sqlite_bench.py
```

## Output

* `sqlite_results.json` — summary metrics
* `sqlite_throughput_by_size.csv` — per-batch throughput; plot `rows` vs
  `rows_per_sec` per variant to recreate the article's degradation chart.
