"""Arrow IPC vs parquet for `interval-store cut`'s spools: the product's own `write_served` of one merged sort, end to end, once per format.

A format is swapped in by patching the seams, not a product flag: `_sort_run`'s `pq.ParquetWriter` (lz4 spools only; the served and groups
writers aren't lz4), `RunSpool` and `_file_rows`. Everything else (`cut_runs`, fences, `piece_plan`, `sort_piece`, `cut_part`, `concat`) is the
image's code. `cut_plan`, called once the pieces are done with every spool still on disk, is the hook for what needs them: digests, memory, span
replays, dictionary/codec samples.

Conditions: one sort at a quarter of the race's 4 sorts × 4 workers. 4 workers; DuckDB 4 threads and a quarter of the race's limit; the same
run budget and piece share; pinned to 4 vCPUs (2 cores: 0, 1 and their siblings 8, 9), the sort's share of the machine.

usage: ipc_bench.py ipc|parquet|compare | rss CASE DIR | replay CASE SPANS | dicts DIR"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as real_pq

from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud import tree_mem as tm

GEN = os.environ.get("BENCH_GEN", "2026-10-09b.g3.c")
MOUNT = os.environ.get("BENCH_MOUNT", "/gcs/oa-gcs-usage-dvx")
STAGE = Path(os.environ.get("BENCH_STAGE", "/stage"))
RESULTS = STAGE / "bench" / "results"
SORT = os.environ.get("BENCH_SORT", "bysize")
#: The race's plan (w4.log): DuckDB's limit for 4 sorts × 4 workers, and bysize's piece share.
RACE_DUCK = int(os.environ.get("BENCH_DUCK_MIB", "30719")) << 20
RACE_AT_ONCE, WORKERS = 4, 4
RACE_SHARE = int(os.environ.get("BENCH_SHARE", "1574180"))
CPUS = {0, 1, 8, 9}
SPANS_KEPT = 20000
DIGEST_PROCS = int(os.environ.get("BENCH_DIGEST_PROCS", "12"))
READ_OPTS = pa.ipc.IpcReadOptions(use_threads=False)
WRITE_OPTS = pa.ipc.IpcWriteOptions(compression="lz4", use_threads=False)


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def result(name: str, doc) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    text = json.dumps(doc, default=str)
    log(f"RESULT {name} {text if len(text) < 4000 else text[:4000] + '…'}")


def pin(cpus: set[int] | None) -> None:
    """To `cpus` (None: every CPU the VM has). Off Linux, nothing."""
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cpus if cpus is not None else set(range(os.cpu_count() or 1)))


def smaps() -> dict[str, int]:
    """This process's `smaps_rollup` bytes (off Linux, RSS alone)."""
    if not tm.has_procfs():
        return {"Rss": tm.own_bytes(), "Pss": tm.own_bytes(), "Pss_Anon": 0, "Pss_File": 0}
    with open("/proc/self/smaps_rollup") as f:
        return {p[0][:-1]: int(p[1]) << 10 for p in (line.split() for line in f) if len(p) == 3 and p[2] == "kB"}


def evict(paths) -> None:
    """Drop `paths` from the page cache (flushed first: a dirty page stays)."""
    if not hasattr(os, "posix_fadvise"):
        return
    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY)
        except FileNotFoundError:  # legitimate: a spool the product just removed
            continue
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


# ── The IPC spool: `_sort_run`'s writer, `RunSpool`, `_file_rows` ──────────

WRITES: list[dict] = []
WRITES_LOCK = Lock()


def rows_file(path) -> str:
    """A spool's batches' row counts: IPC's footer has each batch's offset and lengths, not its rows."""
    return f"{path}.rows.npy"


