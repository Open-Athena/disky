#!/usr/bin/env python3
"""Hourly Iris worker / task tables for a UTC day, from Marin's Finelog object store: the VM → job leg of attributing
bucket requests (specs/access-job-attribution.md phase 2). The `access-jobs` image's entrypoint:

    access_jobs.py -o gs://<data>/access/jobs DAY…

writes `<out>/<DAY>/workers.parquet` (`hour, worker_id, tpu_name, gce_instance_name, zone, address, device_variant`)
then `<out>/<DAY>/tasks.parquet` (`hour, task_id, owner, job, attempt_id, worker_id, n_samples`; `owner` / `job`:
the task id's user and top-level job; `n_samples` = the task's distinct `iris.task` rows that hour, which split a
worker's requests between its tasks). A day whose two files exist
is skipped (`-f` rewrites it); a day Finelog doesn't yet cover through its end (no segment reaches the next midnight)
is left for a later run. Exit 1 when any day failed (the others still run).

The catalog comes from `finelog`'s own client: `ObjectQueryClient(root).pin_catalog(table)` reads `HEAD.json`,
follows the parent-linked catalog deltas back to a checkpoint and folds them forward, returning the pinned stable
segments. Their catalog entries carry no `ts` range, so segments are selected by each object's parquet footer
statistics on `ts`; only the selected segments' projected columns are read (pyarrow, through the client's own
filesystem), and the hourly SQL runs in a local DuckDB. `ObjectQueryClient.query` itself isn't used: at Marin
10b2167aed28 its DuckDB `register_filesystem` call rejects rigging's `CrossRegionGuardedFS` for a `gs://` root.
Cross-region reads count against `MARIN_MIRROR_BUDGET_GB` (rigging's guard; a day is ~1 GB).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

os.environ.setdefault("USE_EXPERIMENTAL_ADAPTIVE_PREFETCHING", "false")

import duckdb
import gcsfs
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from click import argument, command, option
from finelog.client.object_query_client import ObjectQueryClient

err = partial(print, file=sys.stderr)

ROOT = "gs://marin-us-central2/finelog/marin"
WORKER_COLS = ["worker_id", "tpu_name", "gce_instance_name", "zone", "address", "device_variant"]
TASK_COLS = ["task_id", "attempt_id", "worker_id"]
WORKERS_SQL = f"""
    SELECT date_trunc('hour', ts) AS hour, {', '.join(WORKER_COLS)}
    FROM "iris.worker" GROUP BY ALL ORDER BY hour, worker_id, address"""
# An Iris `task_id` is `/<user>/<job>[/<child>…]/<index>`: `owner` is its user, `job` its top-level job
TASKS_SQL = """
    SELECT date_trunc('hour', ts) AS hour, task_id, split_part(task_id, '/', 2) AS owner,
           '/' || split_part(task_id, '/', 2) || '/' || split_part(task_id, '/', 3) AS job, attempt_id, worker_id,
           count(DISTINCT seq) AS n_samples
    FROM "iris.task" GROUP BY ALL ORDER BY hour, task_id, attempt_id, worker_id"""
FILES = ("workers.parquet", "tasks.parquet")  # written in this order: `tasks` present = the day is complete


@dataclass(frozen=True)
class Segment:
    uri: str
    ts_min: dt.datetime  # naive UTC (Finelog's `ts` is a naive `timestamp[us]` holding UTC)
    ts_max: dt.datetime


class NotCovered(Exception):
    """Finelog's newest segment ends before the window does."""


def footer_ts(fs, uri: str) -> tuple[dt.datetime, dt.datetime]:
    with fs.open(uri, "rb") as f:
        md = pq.ParquetFile(f).metadata
    i = md.schema.names.index("ts")
    lo = hi = None
    for g in range(md.num_row_groups):
        st = md.row_group(g).column(i).statistics
        if st is None or not st.has_min_max:
            raise RuntimeError(f"{uri}: row group {g} has no ts statistics")
        lo = st.min if lo is None else min(lo, st.min)
        hi = st.max if hi is None else max(hi, st.max)
    return lo, hi


