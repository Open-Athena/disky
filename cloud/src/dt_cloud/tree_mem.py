"""One memory budget for a whole process tree (`-M`): a compaction's DuckDB limit is what the budget leaves once
everything else the tree holds is counted — this process's own pages, each spawned worker's, and the Arrow/Python
side's buffers, each bounded by construction (a stated number of row groups or batches × the bytes a row) — so `-M`
sizes the box, not just DuckDB:

    -M = base + procs × proc_base + arrow + duck

- `base`: this process's resident bytes when the budget is made (`own_bytes`: the interpreter, its imported libraries,
  whatever it already holds).
- `proc_base`: a spawned worker's own bytes before its task (`spawned_bytes`: one spawned as the cut's are — running this
  process's `__main__`, importing the cut's modules — reports its private pages; the shared libraries' pages are counted
  once, in `base`). `procs`: the workers alive at once.
- `arrow`: the phase's Arrow/Python buffers (`fold_arrow`, `cut_plan`): row groups and batches × `rb`, the inputs'
  largest estimated in-memory bytes a row (`interval_compact.row_bytes`: uncompressed bytes + `STRING_T` a string, at
  least what Arrow holds: a 4-byte offset and the bytes, a dictionary's values ≤ 12 bytes).
- `duck`: DuckDB's `memory_limit`, set per phase (`SET memory_limit`), at least `DUCK_MIN`; a budget that can't leave it
  that much is refused (`TooSmall`, naming the smallest `-M` that fits).

Not counted, because not a function of the settings but of the sort's size: the served sort's footer. The cut's join
(`parquet_pages.concat`) and its groups sidecar (`interval_store.write_groups`) hold a part's footer bytes (~120 B a
column chunk; pyarrow's parsed footer ~50 B) and decode it one row group at a time (`row_group_stream`), the sidecar
`GROUPS_RG` groups at a time: ~0.3 GiB at a billion rows (2.3M column chunks), where the decoded footer was ~40× that.

Freed Arrow memory goes back to the OS at once (`arrow_returns_freed`: jemalloc's decay off, in this process and every
worker), so a phase's peak is what it holds, not what its predecessors held.

Every entry that ends in a served-sort cut takes `-M` this way (`interval-store cut`/`build`/`add`, the `backfill-*`s,
`migrate-object-version`, the staged `publish`/`carry`/`compact`): its DuckDB stages run under `stage_mem` (what this
process leaves), each cut under its `Budget.cut` plan (`interval_store.plan_cut`). The check is measured, not modelled:
each such entry runs under `TreeWatch`, which samples the tree's proportional resident bytes (`tree_pss`) every
`WATCH_S` and logs `<entry>: peak tree PSS X MiB of budget M MiB` at its end; with `$DT_TREE_MEM_ASSERT` set, a peak past
`-M` exits non-zero. Named blocks (`phase`: each sort, the cut's plan, the churn's counts, `add`'s stages, an append's steps) get their own peak,
logged before the overall line: what a stage's real tree held, next to the plan that sized it. `at` names what the tree is
working on (an append's key range), `doing` what a concurrent block is at (a cut's sort and its pass) and `gauges` what else
to read with each sample, so a peak's line says where it happened and how much of it each holder accounts for: DuckDB's and
Arrow's own totals, and over a cut (`tree_parts`, `duck_parts`) the tree split by process, what each process's PSS is backed
by, and what its heap and Arrow pool hold; a spawned worker says its own (`worker_init`), which nothing outside it can read.
Under gauges a watch also logs every `SERIES_S` where the tree stands, and each phase what each gauge reached by itself."""
from __future__ import annotations

import ctypes
import os
import resource
import shutil
import struct
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from tempfile import mkdtemp
from threading import Event, Lock, Thread
from time import monotonic, sleep

import pyarrow as pa

MIB = 1 << 20
#: The least DuckDB limit a phase runs under: its sorts and folds spill beyond it, but each needs some memory of its own.
DUCK_MIN = 128 * MIB