class SpoolWriter:
    """`_sort_run`'s spool, as `mode`'s format; each file's bytes, batches and write seconds kept in `WRITES`."""

    mode = "parquet"

    def __init__(self, out, schema: pa.Schema, compression: str, **opts):
        self.out, self.rows, self.write_s = str(out), [], 0.0
        t0 = monotonic()
        if self.mode == "ipc":
            self.sink = pa.output_stream(self.out, buffer_size=1 << 20)
            self.w = pa.ipc.new_file(self.sink, schema, options=WRITE_OPTS)
        else:
            self.w = real_pq.ParquetWriter(out, schema, compression=compression, **opts)
        self.write_s += monotonic() - t0

    def write_table(self, t: pa.Table, row_group_size: int) -> None:
        t0 = monotonic()
        if self.mode == "ipc":
            for b in t.to_batches():
                self.w.write_batch(b)
                self.rows.append(b.num_rows)
        else:
            self.w.write_table(t, row_group_size=row_group_size)
            self.rows.append(t.num_rows)
        self.write_s += monotonic() - t0

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        t0 = monotonic()
        self.w.close()
        if self.mode == "ipc":
            self.sink.close()
            np.save(rows_file(self.out), np.asarray(self.rows, dtype=np.int64))
        self.write_s += monotonic() - t0
        with WRITES_LOCK:
            WRITES.append({"file": Path(self.out).name, "bytes": os.path.getsize(self.out), "rows": sum(self.rows), "batches": len(self.rows),
                           "write_s": round(self.write_s, 3), "batch_rows": Counter(self.rows).most_common(4)})


class PqShim:
    """`interval_store`'s `pq`: lz4 writers are spools, everything else the real module."""

    def __getattr__(self, name):
        return getattr(real_pq, name)

    @staticmethod
    def ParquetWriter(out, schema, compression="snappy", **opts):
        return SpoolWriter(out, schema, compression, **opts) if compression == "lz4" else real_pq.ParquetWriter(out, schema, compression=compression, **opts)


SPANS: list[tuple[str, int, int]] = []


def keep_span(path: str, a: int, z: int) -> None:
    if len(SPANS) < SPANS_KEPT:
        SPANS.append((path, a, z))


class _Md:
    """What `sort_piece` asks of a spool's footer: `md.schema.to_arrow_schema()`."""

    def __init__(self, schema: pa.Schema):
        self.schema = self
        self._arrow = schema

    def to_arrow_schema(self) -> pa.Schema:
        return self._arrow


class IpcRunSpool:
    """`RunSpool` over an IPC file: its reader held (the footer: 24 B a batch) and `starts` (8 B a batch), one `get_batch` a span's batch. The reader
    is shared by the piece threads: its one lazy step, dictionaries, is done here, and the OS file reads by `pread`."""

    def __init__(self, path: str, mmap: bool = False):
        self.path = path
        self.file = pa.memory_map(path) if mmap else pa.OSFile(path)
        self.r = pa.ipc.open_file(self.file, options=READ_OPTS)
        self.md = _Md(self.r.schema)
        self.starts = np.cumsum([0, *np.load(rows_file(path))])
        if self.r.num_record_batches:
            self.r.get_batch(0)

    def batches(self, a: int, z: int, batch: int, columns: list[str], stat: dict):
        if a >= z:
            return
        keep_span(self.path, a, z)
        yield from span_batches(self.r, self.starts, a, z, columns, stat)


def span_batches(r, starts, a: int, z: int, columns: list[str] | None, stat: dict):
    """Rows `[a, z)` of an IPC reader, a batch at a time, decoding only the batches holding them."""
    g = int(np.searchsorted(starts, a, "right")) - 1
    g1 = int(np.searchsorted(starts, z, "left"))
    for i in range(g, g1):
        t0 = monotonic()
        b = r.get_batch(i)
        lo, hi = int(starts[i]), int(starts[i + 1])
        x, y = max(a, lo), min(z, hi)
        if x > lo or y < hi:
            b = b.slice(x - lo, y - x)
        if columns is not None:
            b = b.select(columns)
        stat["read"] += monotonic() - t0
        stat["groups"] += 1
        stat["decoded"] += hi - lo
        yield b


