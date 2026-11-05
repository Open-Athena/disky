"""Offline: what a served sort's parquet writer settings cost and buy, from real bytes (no serving store, no credentials
beyond reading the files).

`settings-sample`: evenly spaced real row groups of a served sort, rewritten under several writer settings (a label's
setting is `none`, the pre-page writer, or a page size in rows, `interval_store._served_writer`), with per-column chunk and
page-index bytes. Same rows in the same groups; `site/bench/decode-bench.ts` times the site's decoder over them.

`settings-sim`: `readChanged`'s range reads (`site/functions/_lib/index.ts`), mirrored over a sort's real sidecars and real
page indexes, for requests `{a, b, dirs}` (stamps; other keys copied). Plan: `planChanged` over `levelRects(dirs)`. Modes:

- `whole`: the plain writer's real sidecar (`-a`): each kept group read whole (`chunkSpan`, `planRuns`), one wave.
- `p<N>`: the paged writer's real sidecar and page index (`-d`, N its page size): the index wave (`pageIxSpans`) and the data
  wave (`plan`'s merged ranges, `dataSpans`), and a group whose every page is kept read whole after them, as `readChangedPages`.
- `p<M>` for M a multiple of N (`-p`): **derived**: M-row pages' bounds are the union of N's (exact), their bytes N's × the
  sample's per-column chunk ratio, the index's and the file offsets scaled by the sample's ratios (`-S`). No such file exists.

A wave's GETs are `planRuns`' merged spans, its rounds ⌈gets / RUN_READS⌉ and its time `level_sim.wave_ms` (`-T`, `-R`):
modelled, not measured. Base tier only: a run's groups aren't read (runs are this generation's small deltas).
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from math import ceil
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from .image_sha import expect_sha_options
from .layout_sim import RUN_BYTES, RUN_GAP, RUN_READS
from .level_sim import THROUGHPUT_MIB_S, TTFB_MS, merged, wave_ms
from .parquet_pages import _Reader, iter_chunk_indexes

err = partial(print, file=sys.stderr, flush=True)

#: The site's `IV_ROW_COLUMNS` (`IV_CLASS_COLS` included), and `IV_CHURN_COLS`.
ROW_COLUMNS = ["depth", "path", "usr", "vf", "vt", "kind", "size", "n_files", "n_children", "n_desc", "mtime", "wts", "wb", "c2", "c3", "c4", "us", "last_read"]
CHURN_COLUMNS = ["churn_cum", "churn_end"]
#: The site's `PAGE_PLAN`.
PAGE_PLAN = ("depth", "path", "vf", "vt")
SIDECAR_COLS = ["rg", "seg", "d_min", "d_max", "p_min", "p_max", "vf_min", "vf_max", "vt_min", "vt_max", "row_start", "row_end", "rg_json"]
DICT_COLS = ("kind", "us", "usr", "user", "source")
PLAIN_STATS = ("depth", "vf", "vt")

Span = tuple[int, int]


# --- settings-sample ------------------------------------------------------------------------------------------------

def parse_settings(s: str) -> dict[str, int | None]:
    """`A=none,D=256` → `{"A": None, "D": 256}`."""
    out: dict[str, int | None] = {}
    for part in s.split(","):
        label, _, page = part.partition("=")
        if not page:
            raise ValueError(f"{part}: expected LABEL=none or LABEL=<page rows>")
        out[label] = None if page == "none" else int(page)
    return out


def sample_groups(total: int, k: int) -> list[int]:
    """`k` of `total` groups, evenly spaced."""
    k = min(k, total)
    return [i * total // k for i in range(k)]


def writer(out: Path, schema: pa.Schema, page: int | None) -> pq.ParquetWriter:
    """A served sort's writer at `page`-row pages, or the pre-page writer (`None`: default pages, no page index, no `path`
    statistics)."""
    from . import interval_store as ist
    from . import static_names as sn

    if page is not None:
        return ist._served_writer(out, schema, page)
    return pq.ParquetWriter(out, schema, compression=sn.CODEC, use_dictionary=[c for c in DICT_COLS if c in schema.names],
                            write_statistics=[c for c in PLAIN_STATS if c in schema.names])


def _shard(args: tuple[str, list[int], str]) -> None:
    pa.set_cpu_count(1)
    src, picks, out = args
    pf = pq.ParquetFile(src)
    with pq.ParquetWriter(out, pf.schema_arrow, compression="lz4") as w:
        for g in picks:
            w.write_table(pf.read_row_group(g), row_group_size=1 << 30)


def chunk_sizes(path: Path) -> dict:
    """A parquet's size, footer, per-column compressed chunk bytes, and page-index bytes per column (`ci` / `oi`; absent: none)."""
    md = pq.read_metadata(path)
    names = md.schema.to_arrow_schema().names
    chunks = dict.fromkeys(names, 0)
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i, n in enumerate(names):
            chunks[n] += rg.column(i).total_compressed_size
    ci, oi = dict.fromkeys(names, 0), dict.fromkeys(names, 0)
    for group in iter_chunk_indexes(path):
        for n, ix in zip(names, group):
            ci[n] += ix.column_index_length
            oi[n] += ix.offset_index_length
    return {"size": path.stat().st_size, "footer": md.serialized_size, "groups": md.num_row_groups, "rows": md.num_rows, "chunks": chunks, "ci": ci, "oi": oi}