# ── The fold (`interval_compact.compact_range`, per job) ───────────────────
#: Writers a range's fold keeps open at once (`pvl`, `pv`, `rd`; the owner and attribution passes open one each).
FOLD_WRITERS = 3
#: Row groups an idle open `static_names.SortedWriter` pins: its pending rows (< a group) are slices of at most one
#: fetched batch (≤ a group: `_batches(…, rg_rows)`).
WRITER_IDLE = 1
#: Row groups the writer being fed holds: pending slices pinning ≤ 2 fetched batches (the one a remainder was cut from,
#: the one being read), the group's combined copy, and its encoding.
WRITER_ACTIVE = 4
#: A fold job's row groups: one writer fed, the others idle.
FOLD_GROUPS = WRITER_ACTIVE + (FOLD_WRITERS - 1) * WRITER_IDLE

# ── A scan's append (`interval_append.append_range`, one range at a time) ──
#: Range-file groups' worth of rows its one open writer holds: pending slices pinning ≤ 2 fetched batches (2 groups each:
#: `static_names._batches`' default), the group's combined copy, and its encoding.
APPEND_GROUPS = 6

# ── The served sorts' cut (`interval_store.write_served`) ──────────────────
#: Batches a run's or a piece's sort holds in this process (`_sort_run`): the fetched batch, and the spool writer's
#: encoding of it.
SORT_COPIES = 2
#: Spool groups' worth of rows a writing worker holds while it reads (`cut_part` → `_file_rows`): the group's compressed
#: column chunks (pyarrow reads a chunk whole), their decompressed pages, the decoded group, the batch sliced from it.
#: Measured: 2.9–3.9 × a spool group's `rb` bytes past the worker's imports (64Ki-row spool groups, 270–440 B rows).
PART_SPOOL = 4
#: Served groups a writing worker holds: pending rows (≤ a group + a spool batch), the group's combined copy, its encoding.
PART_GROUPS = 3
#: Bytes the parquet writer keeps per column chunk it has written until it closes (`pq.ParquetWriter`: the chunk's
#: metadata and statistics, its page-index builders): a worker's part grows by this × columns every served group.
#: Measured ~2.1 KiB (one-page groups of 19 columns: 36–41 KiB a group).
CHUNK_META = 2304
#: Bytes it keeps per page of each column (its OffsetIndex location, its ColumnIndex entry): measured ~48 B, plus each
#: statistics string column's min and max (`interval_store.STAT_COLS`; `group_meta`'s `stat_bytes`).
PAGE_META = 48
#: The buffer a joining worker copies through (`parquet_pages._copy_range`, without `copy_file_range`).
CONCAT_BUF = 1 << 24
#: The share of `-M` past `base` and the writers' metadata (which the workers don't change: `Budget.cut`) the cut's workers
#: (their `proc_base` and buffers) and its sorts' batches may take; DuckDB's sorts get the rest. Workers only add speed:
#: fewer of them still cut the same bytes.
WORKER_SHARE = 1 / 2


class TooSmall(SystemExit):
    """A budget that can't leave DuckDB `DUCK_MIN` under the phase's settings."""


def _status(field: str) -> int | None:
    """A `/proc/self/status`-style `kB` field's bytes (Linux), else None."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) << 10
    except FileNotFoundError:  # legitimate: no procfs (macOS); callers fall back to `ru_maxrss`
        return None
    return None


def peak_bytes() -> int:
    """This process's peak resident bytes (`ru_maxrss`: bytes on macOS, KiB elsewhere)."""
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return m if sys.platform == "darwin" else m << 10


def own_bytes() -> int:
    """This process's resident bytes now (Linux `VmRSS`; elsewhere its peak, an upper bound)."""
    got = _status("VmRSS")
    return got if got is not None else peak_bytes()


def private_bytes() -> int:
    """This process's private resident bytes now (Linux `smaps_rollup`'s `Private_Clean` + `Private_Dirty`: what a
    process with the same imports adds to a tree; elsewhere `own_bytes`, an upper bound)."""
    try:
        with open("/proc/self/smaps_rollup") as f:
            fields = {p[0]: int(p[1]) << 10 for p in (line.split() for line in f) if len(p) == 3 and p[0].endswith(":") and p[2] == "kB"}
    except FileNotFoundError:  # legitimate: no procfs (macOS)
        return own_bytes()
    return fields["Private_Clean:"] + fields["Private_Dirty:"]


