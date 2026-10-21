"""Compaction of the static name index (specs/static-compaction.md): a generation `G` (its base `static-names/<G>/` and
the newest manifest's runs) folded into a new base generation `G'`, equal to `static-names build` through the last
scan over `G`'s key ranges (`build -r G/ranges.json`), file for file. This module is the store-specific fold
(`fold_gen`); publishing, the successor record, the pointer, R2 and the predecessor's grace are the shared
orchestrator's (`append_runner.compact_store`, `compactor()` here).

How each part folds (the build's own code wherever a part is a function of others):

- `scans.json` (`G`'s, then each run scan's `deltas/<id>/scans.json` entry, kept on folded runs by
  `static_merge.FOLDED_RETAIN`), `ranges.json` (`G`'s).
- `cintervals/`, `chist/` per key range (`fold_range`): `G`'s coalesced versions and every scan's `cdelta` (a close record
  sets `vt`, an open adds a version), one row per `(depth, path, usr, vf)`, the smallest `vt`; written as
  `build_range(coalesced=True)` writes them.
- `shards.json`: the build's planner over the folded histograms (`G`'s target rows and task count); `sx/`, `sidecar/`,
  `sidecar.parquet`: `static_append.merge_shards` of `G`'s and the runs' suffix rows, cut to that plan.
- `catalog/cells.parquet`, `index.parquet`: `static_append.merge_catalogs` of `G`'s and the runs'; the members
  (`members.parquet`, `members.json`): the build's census over the new shards; `meta.json` from both.
- `drill/` and `roots-measure/` (`fold_drill`): roots are read, not recomputed from the suffix shards. Each member's
  root set through the last scan is `G`'s rows of its class there ∪ each run's rows of its class there (combined: the
  smallest `vt` per `(path, usr, vf)`); members with equal sets (digest as the build's: count and two hash sums) alias
  to the least; then the build's own measurement, rollups and writer (`static_local.finish_drill`).
- `names/`, `anchors/` (when `G` has `anchors/meta.json`): the build's base builders over `G'` (`build_base_anchors`).
- `manifests/<last scan>.json` (no runs), and `compaction.json` (the orchestrator's completion record).
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq
from click import IntRange, command, option

from . import append_runner as ar
from . import static_anchors as an
from . import static_append as sa
from . import static_catalog as sc
from . import static_drill as sd
from . import static_local as sl
from . import static_names as sn
from .static_names import CINTERVAL_SCHEMA, CODEC, INTERVAL_RG, PREFIX, err, q

U64 = 1 << 64


# ── Light: key ranges ──────────────────────────────────────────────────────


def fold_range(con, base_file: str, cdelta_files: list[str], out: Path, name: str, rule=None) -> int:
    """Key range `name`'s coalesced versions through the last scan: `G`'s (`base_file`) and every scan's `cdelta`
    (oldest first), one row per `(depth, path, usr, vf)` with the smallest `vt` → `out/cintervals/<name>.parquet` and
    `out/chist/<name>.parquet`, as `build_range(coalesced=True)` writes them. Returns the versions."""
    cols = ", ".join(CINTERVAL_SCHEMA.names)
    parts = [f"SELECT {cols} FROM read_parquet({q(f)})" for f in [base_file, *cdelta_files]]
    con.execute(f"""CREATE OR REPLACE TABLE civ AS SELECT depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY depth, path, usr, vf""")
    rows = sn.write_sorted(sn._batches(con, "SELECT * FROM civ ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                           CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(sn.hist_sql("civ", rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    con.execute("DROP TABLE civ")
    return rows


# ── Light: the catalog ─────────────────────────────────────────────────────


def fold_catalog(con, tiers: list[Path], out: Path, work: Path, *, new_gen: str, plan: dict, membership: dict, rule) -> dict:
    """`G`'s and the runs' catalogs merged into `out/catalog` (`merge_catalogs`: the rebuild's cells and index), the
    members cut by the build's census over `out`'s new shards (`members.parquet`, `members.json`), and `meta.json` as
    the build's `catalog_meta` (its `s`: this fold's seconds)."""
    t0 = monotonic()
    cat = out / "catalog"
    doc = sa.merge_catalogs(tiers, cat)
    census = work / "census"
    shutil.rmtree(census, ignore_errors=True)
    census.mkdir(parents=True)
    for s in plan["shards"]:
        name = f"s{s['i']:04d}"
        pq.write_table(sc.census(con, f"read_parquet({q(str(out / 'sx' / f'{name}.parquet'))})", membership["max_rows"] + 1), census / f"{name}.parquet",
                       compression=CODEC)
    members = sc.cut_members(census, cat / "members.parquet", gen=new_gen, max_rows=membership["max_rows"])
    (cat / "members.json").write_text(json.dumps(members, indent=1) + "\n")
    shutil.rmtree(census)
    short, long_, top = con.execute(f"""SELECT count(*) FILTER (WHERE length(q) <= 2), count(*) FILTER (WHERE length(q) > 2), max(length(q))
        FROM read_parquet({q(str(cat / 'cells.parquet'))}) WHERE bucket = ''""").fetchone()
    assembled = {"cells_rows": doc["cells_rows"], "row_groups": doc["row_groups"], "members_short": short, "members_long": long_, "max_len": top,
                 "bytes": doc["bytes"], "index_bytes": (cat / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG}
    meta = sc.catalog_meta(new_gen, assembled, round(monotonic() - t0, 1), members, rule)
    (cat / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── The drill ──────────────────────────────────────────────────────────────


def _roots_sql(files: list[str]) -> str:
    """Published roots files' rows (`ROOT_SCHEMA`: no `depth`)."""
    if not files:
        return ("SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS path, NULL::VARCHAR AS usr, NULL::BIGINT AS vf, NULL::BIGINT AS vt, "
                "NULL::BIGINT AS size, NULL::BIGINT AS n_files WHERE false")
    return f"SELECT q, path, usr, vf, vt, size, n_files FROM read_parquet([{', '.join(q(f) for f in files)}])"