class KeptRunSpool(ist.RunSpool):
    """The product's `RunSpool`, its spans noted for the replay."""

    def batches(self, a, z, batch, columns, stat):
        if a < z:
            keep_span(self.path, a, z)
        return super().batches(a, z, batch, columns, stat)


def ipc_file_rows(path: str, start: int, n: int, batch: int, columns: list[str]):
    """`_file_rows` over an IPC spool (what a part's writer reads): opened per call as the product's is, batches of at most `batch` rows."""
    starts = np.cumsum([0, *np.load(rows_file(path))])
    stat = {"read": 0.0, "groups": 0, "decoded": 0}
    with pa.OSFile(path) as f:
        r = pa.ipc.open_file(f, options=READ_OPTS)
        for b in span_batches(r, starts, start, start + n, columns, stat):
            for at in range(0, b.num_rows, batch):
                yield b.slice(at, batch)


def ipc_worker_init(label: str = "-") -> None:
    REAL_WORKER_INIT(label)
    ist._file_rows = ipc_file_rows


REAL_WORKER_INIT = tm.worker_init


# ── Digests: a file's rows, in order, per column, however they're chunked ──

def _feed(h, nulls, lens, arr: pa.Array) -> None:
    """Three streams per column, so a chunking can't move a byte between them: values, null flags, string lengths."""
    if isinstance(arr, pa.ChunkedArray):
        for c in arr.chunks:
            _feed(h, nulls, lens, c)
        return
    t = arr.type
    nulls.update(np.asarray(arr.is_null().to_numpy(zero_copy_only=False), dtype=np.uint8).tobytes() if arr.null_count else bytes(len(arr)))
    if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t):
        a = arr.fill_null("") if arr.null_count else arr
        width = np.int64 if pa.types.is_large_string(t) or pa.types.is_large_binary(t) else np.int32
        bufs = a.buffers()
        off = np.frombuffer(bufs[1], dtype=width, count=len(a) + 1, offset=a.offset * np.dtype(width).itemsize)
        lens.update(np.diff(off).astype(np.int64).tobytes())
        if bufs[2] is not None and off[-1] > off[0]:
            h.update(memoryview(bufs[2])[int(off[0]):int(off[-1])])
    elif pa.types.is_integer(t) or pa.types.is_floating(t):
        h.update((arr.fill_null(0) if arr.null_count else arr).to_numpy(zero_copy_only=False).tobytes())
    else:
        for v in arr.to_pylist():
            h.update(repr(v).encode())


def file_digest(path: str, ipc: bool) -> dict:
    """`{rows, schema, cols: {name: digest}}` of a spool. Order-sensitive; a chunking or a format doesn't change it."""
    hs: dict = {}
    nulls: dict = {}
    lens: dict = {}
    rows = 0

    def batches():
        if ipc:
            with pa.OSFile(path) as f:
                r = pa.ipc.open_file(f, options=READ_OPTS)
                yield r.schema
                for i in range(r.num_record_batches):
                    yield r.get_batch(i)
        else:
            pf = real_pq.ParquetFile(path)
            yield pf.schema_arrow
            yield from pf.iter_batches(batch_size=65536, use_threads=False)

    it = batches()
    schema = next(it)
    for b in it:
        rows += b.num_rows
        for name, col in zip(b.schema.names, b.columns):
            h = hs.setdefault(name, hashlib.blake2b(digest_size=16))
            n = nulls.setdefault(name, hashlib.blake2b(digest_size=16))
            _feed(h, n, lens.setdefault(name, hashlib.blake2b(digest_size=16)), col)
    return {"file": Path(path).name, "rows": rows, "schema": str(schema), "cols": {k: h.hexdigest() + nulls[k].hexdigest() + lens[k].hexdigest() for k, h in hs.items()}}


def _digest_init() -> None:
    pin(None)


def _digest_one(a: tuple[str, bool]) -> dict:
    return file_digest(*a)