def arrow_returns_freed() -> None:
    """Freed Arrow memory back to the OS at once: jemalloc (pyarrow's default pool on Linux) otherwise keeps freed pages
    for its decay time, so a phase's RSS would carry its predecessors' buffers. Other pools (mimalloc, the system's)
    have no such knob (`NotImplementedError`); theirs is left as is."""
    try:
        pa.jemalloc_set_decay_ms(0)
    except NotImplementedError:  # legitimate: pyarrow's pool isn't jemalloc here
        pass


class _MallInfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]


@cache
def _mallinfo2():
    fn = ctypes.CDLL(None).mallinfo2
    fn.restype = _MallInfo2
    return fn


def heap_bytes() -> tuple[int, int]:
    """glibc's heap in this process: `(in use, free but kept)`, where the C++ side's own allocations live (a parquet
    writer's metadata, a parsed footer), which Arrow's pool doesn't count. Glibc ≥ 2.33; others raise (a gauge's read fails)."""
    m = _mallinfo2()()
    return m.uordblks + m.hblkhd, m.fordblks


#: The dir a running `TreeWatch` made for its tree's workers to say what only they can read (`_report_to_watch`).
REPORT_ENV = "DT_TREE_REPORT_DIR"
#: A worker's report: its Arrow pool's bytes, its heap's in use and free (`heap_bytes`).
_REPORT = struct.Struct("<qqq")


def _report_to_watch(label: str) -> None:
    """Under a `TreeWatch` (`REPORT_ENV`, inherited), this process's Arrow pool and heap go to `<dir>/<label>.<pid>` every
    `WATCH_S`, overwritten in place, from a daemon thread, for `tree_parts`. Nothing else in the tree can read them."""
    d = os.environ.get(REPORT_ENV)
    if not d:
        return
    fd = os.open(f"{d}/{label}.{os.getpid()}", os.O_WRONLY | os.O_CREAT, 0o600)

    def run() -> None:
        while True:
            os.pwrite(fd, _REPORT.pack(pa.total_allocated_bytes(), *heap_bytes()), 0)
            sleep(WATCH_S)

    Thread(target=run, name="tree-report", daemon=True).start()


def worker_init(label: str = "-") -> None:
    """A spawned cut worker's initializer: one Arrow thread (the cores are split between the workers), freed memory
    returned (`arrow_returns_freed`), and its own bytes told to a running watch under `label` (its sort)."""
    pa.set_cpu_count(1)
    arrow_returns_freed()
    _report_to_watch(label)


def _spawned_private() -> int:
    from . import interval_store, parquet_pages  # noqa: F401 — the cut's tasks'

    return private_bytes()


@cache
def spawned_bytes() -> int:
    """A spawned cut worker's private bytes once its modules are imported (`worker_init`'d, as `write_served`'s pool
    spawns them), measured once per process in a worker of its own."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    with ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=worker_init) as ex:
        return ex.submit(_spawned_private).result()


def fold_arrow(jobs: int, rg_rows: int, rb: int) -> int:
    """The fold's Arrow bytes: `jobs` ranges at once, each `FOLD_GROUPS` row groups of `rg_rows` rows of `rb` bytes."""
    return max(1, jobs) * FOLD_GROUPS * rg_rows * rb


def part_bytes(spool_rows: int, rg_rows: int, rb: int) -> int:
    """A writing worker's buffers: `PART_SPOOL` spool groups (≤ `spool_rows` rows) and `PART_GROUPS` served groups."""
    return (PART_SPOOL * spool_rows + PART_GROUPS * rg_rows) * rb


def group_meta(cols: int, pages: int, stat_bytes: int = 0) -> int:
    """What the parquet writer keeps per served row group it has written, until it closes: `CHUNK_META` a column chunk,
    `PAGE_META` a page of each column, and the min and max of each page of its statistics string columns (`stat_bytes`
    a value, summed over them)."""
    return cols * CHUNK_META + pages * (cols * PAGE_META + 2 * stat_bytes)


def worker_bytes(spool_rows: int, rg_rows: int, rb: int, groups: int = 0, meta: int = 0) -> int:
    """A cut worker's bytes past its imports: the more of its two tasks' buffers (write, join), plus what its writer
    kept for the `groups` served groups of its part (`meta` each, `group_meta`), which the join doesn't free (the
    writer's metadata is the C++ heap's, not Arrow's pool)."""
    return max(part_bytes(spool_rows, rg_rows, rb), CONCAT_BUF) + groups * meta