def _write_variant(args: tuple[str, int | None, list[str], str]) -> tuple[str, dict]:
    pa.set_cpu_count(1)
    label, page, shards, out = args
    schema = pq.ParquetFile(shards[0]).schema_arrow
    with writer(Path(out), schema, page) as w:
        for s in shards:
            pf = pq.ParquetFile(s)
            for g in range(pf.num_row_groups):
                w.write_table(pf.read_row_group(g), row_group_size=1 << 30)
    return label, {"page": page, **chunk_sizes(Path(out))}


@command("settings-sample")
@expect_sha_options("interval-store settings-sample")
@option("-j", "--procs", default=os.cpu_count(), type=IntRange(min=1), help="Processes reading the source")
@option("-n", "--groups", default=3000, type=IntRange(min=1), help="Row groups, evenly spaced over the source")
@option("-o", "--out", required=True, help="Output directory: `<label>.parquet`, `sizes.json`, `meta.json`")
@option("-s", "--settings", default="A=none,D=256,E=1024", help="`LABEL=none` (the pre-page writer) or `LABEL=<page rows>`, comma-separated")
@argument("src")
def settings_sample_cmd(procs, groups, out, settings, src) -> None:
    """SRC's (a local or mounted served sort, only read) sampled row groups under each writer setting."""
    from disk_tree.find.groups import schema_json

    dst = Path(out)
    (dst / "shards").mkdir(parents=True, exist_ok=True)
    md = pq.read_metadata(src)
    picks = sample_groups(md.num_row_groups, groups)
    names = md.schema.to_arrow_schema().names
    pub = dict.fromkeys(names, 0)
    for g in picks:
        rg = md.row_group(g)
        for i, n in enumerate(names):
            pub[n] += rg.column(i).total_compressed_size
    k = min(procs, len(picks))
    shards = [str(dst / "shards" / f"s{i:03d}.parquet") for i in range(k)]
    ctx = get_context("spawn")
    with ProcessPoolExecutor(k, mp_context=ctx) as ex:
        list(ex.map(_shard, [(src, picks[i * len(picks) // k:(i + 1) * len(picks) // k], shards[i]) for i in range(k)]))
    wanted = parse_settings(settings)
    with ProcessPoolExecutor(len(wanted), mp_context=ctx) as ex:
        got = dict(ex.map(_write_variant, [(lb, p, shards, str(dst / f"{lb}.parquet")) for lb, p in wanted.items()]))
    for s in shards:
        os.unlink(s)
    os.rmdir(dst / "shards")
    (dst / "sizes.json").write_text(json.dumps({"source_groups": md.num_row_groups, "picks": picks, "source_chunks": pub, "variants": got}, indent=1) + "\n")
    kv = md.metadata or {}
    (dst / "meta.json").write_text(json.dumps({"schema": schema_json(md)["schema"], "version": int(kv.get(b"version", b"3"))}) + "\n")
    base = next(iter(got.values()))["size"]
    for lb, v in got.items():
        err(f"{lb}: {v['size']:,} bytes (+{100 * (v['size'] / base - 1):.1f}%), footer {v['footer']:,}, chunks {sum(v['chunks'].values()):,}, index {sum(v['ci'].values()) + sum(v['oi'].values()):,}")


# --- settings-sim: a sort's sidecar, the plan ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    d_lo: int
    d_hi: int
    p_lo: str
    p_hi: str


def depth_of(p: str) -> int:
    """The site's `depthOf`: the root 0, a bucket 1."""
    return 0 if p == "" else p.count("/") + 1


def level_rects(dirs: list[str]) -> list[Rect]:
    """The site's `levelRects`: a directory's children, and its own row."""
    out = []
    for p in dirs:
        d = depth_of(p)
        out.append(Rect(d + 1, d + 1, f"{p}/" if p else "", f"{p}0" if p else "￿"))
        if p:
            out.append(Rect(d, d, p, p))
    return out


class Side:
    """A served sort's `.groups.parquet`, and its sort's bounds by depth (a group listed under each depth it spans)."""

    def __init__(self, table: pa.Table, schema: list[dict]):
        self.schema = schema
        self.names = [e["name"] for e in schema[1:]]
        self.d_min, self.d_max = table["d_min"].to_numpy(), table["d_max"].to_numpy()
        self.p_min, self.p_max = table["p_min"].to_pylist(), table["p_max"].to_pylist()
        self.vf_min, self.vf_max, self.vt_min, self.vt_max = (table[c].to_numpy() for c in ("vf_min", "vf_max", "vt_min", "vt_max"))
        self.seg = table["seg"].to_numpy()
        self.rows = table["row_end"].to_numpy() - table["row_start"].to_numpy()
        self.rg_json = table["rg_json"]
        by: dict[int, list[int]] = {}
        for i, (a, b) in enumerate(zip(self.d_min.tolist(), self.d_max.tolist())):
            for d in range(a, b + 1):
                by.setdefault(d, []).append(i)
        self.by_depth = by
        self._json: dict[int, list] = {}

    def __len__(self) -> int:
        return len(self.d_min)

    def chunks(self, i: int) -> list[list[int]]:
        got = self._json.get(i)
        if got is None:
            got = self._json[i] = json.loads(self.rg_json[i].as_py())[2]
        return got

    def neighbours_ok(self, i: int, rects: list[Rect]) -> bool:
        """The site's `pathNeighbours`."""
        if self.d_min[i] == self.d_max[i]:
            return True

        def whole(j: int, d: int) -> bool:
            return 0 <= j < len(self) and self.seg[j] == self.seg[i] and self.d_min[j] == d and self.d_max[j] == d

        for r in rects:
            for d in range(max(r.d_lo, int(self.d_min[i])), min(r.d_hi, int(self.d_max[i])) + 1):
                lo = self.p_max[i - 1] if d == self.d_min[i] and whole(i - 1, d) and self.p_max[i - 1] > self.p_min[i] else self.p_min[i]
                hi = self.p_min[i + 1] if d == self.d_max[i] and whole(i + 1, d) and self.p_min[i + 1] < self.p_max[i] else self.p_max[i]
                if hi >= r.p_lo and lo <= r.p_hi:
                    return True
        return False

    def plan(self, rects: list[Rect], d1: int, d2: int) -> list[int]:
        """`planChanged` over the base tier: the groups meeting a rect (`selectSpans1`, `pathNeighbours`), holding a version
        live in the range (`pqGroups`), that can hold one opening or closing in it. In `rg` order."""
        hit: set[int] = set()
        for r in rects:
            for d in range(r.d_lo, r.d_hi + 1):
                for i in self.by_depth.get(d, ()):
                    if i not in hit and self.p_max[i] >= r.p_lo and self.p_min[i] <= r.p_hi:
                        hit.add(i)
        out = []
        for i in sorted(hit):
            if not (self.vf_min[i] <= d2 and d1 < self.vt_max[i]):
                continue
            if not ((self.vf_max[i] > d1 and self.vf_min[i] <= d2) or (self.vt_max[i] > d1 and self.vt_min[i] <= d2)):
                continue
            if self.neighbours_ok(i, rects):
                out.append(i)
        return out


def chunk_span(chunks: list[list[int]], names: list[str], columns: list[str]) -> Span:
    """The site's `chunkSpan` over a projection."""
    want = set(columns)
    at = [c for n, c in zip(names, chunks) if n in want]
    return (min(c[2] or c[0] for c in at), max((c[2] or c[0]) + c[1] for c in at))


# --- a group's page index -----------------------------------------------------------------------------------------------

@dataclass
class PageIx:
    """Per column its pages `(offset, compressed size, first row)`; the planning columns' per-page null flags and bounds."""
    locs: dict[str, list[tuple[int, int, int]]]
    nulls: list[bool]
    depth: list[tuple[int, int]]
    path: list[tuple[str, str]]
    vf: list[tuple[int, int]]
    vt: list[tuple[int, int]]


def _num(v: bytes) -> int:
    return struct.unpack("<q" if len(v) == 8 else "<i", v)[0]


def parse_ix(names: list[str], chunks: list[list[int]], read) -> PageIx:
    """`parsePageIx`, from a group's index bytes (`read(offset, length)`)."""
    locs: dict[str, list[tuple[int, int, int]]] = {}
    for n, c in zip(names, chunks):
        if len(c) > 3 and c[6]:
            locs[n] = [(p[1], p[2], p[3]) for p in _Reader(read(c[5], c[6])).struct()[1]]
    stats = {}
    for n, c in zip(names, chunks):
        if n in PAGE_PLAN:
            stats[n] = _Reader(read(c[3], c[4])).struct()
    pages = len(locs["depth"])
    nulls = [any(stats[n][1][i] for n in PAGE_PLAN) for i in range(pages)]

    def bounds(n: str, conv) -> list:
        return [(conv(stats[n][2][i]), conv(stats[n][3][i])) if not stats[n][1][i] else None for i in range(pages)]

    return PageIx(locs, nulls, bounds("depth", _num), bounds("path", lambda v: v.decode("utf-8", errors="replace")), bounds("vf", _num), bounds("vt", _num))


def widen(ix: PageIx, k: int, n: int) -> PageIx:
    """`ix` as if its pages held `k` times the rows: every `k` planning pages one, whose bounds are their union (exact). A
    column's locations: each new page a span of the old ones, its compressed size their sum (scaled by `derive`)."""
    if k == 1:
        return ix
    base = ix.locs["depth"]
    size = base[1][2] if len(base) > 1 else n
    width = size * k

    def merge(locs: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
        out: list[list[int]] = []
        for off, sz, first in locs:
            if first % width == 0 or not out:
                out.append([off, sz, first])
            else:
                out[-1][1] += sz
        return [tuple(x) for x in out]

    def union(vals: list) -> list:
        out = []
        for i in range(0, len(vals), k):
            part = [v for v in vals[i:i + k] if v is not None]
            out.append((min(a for a, _ in part), max(b for _, b in part)) if part else None)
        return out

    return PageIx({c: merge(v) for c, v in ix.locs.items()}, [all(ix.nulls[i:i + k]) for i in range(0, len(ix.nulls), k)], union(ix.depth), union(ix.path), union(ix.vf), union(ix.vt))


def pages_of(locs: list[tuple[int, int, int]], a: int, b: int, n: int) -> tuple[int, int]:
    """The site's `pagesOf`: the pages overlapping rows `[a, b)`, inclusive; `(-1, -1)` none."""
    first = last = -1
    for i, (_, _, s) in enumerate(locs):
        e = locs[i + 1][2] if i + 1 < len(locs) else n
        if e > a and s < b:
            if first < 0:
                first = i
            last = i
    return first, last


def plan_group(ix: PageIx, n: int, rects: list[Rect], d1: int, d2: int, columns: list[str]) -> tuple[list[tuple[int, int]], int, int] | None:
    """`readChangedPages`' `plan`: a group's kept pages as row ranges merged where they share a page in any read column, the
    pages kept and skipped. `None`: every row is kept, the group reads whole."""
    pages = ix.locs["depth"]
    kept: list[int] = []
    for i in range(len(pages)):
        if ix.nulls[i]:
            kept.append(i)
            continue
        d0, d9 = ix.depth[i]
        p0, p9 = ix.path[i]
        f0, f9 = ix.vf[i]
        t0, t9 = ix.vt[i]
        meets = any(d9 >= r.d_lo and d0 <= r.d_hi and p9 >= r.p_lo and p0 <= r.p_hi for r in rects)
        if meets and ((f9 > d1 and f0 <= d2) or (t9 > d1 and t0 <= d2)):
            kept.append(i)
    ranges: list[list[int]] = []
    for i in kept:
        a = pages[i][2]
        b = pages[i + 1][2] if i + 1 < len(pages) else n
        if ranges and ranges[-1][1] == a:
            ranges[-1][1] = b
        else:
            ranges.append([a, b])
    out: list[list[int]] = []
    for r in ranges:
        if out and any(pages_of(ix.locs[c], r[0], r[1], n)[0] <= pages_of(ix.locs[c], out[-1][0], out[-1][1], n)[1] for c in columns):
            out[-1][1] = r[1]
        else:
            out.append(list(r))
    if sum(b - a for a, b in out) == n:
        return None
    return [(a, b) for a, b in out], len(kept), len(pages) - len(kept)


def data_spans(ix: PageIx, names: list[str], chunks: list[list[int]], ranges: list[tuple[int, int]], n: int, columns: list[str]) -> list[Span]:
    """`dataSpans`: per range and read column, its dictionary page and its pages."""
    by = dict(zip(names, chunks))
    out = []
    for a, b in ranges:
        for c in columns:
            locs = ix.locs[c]
            f, l = pages_of(locs, a, b, n)
            if f < 0:
                continue
            dpo, _, dict_off = by[c][:3]
            if dict_off or dpo < locs[0][0]:
                out.append((dict_off or dpo, locs[0][0]))
            out.append((locs[f][0], locs[l][0] + locs[l][1]))
    return out


def index_spans(names: list[str], chunks: list[list[int]]) -> list[Span]:
    """`pageIxSpans`: the planning columns' ColumnIndexes, every column's OffsetIndex."""
    out = []
    for n, c in zip(names, chunks):
        if n in PAGE_PLAN:
            out.append((c[3], c[3] + c[4]))
        out.append((c[5], c[5] + c[6]))
    return out


@dataclass(frozen=True)
class Derive:
    """A page size no file was cut at: the sample's ratios (`sizes.json`), the wider page's over the real one's."""
    chunk: dict[str, float]
    index: float
    offset: float

    @classmethod
    def of(cls, sizes: dict, have: int, want: int) -> Derive:
        by = {v["page"]: v for v in sizes["variants"].values()}
        a, b = by[have], by[want]
        ia = sum(a["ci"].values()) + sum(a["oi"].values())
        ib = sum(b["ci"].values()) + sum(b["oi"].values())
        return cls({c: b["chunks"][c] / a["chunks"][c] if a["chunks"][c] else 1.0 for c in a["chunks"]}, ib / ia, sum(b["chunks"].values()) / sum(a["chunks"].values()))


def scale_locs(ix: PageIx, d: Derive) -> PageIx:
    """Offsets by the file's ratio, each column's page bytes by its own (rounded: a byte is a byte)."""
    return PageIx({c: [(round(o * d.offset), max(1, round(s * d.chunk[c])), f) for o, s, f in v] for c, v in ix.locs.items()}, ix.nulls, ix.depth, ix.path, ix.vf, ix.vt)


def scale_chunks(chunks: list[list[int]], names: list[str], d: Derive) -> list[list[int]]:
    """A group's compact metadata under `d`: the same layout, offsets and sizes scaled (`scale_locs`; the index spans by
    `index`)."""
    out = []
    for n, c in zip(names, chunks):
        head = [round(c[0] * d.offset), round(c[1] * d.chunk[n]), round(c[2] * d.offset)]
        out.append(head + ([round(c[3] * d.index), max(1, round(c[4] * d.index)), round(c[5] * d.index), max(1, round(c[6] * d.index))] if len(c) > 3 else []))
    return out


def wave_of(spans: list[Span], ttfb: float, mib_s: float) -> dict:
    """A wave's merged reads as the site's `planRuns`, `RUN_READS` at a time."""
    sizes = [e - s for s, e in merged(spans, RUN_GAP, RUN_BYTES)]
    return {"gets": len(sizes), "rounds": ceil(len(sizes) / RUN_READS), "bytes": sum(sizes), "ms": round(wave_ms(sizes, ttfb, mib_s), 1)}


def sum_waves(waves: list[dict]) -> dict:
    return {k: round(sum(w[k] for w in waves), 1) if k == "ms" else sum(w[k] for w in waves) for k in ("gets", "rounds", "bytes", "ms")}


def sim_paged(side: Side, ixs: dict[int, PageIx], kept: list[int], rects: list[Rect], d1: int, d2: int, cols: list[str], ttfb: float, mib_s: float, k: int = 1, d: Derive | None = None) -> dict:
    """One request under a page size: `k` times the real index's, its bytes by `d`."""
    ix_spans: list[Span] = []
    data: list[Span] = []
    whole: list[Span] = []
    decode: list[int] = []
    n_whole = pages = skipped = rows = whole_rows = 0
    for i in kept:
        n = int(side.rows[i])
        chunks = side.chunks(i)
        ix = widen(ixs[i], k, n)
        p = plan_group(ix, n, rects, d1, d2, cols)
        if d is not None:
            chunks = scale_chunks(chunks, side.names, d)
            ix = scale_locs(ix, d)
        ix_spans += index_spans(side.names, chunks)
        if p is None:
            n_whole += 1
            whole_rows += n
            whole.append(chunk_span(chunks, side.names, cols))
            continue
        ranges, kp, sk = p
        pages += kp
        skipped += sk
        got = sum(b - a for a, b in ranges)
        rows += got
        decode.append(got)
        data += data_spans(ix, side.names, chunks, ranges, n, cols)
    wi = wave_of(ix_spans, ttfb, mib_s)
    wd = wave_of(data, ttfb, mib_s)
    ww = wave_of(whole, ttfb, mib_s)
    return {
        "groups": len(kept), "paged_groups": len(decode), "whole_groups": n_whole, "pages": pages, "pskip": skipped, "rows": rows + whole_rows,
        "paged_rows": decode, "index": wi, "data": wd, "rest": ww, "cold": sum_waves([wi, wd, ww]), "held": sum_waves([wd, ww]),
    }


def sim_whole(side: Side, kept: list[int], cols: list[str], ttfb: float, mib_s: float) -> dict:
    """One request whole-group: each kept group's `chunkSpan`, `planRuns`."""
    w = wave_of([chunk_span(side.chunks(i), side.names, cols) for i in kept], ttfb, mib_s)
    return {"groups": len(kept), "rows": int(sum(int(side.rows[i]) for i in kept)), "cold": w}


def map_read(side: Side, cols: list[str], d: Derive | None, ttfb: float, mib_s: float, widths: tuple[int, ...] = (1, 24), samples: int = 2000) -> dict:
    """A Map read of `w` adjacent whole groups, over evenly spaced windows: bytes, GETs, rounds, predicted ms."""
    out = {}
    n = len(side)
    for w in widths:
        if w > n:
            continue
        at = [i * (n - w) // samples for i in range(samples)]
        got = []
        for a in at:
            spans = []
            for i in range(a, a + w):
                c = side.chunks(i)
                spans.append(chunk_span(scale_chunks(c, side.names, d) if d else c, side.names, cols))
            got.append(wave_of(spans, ttfb, mib_s))
        out[str(w)] = {k: {"median": float(np.median([g[k] for g in got])), "p90": float(np.percentile([g[k] for g in got], 90))} for k in ("bytes", "gets", "rounds", "ms")}
    return out


def read_side(store, key: str) -> Side:
    with store.open(key) as f:
        t = pq.read_table(f, columns=SIDECAR_COLS)
    with store.open(key) as f:
        kv = pq.ParquetFile(f).metadata.metadata or {}
    return Side(t, json.loads(kv[b"schema"]))


def fetch_ixs(store, key: str, side: Side, groups: list[int], threads: int) -> dict[int, PageIx]:
    """Each group's page index: its index runs read once, parsed. `store.read_range` a run at a time, `threads` at once."""
    def one(i: int) -> tuple[int, PageIx]:
        chunks = side.chunks(i)
        runs = merged(index_spans(side.names, chunks), RUN_GAP, RUN_BYTES)
        got = [(s, store.read_range(key, s, e)) for s, e in runs]

        def read(off: int, n: int) -> bytes:
            for s, b in got:
                if s <= off and off + n <= s + len(b):
                    return b[off - s:off - s + n]
            raise ValueError(f"group {i}: index [{off}, +{n}) outside its runs")

        return i, parse_ix(side.names, chunks, read)

    with ThreadPoolExecutor(threads) as ex:
        return dict(ex.map(one, groups))


def footer_cost(url: str) -> dict:
    """A parquet's thrift footer read by pyarrow, in a process of its own: seconds, peak RSS."""
    code = ("import json,resource,sys,time\nimport pyarrow.parquet as pq\nfrom dt_cloud.blobstore import Store\n"
            "root,key=sys.argv[1:3]\nf=Store(root).open(key)\nt=time.monotonic();m=pq.ParquetFile(f).metadata;s=time.monotonic()-t\n"
            "print(json.dumps({'footer':m.serialized_size,'groups':m.num_row_groups,'read_s':round(s,1),'peak_rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss>>10}))")
    root, _, key = url.rpartition("/served/")
    return json.loads(subprocess.run([sys.executable, "-c", code, root, f"served/{key}"], capture_output=True, text=True, check=True).stdout)


def percentiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    a = np.asarray(xs, dtype=float)
    return {"p50": float(np.percentile(a, 50)), "p90": float(np.percentile(a, 90)), "max": float(a.max()), "sum": float(a.sum())}


@command("settings-sim")
@expect_sha_options("interval-store settings-sim")
@option("-a", "--plain", required=True, help="Root of the plain writer's sort (`<root>/served/path.parquet`, a dir or URL, only read)")
@option("-d", "--paged", required=True, help="Root of the paged writer's sort, with its page index (only read)")
@option("-f", "--footers", is_flag=True, help="Also read each sort's whole thrift footer, in a process of its own (seconds, peak RSS)")
@option("-M", "--map-only", is_flag=True, help="Only the Map reads and sidecar facts")
@option("-o", "--out", required=True, help="Output JSONL: each request's modes")
@option("-p", "--pages", default="256,1024", help="Page sizes (rows): the paged sort's own, and multiples of it (derived)")
@option("-R", "--throughput", default=THROUGHPUT_MIB_S, type=float, help="One get's throughput, MiB/s (`level_sim`'s default)")
@option("-s", "--sort", default="path", help="The served sort")
@option("-S", "--sizes", required=True, help="`settings-sample`'s `sizes.json`: the ratios a derived page size scales by")
@option("-t", "--threads", default=32, type=IntRange(min=1), help="Page-index fetches in flight")
@option("-T", "--ttfb", default=TTFB_MS, type=float, help="One get's time to first byte, ms (`level_sim`'s default)")
@argument("requests")
def settings_sim_cmd(plain, paged, footers, map_only, out, pages, throughput, sort, sizes, threads, ttfb, requests) -> None:
    """REQUESTS (JSONL of `{"a", "b", "dirs"}`, stamps; other keys are copied) under the plain writer's whole-group reads and
    the paged writer's page reads, plus the Map's whole-group reads and sidecar facts. A summary JSON goes to stdout."""
    from .blobstore import Store

    sa, sd = Store(plain), Store(paged)
    key = f"served/{sort}.parquet"
    car = f"served/{sort}.groups.parquet"
    A, D = read_side(sa, car), read_side(sd, car)
    if A.names != D.names or len(A) != len(D) or not np.array_equal(A.rows, D.rows):
        raise SystemExit("the two sorts' groups differ")
    size_doc = json.loads(Path(sizes).read_text())
    plist = [int(x) for x in pages.split(",")]
    own = plist[0]
    have = set(D.names)
    map_cols = [c for c in ROW_COLUMNS if c in have]
    diff_cols = map_cols + [c for c in CHURN_COLUMNS if c in have]
    derive = {p: Derive.of(size_doc, own, p) for p in plist if p != own}
    if any(p % own for p in plist):
        raise SystemExit(f"{plist}: each a multiple of {own}")
    summary: dict = {
        "groups": len(A), "map_cols": map_cols, "diff_cols": diff_cols,
        "map": {"whole": map_read(A, map_cols, None, ttfb, throughput), f"p{own}": map_read(D, map_cols, None, ttfb, throughput),
                **{f"p{p}": map_read(D, map_cols, d, ttfb, throughput) for p, d in derive.items()}},
        "sidecars": {lb: {"size": st.size(car), "rg_json_chars_median": float(np.median([len(st_side.rg_json[i].as_py()) for i in range(0, len(st_side), 97)]))} for lb, st, st_side in (("plain", sa, A), ("paged", sd, D))},
    }
    if footers:
        summary["footers"] = {"plain": footer_cost(sa.uri(key)), "paged": footer_cost(sd.uri(key))}
    if map_only:
        print(json.dumps(summary, indent=1))
        return
    reqs = [json.loads(line) for line in open(requests) if line.strip()]
    plans = []
    want: set[int] = set()
    for q in reqs:
        rects = level_rects(q["dirs"])
        kept = D.plan(rects, q["a"], q["b"])
        plans.append((rects, kept))
        want |= set(kept)
    err(f"{len(reqs)} requests, {len(want)} groups' page indexes")
    ixs = fetch_ixs(sd, key, D, sorted(want), threads)
    with open(out, "w") as f:
        for q, (rects, kept) in zip(reqs, plans):
            modes = {"whole": sim_whole(A, kept, diff_cols, ttfb, throughput)}
            for p in plist:
                modes[f"p{p}"] = sim_paged(D, ixs, kept, rects, q["a"], q["b"], diff_cols, ttfb, throughput, p // own, derive.get(p))
            f.write(json.dumps({**{k: v for k, v in q.items() if k != "dirs"}, "n_dirs": len(q["dirs"]), "kept": len(kept), "modes": modes}) + "\n")
    print(json.dumps(summary, indent=1))
