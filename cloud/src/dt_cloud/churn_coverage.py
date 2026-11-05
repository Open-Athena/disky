"""Offline, over a generation's folded path versions (`pvl/`): what `dm=churn` on the as-of walk can show, and what a
dedicated churn index would cost (specs/diff-progressive.md §3c).

`interval-store churn-coverage GEN CASES`: the walk's cells are chosen by a byte floor, so a small directory with heavy
churn rolls into its parent's `(other)`. Per case `{from, to, path}`, the walk is replayed from both scans' rows (the same
floor, attenuation, expansion rule and `top` as `buildDiff`, `LOOKUP_CAP` aside, see `Coverage`) and measured against the
same paths' exact churn: the share of the case's churn left in `(other)`s, its largest single `(other)`'s share of its
parent, the largest churner not drawn, and the churners a map chosen by churn would draw (churn ≥ τ = C · minArea / canvas)
that the walk doesn't. `-f` replays lower byte floors too (`minArea`'s factor), each with the rows it would keep.

`interval-store churn-index-model GEN CASES`: a MODEL of a dedicated churn index, nothing built. Rows and bytes per
level from the real changes: E = (path, adjacent scan pair) changes, and rows = Σ over levels of the distinct (path, dyadic
block) with churn > 0; the sample's bytes per row come from cutting one key range's rows, extrapolated. For each `-m`
minArea and each case with its range: canonical blocks m, then rows and groups decoded to find the candidates at τ/m, under
a per-parent key `(L, b, parent, churn desc, child)` and a global `(L, b, depth, ⌊log2 churn⌋ desc, path)`, and the row
groups of the as-of reads that make the candidates' values exact, planned from the `path` sort's sidecar.

Both are DuckDB + parquet over a local copy of the generation (`pvl/`, `scans.json`, `served/path.groups.parquet`), with
no imports from this package, so a Batch node can run the file as it stands: `python churn_coverage.py churn-coverage …`."""
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from click import FloatRange, IntRange, argument, command, option

err = partial(print, file=sys.stderr, flush=True)

#: `view.ts`: `MIN_AREA_DEFAULT`, `ATTEN_DEFAULT`, the route's default `top`. `served/` row groups hold `GROUP_ROWS`.
MIN_AREA = 12
ATTEN = 2.0
TOP = 500
GROUP_ROWS = 8192
#: `index.ts`: reads in flight, per round.
RUN_READS = 6


def stamps(gen: Path) -> dict[str, int]:
    return {s["id"]: s["ts"] for s in json.loads((gen / "scans.json").read_text())["scans"]}