def segments(client: ObjectQueryClient, table: str, start: dt.datetime, end: dt.datetime) -> list[Segment]:
    """`table`'s pinned segments meeting `[start, end)`; NotCovered when none reaches `end`."""
    catalog = client.pin_catalog(table)
    fs = client._filesystem  # rigging's (cross-region guarded) filesystem: the client's own reads' path
    with ThreadPoolExecutor(32) as ex:
        ranges = list(ex.map(lambda s: footer_ts(fs, s.object_uri), catalog.segments))
    newest = max((hi for _, hi in ranges), default=None)
    if newest is None or newest < end:
        raise NotCovered(f"{table}: newest row {newest}, before {end}")
    sel = [Segment(s.object_uri, lo, hi) for s, (lo, hi) in zip(catalog.segments, ranges) if lo < end and hi >= start]
    err(f"{table}: catalog generation {catalog.catalog_generation}, {len(sel)} of {len(catalog.segments)} segments meet {start:%F}")
    return sel


def stage(con: duckdb.DuckDBPyConnection, client: ObjectQueryClient, table: str, start: dt.datetime, end: dt.datetime,
          columns: list[str]) -> None:
    """`table`'s rows in `[start, end)`, projected to `seq, ts, *columns`, into `con` as a table named like it."""
    fs = client._filesystem
    cols = ["seq", "ts", *columns]

    def read(seg: Segment) -> pa.Table:
        with fs.open(seg.uri, "rb", block_size=8 << 20) as f:
            schema = pq.ParquetFile(f).schema_arrow
            strings = [c for c in cols if pa.types.is_string(schema.field(c).type)]
            t = pq.read_table(f, columns=cols, read_dictionary=strings)
        ts = t["ts"]
        return t.filter(pc.and_(pc.greater_equal(ts, pa.scalar(start, ts.type)), pc.less(ts, pa.scalar(end, ts.type))))

    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    t0, rows = time.time(), 0
    sel = segments(client, table, start, end)
    with ThreadPoolExecutor(8) as ex:
        for n, t in enumerate(ex.map(read, sel)):
            con.register("batch", t.select(cols))
            con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM batch' if n == 0 else f'INSERT INTO "{table}" SELECT * FROM batch')
            con.unregister("batch")
            rows += t.num_rows
    if not sel:
        raise RuntimeError(f"{table}: no segment meets {start:%F}")
    err(f"{table}: {rows} rows from {len(sel)} segments, {time.time() - t0:.0f}s")


def write_day(day: dt.date, out: str, root: str, fs: gcsfs.GCSFileSystem | None) -> None:
    start = dt.datetime.combine(day, dt.time())
    end = start + dt.timedelta(days=1)
    client = ObjectQueryClient(root)
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect(f"{tmp}/day.duckdb")
        con.execute("SET TimeZone = 'UTC'")
        stage(con, client, "iris.worker", start, end, WORKER_COLS)
        stage(con, client, "iris.task", start, end, TASK_COLS)
        for name, sql in zip(FILES, (WORKERS_SQL, TASKS_SQL)):
            local = f"{tmp}/{name}"
            con.execute(f"COPY ({sql}) TO '{local}' (FORMAT parquet, COMPRESSION zstd)")
            dest = f"{out}/{day}/{name}"
            rows = pq.read_metadata(local).num_rows
            if fs:
                fs.put_file(local, dest)
            else:
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(local).replace(dest)
            err(f"{dest}: {rows} rows")


@command()
@option("-f", "--force", is_flag=True, help="Rewrite days already written")
@option("-o", "--out", required=True, envvar="ACCESS_JOBS_OUT", help="Output root (`gs://…` or a local dir): `<out>/<DAY>/{workers,tasks}.parquet`")
@option("-r", "--root", default=ROOT, show_default=True, envvar="FINELOG_ROOT", help="Finelog's object store")
@argument("days", nargs=-1, required=True)
def main(force: bool, out: str, root: str, days: tuple[str, ...]) -> None:
    """Write each DAY's (YYYY-MM-DD, UTC) hourly Iris worker and task tables under OUT."""
    out = out.rstrip("/")
    fs = gcsfs.GCSFileSystem() if out.startswith("gs://") else None
    exists = fs.exists if fs else (lambda p: Path(p).exists())
    print(f"access-jobs: image {os.environ.get('DISKY_GIT_SHA') or 'unstamped'}", file=sys.stderr)
    failed = []
    for d in days:
        day = dt.date.fromisoformat(d)
        if not force and all(exists(f"{out}/{day}/{name}") for name in FILES):
            err(f"{day}: already written")
            continue
        try:
            write_day(day, out, root, fs)
        except NotCovered as e:
            err(f"{day}: not yet covered by Finelog ({e}); left for a later run")
        except Exception:
            traceback.print_exc()
            failed.append(d)
    if failed:
        err(f"access-jobs: failed {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
