# UUIDv4 vs UUIDv7 vs BIGINT — Primary Key Insert Benchmark

A small, self-contained benchmark that reproduces the claim from the article
**"Stop Using UUIDv4 as Your Primary Key"** on a *real* PostgreSQL instance,
running locally in Docker.

It creates three tables that are **identical except for the primary-key type**,
inserts the same rows into each, and measures what the random key actually costs:

* **insert throughput** (rows/sec), overall and per batch as the table grows,
* **WAL generated** — the write-amplification tax random keys put on Postgres,
* **primary-key index size** — random inserts bloat the B-tree.

The keys are generated **server-side via column DEFAULTs**, exactly like the line
the article is about:

```sql
id uuid DEFAULT gen_random_uuid()     -- the random one (UUIDv4)
id uuid DEFAULT uuidv7()              -- the time-ordered one (UUIDv7)
id bigint GENERATED ALWAYS AS IDENTITY -- the sequential baseline
```

---

## A quick word on Postgres specifically

In MySQL/InnoDB and SQL Server the primary key is the **clustering key**: rows are
physically stored in PK order, so a random PK scatters the whole *table*.

PostgreSQL is different — a table is a **heap** (rows are appended wherever there's
room, regardless of PK). So in Postgres the random-UUID penalty doesn't land on the
table heap; it lands on:

1. the **primary-key B-tree index** (random inserts → page splits, cache misses), and
2. **WAL full-page-write amplification** (scattered index pages dirty more 8 KB pages).

Same mechanism as the article describes, the cost just lives in the index + WAL.
This benchmark measures all three so you can see it.

---

## Prerequisites

* **Docker** and **Docker Compose v2** (`docker compose`, not the old `docker-compose`)
* **Python 3.10+**

That's it. No local Postgres needed — it runs in a container.

---

## Quick start

```bash
# 1. start PostgreSQL 18 in the background (listens on localhost:5433)
docker compose up -d

# 2. install the one Python dependency
pip install -r requirements.txt

# 3. run the benchmark
python benchmark.py
```

Or, if you have `make`:

```bash
make up        # start postgres and wait until it's healthy
make install   # pip install -r requirements.txt
make run       # python benchmark.py
```

Default run is **2,000,000 rows per table** and takes a few minutes. You'll see
something like:

```
· PostgreSQL 18.x · shared_buffers=128MB
· native uuidv7() found (PostgreSQL 18+) — using it

  uuidv4  |   14.20s |   140,800 rows/s | WAL   612.4 MB | index  92.1 MB | heap 246.0 MB
  uuidv7  |    5.90s |   338,900 rows/s | WAL   430.7 MB | index  61.3 MB | heap 246.0 MB
  bigint  |    4.10s |   487,800 rows/s | WAL   398.2 MB | index  43.0 MB | heap 230.0 MB

=== relative to UUIDv4 ===
  UUIDv7 insert throughput : 2.41x faster
  BIGINT insert throughput : 3.46x faster
  UUIDv4 WAL generated     : 1.42x more than UUIDv7
  UUIDv4 index size        : 1.50x UUIDv7
```

> The exact numbers depend entirely on your hardware, RAM, and `ROWS`. What's
> reproducible is the **direction**: UUIDv4 is the slowest, generates the most WAL,
> and grows the fattest index — and the gap widens as the table grows.

Two files are written next to the script:

* `results.json` — all metrics, machine-readable
* `throughput_by_size.csv` — per-batch throughput, so you can plot the degradation curve

---

## Reading the output