#: A root's `depth`: its path's segments (as the suffix shards record it).
DEPTH = "len(string_split(path, '/'))::UTINYINT"


def fold_drill(con, tiers: list[sd.Tier], out: Path, work: Path, *, R: int, K: int, floor: int, chunk_rows: int = sl.CHUNK_ROWS,
               step=sl._nop_step) -> dict:
    """`G'`'s `drill/` and `roots-measure/` in `out` from the tiers' drills (`G`'s, then each run's, oldest first) and
    `out/catalog/members.parquet`: each member's roots through the last scan, read from the tiers through each one's
    class map (every tier's `aliases.parquet` maps every member it knows to its canonical, which holds that class's rows
    there; a member a tier doesn't know has none there), combined; the classes by root-set digest, as the build's
    (`static_local.build_base_drill`); then `static_local.finish_drill` over the canonical members' roots and the short
    literals'."""
    t = monotonic()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    n = len(tiers)
    con.execute(f"CREATE OR REPLACE TABLE dmem AS SELECT q FROM read_parquet({q(str(out / 'catalog' / 'members.parquet'))})")
    for i, tr in enumerate(tiers):
        con.register(f"al_in{i}", tr.alias_table())
        con.execute(f"CREATE OR REPLACE TABLE ta{i} AS SELECT q, canonical FROM al_in{i}")
        con.unregister(f"al_in{i}")
    cs = ", ".join(f"ta{i}.canonical AS c{i}" for i in range(n))
    joins = " ".join(f"LEFT JOIN ta{i} USING (q)" for i in range(n))
    # a member's tuple of classes, one per tier: members with equal tuples hold equal rows
    con.execute(f"CREATE OR REPLACE TABLE mt AS SELECT m.q, {cs} FROM dmem AS m {joins}")
    con.execute(f"""CREATE OR REPLACE TABLE tup AS SELECT row_number() OVER (ORDER BY {', '.join(f'c{i} NULLS FIRST' for i in range(n))}) AS tid, *
        FROM (SELECT DISTINCT {', '.join(f'c{i}' for i in range(n))} FROM mt)""")
    on = " AND ".join(f"t.c{i} IS NOT DISTINCT FROM mt.c{i}" for i in range(n))
    con.execute(f"CREATE OR REPLACE TABLE mtid AS SELECT mt.q, t.tid FROM mt JOIN tup AS t ON {on}")
    parts = [f"""SELECT t.tid, r.path, r.usr, r.vf, r.vt, r.size, r.n_files FROM ({_roots_sql(tr.files('long', 'roots'))}) AS r
                 JOIN tup AS t ON t.c{i} = r.q""" for i, tr in enumerate(tiers)]
    con.execute(f"""CREATE OR REPLACE TABLE troots AS SELECT tid, path, usr, vf, min(vt) AS vt, any_value(size) AS size, any_value(n_files) AS n_files
        FROM ({' UNION ALL '.join(parts)}) GROUP BY tid, path, usr, vf""")
    t = step("drill/fold/long-roots", t, tuples=sl.count(con, "tup"), rows=sl.count(con, "troots"))
    con.execute(f"""CREATE OR REPLACE TABLE tdig AS SELECT tid, count(*)::BIGINT AS n, (sum({ROW_HASH}) % {U64})::UBIGINT AS h1,
        (sum(hash({ROW_HASH}, 'x')) % {U64})::UBIGINT AS h2 FROM troots GROUP BY tid""")
    con.execute("CREATE OR REPLACE TABLE dgq AS SELECT m.q, d.n, d.h1, d.h2 FROM mtid AS m JOIN tdig AS d USING (tid)")
    con.execute("CREATE OR REPLACE TABLE al AS SELECT q, min(q) OVER (PARTITION BY n, h1, h2) AS canonical FROM dgq")
    missing = con.execute("SELECT count(*) FROM dmem ANTI JOIN al USING (q)").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} members without roots in any tier: the runs' drills don't cover the folded catalog's members")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    con.execute("CREATE OR REPLACE TABLE canon AS SELECT DISTINCT canonical AS q FROM al")
    t = step("drill/fold/aliases", t, members=aliases.num_rows, canonical=sl.count(con, "canon"))
    con.execute(f"""CREATE OR REPLACE TABLE rt AS SELECT c.q, {DEPTH} AS depth, r.path, r.usr, r.vf, r.vt, r.size, r.n_files
        FROM canon AS c JOIN mtid AS m USING (q) JOIN troots AS r USING (tid)""")
    long_groups = sl.plan_groups(con, con.execute("SELECT q, count(*)::BIGINT FROM rt GROUP BY q ORDER BY q").fetchall(), chunk_rows)
    n_long = sl.count(con, "rt")
    sl.spill_roots(con, "rt", work / "long" / "fold")
    con.execute("DROP TABLE rt; DROP TABLE troots; DROP TABLE tdig; DROP TABLE mt; DROP TABLE mtid; DROP TABLE tup")
    for i in range(n):
        con.execute(f"DROP TABLE ta{i}")
    t = step("drill/fold/canonical-roots", t, lcan=n_long, groups=long_groups)
    parts = [f"SELECT * FROM ({_roots_sql(tr.files('short', 'roots'))})" for tr in tiers]
    con.execute(f"""CREATE OR REPLACE TABLE rt AS SELECT q, {DEPTH} AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY q, path, usr, vf""")
    short_groups = sl.plan_groups(con, con.execute("SELECT q, count(*)::BIGINT FROM rt GROUP BY q ORDER BY q").fetchall(), chunk_rows)
    n_short = sl.count(con, "rt")
    sl.spill_roots(con, "rt", work / "short" / "fold")
    con.execute("DROP TABLE rt")
    t = step("drill/fold/short-roots", t, sall=n_short, groups=short_groups)
    done = sl.finish_drill(con, out, work, aliases=aliases, long_groups=long_groups, short_groups=short_groups, R=R, K=K, floor=floor, step=step, t=t)
    return {"members": aliases.num_rows, "canonical": len(set(aliases.column("canonical").to_pylist())), "groups": done["groups"]}