def connect(threads: int, mem: str, tmp: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET threads={threads}; SET memory_limit='{mem}'; SET temp_directory='{tmp}'; SET preserve_insertion_order=false")
    return con


def pvl_glob(gen: Path) -> str:
    return str(gen / "pvl" / "*.parquet")


def changed_paths(con: duckdb.DuckDBPyConnection, gen: Path, d1: int, d2: int, out: Path) -> None:
    """Every path with a version opening or closing in (d1, d2], with its bytes and objects at both scans and its range
    churn: as `diffLevel.pathChange` from those versions alone. `c1`: the version live at d1's `churn_cum`, else the first
    in the range's less its size; `c2`: the version live at d2's `churn_cum`, else the last's `churn_end`."""
    con.execute(f"""
        COPY (
          SELECT depth, path, any_value(kind) AS kind,
                 coalesce(max(size) FILTER (vf <= {d1} AND {d1} < vt), 0) AS a,
                 coalesce(max(size) FILTER (vf <= {d2} AND {d2} < vt), 0) AS b,
                 coalesce(max(n_files) FILTER (vf <= {d1} AND {d1} < vt), 0) AS oa,
                 coalesce(max(n_files) FILTER (vf <= {d2} AND {d2} < vt), 0) AS ob,
                 coalesce(max(churn_cum) FILTER (vf <= {d2} AND {d2} < vt), arg_max(churn_end, vf)) -
                 coalesce(max(churn_cum) FILTER (vf <= {d1} AND {d1} < vt), arg_min(churn_cum - size, vf)) AS churn
          FROM read_parquet('{pvl_glob(gen)}')
          WHERE (vf > {d1} AND vf <= {d2}) OR (vt > {d1} AND vt <= {d2})
          GROUP BY depth, path
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
    """)


def live_above(con: duckdb.DuckDBPyConnection, gen: Path, d1: int, d2: int, floors: dict[str, float], out: Path) -> None:
    """Every version live at d1 or d2, under each case's path at that case's lowest floor: what the walks can keep."""
    within = " OR ".join(f"(size >= {f} {'' if p == '' else f'AND path LIKE {quoted(p + chr(47) + chr(37))}'})" for p, f in floors.items())
    con.execute(f"""
        COPY (
          SELECT depth, path,
                 max(size) FILTER (vf <= {d1} AND {d1} < vt) AS a,
                 max(size) FILTER (vf <= {d2} AND {d2} < vt) AS b
          FROM read_parquet('{pvl_glob(gen)}')
          WHERE ({within}) AND ((vf <= {d1} AND {d1} < vt) OR (vf <= {d2} AND {d2} < vt))
          GROUP BY depth, path
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
    """)


def quoted(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def parent_of(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


@dataclass(frozen=True)
class Node:
    path: str
    depth: int
    a: int
    b: int
    churn: int


@dataclass(frozen=True)
class Coverage:
    """One case replayed at one floor. Shares are of the case's churn C, unless named otherwise.

    `absent_both_*`: the outermost undrawn paths live at neither scan (created and gone inside the range): no floor draws
    these, since an as-of read at D1 or D2 has no row for them. `drawn` counts the cells with churn (a churnless leaf is
    filler), `expanded` includes the path itself.

    `missed`: paths over τ under an `(other)`, each its own cell on a map chosen by churn (`missed_share`: the outermost
    such paths' churn). `hidden_in_leaves`: paths over τ inside a cell the walk draws as a leaf (its children sub-floor).
    The replay is given the changed paths over τ/64 and those the walk keeps, so a `largest_undrawn` under τ/64 reads 0.

    Not modelled: `LOOKUP_CAP` (a level's unanswered names fold on the live walk too, so a capped case's `(other)` is at
    least this), and `HARD_CAP` on a view's kept rows."""
    frm: str
    to: str
    path: str
    area_factor: float
    atten: float
    floor: float
    churn: int
    drawn: int
    expanded: int
    kept_rows: int
    other_share: float
    root_other_share: float
    worst_other_share: float
    top_cut_share: float
    largest_undrawn: int
    largest_undrawn_share: float
    largest_undrawn_size: int
    largest_undrawn_path: str
    tau: float
    missed: int
    missed_share: float
    hidden_in_leaves: int
    absent_both_share: float
    absent_both_largest: int


def replay(
    frm: str,
    to: str,
    path: str,
    root: tuple[int, int],
    total: int,
    changed: list[Node],
    live: list[Node],
    w: int,
    h: int,
    area_factor: float = 1.0,
    atten: float = ATTEN,
    min_area: int = MIN_AREA,
    top: int = TOP,
) -> Coverage:
    """`buildDiff`'s walk under `path`, from the rows both scans hold: `root` = its bytes at both scans, `total` its range
    churn, `changed` its descendants with a version change in the range (their exact churn), `live` those of at least
    the lowest floor."""
    d_p = 0 if path == "" else path.count("/") + 1
    k = min_area * area_factor / (w * h)
    floor = max(root) * k
    thr = lambda d: floor * atten ** max(0, d - d_p - 1)  # noqa: E731
    churn = {n.path: n.churn for n in changed}
    size = {n.path: (n.a, n.b) for n in live}
    for n in changed:
        size.setdefault(n.path, (n.a, n.b))
    # Each side's kept rows (ancestor-closed: a parent's bytes ≥ a child's, its floor ≤ the child's); the fleet root's
    # depth-1 rows are all drawn on a churn walk.
    kids: dict[str, list[str]] = {}
    kept_rows = 0
    for n in live:
        if path == "" and n.depth == 1 or max(n.a or 0, n.b or 0) >= thr(n.depth):
            kids.setdefault(parent_of(n.path), []).append(n.path)
            kept_rows += (1 if (n.a or 0) >= thr(n.depth) else 0) + (1 if (n.b or 0) >= thr(n.depth) else 0)
    # The walk: a node with kept children expands when its bytes or churn moved; its drawn children are those.
    drawn: dict[str, int] = {}
    others: dict[str, int] = {}
    expanded: dict[str, int] = {}
    level = [(path, total)]
    while level:
        nxt = []
        for p, c in level:
            names = kids.get(p, [])
            a, b = size.get(p, (0, 0)) if p != path else root
            same = (a or 0) == (b or 0) and c == 0
            if p != path and (same or not names):
                drawn[p] = c
                continue
            if p == path and not names:
                continue
            expanded[p] = c
            s = 0
            for q in names:
                s += churn.get(q, 0)
                nxt.append((q, churn.get(q, 0)))
            others[p] = c - s
        level = nxt
    # `top`: the leaves by churn; the rest fold into their parents' `(other)`.
    ranked = sorted(((q, c) for q, c in drawn.items() if c > 0), key=lambda kv: -kv[1])
    cut = sum(c for _, c in ranked[top:])
    for q, c in ranked[top:]:
        others[parent_of(q)] = others.get(parent_of(q), 0) + c
    drawn_paths = set(expanded) | {q for q, _ in ranked[:top]}
    in_other = sum(others.values())
    # The largest churner no cell shows: a path, maximal, under an expanded node and not drawn.
    undrawn = [n for n in changed if n.path not in drawn_paths and parent_of(n.path) in expanded and n.churn > 0]
    # Descendants of drawn leaves are inside their cells; only an undrawn child of an expanded node is in `(other)`.
    big = max(undrawn, key=lambda n: n.churn, default=None)
    # What a map chosen by churn would draw and this doesn't: any undrawn path over τ under an `(other)`.
    tau = total * min_area / (w * h)
    leaves = set(dict(ranked[:top]))
    over = [n for n in changed if n.churn > 0 and n.churn >= tau and n.path not in drawn_paths]
    missed = [n for n in over if not _under_drawn(n.path, path, leaves)]
    # Maximal missed paths' churn: a child's churn is inside its parent's, so count each `(other)`'s once.
    missed_set = {n.path for n in missed}
    missed_top = [n for n in missed if parent_of(n.path) not in missed_set]
    # Live at neither scan: under `(other)` at any floor.
    absent = [n for n in changed if n.churn > 0 and not n.a and not n.b and n.path not in drawn_paths and not _under_drawn(n.path, path, leaves)]
    absent_set = {n.path for n in absent}
    absent_top = [n for n in absent if parent_of(n.path) not in absent_set]
    worst = max((o / expanded[p] for p, o in others.items() if expanded.get(p)), default=0.0)
    return Coverage(
        frm=frm, to=to, path=path, area_factor=area_factor, atten=atten, floor=floor, churn=total, drawn=len(drawn_paths - {path}), expanded=len(expanded), kept_rows=kept_rows,
        other_share=in_other / total if total else 0.0,
        root_other_share=others.get(path, 0) / total if total else 0.0,
        worst_other_share=worst,
        top_cut_share=cut / total if total else 0.0,
        largest_undrawn=big.churn if big else 0,
        largest_undrawn_share=big.churn / total if big and total else 0.0,
        largest_undrawn_size=max(big.a, big.b) if big else 0,
        largest_undrawn_path=big.path if big else "",
        tau=tau,
        missed=len(missed),
        missed_share=sum(n.churn for n in missed_top) / total if total else 0.0,
        hidden_in_leaves=len(over) - len(missed),
        absent_both_share=sum(n.churn for n in absent_top) / total if total else 0.0,
        absent_both_largest=max((n.churn for n in absent_top), default=0),
    )


def _ancestors(p: str, stop: str):
    q = parent_of(p)
    while True:
        yield q
        if q == stop or q == "":
            return
        q = parent_of(q)


def _under_drawn(p: str, stop: str, leaves: set[str]) -> bool:
    return any(q in leaves for q in _ancestors(p, stop))


def share(x: float) -> str:
    return f"{100 * x:.1f}%"


def size_of(n: float) -> str:
    for unit, f in (("P", 2 ** 50), ("T", 2 ** 40), ("G", 2 ** 30), ("M", 2 ** 20)):
        if n >= f:
            return f"{n / f:.1f}{unit}"
    return f"{n:.0f}"


def _nodes(con: duckdb.DuckDBPyConnection, f: Path, path: str, cols: str, cond: str = "true") -> list[tuple]:
    under = "true" if path == "" else f"path LIKE {quoted(path + '/%')}"
    return con.execute(f"SELECT {cols} FROM read_parquet('{f}') WHERE {under} AND {cond}").fetchall()


def root_size(con: duckdb.DuckDBPyConnection, gen: Path, path: str, d: int) -> int:
    if path == "":
        q = f"SELECT coalesce(sum(size), 0) FROM read_parquet('{pvl_glob(gen)}') WHERE depth = 1 AND vf <= {d} AND {d} < vt"
    else:
        depth = path.count("/") + 1
        q = f"SELECT coalesce(sum(size), 0) FROM read_parquet('{pvl_glob(gen)}') WHERE depth = {depth} AND path = {quoted(path)} AND vf <= {d} AND {d} < vt"
    return int(con.execute(q).fetchone()[0])


@command("churn-coverage")
@option("-a", "--attens", default="2", help="Attenuations to replay, comma-separated: 2 is the walk's default (a floor doubling per level below P's children), 1 a flat floor")
@option("-f", "--factors", default="1", help="minArea factors to replay, comma-separated: 1 is the walk's floor; 0.1 reads ten times lower")
@option("-H", "--height", default=1024, type=IntRange(1), help="Canvas height (`view.QUANT`-rounded, as the route)")
@option("-M", "--memory", default="24GB", help="DuckDB memory limit")
@option("-o", "--out", type=Path, help="JSONL: one record per case and factor")
@option("-p", "--threads", default=8, type=IntRange(1), help="DuckDB threads")
@option("-T", "--tmp", default="tmp/churn-coverage", help="Work dir: per-pair tables, DuckDB spill")
@option("-w", "--width", default=1664, type=IntRange(1), help="Canvas width")
@argument("gen", type=Path)
@argument("cases", type=Path)
def churn_coverage_cmd(attens, factors, height, memory, out, threads, tmp, width, gen, cases) -> None:
    """Replay the walk per case against the paths' exact churn: what lands in `(other)`, the largest churner not drawn."""
    work = Path(tmp)
    work.mkdir(parents=True, exist_ok=True)
    con = connect(threads, memory, str(work / "duck"))
    ts = stamps(gen)
    cs = json.loads(cases.read_text())
    fs = [float(x) for x in factors.split(",")]
    roots = {}
    for c in cs:
        d1, d2 = ts[c["from"]], ts[c["to"]]
        roots[(c["from"], c["to"], c["path"])] = (root_size(con, gen, c["path"], d1), root_size(con, gen, c["path"], d2))
    rows: list[Coverage] = []
    for pair in sorted({(c["from"], c["to"]) for c in cs}):
        d1, d2 = ts[pair[0]], ts[pair[1]]
        ch = work / f"changed-{d1}-{d2}.parquet"
        lv = work / f"live-{d1}-{d2}.parquet"
        if not ch.exists():
            changed_paths(con, gen, d1, d2, ch)
            err(f"{pair}: changed paths")
        floors = {c["path"]: max(roots[(pair[0], pair[1], c["path"])]) * MIN_AREA * min(fs) / (width * height) for c in cs if (c["from"], c["to"]) == pair}
        if not lv.exists():
            live_above(con, gen, d1, d2, floors, lv)
            err(f"{pair}: live rows")
        for c in cs:
            if (c["from"], c["to"]) != pair:
                continue
            d_p = 0 if c["path"] == "" else c["path"].count("/") + 1
            total = _nodes(con, ch, c["path"], "coalesce(sum(churn), 0)", f"depth = {d_p + 1}")[0][0]
            tau = int(total) * MIN_AREA / (width * height)
            live = [Node(p, d, a or 0, b or 0, 0) for d, p, a, b in _nodes(con, lv, c["path"], "depth, path, a, b", f"greatest(coalesce(a, 0), coalesce(b, 0)) >= {floors[c['path']]}")]
            con.execute("CREATE OR REPLACE TEMP TABLE names (path VARCHAR)")
            con.execute("INSERT INTO names SELECT path FROM read_parquet(?)", [str(lv)])
            changed = [Node(p, d, a, b, x) for d, p, a, b, x in _nodes(con, ch, c["path"], "depth, path, a, b, churn", f"(churn >= {tau / 64} OR depth = {d_p + 1} OR path IN (SELECT path FROM names))")]
            for f in fs:
                for at in (float(x) for x in attens.split(",")):
                    rows.append(replay(pair[0], pair[1], c["path"], roots[(pair[0], pair[1], c["path"])], int(total), changed, live, width, height, f, at))
                    err(coverage_line(rows[-1]))
    if out:
        out.write_text("".join(json.dumps(asdict(r)) + "\n" for r in rows))


def coverage_line(r: Coverage) -> str:
    return (f"{r.frm}→{r.to} {r.path or '/':42} ×{r.area_factor:<5} a{r.atten:<3} floor {size_of(r.floor):>7} C {size_of(r.churn):>7} cells {r.drawn:>5}"
            f" other {share(r.other_share):>6} (root {share(r.root_other_share):>6}, worst {share(r.worst_other_share):>6})"
            f" largest undrawn {size_of(r.largest_undrawn):>7} ({share(r.largest_undrawn_share):>6}) missed≥τ {r.missed:>5} ({share(r.missed_share):>6}) in leaves {r.hidden_in_leaves} absent both {share(r.absent_both_share):>6} top cut {share(r.top_cut_share):>6} kept {r.kept_rows}")


# --- the dedicated churn index: a model ------------------------------------------------------------------------------


def increments_sql(gen: Path, where: str, file: str | None = None) -> str:
    """(path, ordinal of the scan, its churn increment > 0), from the pieces' `churn_cum` at `vf` and `churn_end` at `vt`."""
    src = f"read_parquet('{file or pvl_glob(gen)}')"
    return f"""
        WITH ev AS (
          SELECT depth, path, vf AS t, churn_cum AS c FROM {src} {where}
          UNION ALL
          SELECT depth, path, vt AS t, churn_end AS c FROM {src} {where} {'AND' if where else 'WHERE'} churn_end IS NOT NULL
        ), pt AS (SELECT depth, path, t, max(c) AS c FROM ev GROUP BY depth, path, t),
        inc AS (SELECT depth, path, t, c - lag(c, 1, 0) OVER (PARTITION BY path ORDER BY t) AS d FROM pt)
        SELECT depth, path, t, d FROM inc WHERE d > 0
    """


def ordinals_table(con: duckdb.DuckDBPyConnection, ts: list[int]) -> None:
    con.execute("CREATE OR REPLACE TEMP TABLE ords (t BIGINT, i INT)")
    con.executemany("INSERT INTO ords VALUES (?, ?)", list(zip(ts, range(len(ts)))))


def level_counts(con: duckdb.DuckDBPyConnection, gen: Path, levels: int, file: str) -> tuple[int, list[int], dict[int, int]]:
    """One key range's E, its rows per level, and its E per depth (a path is in one range, so these sum over ranges)."""
    sel = ", ".join(f"count(DISTINCT ((i - 1) >> {L})) AS l{L}" for L in range(levels))
    q = f"SELECT path, depth, count(*) AS e, {sel} FROM ({increments_sql(gen, '', file)}) JOIN ords USING (t) GROUP BY path, depth"
    con.execute(f"CREATE OR REPLACE TEMP TABLE c AS {q}")
    r = con.execute(f"SELECT coalesce(sum(e), 0), {', '.join(f'coalesce(sum(l{L}), 0)' for L in range(levels))} FROM c").fetchone()
    by_depth = dict(con.execute("SELECT depth, sum(e) FROM c GROUP BY depth").fetchall())
    return int(r[0]), [int(x) for x in r[1:]], {int(k): int(v) for k, v in by_depth.items()}


def blocks_of(i1: int, i2: int) -> list[tuple[int, int]]:
    """(i1, i2] (scan ordinals; transition j is the pair (j−1, j), numbered 1..S) as canonical dyadic blocks `(L, b)`:
    `[b·2^L + 1, (b+1)·2^L]`, the fewest whose union it is."""
    out = []
    lo = i1
    while lo < i2:
        L = 0
        while lo % (1 << (L + 1)) == 0 and lo + (1 << (L + 1)) <= i2:
            L += 1
        out.append((L, lo >> L))
        lo += 1 << L
    return out


def sample_bytes_per_row(con: duckdb.DuckDBPyConnection, gen: Path, levels: int, file: str, out: Path) -> float:
    """One range's index rows cut as the index would be: sorted by key, zstd, `GROUP_ROWS`-row groups. A sample: a key range
    holds similar neighbours, so bytes per row is optimistic for a global key."""
    block_rows = " UNION ALL ".join(
        f"SELECT {L} AS L, ((i - 1) >> {L}) AS b, depth, path, sum(d) AS churn FROM inc GROUP BY ALL" for L in range(levels))
    con.execute(f"""
        COPY (WITH inc AS (SELECT depth, path, i, d FROM ({increments_sql(gen, '', file)}) JOIN ords USING (t))
              SELECT L::SMALLINT AS L, b::INT AS b, depth::SMALLINT AS depth, path, churn::BIGINT AS churn FROM ({block_rows})
              ORDER BY L, b, depth, churn DESC, path)
        TO '{out}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {GROUP_ROWS})
    """)
    m = pq.read_metadata(out)
    return out.stat().st_size / max(1, m.num_rows)


@dataclass(frozen=True)
class ReadModel:
    """MODEL of one full-map read of a case, from the real per-block churn; nothing is built and nothing timed.
    `parent_*`: per-parent key, each expanded directory's run in each block down to τ/m: `rows` needed, `groups` ≥ one per
    run (siblings sharing a group not credited), decoding `groups × GROUP_ROWS`. `global_*`: global key, each block's
    rows at P's depths down to the ⌊log2⌋ bucket holding τ/m, whatever their path (a drill's rows outside P are decoded and
    dropped); runs are per (block, depth). `exact_groups`: the `path` sort's row groups holding the candidates' versions at
    D1 and D2 (planned from the sidecar), which make their values exact, the same for both keys. Rounds: `RUN_READS` reads
    in flight, an index read two rounds (a footer's page index aside)."""
    frm: str
    to: str
    path: str
    min_area: int
    churn: int
    m: int
    tau_m: float
    candidates: int
    cells: int
    expanded: int
    parent_rows: int
    parent_groups: int
    parent_runs: int
    parent_rounds: int
    global_rows: int
    global_groups: int
    global_runs: int
    global_rounds: int
    exact_groups: int
    exact_rounds: int


def read_model(con: duckdb.DuckDBPyConnection, gen: Path, ts: dict[str, int], c: dict, min_area: int, w: int, h: int, tmp: Path, groups: str) -> ReadModel:
    order = sorted(ts.values())
    i1, i2 = order.index(ts[c["from"]]), order.index(ts[c["to"]])
    blocks = blocks_of(i1, i2)
    m = len(blocks)
    path = c["path"]
    d_p = 0 if path == "" else path.count("/") + 1
    table = tmp / f"blocks-{i1}-{i2}.parquet"
    if not table.exists():
        # Per path per block: the increments in the range, summed.
        case = " ".join(f"WHEN i > {b << L} AND i <= {(b + 1) << L} THEN {n}" for n, (L, b) in enumerate(blocks))
        d1, d2 = order[i1], order[i2]
        pre = f"WHERE path IN (SELECT path FROM read_parquet('{tmp}/changed-{d1}-{d2}.parquet'))"
        con.execute(f"""
            COPY (SELECT block, depth, path, sum(d)::BIGINT AS churn FROM (
                    SELECT CASE {case} END AS block, depth, path, d FROM ({increments_sql(gen, pre)}) JOIN ords USING (t) WHERE i > {i1} AND i <= {i2})
                  GROUP BY ALL) TO '{table}' (FORMAT parquet, COMPRESSION zstd)
        """)
    prefix = "" if path == "" else f"AND path LIKE {quoted(path + '/%')}"
    scoped = f"read_parquet('{table}') WHERE depth > {d_p} {prefix}"
    total = int(con.execute(f"SELECT coalesce(sum(churn), 0) FROM {scoped} AND depth = {d_p + 1}").fetchone()[0])
    tau = total * min_area / (w * h)
    tau_m = tau / m
    con.execute(f"CREATE OR REPLACE TEMP TABLE cand AS SELECT block, depth, path, churn FROM {scoped} AND churn >= {tau_m}")
    # A candidate's ancestors clear τ/m in its block too (churn is additive), so the set is ancestor-closed.
    candidates = int(con.execute("SELECT count(DISTINCT path) FROM cand").fetchone()[0])
    # Exact range churn per candidate: Σ blocks, from the changes table, and the cells over τ.
    con.execute(f"CREATE OR REPLACE TEMP TABLE tot AS SELECT depth, path, sum(churn) AS churn FROM {scoped} GROUP BY ALL")
    cells = int(con.execute(f"SELECT count(*) FROM tot JOIN (SELECT DISTINCT path FROM cand) USING (path) WHERE churn >= {tau}").fetchone()[0])
    # Per parent: each expanded directory (a cell over τ, or P) reads, in each block, its children's rows ≥ τ/m.
    parents = con.execute(f"""
        WITH expanded AS (SELECT path AS parent FROM tot WHERE churn >= {tau} UNION ALL SELECT {quoted(path)}),
             kid AS (SELECT block, regexp_replace(path, '/[^/]*$', '') AS parent, count(*) AS n FROM cand WHERE depth > {d_p + 1} GROUP BY ALL
                     UNION ALL SELECT block, {quoted(path)} AS parent, count(*) AS n FROM cand WHERE depth = {d_p + 1} GROUP BY ALL)
        SELECT count(*), coalesce(sum(n), 0), coalesce(sum(ceil(n * 1.0 / {GROUP_ROWS})), 0) FROM kid JOIN expanded USING (parent)
    """).fetchone()
    parent_runs, parent_rows, parent_groups = int(parents[0]), int(parents[1]), int(parents[2])
    # Global: each block, every path at P's depths down to τ/m rounded to its ⌊log2⌋ bucket, under P or not.
    bucket = 2 ** math.floor(math.log2(tau_m)) if tau_m > 0 else 1
    glob = con.execute(f"""
        SELECT count(*), count(DISTINCT (block, depth)) FROM read_parquet('{table}') WHERE depth > {d_p} AND churn >= {bucket}
    """).fetchone()
    global_rows, global_runs = int(glob[0]), int(glob[1])
    per_block = con.execute(f"""
        SELECT coalesce(sum(ceil(n * 1.0 / {GROUP_ROWS})), 0) FROM (
          SELECT block, depth, count(*) AS n FROM read_parquet('{table}') WHERE depth > {d_p} AND churn >= {bucket} GROUP BY ALL)
    """).fetchone()
    # Exact values: the candidates' as-of rows at both scans, row groups from the `path` sort's sidecar.
    d1, d2 = order[i1], order[i2]
    exact = con.execute(f"""
        WITH q AS (SELECT DISTINCT depth, path FROM cand),
             g AS (SELECT rg, d_min, d_max, p_min, p_max, vf_min, vt_max FROM read_parquet('{groups}')
                   WHERE (vf_min <= {d1} AND vt_max > {d1}) OR (vf_min <= {d2} AND vt_max > {d2}))
        SELECT count(DISTINCT rg) FROM (
          SELECT g.rg FROM g JOIN q ON q.depth BETWEEN g.d_min AND g.d_max AND (g.d_min <> g.d_max OR q.path BETWEEN g.p_min AND g.p_max))
    """).fetchone()
    exact_groups = int(exact[0])
    return ReadModel(
        frm=c["from"], to=c["to"], path=path, min_area=min_area, churn=total, m=m, tau_m=tau_m, candidates=candidates, cells=cells,
        expanded=int(con.execute(f"SELECT count(*) FROM tot WHERE churn >= {tau}").fetchone()[0]),
        parent_rows=parent_rows, parent_groups=parent_groups, parent_runs=parent_runs, parent_rounds=2 * math.ceil(parent_runs / RUN_READS),
        global_rows=global_rows, global_groups=int(per_block[0]), global_runs=global_runs, global_rounds=2 * math.ceil(global_runs / RUN_READS),
        exact_groups=exact_groups, exact_rounds=math.ceil(exact_groups / RUN_READS),
    )


def footer_columns(path: Path) -> dict[str, int]:
    """Compressed bytes per column over a parquet's row groups, from its footer."""
    md = pq.read_metadata(path)
    out: dict[str, int] = {}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i in range(rg.num_columns):
            col = rg.column(i)
            out[col.path_in_schema] = out.get(col.path_in_schema, 0) + col.total_compressed_size
    return out


@command("churn-index-model")
@option("-c", "--columns-of", "columns_of", multiple=True, type=Path, help="A served sort (local parquet, footer only): its bytes per column, and the churn columns' share")
@option("-H", "--height", default=1024, type=IntRange(1), help="Canvas height")
@option("-M", "--memory", default="24GB", help="DuckDB memory limit")
@option("-m", "--min-areas", default="12,2304", help="τ's minArea (px²), comma-separated: 12 the walk's, 2304 a 48² legible cell")
@option("-o", "--out", type=Path, help="JSON: the sizing and one read model per case and minArea")
@option("-p", "--threads", default=8, type=IntRange(1), help="DuckDB threads")
@option("-s", "--sample-every", default=64, type=IntRange(1), help="Bytes per row from every Nth key range's rows")
@option("-T", "--tmp", default="tmp/churn-index-model", help="Work dir (needs `churn-coverage`'s `changed-*` under it): tables, DuckDB spill")
@option("-w", "--width", default=1664, type=IntRange(1), help="Canvas width")
@option("-x", "--max-share", default=1.0, type=FloatRange(0, 1), help="Size only this share of the key ranges (a smoke test)")
@argument("gen", type=Path)
@argument("cases", type=Path)
def churn_index_model_cmd(columns_of, height, memory, min_areas, out, threads, sample_every, tmp, width, max_share, gen, cases) -> None:
    """A MODEL, nothing built: E, rows per level, bytes per row from a sample; one full-map read per case under both keys."""
    work = Path(tmp)
    work.mkdir(parents=True, exist_ok=True)
    con = connect(threads, memory, str(work / "duck"))
    ts = stamps(gen)
    order = sorted(ts.values())
    ordinals_table(con, order)
    S = len(order) - 1
    levels = math.ceil(math.log2(S)) + 1
    files = sorted((gen / "pvl").glob("*.parquet"))
    files = files[: max(1, int(len(files) * max_share))]
    E, rows = 0, [0] * levels
    by_depth: dict[int, int] = {}
    per_row, sampled = 0.0, 0
    for n, f in enumerate(files):
        e, r, d = level_counts(con, gen, levels, str(f))
        E += e
        rows = [x + y for x, y in zip(rows, r)]
        for k, v in d.items():
            by_depth[k] = by_depth.get(k, 0) + v
        if n % sample_every == 0 and e:
            per_row += sample_bytes_per_row(con, gen, levels, str(f), work / f"sample-{n}.parquet") * e
            sampled += e
        err(f"{n + 1}/{len(files)} {f.name}: E so far {E}, rows {sum(rows)}")
    sizing = {
        "scans": S + 1, "levels": levels, "E": E, "rows_per_level": rows, "rows": sum(rows), "E_by_depth": dict(sorted(by_depth.items())),
        "bytes_per_row_sample": per_row / sampled if sampled else None, "model": "rows are exact over the base; bytes are the sample's bytes per row times rows",
        "bytes_model": (per_row / sampled) * sum(rows) if sampled else None,
        "columns": {str(p): footer_columns(p) for p in columns_of},
    }
    err(json.dumps(sizing))
    groups = gen / "served" / "path.groups.parquet"
    models: list[ReadModel] = []
    for c in json.loads(cases.read_text()):
        for ma in (int(x) for x in min_areas.split(",")):
            models.append(read_model(con, gen, ts, c, ma, width, height, work, str(groups)))
            err(json.dumps(asdict(models[-1])))
    if out:
        out.write_text(json.dumps({"sizing": sizing, "reads": [asdict(x) for x in models]}, indent=1) + "\n")


if __name__ == "__main__":
    {"churn-coverage": churn_coverage_cmd, "churn-index-model": churn_index_model_cmd}[sys.argv.pop(1)]()
