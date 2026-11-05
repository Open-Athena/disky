"""Which served sorts have pages (`interval_store.served_page_rows`, specs/diff-progressive.md §3b): the level sort always, `path`
only in a generation without the level sort, the rest never. A sort without them is what the served sorts were before
they had pages. Through a build, appends, carries, a compaction and `backfill-level`, whichever way a generation got its
flag; a generation holding both kinds of file (a backfill mid-life) still folds and carries to a fresh build's bytes."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_level import _add, _build, _cli, _newest_runs, _rows
from test_interval_local import BASE, DATES, RG, world  # noqa: F401 — the shared scans

from dt_cloud import interval_level as ilv
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud import tree_mem as tm
from dt_cloud.parquet_pages import chunk_indexes, page_locations

#: An unpaged sort's statistics; a paged one's add `path` (its pages' bounds).
UNPAGED_STATS = {"depth", "vf", "vt"}


def _indexed(f: Path) -> bool:
    """Whether served sort `f` has a page index: every chunk of every group (a sort is one or the other, never between),
    its sidecar's compact tuples agreeing."""
    has = {bool(ix.offset_index_length) for g in chunk_indexes(f) for ix in g}
    assert len(has) == 1, f
    tuples = {len(c) for r in pq.read_table(f.with_name(f.name.removesuffix(".parquet") + ".groups.parquet")).to_pylist() for c in json.loads(r["rg_json"])[2]}
    # A tuple's page-index ranges (4) follow its 3 offsets.
    assert tuples == ({7} if True in has else {3}), f
    return True in has


def _stats(f: Path) -> set[str]:
    md = pq.read_metadata(f)
    rg = md.row_group(0)
    return {rg.column(c).path_in_schema for c in range(md.num_columns) if rg.column(c).is_stats_set}


def _paged(d: Path) -> dict[str, bool]:
    """Each served sort in dir `d` (a base's or a run's `served/`) with rows: whether it has a page index."""
    return {p.stem: _indexed(p) for p in sorted(d.glob("*.parquet")) if not p.name.endswith(".groups.parquet") and pq.read_metadata(p).num_row_groups}


def _sorts(d: Path, pages: set[str]) -> dict[str, bool]:
    """What `_paged(d)` should be: its sorts, `pages` of them paged."""
    return {p.stem: p.stem in pages for p in sorted(d.glob("*.parquet")) if not p.name.endswith(".groups.parquet") and pq.read_metadata(p).num_row_groups}


def _assert_paged(d: Path, pages: set[str], must: set[str]) -> None:
    got = _paged(d)
    assert got == _sorts(d, pages), d
    assert must <= set(got), (d, sorted(got))
    for sort, paged in got.items():
        if sort != ilv.LEVEL:
            assert _stats(d / f"{sort}.parquet") & {"path"} == ({"path"} if paged else set()), (d, sort)


@pytest.fixture(scope="module")
def plain(world, tmp_path_factory):  # noqa: F811
    """A base without the level sort, the rest added (the carries merge them)."""
    tmp = tmp_path_factory.mktemp("plain")
    store = tmp / "store"
    _build(world, store, tmp / "wb")
    _add(world, store, tmp / "wa")
    return store / ist.PREFIX / "g"


@pytest.fixture(scope="module")
def leveled(world, tmp_path_factory):  # noqa: F811
    """A `-l` base, the rest added (the carries merge them), and a fresh `-l` build over every scan."""
    tmp = tmp_path_factory.mktemp("lvl")
    store = tmp / "store"
    _build(world, store, tmp / "wb", "-l")
    _add(world, store, tmp / "wa")
    full = tmp / "full"
    _build(world, full, tmp / "wf", "-l", through=DATES[-1])
    return {"store": store, "gen": store / ist.PREFIX / "g", "full": full / ist.PREFIX / "g"}


def test_without_the_level_sort_only_path_has_pages(plain):
    """Its diffs read `path` by page; nothing reads the others'. Base, level-0 runs and merged runs alike."""
    for d in [plain, *(plain / "deltas" / x for x in DATES[BASE:]), *_newest_runs(plain)]:
        _assert_paged(d / "served", {"path"}, {"path", "bysize"})