def sort_bytes(procs: int, spool_rows: int, rb: int) -> int:
    """One sort's run or piece batches in this process: its `procs` runs (or pieces) at once, `spool_rows` each (a
    merged sort's run spools' groups are no larger), `SORT_COPIES` each."""
    return SORT_COPIES * procs * spool_rows * rb


@dataclass(frozen=True)
class CutPlan:
    """The served sorts' cut under a budget: `at_once` sorts, each over `procs` worker processes (and `procs` runs or
    pieces sorting at once) spooling `spool_rows`-row groups; `arrow` the Arrow bytes and `duck` DuckDB's limit that
    leaves."""

    at_once: int
    procs: int
    spool_rows: int
    arrow: int
    duck: int


@dataclass(frozen=True)
class Budget:
    """`-M` (`total`) over the process tree, with this process's own bytes (`base`) and a worker's (`proc_base`) as
    measured when it was made (`Budget.measure`)."""

    total: int
    base: int
    proc_base: int

    @classmethod
    def measure(cls, total: int) -> Budget:
        arrow_returns_freed()
        return cls(total, own_bytes(), spawned_bytes())

    def duck(self, arrow: int, procs: int = 0, what: str = "") -> int:
        """DuckDB's limit: `total − base − procs × proc_base − arrow`; `TooSmall` below `DUCK_MIN`."""
        d = self.total - self.base - procs * self.proc_base - arrow
        if d < DUCK_MIN:
            need = self.total - d + DUCK_MIN
            raise TooSmall(f"memory budget {self.total / MIB:,.0f} MiB too small{f' for {what}' if what else ''}: this process "
                           f"{self.base / MIB:,.0f} MiB, {procs} workers × {self.proc_base / MIB:,.0f} MiB, Arrow buffers "
                           f"{arrow / MIB:,.0f} MiB leave DuckDB {d / MIB:,.0f} MiB (< {DUCK_MIN / MIB:,.0f}); needs ≥ {need / MIB:,.0f} MiB")
        return d

    def fold(self, jobs: int, rg_rows: int, rb: int, rows: int | None = None) -> tuple[int, int]:
        """The fold's `(arrow, duck)`: `fold_arrow(jobs, min(rg_rows, rows), rb)` (`rows`: the inputs', which no group
        exceeds), DuckDB the rest (no workers)."""
        a = fold_arrow(jobs, min(rg_rows, rows or rg_rows), rb)
        return a, self.duck(a, 0, f"{jobs} fold jobs")

    def append(self, rg_rows: int, rb: int) -> tuple[int, int]:
        """A scan's append's `(arrow, duck)`: `APPEND_GROUPS` groups of `rg_rows` rows of `rb` bytes (the delta or state
        file being written), DuckDB the rest (no workers; each run's cut re-splits it, `interval_store.plan_cut`)."""
        a = APPEND_GROUPS * rg_rows * rb
        return a, self.duck(a, 0, "the append")

    def cut(self, at_once: int, cores: int, rg_rows: int, rb: int, spool_rows: int, rows: int | None = None, *,
            meta: int = 0, sort_rows: int | None = None) -> CutPlan:
        """The cut's plan: the most workers (≤ `cores // at_once` a sort) whose bytes — `at_once × (procs × (proc_base +
        worker_bytes) + sort_bytes)`, each row count charged at most `rows` (the sorts' rows: no batch or group holds
        more) — fit `WORKER_SHARE` of `total − base`, past the metadata their writers keep, and leave DuckDB `DUCK_MIN`; none
        fitting: one. A worker's part is its sort's served groups (of `sort_rows`, default `rows`, the most any one sort
        has) over the workers, each kept by its writer (`meta` bytes, `group_meta`) until the part closes: all of a sort's
        groups' metadata at once, however many workers. So that metadata doesn't choose them: it comes off before the
        share, and only what a rounded-up part adds to it counts as theirs. It's still charged: DuckDB gets the rest, which
        its runs and pieces (`interval_store.run_budget`) are sized to."""
        cap = (lambda r: min(r, rows)) if rows else (lambda r: r)  # noqa: E731
        top = max(1, cores // max(1, at_once))
        groups = -(-(sort_rows or rows or 0) // rg_rows)
        fixed = at_once * groups * meta
        left = self.total - self.base - fixed
        room = min(left * WORKER_SHARE, left - DUCK_MIN)

        def cost(procs: int) -> tuple[int, int]:
            sort = SORT_COPIES * cap(procs * spool_rows) * rb  # `sort_bytes`, capped
            arrow = at_once * (procs * worker_bytes(cap(spool_rows), cap(rg_rows), rb, -(-groups // procs), meta) + sort)
            return arrow, arrow + at_once * procs * self.proc_base

        procs = next((p for p in range(top, 0, -1) if cost(p)[1] - fixed <= room), 1)
        arrow, _ = cost(procs)
        return CutPlan(at_once, procs, spool_rows, arrow, self.duck(arrow, at_once * procs, f"the cut ({at_once} sorts × {procs} workers)"))


def duck_sql(limit: int) -> str:
    """DuckDB's `SET memory_limit` to `limit` bytes (whole MiB, rounded down)."""
    return f"SET memory_limit='{limit // MIB}MiB'"


def stage_mem(tree: Budget, what: str = "") -> str:
    """DuckDB's limit for an entry's stages outside the fold and the cut (whose Arrow side is a batch or two, freed at
    once): all of `-M` this process leaves (`Budget.duck` with no workers or buffers), as `static_names.connect` takes it."""
    return f"{tree.duck(0, 0, what) // MIB}MiB"


# ── The check: the tree's peak, measured ───────────────────────────────────

#: Set (to anything but `""` or `0`) to make `TreeWatch` exit non-zero when the tree's peak passes the budget.
ASSERT_ENV = "DT_TREE_MEM_ASSERT"
#: Seconds between `TreeWatch`'s samples.
WATCH_S = 1.0


def has_procfs() -> bool:
    return os.path.exists("/proc/self/smaps_rollup")


def tree_rollups(root: int | None = None) -> Iterator[tuple[int, dict[str, int]]]:
    """`(pid, its `smaps_rollup`'s byte fields: `Pss`, `Pss_Anon`, …)` of `root` (default this process) and every descendant.
    Linux only (`has_procfs`)."""
    todo = [os.getpid() if root is None else root]
    while todo:
        pid = todo.pop()
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                fields = {p[0][:-1]: int(p[1]) << 10 for p in (line.split() for line in f) if len(p) == 3 and p[2] == "kB"}
            for task in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{task}/children") as f:
                    todo += [int(c) for c in f.read().split()]
        except (FileNotFoundError, ProcessLookupError):  # legitimate: the process exited between reads
            continue
        if fields:  # legitimate: empty, a process exiting as it was read
            yield pid, fields


def tree_pss(root: int | None = None) -> int:
    """The proportional resident bytes (`smaps_rollup`'s `Pss`) of `root` (default this process) and every descendant:
    the tree's share of physical memory, a page shared by several of them (their libraries) counted once across them.
    Linux only (`has_procfs`)."""
    return sum(fields["Pss"] for _, fields in tree_rollups(root))


def _worker_reports(pids: set[int]) -> Iterator[tuple[str, int, tuple[int, int, int]]]:
    """`(label, pid, report)` of each live worker in `pids` that has reported to the running watch (`_report_to_watch`)."""
    d = os.environ.get(REPORT_ENV)
    for name in os.listdir(d) if d else []:
        label, _, pid = name.rpartition(".")
        if int(pid) in pids:
            with open(f"{d}/{name}", "rb") as f:
                got = f.read(_REPORT.size)
            if len(got) == _REPORT.size:  # legitimate: short, a worker that hasn't written its first report yet
                yield label, int(pid), _REPORT.unpack(got)


def tree_parts() -> dict[str, int]:
    """A gauge of what the tree's PSS (`tree_pss`) is made of, read from one walk of it. `own`: this process, and of it
    `own anon`, `own file` (file-backed pages, which the kernel can drop), `own heap`, `own heap free` (`heap_bytes`).
    `workers`: every descendant, the same, and `workers Arrow` (each one's own pool; this process's is the `Arrow` gauge).
    `<label> workers`, `<label> heap`: those of the workers `worker_init`'d under a label (a cut's sort)."""
    rollups = dict(tree_rollups())
    own = rollups.pop(os.getpid())
    heap, free = heap_bytes()
    parts = {"own": own["Pss"], "own anon": own["Pss_Anon"], "own file": own["Pss_File"], "own heap": heap, "own heap free": free,
             "workers": sum(r["Pss"] for r in rollups.values()), "workers anon": sum(r["Pss_Anon"] for r in rollups.values()),
             "workers file": sum(r["Pss_File"] for r in rollups.values()), "workers Arrow": 0, "workers heap": 0, "workers heap free": 0}
    labels: Counter[str] = Counter()
    for label, pid, (arrow, used, kept) in sorted(_worker_reports(set(rollups))):
        parts["workers Arrow"] += arrow
        parts["workers heap"] += used
        parts["workers heap free"] += kept
        labels[f"{label} workers"] += rollups[pid]["Pss"]
        labels[f"{label} heap"] += used
    return {**parts, **labels}


@dataclass
class PhasePeak:
    """A `phase`'s peak tree PSS, over the `samples` taken while it ran; `at`, `doing` and `gauges`: what `at` and `doing`
    named and what `gauges` read at that peak's sample; `highs`: the most each gauge read in any of them."""

    peak: int = 0
    samples: int = 0
    at: str | None = None
    gauges: dict[str, int | None] = field(default_factory=dict)
    doing: dict[str, str] = field(default_factory=dict)
    highs: dict[str, int] = field(default_factory=dict)

    def add(self, pss: int, at: str | None = None, gauges: dict[str, int | None] | None = None, doing: dict[str, str] | None = None) -> None:
        if pss > self.peak:
            self.peak, self.at, self.gauges, self.doing = pss, at, gauges or {}, doing or {}
        for k, v in (gauges or {}).items():
            if v is not None:
                self.highs[k] = max(self.highs.get(k, 0), v)
        self.samples += 1


#: The running `TreeWatch`es, innermost last: what `phase` tags.
_WATCHES: list[TreeWatch] = []


@contextmanager
def phase(name: str) -> Iterator[None]:
    """The block is `name` to the innermost running `TreeWatch` (none: nothing; a call from anywhere, any thread).
    Blocks may nest or overlap: a sample counts towards every phase running then, so overlapping sorts' peaks are the
    tree's while each ran, not each's own. A name run again keeps its largest peak."""
    w = _WATCHES[-1] if _WATCHES else None
    if w is None:
        yield
        return
    w._begin(name)
    try:
        yield
    finally:
        w._end(name)


@contextmanager
def at(label: str) -> Iterator[None]:
    """The innermost running `TreeWatch`'s samples in the block are at `label` (none running: nothing): each phase's line
    names the label of its peak, and the watch's end the labels with the largest peaks (`AT_TOP`)."""
    w = _WATCHES[-1] if _WATCHES else None
    if w is None:
        yield
        return
    prev, w.at_label = w.at_label, label
    try:
        yield
    finally:
        w.at_label = prev


def doing(what: str, step: str | None) -> None:
    """From now the innermost running `TreeWatch`'s samples say `what` is at `step`, until the next call for it (`None`: it's
    done; none running: nothing). For blocks that overlap, which one `at` label can't name: a peak's line lists them."""
    w = _WATCHES[-1] if _WATCHES else None
    if w is None:
        return
    with w._lock:
        if step is None:
            w.doings.pop(what, None)
        else:
            w.doings[what] = step


@contextmanager
def gauges(**fns: Callable[[], int | dict[str, int]]) -> Iterator[None]:
    """The innermost running `TreeWatch` also reads these (name → bytes, or a read of several: name → bytes) with each
    sample in the block (none running: nothing): each phase's line gives their values at its peak, or says a read failed
    (`TreeWatch._read_gauges`), and another the most each reached."""
    w = _WATCHES[-1] if _WATCHES else None
    if w is None:
        yield
        return
    prev, w.gauge_fns = w.gauge_fns, {**w.gauge_fns, **fns}
    try:
        yield
    finally:
        w.gauge_fns = prev


def duck_bytes(con) -> Callable[[], int]:
    """A gauge of the bytes DuckDB's buffer manager holds (`duckdb_memory()`: what `memory_limit` limits), read on its own
    cursor, so from the watch's thread while `con` runs a query."""
    cur = con.cursor()
    return lambda: int(cur.execute("SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()").fetchone()[0])


def duck_parts(con) -> Callable[[], dict[str, int]]:
    """A gauge of `duck_bytes`' total (`DuckDB`), its tags past `DUCK_TAG_MIN` (`DuckDB <tag>`) and what DuckDB has spilled
    (`DuckDB spill`), read as `duck_bytes` reads."""
    cur = con.cursor()

    def read() -> dict[str, int]:
        rows = cur.execute("SELECT tag, memory_usage_bytes, temporary_storage_bytes FROM duckdb_memory()").fetchall()
        return {"DuckDB": sum(int(r[1]) for r in rows), **{f"DuckDB {r[0]}": int(r[1]) for r in sorted(rows) if r[1] >= DUCK_TAG_MIN},
                "DuckDB spill": sum(int(r[2]) for r in rows)}

    return read


def cut_gauges(con) -> dict[str, Callable[[], int | dict[str, int]]]:
    """What a cut's watch reads with each sample (`gauges`): DuckDB's parts, this process's Arrow pool, the tree's parts."""
    return {"DuckDB": duck_parts(con), "Arrow": pa.total_allocated_bytes, "tree": tree_parts}


#: Labels `TreeWatch` logs at its end, largest peak first.
AT_TOP = 5
#: A DuckDB memory tag holding less isn't worth its place on a line (`duck_parts`).
DUCK_TAG_MIN = 64 * MIB
#: Seconds between a watch's `now` lines, while it has gauges.
SERIES_S = 60.0


class TreeWatch:
    """The process tree's peak memory over a block, against `total` (`-M`): `tree_pss` sampled every `every` seconds on
    a daemon thread (and once at the end). On exit it logs `<what>: peak tree PSS X MiB of budget M MiB`, after a
    `<what>: phase <name>: …` line per `phase` the block ran (`phases`, in first-run order, sampled at least at their
    start and end) and `(untagged)` for the samples outside them (`untagged`); with `check` (default:
    `$DT_TREE_MEM_ASSERT` set) an overall peak past `total` exits non-zero (`SystemExit`), and a machine without procfs
    is refused at the start (it can't measure). Without procfs and `check` it logs that nothing was measured."""

    def __init__(self, total: int, what: str, *, every: float = WATCH_S, check: bool | None = None):
        self.total, self.what, self.every = total, what, every
        self.check = os.environ.get(ASSERT_ENV, "") not in ("", "0") if check is None else check
        self.peak, self.samples = 0, 0
        self.phases: dict[str, PhasePeak] = {}
        self.untagged = PhasePeak()
        self.at_label: str | None = None
        self.gauge_fns: dict[str, Callable[[], int | dict[str, int]]] = {}
        self.doings: dict[str, str] = {}
        self._said = 0.0
        self.at_peaks: dict[str, int] = {}
        self.gauge_fails: Counter[str] = Counter()
        self._gauge_lock = Lock()
        self._running: Counter[str] = Counter()
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def _read_gauges(self, fns: dict[str, Callable[[], int | dict[str, int]]]) -> dict[str, int | None]:
        """Each gauge's bytes (a read of several: each of its names'), None where its read failed (counted, and said on the
        phase's line). A gauge is a diagnostic,
        so its failure doesn't raise: on a phase's thread that would fail the work being measured, and on the watch's it
        would end the sampling, leaving a peak that understates the tree and still logs as normal."""
        read: dict[str, int | None] = {}
        with self._gauge_lock:  # a sample is on the watch's thread or a phase's: one cursor between them
            for name, fn in fns.items():
                try:
                    got = fn()
                    read.update(got if isinstance(got, dict) else {name: got})
                except Exception:
                    read[name] = None
                    self.gauge_fails[name] += 1
        return read

    def _sample(self) -> None:
        pss = tree_pss()
        at_ = self.at_label
        read = self._read_gauges(self.gauge_fns)
        with self._lock:
            self.peak = max(self.peak, pss)
            self.samples += 1
            if at_ is not None:
                self.at_peaks[at_] = max(self.at_peaks.get(at_, 0), pss)
            running = [name for name, n in self._running.items() if n]
            doings = dict(self.doings)
            for name in running:
                self.phases[name].add(pss, at_, read, doings)
            if not running:
                self.untagged.add(pss, at_, read, doings)
        if read and monotonic() - self._said >= SERIES_S:
            self._said = monotonic()
            print(f"{self.what}: now tree PSS {pss / MIB:,.0f} MiB ({100 * pss / self.total:.0f}%{self._detail(at_, doings, read)})", file=sys.stderr, flush=True)

    def _begin(self, name: str) -> None:
        with self._lock:
            self.phases.setdefault(name, PhasePeak())
            self._running[name] += 1
        self._sample()

    def _end(self, name: str) -> None:
        self._sample()
        with self._lock:
            self._running[name] -= 1

    def _gauge(self, name: str, value: int | None) -> str:
        return f"{name} unread ({self.gauge_fails[name]} failed)" if value is None else f"{name} {value / MIB:,.0f} MiB"

    def _detail(self, at_: str | None, doings: dict[str, str], read: dict[str, int | None]) -> str:
        steps = ", ".join(f"{k} {v}" for k, v in sorted(doings.items()))
        return f"{f'; at {at_}' if at_ else ''}{f'; doing {steps}' if steps else ''}{''.join(f'; {self._gauge(k, v)}' for k, v in read.items())}"

    def _line(self, p: PhasePeak) -> str:
        return (f"peak tree PSS {p.peak / MIB:,.0f} MiB ({100 * p.peak / self.total:.0f}%; {p.samples} samples{self._detail(p.at, p.doing, p.gauges)})"
                f"{' — OVER BUDGET' if p.peak > self.total else ''}")

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self._sample()

    def __enter__(self) -> TreeWatch:
        if not has_procfs():
            if self.check:
                raise SystemExit(f"{self.what}: ${ASSERT_ENV} set, but the tree's memory is read from procfs (Linux), which this machine lacks")
            return self
        self._t0 = monotonic()
        self._said = self._t0
        self._reports, self._outer_reports = mkdtemp(prefix="tree-watch-"), os.environ.get(REPORT_ENV)
        os.environ[REPORT_ENV] = self._reports
        self._sample()
        _WATCHES.append(self)
        self._thread = Thread(target=self._run, name="tree-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, typ, exc, tb) -> None:
        if self._thread is None:
            print(f"{self.what}: peak tree PSS not measured (no procfs); budget {self.total / MIB:,.0f} MiB", file=sys.stderr, flush=True)
            return
        self._stop.set()
        self._thread.join()
        _WATCHES.remove(self)
        self._sample()
        if self._outer_reports is None:
            del os.environ[REPORT_ENV]
        else:
            os.environ[REPORT_ENV] = self._outer_reports
        shutil.rmtree(self._reports)
        over = self.peak > self.total
        if self.phases:
            for name, p in [*self.phases.items(), ("(untagged)", self.untagged)]:
                print(f"{self.what}: phase {name}: {self._line(p)}", file=sys.stderr, flush=True)
                if p.highs:
                    print(f"{self.what}: phase {name}: each at its highest: " + "; ".join(f"{k} {v / MIB:,.0f} MiB" for k, v in p.highs.items()), file=sys.stderr, flush=True)
        if self.at_peaks:
            top = sorted(self.at_peaks.items(), key=lambda kv: (-kv[1], kv[0]))[:AT_TOP]
            print(f"{self.what}: largest of {len(self.at_peaks)}: " + ", ".join(f"{k} {v / MIB:,.0f} MiB" for k, v in top), file=sys.stderr, flush=True)
        print(f"{self.what}: peak tree PSS {self.peak / MIB:,.0f} MiB of budget {self.total / MIB:,.0f} MiB "
              f"({100 * self.peak / self.total:.0f}%; {self.samples} samples over {monotonic() - self._t0:.0f}s)"
              f"{' — OVER BUDGET' if over else ''}", file=sys.stderr, flush=True)
        if typ is None and over and self.check:
            raise SystemExit(f"{self.what}: peak tree PSS {self.peak / MIB:,.0f} MiB over its budget {self.total / MIB:,.0f} MiB (${ASSERT_ENV})")


def watch(mem: str, what: str) -> TreeWatch:
    """`TreeWatch` over an entry's `-M` (a DuckDB-style size: `4GB`, `100GB`)."""
    from .static_names import mem_bytes

    return TreeWatch(mem_bytes(mem), what)