def digest_tree(work: Path, ipc: bool) -> list[dict]:
    files = sorted(str(p) for p in [*work.glob("run-*.parquet"), *work.glob("piece-*.parquet")])
    t0 = monotonic()
    with ProcessPoolExecutor(DIGEST_PROCS, mp_context=get_context("spawn"), initializer=_digest_init) as ex:
        got = list(ex.map(_digest_one, [(f, ipc) for f in files], chunksize=4))
    log(f"digests: {len(got)} spools in {monotonic() - t0:.0f}s")
    return got


# ── Separate processes: what holding the spools costs, and spans alone ─────

def rss_case(case: str, work: Path) -> None:
    """Resident bytes of holding every run spool for a piece's reads, in a fresh process."""
    files = sorted(str(p) for p in work.glob("run-*.parquet"))
    gc.collect()
    tm.arrow_returns_freed()
    before = smaps()
    t0 = monotonic()
    if case == "parquet":
        held = [ist.RunSpool(f) for f in files]
        batches = sum(s.md.num_row_groups for s in held)
    else:
        held = [IpcRunSpool(f, mmap=case == "ipc-mmap") for f in files]
        batches = sum(s.r.num_record_batches for s in held)
    s = monotonic() - t0
    gc.collect()
    after = smaps()
    delta = {k: after[k] - before.get(k, 0) for k in ("Rss", "Pss", "Pss_Anon", "Pss_File")}
    print("RSSCASE", json.dumps({"case": case, "files": len(files), "batches": batches, "open_s": round(s, 2), "delta": delta,
                                 "arrow_pool": pa.total_allocated_bytes(), "b_per_batch": round(delta["Pss"] / max(1, batches), 1)}), flush=True)
    del held