def test_with_the_level_sort_only_it_has_pages(leveled):
    """Its diffs read the level sort; `path` is a fallback read whole. Base, level-0 runs and merged runs alike."""
    gen = leveled["gen"]
    for d in [gen, *(gen / "deltas" / x for x in DATES[BASE:]), *_newest_runs(gen)]:
        _assert_paged(d / "served", {ilv.LEVEL}, {"path", "bysize", ilv.LEVEL})


def test_a_compaction_keeps_the_generations_rule(world, leveled, plain, tmp_path):  # noqa: F811
    for src, pages, name in ((leveled["gen"], {ilv.LEVEL}, "l"), (plain, {"path"}, "p")):
        store = tmp_path / name / "store"
        shutil.copytree(src.parent.parent, store)
        out = _add(world, store, tmp_path / name / "w", "-K")
        _assert_paged(store / ist.PREFIX / out["compact"]["gen"] / "served", pages, {"path", "bysize"})


def test_a_backfill_mid_life(world, leveled, tmp_path):  # noqa: F811
    """`backfill-level` writes only the level sort: the base and the runs it finds keep `path`'s pages, later runs and carries
    drop them (the flag is the generation's now). A carry over both kinds, and a compaction over them, are unaffected: they
    read delta and range files, never the served sorts."""
    store = tmp_path / "store"
    _build(world, store, tmp_path / "wb")
    _add(world, store, tmp_path / "wa", "-t", DATES[-3], "-N")
    gen = store / ist.PREFIX / "g"
    before = DATES[BASE:-2]
    assert len(before) >= 1
    _cli(ilv.backfill_cmd, ["-g", "g", "-o", str(store), "-w", str(tmp_path / "wl"), "-r", str(RG), "-p", "2", "-M", "1GB"])
    _add(world, store, tmp_path / "wa2", "-t", DATES[-2], "-N")
    for d in [gen, *(gen / "deltas" / x for x in before)]:
        _assert_paged(d / "served", {"path", ilv.LEVEL}, {"path", ilv.LEVEL})
    _assert_paged(gen / "deltas" / DATES[-2] / "served", {ilv.LEVEL}, {"path", ilv.LEVEL})

    # `cut -s path`: by the generation it writes, whose backfill recorded itself by its doc, not in `scans.json`.
    out = tmp_path / "cut"
    res = CliRunner().invoke(ist.cli, ["cut", "-b", str(store), "-g", "g", "-m", str(store), "-o", str(out), "-T", str(tmp_path / "t"), "-p", "2", "-M", "1GB", "-U",
                                       "-r", str(RG), "-s", "path"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert _paged(out / "served") == {"path": False}

    # The next add's carries, over runs of both kinds: their output a run cut now.
    merged = tmp_path / "merged"
    shutil.copytree(store, merged)
    _add(world, merged, tmp_path / "wm")
    carried = _newest_runs(merged / ist.PREFIX / "g")
    assert [("_" in r.name, (r / "served" / "path.parquet").is_file()) for r in carried] == [(True, True)]
    _assert_paged(carried[0] / "served", {ilv.LEVEL}, {"path", ilv.LEVEL})
    # Compacted, the mix is a fresh `-l` build's `path`, byte for byte.
    full = leveled["full"]
    compact = tmp_path / "compact"
    shutil.copytree(store, compact)
    out = _add(world, compact, tmp_path / "wc", "-K")
    new_gen = compact / ist.PREFIX / out["compact"]["gen"]
    _assert_paged(new_gen / "served", {ilv.LEVEL}, {"path", ilv.LEVEL})
    assert (new_gen / "served" / "path.parquet").read_bytes() == (full / "served" / "path.parquet").read_bytes()
    # The level sort's segments follow the generation's key ranges (the base's, here): the same rows, cut differently.
    assert _rows(new_gen / "served" / f"{ilv.LEVEL}.parquet") == _rows(full / "served" / f"{ilv.LEVEL}.parquet")


def test_an_unpaged_sort_is_the_writer_before_pages(tmp_path):
    """The bytes of pyarrow's writer as `write_served` called it before 2b.1 (`data_page_size`, `write_batch_size` left alone),
    long paths taking a chunk past its 1 MiB default into two pages, and no page index in the sidecar's tuples."""
    n = 8192
    t = pa.table({"depth": pa.array([3] * n, pa.uint8()), "path": [f"bucket/{'x' * 200}/{i:08d}" for i in range(n)],
                  "kind": ["file"] * n, "us": pa.array([None] * n, pa.string()), "vf": pa.array(range(n), pa.int64()), "vt": pa.array(range(n), pa.int64())})
    got, ref = tmp_path / "got.parquet", tmp_path / "ref.parquet"
    with ist._served_writer(got, t.schema, None) as w:
        w.write_table(t, row_group_size=n)
    with pq.ParquetWriter(ref, t.schema, compression="zstd", use_dictionary=["kind", "us"], write_statistics=["depth", "vf", "vt"]) as w:
        w.write_table(t, row_group_size=n)
    assert got.read_bytes() == ref.read_bytes()
    assert _stats(got) == UNPAGED_STATS
    assert [bool(ix.offset_index_length) for ix in chunk_indexes(got)[0]] == [False] * 6
    # Where those pages fall: the same writer with its index on (it doesn't move a page), a batch of 1,024 values at a time.
    indexed = tmp_path / "indexed.parquet"
    with pq.ParquetWriter(indexed, t.schema, compression="zstd", use_dictionary=["kind", "us"], write_statistics=["depth", "vf", "vt"], write_page_index=True) as w:
        w.write_table(t, row_group_size=n)
    assert [p.first_row_index for p in page_locations(indexed, chunk_indexes(indexed)[0][1])] == [0, 5120]


def test_the_plan_charges_each_sorts_pages(leveled, monkeypatch, tmp_path):
    """`cut_gen`'s plan: what the costliest `at_once` sorts' writers keep, each by its own columns and pages (none of the
    unpaged sorts'), as the `meta` a group `Budget.cut` multiplies by `at_once` sorts of the most groups."""
    got = []
    real = tm.Budget.cut

    def cut(self, at_once, cores, rg_rows, rb, spool_rows, rows=None, *, meta=0, sort_rows=None):
        got.append({"at_once": at_once, "meta": meta, "sort_rows": sort_rows})
        return real(self, at_once, cores, rg_rows, rb, spool_rows, rows, meta=meta, sort_rows=sort_rows)

    monkeypatch.setattr(tm.Budget, "cut", cut)
    gen = leveled["gen"]
    scans = json.loads((gen / "scans.json").read_text())
    tree = tm.Budget.measure(sn.mem_bytes("1GB"))
    con = duckdb.connect()
    ist.cut_gen(con, gen, scans, rg_rows=RG, jobs=2, tree=tree, served=tmp_path / "served")
    has_gen, churn = ist.has_generation(scans), ist.has_churn(scans)
    todo = ist.served_sorts(scans["slices"], ist.attributed(scans), True)
    from dt_cloud.interval_compact import input_rows

    def files(s: str) -> list[str]:
        return [str(p) for p in (gen / ist.SORTS[s][0]).glob("r*.parquet")]

    pvl = sorted(files("path"))
    extra = ilv.extra_rows(duckdb.connect(), pvl)
    rows = {s: input_rows(files(s)) + (extra if s == ilv.LEVEL else 0) for s in todo}
    charge = {}
    for s in todo:
        cols = len(ist.served_schema(s, has_gen, churn))
        groups = -(-rows[s] // RG)
        # The level sort's pages charge a page's entries; the rest nothing a page.
        charge[s] = groups * (ist.writer_meta(files(s), RG, 256, cols=cols) if s == ilv.LEVEL else tm.group_meta(cols, 0))
    most = max(rows.values())
    want = -(-sum(sorted(charge.values(), reverse=True)[:2]) // (2 * -(-most // RG)))
    assert got == [{"at_once": 2, "meta": want, "sort_rows": most}]
