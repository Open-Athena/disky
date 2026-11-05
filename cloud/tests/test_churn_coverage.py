"""`interval-store churn-coverage` / `churn-index-model`: the walk's replay and the index's arithmetic, on tiny inline stores.

The tree, bytes at both scans (a, b), churn over the range:

    b1            (100, 100)  churn 44   floor at w=h=100, min_area 100 (k = 0.01): 1.01 at depth 1, 2.02 at depth 2 (atten 2)
    b1/big        (60, 60)    churn 0    drawn, unchanged
    b1/hot        (1, 1)      churn 40   under the depth-2 floor: `(other)`, the heavy small directory
    b1/warm       (10, 12)    churn 4    drawn
    b2            (1, 1)      churn 6    a bucket, drawn as a leaf at any floor
"""
import json
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from dt_cloud.churn_coverage import Node, blocks_of, connect, level_counts, ordinals_table, replay


def world():
    changed = [
        Node("b1", 1, 100, 100, 44),
        Node("b1/hot", 2, 1, 1, 40),
        Node("b1/warm", 2, 10, 12, 4),
        Node("b2", 1, 1, 1, 6),
    ]
    live = [
        Node("b1", 1, 100, 100, 0),
        Node("b1/big", 2, 60, 60, 0),
        Node("b1/warm", 2, 10, 12, 0),
        Node("b2", 1, 1, 1, 0),
    ]
    return changed, live


def test_a_small_heavy_directory_lands_in_its_parents_other():
    changed, live = world()
    r = replay("a", "b", "", (101, 101), 50, changed, live, 100, 100, min_area=100)
    # Cells with churn: b1, b1/warm, b2; b1/big is filler.
    assert (r.floor, r.churn, r.expanded, r.drawn) == (1.01, 50, 2, 3)
    assert (r.other_share, r.root_other_share) == (40 / 50, 0.0)
    assert r.worst_other_share == 40 / 44
    assert (r.largest_undrawn, r.largest_undrawn_share, r.largest_undrawn_size, r.largest_undrawn_path) == (40, 40 / 50, 1, "b1/hot")
    assert (r.tau, r.missed, r.missed_share, r.hidden_in_leaves) == (0.5, 1, 40 / 50, 0)


def test_a_path_live_at_neither_scan_stays_in_other_at_any_floor():
    changed = [Node("b1", 1, 50, 50, 30), Node("b1/gone", 2, 0, 0, 30)]
    live = [Node("b1", 1, 50, 50, 0), Node("b1/here", 2, 50, 50, 0)]
    r = replay("a", "b", "", (50, 50), 30, changed, live, 100, 100, area_factor=0.001, min_area=100)
    assert (r.other_share, r.absent_both_share, r.absent_both_largest, r.largest_undrawn_path) == (1.0, 1.0, 30, "b1/gone")


def test_a_lower_floor_draws_it():
    changed, live = world()
    live = [*live, Node("b1/hot", 2, 1, 1, 0)]
    r = replay("a", "b", "", (101, 101), 50, changed, live, 100, 100, area_factor=0.1, min_area=100)
    assert (r.floor, r.other_share, r.largest_undrawn, r.missed, r.drawn) == (0.101, 0.0, 0, 0, 4)


def test_a_rename_under_identical_bytes_expands():
    changed = [Node("b1", 1, 100, 100, 200), Node("b1/x", 2, 100, 0, 100), Node("b1/y", 2, 0, 100, 100)]
    live = [Node("b1", 1, 100, 100, 0), Node("b1/x", 2, 100, 0, 0), Node("b1/y", 2, 0, 100, 0)]
    r = replay("a", "b", "", (100, 100), 200, changed, live, 100, 100, min_area=100)
    assert (r.expanded, r.drawn, r.other_share) == (2, 3, 0.0)


def test_top_folds_the_smallest_leaves():
    changed = [Node(f"b1/{n}", 2, 50, 51, c) for n, c in (("p", 9), ("q", 7), ("r", 4))]
    live = [Node("b1", 1, 150, 153, 0), *(Node(f"b1/{n}", 2, 50, 51, 0) for n in "pqr")]
    r = replay("a", "b", "", (150, 153), 20, [Node("b1", 1, 150, 153, 20), *changed], live, 100, 100, min_area=100, top=1)
    assert (r.drawn, r.top_cut_share, r.other_share) == (2, 11 / 20, 11 / 20)