| Column         | What it means                                                        |
|----------------|----------------------------------------------------------------------|
| `rows/s`       | Insert throughput. Higher is better. UUIDv4 should be the lowest.    |
| `WAL`          | Write-ahead-log bytes generated. UUIDv4 writes the most (amplification). |
| `index`        | Final size of the primary-key B-tree. UUIDv4's is the most bloated.  |
| `heap`         | Table heap size. Roughly equal for all (Postgres heaps aren't PK-ordered) — this is the honest control showing the difference is in the index, not the table. |

---

## Seeing the collapse (a.k.a. the whole point)

Here's the trap the article describes, and you can feel it yourself:

**Run it small first** — say 200k rows:

```bash
ROWS=200000 python benchmark.py
```

The index fits comfortably in cache, every insert is fast, and UUIDv4 looks
*totally fine*. This is exactly why it sails through code review and dev testing.

**Now run it big** — past what fits in memory:

```bash
ROWS=10000000 python benchmark.py     # ~10M rows; give it some time
```

Once the UUIDv4 index no longer fits in cache, every random insert turns into a
page fetch, and its throughput **falls off a cliff** while UUIDv7 keeps appending
to the hot right edge of its B-tree. That widening gap *is* the article's chart.

To make the collapse appear at lower row counts on a high-RAM machine, shrink the
memory the database is allowed to touch:

* The included `docker-compose.yml` already sets `shared_buffers=128MB` and
  `mem_limit: 1g` so the working set spills past RAM sooner.
* Push it harder by lowering `shared_buffers` (e.g. `64MB`) in `docker-compose.yml`,
  or lowering `mem_limit`, then `docker compose up -d` again.
* The single most reliable lever is simply **more rows**. The effect is a
  working-set-exceeds-RAM phenomenon; enough rows always triggers it.

The per-batch curve in `throughput_by_size.csv` is the best place to watch this:
UUIDv4's `rows_per_sec` declines as `rows` grows, while UUIDv7's stays roughly flat.

---

## Configuration

All via environment variables:

| Variable        | Default     | Meaning                                            |
|-----------------|-------------|----------------------------------------------------|
| `ROWS`          | `2000000`   | Rows inserted per table                            |
| `CHUNK`         | `50000`     | Rows per batch / per commit                        |
| `PAYLOAD_BYTES` | `80`        | Size of the non-key payload column (kept identical)|
| `PGHOST`        | `localhost` | Postgres host                                       |
| `PGPORT`        | `5433`      | Postgres port (compose maps container 5432 → 5433) |
| `PGDATABASE`    | `uuidbench` | Database name                                       |
| `PGUSER`        | `postgres`  | User                                                |
| `PGPASSWORD`    | `benchmark` | Password                                            |
| `DATABASE_URL`  | *(unset)*   | If set, overrides all PG* vars (libpq conninfo/URL)|

Example — a big, cache-busting run:

```bash
ROWS=10000000 CHUNK=100000 python benchmark.py
```

---

## PostgreSQL version note

* **PostgreSQL 18+** ships a native, C-level `uuidv7()` (RFC 9562). The compose file
  pins `postgres:18`, so you get the **clean** comparison out of the box.
* **PostgreSQL < 18**: the script installs a dependency-free `plpgsql` `uuidv7()`
  fallback automatically, so it still runs. But that fallback costs more per row
  than the native function, so at *small* scale the v7 generation overhead can mask
  the locality win. Use PG 18, or large `ROWS`, for a fair picture.

---

## Plotting the curve (optional)

```bash
pip install matplotlib pandas
python - <<'PY'
import pandas as pd, matplotlib.pyplot as plt
df = pd.read_csv("throughput_by_size.csv")
for name, g in df.groupby("variant"):
    plt.plot(g["rows"]/1000, g["rows_per_sec"]/1000, marker="o", label=name)
plt.xlabel("rows inserted (thousands)"); plt.ylabel("k rows / sec")
plt.legend(); plt.title("Insert throughput as the table grows"); plt.tight_layout()
plt.savefig("throughput.png", dpi=150); print("wrote throughput.png")
PY
```

---

## Cleanup

```bash
docker compose down       # stop the container, keep the data volume
docker compose down -v    # stop and wipe the volume for a clean re-run
# or: make down / make reset
```

---

## Caveats (read these before quoting numbers)

* This is a **throughput** benchmark, not a durability test. The compose file sets
  `fsync=off` and `synchronous_commit=off` so every variant runs fast and *equally*;
  never use those settings in production.
* Absolute numbers vary wildly by CPU, disk, RAM, and `ROWS`. Trust the **ratios and
  the trend**, not the raw figures — and best of all, run it on hardware that
  resembles yours.
* The benchmark inserts in batches via `generate_series`, which is the DB-side insert
  path. Application-side single-row inserts will be slower across the board but show
  the same relative ordering.

---

*Companion to the article "Stop Using UUIDv4 as Your Primary Key." Built to be
re-run, not trusted on faith — which is rather the point.*