ROW_HASH = "hash(path, usr, vf, vt, size, n_files)"


# ── The whole fold ─────────────────────────────────────────────────────────


def run_scans(gen_dir: Path, gen: str, runs: list[dict]) -> list[dict]:
    """The `scans.json` entries of every scan the runs hold (`deltas/<id>/scans.json`, kept on folded runs)."""
    out = []
    for r in runs:
        for sid in r["scans"]:
            p = gen_dir / ar.run_key(sid, sid) / "scans.json"
            if not p.is_file():
                raise SystemExit(f"{gen}: {p.relative_to(gen_dir)} missing: scan {sid}'s entry for the compacted `scans.json`")
            out.append(json.loads(p.read_text())["scans"][0])
    return out


def fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
             chunk_rows: int = sl.CHUNK_ROWS, log: Callable[[str], None] = err) -> dict:
    """Generation `gen` (its base in `gen_dir`, the runs `runs`: the newest manifest's, oldest first) folded into `new_gen`
    in `out` (every part but `compaction.json`); its `manifests/<last>.json` lists no runs. Returns a summary (rows, and
    per part its seconds, peak work-disk GiB and peak RSS GiB)."""
    t0, times, peaks = monotonic(), {}, {}
    if not runs:
        raise SystemExit(f"{gen}: no runs to compact")
    base = json.loads((gen_dir / "scans.json").read_text())
    rule = sn.gen_rule(base)
    run_dirs = [gen_dir / r["key"] for r in runs]
    days = [s for r in runs for s in r["scans"]]
    lack = [r["key"] for r in runs if not (gen_dir / r["key"] / "drill" / "meta.json").is_file()]
    if not (gen_dir / "drill" / "meta.json").is_file() or lack:
        raise SystemExit(f"{gen}: compaction needs every tier's drill/ (the base's{' and ' + ', '.join(lack) if lack else ''})")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = sl.DiskPeak(work)
    steps: dict[str, float] = {}

    def done(part: str, t: float) -> float:
        times[part] = round(monotonic() - t, 1)
        peaks[part] = {"disk_gb": disk.take(), "rss_gb": sl.rss_peak_gb()}
        log(f"compact {gen} → {new_gen}: {part} in {times[part]}s (work disk peak {peaks[part]['disk_gb']} GiB, RSS peak {peaks[part]['rss_gb']} GiB)")
        return monotonic()

    def step(name: str, t: float, **counts: int) -> float:
        steps[name] = round(monotonic() - t, 1)
        n = "".join(f", {k} {v:,}" for k, v in counts.items())
        log(f"compact {gen} → {new_gen}: {name} in {steps[name]}s (work disk peak {disk.take_step()} GiB, RSS peak {sl.rss_peak_gb()} GiB{n})")
        return monotonic()

    con = sn.connect(threads, mem, work / "duckdb")
    doc = dict(base)
    doc["scans"] = [*base["scans"], *run_scans(gen_dir, gen, runs)]
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    shutil.copyfile(gen_dir / "ranges.json", out / "ranges.json")
    ranges = json.loads((out / "ranges.json").read_text())
    t = monotonic()
    versions = 0
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        versions += fold_range(con, str(gen_dir / "cintervals" / f"{name}.parquet"),
                               [str(gen_dir / ar.run_key(d, d) / "cdelta" / f"{name}.parquet") for d in days], out, name, rule)
    t = done("intervals", t)
    old = json.loads((gen_dir / "shards.json").read_text())
    plan = sn.plan_shards(sorted((out / "chist").glob("*.parquet")), old["target_rows"], len(old["tasks"]))
    sa.merge_shards([gen_dir, *run_dirs], out, plan=plan)
    (out / "shards.json").write_text(json.dumps(plan, indent=1) + "\n")
    side = sn.concat_sidecars([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
    sn.write_sidecar(side, out / "sidecar.parquet")
    t = done("shards", t)
    membership = json.loads((gen_dir / "catalog" / "meta.json").read_text())["membership"]
    cat = fold_catalog(con, [gen_dir / "catalog", *(d / "catalog" for d in run_dirs)], out, work, new_gen=new_gen, plan=plan,
                       membership=membership, rule=rule)
    t = done("catalog", t)
    dm = json.loads((gen_dir / "drill" / "meta.json").read_text())
    tiers = [sd.Tier(gen_dir / "drill", "base"), *(sd.Tier(d / "drill", r["key"]) for d, r in zip(run_dirs, runs))]
    drill = fold_drill(con, tiers, out, work / "drill", R=dm["R"], K=dm["K"], floor=dm.get("floor", sl.FLOOR), chunk_rows=chunk_rows, step=step)
    t = done("drill", t)
    anchors = None
    if (gen_dir / an.ANCHORS / "meta.json").is_file():
        am = json.loads((gen_dir / an.ANCHORS / "meta.json").read_text())
        anchors = sl.build_base_anchors(con, out, R=am["R"], K=am["K"], scans=[s["id"] for s in doc["scans"]], rule=rule)
        t = done("anchors", t)
    (out / "manifests").mkdir()
    ids = [s["id"] for s in doc["scans"]]
    (out / "manifests" / f"{ids[-1]}.json").write_text(json.dumps(sa.manifest(new_gen, ids, []), indent=1) + "\n")
    disk.close()
    return {"from": gen, "gen": new_gen, "runs": [r["key"] for r in runs], "base_scans": len(base["scans"]), "scans": len(ids),
            "versions": versions, "suffix_rows": int(sum(side.column("rows").to_pylist())), "shards": len(plan["shards"]),
            "members": drill["members"], "cells_rows": cat["cells_rows"], "drill": drill, "anchors": anchors, "s": round(monotonic() - t0, 1),
            "stages": times, "steps": steps, "peaks": peaks}


def fold(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
         chunk_rows: int = sl.CHUNK_ROWS, jobs: int = 1) -> dict:
    """`append_runner.Compactor.fold` for the static store (`fold_gen`; `jobs` unused: one range at a time)."""
    return fold_gen(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows)


def compactor(chunk_rows: int = sl.CHUNK_ROWS) -> ar.Compactor:
    """The static store's plug into the shared compaction (`append_runner.compact_store`): its fold and upload order; its
    predecessors are left to `store-gc -k` (readers pin `STATIC_GEN`, so a compacted generation may still be served)."""
    return ar.Compactor(prefix=PREFIX, fold=lambda *a, **kw: fold(*a, **kw, chunk_rows=chunk_rows), publish_order=sl.publish_order,
                        last=("scans.json", "catalog/meta.json", "drill/meta.json", f"{an.ANCHORS}/meta.json"), prune=False, md5s=True)


# ── CLI (one machine) ──────────────────────────────────────────────────────


@command("compact")
@option("-c", "--chunk-rows", default=sl.CHUNK_ROWS, type=IntRange(min=1), help="The drill's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="The generation (`<store>/static-names/GEN/`); one compacted already is followed to its newest successor")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the generation is mirrored into it")
def compact_cmd(chunk_rows, gen, mem, store_root, threads, work) -> None:
    """Fold the generation's base and its newest manifest's runs into a new generation now (`<gen>.c<NNN>`, equal to
    `build -r <gen>/ranges.json` through the last scan), whatever the compaction policy says; published (data, then
    `compaction.json`), then `successor.json` in the old generation and `static-names/CURRENT.json` when it named it (or
    nothing). The old generation stays (`store-gc -k` collects it). Prints a JSON summary."""
    from .blobstore import Store, mirror

    sl._check_gen(gen)
    store = Store(store_root)
    gen = ar.resolve_gen(store, PREFIX, gen)
    prefix = f"{PREFIX}/{gen}"
    with sl.workdir(work) as w:
        gen_dir = w / "store" / prefix
        mirror(store, prefix, gen_dir)
        why = ar.forced(gen_dir, "forced (compact)")
        if why is None:
            raise SystemExit(f"{store.root}/{prefix}: no runs to compact")
        out = ar.compact_store(store, compactor(chunk_rows), gen, gen_dir, w, policy=ar.NEVER, why=why, threads=threads, mem=mem)
    print(json.dumps(out, indent=1))