def replay_case(case: str, spans_file: str) -> None:
    """The kept spans read alone (no DuckDB), 4 threads, page cache dropped first: `parquet` (`RunSpool`), `held` (one IPC reader a run, plain file),
    `open` (a reader per span, nothing held between), `mmap` (held, memory-mapped)."""
    spans = json.loads(Path(spans_file).read_text())
    paths = sorted({p for p, _, _ in spans})
    evict(paths)
    before = smaps()
    if case == "parquet":
        spools = {p: ist.RunSpool(p) for p in paths}
    elif case in ("held", "mmap"):
        spools = {p: IpcRunSpool(p, mmap=case == "mmap") for p in paths}
    else:
        spools = {p: np.cumsum([0, *np.load(rows_file(p))]) for p in paths}
    cols = None
    times: list[float] = [0.0] * len(spans)
    tot = Counter()
    lock = Lock()

    def one(i: int) -> None:
        p, a, z = spans[i]
        st = {"read": 0.0, "groups": 0, "decoded": 0}
        t0 = monotonic()
        n = 0
        if case == "open":
            with pa.OSFile(p) as f:
                r = pa.ipc.open_file(f, options=READ_OPTS)
                for b in span_batches(r, spools[p], a, z, None, st):
                    n += b.num_rows
        elif case == "parquet":
            names = spools[p].md.schema.to_arrow_schema().names
            for b in spools[p].batches(a, z, ist.CUT_SPOOL_RG, names, st):
                n += b.num_rows
        else:
            for b in span_batches(spools[p].r, spools[p].starts, a, z, cols, st):
                n += b.num_rows
        times[i] = monotonic() - t0
        with lock:
            tot["rows"] += n
            tot["groups"] += st["groups"]
            tot["decoded"] += st["decoded"]

    t0 = monotonic()
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(one, range(len(spans))))
    wall = monotonic() - t0
    xs = sorted(times)
    after = smaps()
    print("REPLAY", json.dumps({"case": case, "spans": len(spans), "runs": len(paths), "wall_s": round(wall, 2), "span_s_sum": round(sum(xs), 2),
                                "p50_ms": round(xs[len(xs) // 2] * 1e3, 2), "p90_ms": round(xs[int(len(xs) * .9)] * 1e3, 2), "p99_ms": round(xs[int(len(xs) * .99)] * 1e3, 2),
                                "max_ms": round(xs[-1] * 1e3, 2), **tot, "delta_pss_anon": after["Pss_Anon"] - before.get("Pss_Anon", 0),
                                "delta_pss_file": after["Pss_File"] - before.get("Pss_File", 0)}), flush=True)


def dicts_case(work: Path) -> None:
    """IPC variants of a sample of run spools, each rewritten whole in its batches: bytes, write and full-decode seconds. IPC files carry one dictionary
    per column across the file (replacement isn't allowed), which a reader holds, so a dictionary's bytes are also resident per open reader."""
    files = sorted(str(p) for p in work.glob("run-*.parquet"))
    picks = files[:: max(1, len(files) // 8)][:8]
    tmp = STAGE / "bench" / "dict-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = []
    for f in picks:
        with pa.OSFile(f) as src:
            r = pa.ipc.open_file(src, options=READ_OPTS)
            t = r.read_all()
        row: dict = {"file": Path(f).name, "rows": t.num_rows, "lz4_bytes": os.path.getsize(f), "uncompressed_bytes": t.nbytes}
        strs = [n for n, c in zip(t.schema.names, t.schema.types) if pa.types.is_string(c)]
        low = [n for n in strs if n not in ("path", "__u")]
        by_col: dict[str, dict] = {}

        def encoded(names: list[str]) -> pa.Table:
            u = t
            for n in names:
                u = u.set_column(u.schema.get_field_index(n), n, pc.dictionary_encode(u.column(n)))
            return u

        for name, tbl, opts in (
            ("zstd1", t, pa.ipc.IpcWriteOptions(compression=pa.Codec("zstd", compression_level=1), use_threads=False)),
            ("lz4_dict_low", encoded(low), pa.ipc.IpcWriteOptions(compression="lz4", use_threads=False, unify_dictionaries=True)),
            ("lz4_dict_all_strings", encoded(strs), pa.ipc.IpcWriteOptions(compression="lz4", use_threads=False, unify_dictionaries=True)),
        ):
            p = tmp / f"{name}.arrow"
            t0 = monotonic()
            with pa.output_stream(str(p), buffer_size=1 << 20) as sink, pa.ipc.new_file(sink, tbl.schema, options=opts) as w:
                w.write_table(tbl, max_chunksize=1024)
            ws = monotonic() - t0
            evict([str(p)])
            t0 = monotonic()
            with pa.OSFile(str(p)) as src:
                r = pa.ipc.open_file(src, options=READ_OPTS)
                for i in range(r.num_record_batches):
                    r.get_batch(i)
            by_col[name] = {"bytes": os.path.getsize(p), "write_s": round(ws, 2), "decode_s": round(monotonic() - t0, 2)}
            p.unlink()
        evict([f])
        t0 = monotonic()
        with pa.OSFile(f) as src:
            r = pa.ipc.open_file(src, options=READ_OPTS)
            for i in range(r.num_record_batches):
                r.get_batch(i)
        row["lz4_decode_s"] = round(monotonic() - t0, 2)
        row["variants"] = by_col
        row["column_bytes"] = {n: t.column(n).nbytes for n in t.schema.names}
        out.append(row)
        print("DICTS", json.dumps(row), flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


# ── One format's whole sort ────────────────────────────────────────────────

class Sampler:
    """Per stage: wall seconds, and the most the tree's PSS, this process's anon and file PSS, Arrow, DuckDB and the disk reached."""

    def __init__(self, con):
        self.duck = tm.duck_bytes(con)
        self.stage, self.t0 = "start", monotonic()
        self.stages: dict[str, dict] = {}
        self.stop = Event()
        self.lock = Lock()
        self.t = Thread(target=self.loop, daemon=True)

    def enter(self, stage: str) -> None:
        with self.lock:
            now = monotonic()
            self.stages.setdefault(self.stage, {})["s"] = round(self.stages.get(self.stage, {}).get("s", 0) + now - self.t0, 1)
            self.stage, self.t0 = stage, now
        log(f"STAGE {stage} {self.stages}")

    def sample(self) -> None:
        roll = dict(tm.tree_rollups()) if tm.has_procfs() else {}
        own = roll.get(os.getpid(), {})
        vals = {"tree_pss": sum(r["Pss"] for r in roll.values()), "own_anon": own.get("Pss_Anon", 0), "own_file": own.get("Pss_File", 0), "workers_pss": sum(r["Pss"] for p, r in roll.items() if p != os.getpid()),
                "arrow": pa.total_allocated_bytes(), "duck": self.duck(), "disk_used": shutil.disk_usage(STAGE).used if STAGE.exists() else 0}
        with self.lock:
            d = self.stages.setdefault(self.stage, {})
            for k, v in vals.items():
                d[k] = max(d.get(k, 0), v)

    def loop(self) -> None:
        while not self.stop.wait(2.0):
            try:
                self.sample()
            except Exception as e:  # a gauge must not fail the work it measures: its read failing is said, not raised
                log(f"sampler: {e!r}")


class Evictor:
    """The spools matching `pattern` out of the page cache every 30 s: a real cut (spools several times the RAM left) barely caches them, a quarter of one here
    would fit whole, so reads are timed from the SSD in both formats."""

    def __init__(self, work: Path, pattern: str):
        self.work, self.pattern = work, pattern
        self.stop = Event()
        Thread(target=self.loop, daemon=True).start()

    def loop(self) -> None:
        while True:
            evict(self.work.glob(self.pattern))
            if self.stop.wait(30.0):
                return


def child(*argv: str) -> None:
    """This script in a fresh process, unpinned unless `replay`; its output in the log."""
    t0 = monotonic()
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv], stdout=subprocess.PIPE, text=True, env=env)
    for line in r.stdout.splitlines():
        log(line)
    with open(RESULTS / "children.jsonl", "a") as f:
        for line in r.stdout.splitlines():
            f.write(line + "\n")
    log(f"child {argv[:2]} rc={r.returncode} in {monotonic() - t0:.0f}s")


def run_mode(mode: str) -> None:
    pin(CPUS)
    tm.arrow_returns_freed()
    RESULTS.mkdir(parents=True, exist_ok=True)
    SpoolWriter.mode = mode
    ist.pq = PqShim()
    ist.RunSpool = IpcRunSpool if mode == "ipc" else KeptRunSpool
    if mode == "ipc":
        ist._file_rows = ipc_file_rows
        tm.worker_init = ipc_worker_init

    root = Path(os.environ["BENCH_GEN_DIR"]) if "BENCH_GEN_DIR" in os.environ else Path(MOUNT) / ist.PREFIX / GEN
    tmp = STAGE / "tmp" / mode
    con = sn.connect(4, f"{RACE_DUCK}B", tmp)
    con.execute(tm.duck_sql(RACE_DUCK))
    budget = ist.run_budget(con, RACE_AT_ONCE * WORKERS)
    budget = int(os.environ.get("BENCH_BUDGET", budget))
    con.execute(tm.duck_sql(RACE_DUCK // RACE_AT_ONCE))
    scans = json.loads((root / "scans.json").read_text())
    has_gen = ist.has_generation(scans) or ist.source_has_generation(con, f"{root}/pvl/r*.parquet")
    schema = ist.served_schema(SORT, has_gen, ist.has_churn(scans))
    sub = ist.SORTS[SORT][0]
    files = sorted(str(p) for p in (root / sub).glob("r*.parquet"))
    files = files[:int(os.environ.get("BENCH_FILES", len(files)))]
    stamps = [s["ts"] for s in scans["scans"]]
    out = STAGE / "bench" / mode / "served" / f"{SORT}.parquet"
    work = out.with_name(f".{out.name}.cut")
    log(f"mode {mode}: {len(files)} range files, run budget {budget:,} B, share {RACE_SHARE:,}, DuckDB {tm.duck_sql(RACE_DUCK // RACE_AT_ONCE)}, affinity "
        f"{sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else '-'}, nofile {__import__('resource').getrlimit(__import__('resource').RLIMIT_NOFILE)}")

    samp = Sampler(con)
    samp.t.start()
    real_doing, real_cut_plan, real_summary = tm.doing, ist.cut_plan, ist.piece_summary
    evictors: list[Evictor] = []
    piece_stats: list[dict] = []

    def doing(what, step):
        real_doing(what, step)
        if step == "runs":
            samp.enter("runs")
        elif step == "pieces":
            samp.enter("pieces")
            evictors.append(Evictor(work, "run-*.parquet"))
        elif step == "write":
            samp.enter("write")
            evictors.append(Evictor(work, "piece-*.parquet"))
        elif step == "join":
            samp.enter("join")
            while evictors:
                evictors.pop().stop.set()

    def summary(stats):
        if len(stats) > len(piece_stats):
            piece_stats[:] = stats
        return real_summary(stats)

    def cut_plan(segs, rg_rows, k):
        samp.enter("after-pieces")
        while evictors:
            evictors.pop().stop.set()
        after_pieces(con, mode, work, root, budget, stamps, piece_stats, has_gen)
        samp.enter("cut-plan")
        return real_cut_plan(segs, rg_rows, k)

    tm.doing, ist.cut_plan, ist.piece_summary = doing, cut_plan, summary
    t0 = monotonic()
    doc = ist.write_served(con, files, SORT, out, schema, stamps=stamps, procs=WORKERS, budget=budget, piece_rows=RACE_SHARE,
                           part_rows=int(os.environ.get("BENCH_PART_ROWS", ist.CUT_PART_ROWS)))
    wall = monotonic() - t0
    samp.enter("done")
    samp.stop.set()
    h = hashlib.md5()
    with open(out, "rb") as f:
        while b := f.read(1 << 24):
            h.update(b)
    result(f"{mode}-final", {"doc": doc, "wall_s": round(wall, 1), "md5": h.hexdigest(), "served_bytes": out.stat().st_size, "stages": samp.stages,
                             "digest_s_excluded": samp.stages.get("after-pieces", {}).get("s")})
    groups = out.with_name(f"{out.stem}.groups.parquet")
    h2 = hashlib.md5(groups.read_bytes()).hexdigest() if groups.exists() else None
    log(f"DIGEST {mode} {SORT}.parquet {out.stat().st_size} {h.hexdigest()} groups {h2}")
    shutil.rmtree(out.parent, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)


def after_pieces(con, mode: str, work: Path, root: Path, budget: int, stamps: list[int], piece_stats: list[dict], has_gen) -> None:
    """Every spool is on disk, the product is between its pieces and its write: what needs them."""
    with WRITES_LOCK:
        writes = list(WRITES)
    result(f"{mode}-writes", writes)
    runs = [w for w in writes if w["file"].startswith("run-")]
    pieces = [w for w in writes if w["file"].startswith("piece-")]

    def agg(ws: list[dict]) -> dict:
        b = sorted(w["bytes"] for w in ws) or [0]
        return {"files": len(ws), "bytes": sum(b), "rows": sum(w["rows"] for w in ws), "batches": sum(w["batches"] for w in ws), "write_s_sum": round(sum(w["write_s"] for w in ws), 1),
                "bytes_p50": b[len(b) // 2], "bytes_max": b[-1]}

    batch_rows: Counter = Counter()
    for w in runs:
        for n, c in w["batch_rows"]:
            batch_rows[n] += c
    result(f"{mode}-spools", {"runs": agg(runs), "pieces": agg(pieces), "run_batch_rows_top": batch_rows.most_common(6)})
    result(f"{mode}-pieces", piece_stats)

    def q(xs, p):
        ys = sorted(xs)
        return round(ys[min(len(ys) - 1, int(p * len(ys)))], 3) if ys else 0

    if piece_stats:
        result(f"{mode}-piece-quantiles", {k: {"sum": round(sum(s[k] for s in piece_stats), 1), "p50": q([s[k] for s in piece_stats], .5), "p90": q([s[k] for s in piece_stats], .9), "max": max(s[k] for s in piece_stats)}
                                           for k in ("rows", "spans", "groups", "decoded", "read", "sort", "write", "s")})
    # Other sorts' spool shape, for the whole cut's disk: a sample of their (non-merged, 64K-row) runs, `_sort_run` as `write_served` calls it.
    for other in ("path", "slices"):
        sub = ist.SORTS[other][0]
        fs = sorted(str(p) for p in (root / sub).glob("r*.parquet"))
        if not fs:
            continue
        try:
            rels = ist.cut_runs(fs, budget)
            picks = rels[:: max(1, len(rels) // 12)][:12]
            scans = json.loads((root / "scans.json").read_text())
            names = ist.served_schema(other, has_gen, ist.has_churn(scans)).names
            sel = f"{ist.qcols(names)}, {ist.seg_sql(stamps)} AS __seg"
            sample = work / f"sample-{other}"
            sample.mkdir(exist_ok=True)
            with WRITES_LOCK:
                mark = len(WRITES)
            sn.each(con, lambda cur, a: ist._sort_run(cur, f"SELECT {sel} FROM {a[1]} ORDER BY __seg, {ist.SORTS[other][1]}", sample / f"s-{a[0]:05d}.parquet", ist.CUT_SPOOL_RG, other),
                    list(enumerate(picks)), WORKERS)
            with WRITES_LOCK:
                got = WRITES[mark:]
            result(f"{mode}-sample-{other}", {"of_runs": len(rels), "files": got})
        finally:
            shutil.rmtree(work / f"sample-{other}", ignore_errors=True)
    # Spans of the real pass, for the replays.
    spans_file = RESULTS / f"{mode}-spans.json"
    spans_file.write_text(json.dumps(SPANS))
    try:
        for case in (["ipc-os", "ipc-mmap"] if mode == "ipc" else ["parquet"]):
            child("rss", case, str(work))
        for case in (["held", "open", "mmap"] if mode == "ipc" else ["parquet"]):
            child("replay", case, str(spans_file))
        if mode == "ipc":
            child("dicts", str(work))
    finally:
        spans_file.unlink(missing_ok=True)
    result(f"{mode}-digests", digest_tree(work, mode == "ipc"))


def compare() -> None:
    a = {d["file"]: d for d in json.loads((RESULTS / "ipc-digests.json").read_text())}
    b = {d["file"]: d for d in json.loads((RESULTS / "parquet-digests.json").read_text())}
    same = [f for f in a if f in b and a[f]["cols"] == b[f]["cols"] and a[f]["rows"] == b[f]["rows"]]
    differ = [f for f in a if f in b and f not in same]
    fa, fb = json.loads((RESULTS / "ipc-final.json").read_text()), json.loads((RESULTS / "parquet-final.json").read_text())
    schemas = {(a[f]["file"].split("-")[0]): (a[f]["schema"] == b[f]["schema"], a[f]["schema"], b[f]["schema"]) for f in a if f in b}
    result("compare", {"files": [len(a), len(b)], "same": len(same), "differ": differ[:20], "only_ipc": sorted(set(a) - set(b))[:5], "only_parquet": sorted(set(b) - set(a))[:5],
                       "rows": [sum(d["rows"] for d in a.values()), sum(d["rows"] for d in b.values())], "schemas": schemas, "served_md5_equal": fa["md5"] == fb["md5"], "md5": [fa["md5"], fb["md5"]],
                       "served_bytes": [fa["served_bytes"], fb["served_bytes"]]})


def main() -> None:
    cmd = sys.argv[1]
    if cmd in ("ipc", "parquet"):
        run_mode(cmd)
    elif cmd == "compare":
        compare()
    elif cmd == "rss":
        pin(None)
        rss_case(sys.argv[2], Path(sys.argv[3]))
    elif cmd == "replay":
        pin(CPUS)
        replay_case(sys.argv[2], sys.argv[3])
    elif cmd == "dicts":
        pin(None)
        dicts_case(Path(sys.argv[2]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
