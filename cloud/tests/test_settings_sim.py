"""`interval-store settings-sample` / `settings-sim` over the committed `iv-churn` generations (see `test_layout_sim.py` for the base's rows): `interval-store-p2` is the same
sort cut in 2-row pages, with a page index.

`b1/x` over 08-02 → 08-03 (rects: children at depth 3, its own row at depth 2): whole-group reads of rg0, rg1, rg5, rg6; by page: rg0's page 1 (rows 2-3, `b1/a.bin` and `b1/x`, both
opened at s3), rg1's page 1 (rows 2-3, `b1/x/b.bin` and `b1/x/c.bin`), rg5's page 1 (rows 2-3, `b1/x` and `b1/x/c.bin`, closing at s3); rg6, of two rows in one kept page, whole.
"""
import json
from pathlib import Path

import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud.blobstore import Store
from dt_cloud.cli import main
from dt_cloud.settings_sim import PageIx, Rect, data_spans, level_rects, parse_settings, plan_group, read_side, sample_groups, widen

ROOT = Path(__file__).parents[2]
FX = ROOT / "site/functions/_lib/fixtures/iv-churn"
PAGED = FX / "interval-store-p2/g"
S2, S3 = 1785628800, 1785715200


def ix4() -> PageIx:
    """Four 2-row pages of `depth`, `path`, `vf`, `vt`, and a column `us` of one page (all null)."""
    pages = [(100 + 10 * i, 10, 2 * i) for i in range(4)]
    return PageIx(
        locs={"depth": pages, "path": pages, "vf": pages, "vt": pages, "us": [(900, 5, 0)]},
        nulls=[False] * 4,
        depth=[(1, 1), (2, 2), (2, 2), (3, 3)],
        path=[("a", "b"), ("c", "d"), ("e", "f"), ("g", "h")],
        vf=[(1, 1), (5, 5), (1, 1), (5, 5)],
        vt=[(9, 9)] * 4,
    )


def test_plan_group_keeps_pages_meeting_a_rect_and_the_range():
    rects = [Rect(2, 2, "c", "f")]
    assert plan_group(ix4(), 8, rects, 4, 5, ["depth", "path"]) == ([(2, 4)], 1, 3)


def test_plan_group_merges_ranges_sharing_a_page_of_a_read_column():
    rects = [Rect(1, 3, "a", "h")]
    # Pages 1 and 3 kept. Apart unless a read column's one page spans them, as hyparquet reads them.
    assert plan_group(ix4(), 8, rects, 4, 5, ["depth", "path"]) == ([(2, 4), (6, 8)], 2, 2)
    assert plan_group(ix4(), 8, rects, 4, 5, ["depth", "us"]) == ([(2, 8)], 2, 2)


def test_plan_group_every_row_kept_reads_whole():
    assert plan_group(ix4(), 8, [Rect(1, 3, "a", "h")], 0, 5, ["depth"]) is None


def test_widen_unions_bounds_and_sums_bytes():
    w = widen(ix4(), 2, 8)
    assert w.depth == [(1, 2), (2, 3)]
    assert w.path == [("a", "d"), ("e", "h")]
    assert w.vf == [(1, 5), (1, 5)]
    assert w.locs["depth"] == [(100, 20, 0), (120, 20, 4)]
    assert w.locs["us"] == [(900, 5, 0)]


def test_data_spans_are_each_columns_pages_and_dictionary():
    ix = ix4()
    # Columns `depth` (no dictionary) and `us` (its one page after a dictionary at 880).
    chunks = [[100, 40, 0], [100, 40, 0], [100, 40, 0], [100, 40, 0], [900, 25, 880]]
    assert data_spans(ix, ["depth", "path", "vf", "vt", "us"], chunks, [(2, 6)], 8, ["depth", "us"]) == [(110, 130), (880, 900), (900, 905)]


def test_level_rects():
    assert level_rects(["", "a/b"]) == [Rect(1, 1, "", "￿"), Rect(3, 3, "a/b/", "a/b0"), Rect(2, 2, "a/b", "a/b")]


def test_settings():
    assert parse_settings("A=none,D=256") == {"A": None, "D": 256}
    assert sample_groups(10, 3) == [0, 3, 6]


def test_sample_keeps_rows_and_groups(tmp_path):
    res = CliRunner().invoke(main, ["interval-store", "settings-sample", "-j", "1", "-n", "4", "-s", "A=none,D=2,E=4", "-o", str(tmp_path / "s"), str(PAGED / "served/path.parquet")])
    assert res.exit_code == 0, res.output
    sizes = json.loads((tmp_path / "s/sizes.json").read_text())
    assert {k: (v["page"], v["groups"], v["rows"]) for k, v in sizes["variants"].items()} == {"A": (None, 4, 12), "D": (2, 4, 12), "E": (4, 4, 12)}
    assert sum(sizes["variants"]["A"]["ci"].values()) == 0
    assert sum(sizes["variants"]["D"]["ci"].values()) > sum(sizes["variants"]["E"]["ci"].values()) > 0
    rows = {k: pq.read_table(tmp_path / f"s/{k}.parquet").to_pylist() for k in "ADE"}
    assert rows["A"] == rows["D"] == rows["E"]
    assert json.loads((tmp_path / "s/meta.json").read_text())["schema"][0]["name"] == "schema"


def test_sim_b1x(tmp_path):
    (tmp_path / "s").mkdir()
    sample = CliRunner().invoke(main, ["interval-store", "settings-sample", "-j", "1", "-n", "8", "-s", "A=none,D=2,E=4", "-o", str(tmp_path / "s"), str(PAGED / "served/path.parquet")])
    assert sample.exit_code == 0, sample.output
    reqs = tmp_path / "reqs.jsonl"
    reqs.write_text(json.dumps({"case": "x", "a": S2, "b": S3, "dirs": ["b1/x"]}) + "\n")
    out = tmp_path / "out.jsonl"
    res = CliRunner().invoke(main, ["interval-store", "settings-sim", "-a", str(PAGED), "-d", str(PAGED), "-S", str(tmp_path / "s/sizes.json"), "-p", "2,4", "-o", str(out), str(reqs)])
    assert res.exit_code == 0, res.output
    got = json.loads(out.read_text())
    assert (got["case"], got["n_dirs"], got["kept"]) == ("x", 1, 4)
    p2 = got["modes"]["p2"]
    assert {k: p2[k] for k in ("groups", "paged_groups", "whole_groups", "pages", "pskip", "rows", "paged_rows")} == {
        "groups": 4, "paged_groups": 3, "whole_groups": 1, "pages": 3, "pskip": 3, "rows": 8, "paged_rows": [2, 2, 2],
    }
    # Pages of 4 are every group's whole: nothing skipped, so each reads whole, after the index.
    p4 = got["modes"]["p4"]
    assert {k: p4[k] for k in ("groups", "paged_groups", "whole_groups", "rows")} == {"groups": 4, "paged_groups": 0, "whole_groups": 4, "rows": 14}
    assert got["modes"]["whole"]["rows"] == 14
    assert read_side(Store(str(PAGED)), "served/path.groups.parquet").plan(level_rects(["b1/x"]), S2, S3) == [0, 1, 5, 6]