@pytest.mark.parametrize("i1,i2,blocks", [
    (0, 1, [(0, 0)]),
    (0, 8, [(3, 0)]),
    (3, 8, [(0, 3), (2, 1)]),
    (5, 12, [(0, 5), (1, 3), (2, 2)]),
    (113, 120, [(0, 113), (1, 57), (2, 29)]),
])
def test_canonical_blocks(i1, i2, blocks):
    assert blocks_of(i1, i2) == blocks
    # A partition of the transitions (i1, i2].
    assert sorted(j for L, b in blocks for j in range((b << L) + 1, ((b + 1) << L) + 1)) == list(range(i1 + 1, i2 + 1))


def pvl(path: Path, rows: list[tuple]) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (depth INT, path VARCHAR, kind VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT, churn_cum BIGINT, churn_end BIGINT)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")


OPEN = 1 << 62
# Scans 10, 20, 30, 40, 50 (ordinals 0..4). `x`: changes at 20, gone at 30, back at 50. `y`: appears at 40, no churn before.
ROWS = [
    (1, "b", "dir", 10, 20, 8, 1, 0, 2),
    (1, "b", "dir", 20, 30, 10, 1, 2, 12),
    (1, "b", "dir", 30, 40, 0, 0, 12, 19),
    (1, "b", "dir", 40, 50, 7, 1, 19, 19),
    (1, "b", "dir", 50, OPEN, 16, 2, 28, None),
    (2, "b/x", "file", 10, 20, 8, 1, 0, 2),
    (2, "b/x", "file", 20, 30, 10, 1, 2, 12),
    (2, "b/x", "file", 50, OPEN, 9, 1, 21, None),
    (2, "b/y", "file", 40, OPEN, 7, 1, 7, None),
]


def test_level_counts_count_changes_and_blocks(tmp_path):
    f = tmp_path / "pvl" / "r0000.parquet"
    pvl(f, ROWS)
    con = connect(1, "1GB", str(tmp_path / "duck"))
    ordinals_table(con, [10, 20, 30, 40, 50])
    e, rows, by_depth = level_counts(con, tmp_path, 3, str(f))
    # Changes: b at 20, 30, 40, 50 (4); b/x at 20, 30, 50 (3); b/y at 40 (1).
    assert (e, by_depth) == (8, {1: 4, 2: 4})
    # Blocks of (i − 1) >> L, i the scan's ordinal: L0 one each; L1 b {0, 1}, b/x {0, 1}, b/y {1}; L2 b {0}, b/x {0}, b/y {0}.
    assert rows == [8, 5, 3]


def test_the_commands_over_a_tiny_generation(tmp_path):
    from dt_cloud.churn_coverage import churn_coverage_cmd, churn_index_model_cmd
    gen = tmp_path / "g"
    pvl(gen / "pvl" / "r0000.parquet", ROWS)
    (gen / "scans.json").write_text(json.dumps({"scans": [{"id": f"s{i}", "ts": (i + 1) * 10} for i in range(5)]}))
    (gen / "served").mkdir()
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 0 AS rg, 1 AS d_min, 2 AS d_max, 'b' AS p_min, 'b/y' AS p_max, 10::BIGINT AS vf_min, {OPEN}::BIGINT AS vt_max) TO '{gen}/served/path.groups.parquet' (FORMAT parquet)")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"from": "s1", "to": "s4", "path": ""}]))
    out, model = tmp_path / "out.jsonl", tmp_path / "model.json"
    work = tmp_path / "work"
    res = CliRunner().invoke(churn_coverage_cmd, ["-w", "100", "-H", "100", "-o", str(out), "-p", "1", "-M", "1GB", "-T", str(work), str(gen), str(cases)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    r = json.loads(out.read_text())
    # (20, 50]: b's cumulative churn 2 → 28.
    assert (r["churn"], r["other_share"], r["drawn"]) == (26, 0.0, 3)
    res = CliRunner().invoke(churn_index_model_cmd, ["-w", "100", "-H", "100", "-o", str(model), "-p", "1", "-M", "1GB", "-T", str(work), "-m", "12", "-s", "1", str(gen), str(cases)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    m = json.loads(model.read_text())
    assert (m["sizing"]["E"], m["sizing"]["rows_per_level"], m["sizing"]["levels"]) == (8, [8, 5, 3], 3)
    read = m["reads"][0]
    # (s1, s4] = ordinals (1, 4]: blocks (0, 1), (1, 1).
    assert (read["m"], read["churn"], read["exact_groups"]) == (2, 26, 1)
