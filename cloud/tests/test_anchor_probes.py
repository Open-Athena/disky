from types import SimpleNamespace

import duckdb
import pytest

from dt_cloud import static_anchors as an


@pytest.mark.parametrize("batch_keys, queries", [(1, 4), (256, 2)])
def test_prior_probes_are_exact_and_batched_by_shard(tmp_path, batch_keys, queries):
    con = duckdb.connect()
    con.execute("CREATE TABLE source(s VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf TIMESTAMP, vt TIMESTAMP, size BIGINT, n_files BIGINT)")
    rows = [
        ("foo", "b/a/x", 1), ("foo", "b/a/deep/y", 2),
        ("foo", "b/ab/not-covered", 3), ("foo", "b/z/é", 4),
        ("bar", "b/elsewhere/x", 5), ("bar", "b/a/x", 6),
        ("other", "b/a/x", 7),
    ]
    con.executemany("INSERT INTO source VALUES (?, 3, ?, NULL, TIMESTAMP '2026-10-01', TIMESTAMP '2027-01-01', ?, 1)", rows)
    file = str(tmp_path / "source.parquet")
    con.execute(f"COPY source TO {an.q(file)} (FORMAT PARQUET)")
    con.execute("CREATE TABLE near(k VARCHAR, dir VARCHAR)")
    con.executemany("INSERT INTO near VALUES (?, ?)", [
        ("foo", "b/a"), ("foo", "b/a/deep"), ("foo", "b/a"),
        ("foo", "b/z"), ("bar", ""), ("bar", "b/a"), ("missing", "b/a"),
    ])
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file if key != "missing" else None)
    assert an.probe_prior_rows(con, [tier, tier], "end", batch_keys=batch_keys) == queries
    assert con.execute("SELECT k, path, size FROM prows ORDER BY k, path, size").fetchall() == [
        ("bar", "b/a/x", 6), ("bar", "b/a/x", 6),
        ("bar", "b/elsewhere/x", 5), ("bar", "b/elsewhere/x", 5),
        ("foo", "b/a/deep/y", 2), ("foo", "b/a/deep/y", 2),
        ("foo", "b/a/x", 1), ("foo", "b/a/x", 1),
        ("foo", "b/z/é", 4), ("foo", "b/z/é", 4),
    ]
    assert con.execute("SELECT k, lo, hi FROM probe_ranges ORDER BY k, lo").fetchall() == [
        ("bar", "", None), ("foo", "b/a/", "b/a0"),
        ("foo", "b/z/", "b/z0"), ("missing", "b/a/", "b/a0"),
    ]


def test_probe_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="^batch_keys must be positive$"):
        an.probe_prior_rows(None, [], "end", batch_keys=0)


def test_probe_join_state_is_bounded_by_the_current_key_batch(tmp_path):
    import json

    con = duckdb.connect()
    con.execute("CREATE TABLE source AS SELECT CASE WHEN i < 400 THEN 'foo' ELSE 'bar' END AS s, 3::UTINYINT AS depth, 'b/a/' || i AS path, NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf, TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files FROM range(800) AS r(i)")
    file = str(tmp_path / 'source.parquet')
    con.execute(f'COPY source TO {an.q(file)} (FORMAT PARQUET)')
    con.execute("CREATE TABLE near AS SELECT 'unused' || i AS k, 'b/a' AS dir FROM range(20) AS r(i) UNION ALL SELECT 'foo', 'b/a' UNION ALL SELECT 'bar', 'b/a'")
    con.execute(f'CREATE TABLE prows({an.AR_COLS})')
    profile = tmp_path / 'profile.json'
    con.execute("SET enable_profiling='json'")
    con.execute(f'SET profiling_output={an.q(str(profile))}')
    right_rows = []

    def joins(node):
        if node.get('operator_name') == 'ASOF_JOIN':
            yield node
        for child in node.get('children', []):
            yield from joins(child)

    class Observed:
        def execute(self, sql):
            result = con.execute(sql)
            if sql.startswith('INSERT INTO prows'):
                right_rows.extend(node['children'][1]['operator_cardinality'] for node in joins(json.loads(profile.read_text())))
            return result

    tier = SimpleNamespace(shard_file=lambda mode, key: file if key in ('foo', 'bar') else None)
    assert an.probe_prior_rows(Observed(), [tier], 'end', batch_keys=1) == 2
    assert right_rows == [1, 1]
    assert con.execute('SELECT k, count(*) FROM prows GROUP BY k ORDER BY k').fetchall() == [('bar', 400), ('foo', 400)]


def test_candidate_bounds_include_unknown_statistics_and_exact_edges():
    con = duckdb.connect()
    con.execute('CREATE TABLE cand(k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO cand VALUES (?, ?, ?, ?)', [
        ('foo', 0, '', 1), ('foo', 1, 'a', 1), ('foo', 1, 'b', 1),
        ('foo', 1, 'c', 1), ('missing', 0, '', 1),
    ])
    con.execute('CREATE TABLE ktk(k VARCHAR, p_min VARCHAR, p_max VARCHAR, rows BIGINT)')
    con.executemany('INSERT INTO ktk VALUES (?, ?, ?, ?)', [
        ('foo', None, None, 2), ('foo', 'a/a', 'a/m', 3),
        ('foo', 'a/z', 'b/c', 5), ('foo', 'b/a', 'b/z', 7),
        ('foo', 'b/z', None, 11), ('foo', None, 'a/a', 13),
        ('foo', 'b/a', 'b/z', 17),
        ('other', None, None, 999),
    ])
    an.near_candidates(con, 0)
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('foo', 0, '', 1, 58), ('foo', 1, 'a', 1, 23),
        ('foo', 1, 'b', 1, 42), ('foo', 1, 'c', 1, 13),
        ('missing', 0, '', 1, 0),
    ]
    an.near_candidates(con, 24)
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('foo', 0, '', 1, 58), ('foo', 1, 'b', 1, 42),
    ]


def test_run_tiers_bound_by_the_lesser_of_their_groups_and_the_keys_rows():
    """The base by its groups; the run tiers by their groups or the key's rows over them, whichever is less: a run
    group holding other keys spans their paths, so it meets every `dir`, but `foo` has only 3 rows there."""
    con = duckdb.connect()
    con.execute('CREATE TABLE cand(k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO cand VALUES (?, ?, ?, ?)', [('foo', 0, '', 1), ('foo', 1, 'a', 1), ('foo', 1, 'b', 1), ('big', 1, 'b', 1)])
    con.execute('CREATE TABLE ktk(k VARCHAR, t INTEGER, p_min VARCHAR, p_max VARCHAR, rows BIGINT)')
    con.executemany('INSERT INTO ktk VALUES (?, ?, ?, ?, ?)', [
        ('foo', 0, 'a/a', 'a/m', 200),   # the base: only foo's rows, under a/
        ('foo', 1, 'a', 'z/z', 8000),    # a run: a group of many keys
        ('foo', 2, 'a', 'z/z', 8000),    # another
        ('big', 1, 'b/a', 'b/z', 300),   # a run group of big's own rows: tighter than its count
    ])
    con.execute('CREATE TABLE rc(k VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO rc VALUES (?, ?)', [('foo', 3), ('big', 5000)])
    an.near_candidates(con, 100, "rc")
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('big', 1, 'b', 1, 300), ('foo', 0, '', 1, 203), ('foo', 1, 'a', 1, 203),
    ]
    an.near_candidates(con, 100, None)
    assert con.execute('SELECT k, dir, bound FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('big', 'b', 300), ('foo', '', 16200), ('foo', 'a', 16200), ('foo', 'b', 16000),
    ]


def test_candidate_bounds_scale_with_many_directories_of_one_key():
    con = duckdb.connect()
    con.execute("CREATE TABLE cand AS SELECT 'key' AS k, 1 AS lvl, printf('b/%06d', i) AS dir, 1::BIGINT AS n FROM range(20000) AS r(i)")
    con.execute("CREATE TABLE ktk AS SELECT k, dir || '/a' AS p_min, dir || '/z' AS p_max, 10::BIGINT AS rows FROM cand")
    an.near_candidates(con, 10)
    assert con.execute('SELECT count(*), min(bound), max(bound), sum(bound) FROM near').fetchone() == (20000, 10, 10, 200000)


def test_tier_reads_each_immutable_shard_plan_once(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    plan = tmp_path / 'shards.json'
    plan.write_text(json.dumps({'shards': [{'i': 7, 'lo': 'aaa', 'hi': 'mmm'}, {'i': 19, 'lo': 'mmm', 'hi': None}]}) + '\n')
    read_text = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    tier = an.Tier(tmp_path)
    keys = ['000', 'aaa', 'aaaz', 'lll', 'mmm', 'zzz', 'é']
    expected = [None, *[str(tmp_path / 'sx/s0007.parquet')] * 3, *[str(tmp_path / 'sx/s0019.parquet')] * 3]
    assert [tier.shard_file('end', key) for key in keys] == expected
    assert [tier.shard_file('end', key) for key in keys] == expected
    assert reads == [plan]


def test_prior_dir_counts_match_the_ancestor_expansion():
    """`pd` by running counts equals counting each distinct version at each of its ancestors (`_level_dirs`), including
    a version listed by two tiers, a sibling sharing a prefix (`a/b` vs `a/bc`), and a near with no versions under it."""
    con = duckdb.connect()
    con.execute("CREATE TABLE prows (k VARCHAR, depth INTEGER, path VARCHAR, usr VARCHAR, vf BIGINT)")
    con.execute("""INSERT INTO prows VALUES
        ('q', 3, 'a/b/x', '', 1), ('q', 3, 'a/b/x', '', 1), ('q', 3, 'a/b/x', '', 2), ('q', 3, 'a/b/y', 'u', 1),
        ('q', 3, 'a/bc/z', '', 1), ('q', 2, 'a/w', '', 1), ('q', 3, 'c/d/e', '', 1), ('r', 2, 'a/b', '', 1)""")
    con.execute("CREATE TABLE near (k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)")
    con.execute("""INSERT INTO near VALUES ('q', 0, '', 1), ('q', 1, 'a', 1), ('q', 2, 'a/b', 1), ('q', 2, 'a/bc', 1),
        ('q', 1, 'c', 1), ('q', 1, 'z', 1), ('r', 1, 'a', 1), ('s', 0, '', 1)""")
    an.prior_dir_counts(con)
    got = con.execute("SELECT k, lvl, dir, n FROM pd ORDER BY k, lvl, dir").fetchall()
    con.execute("CREATE TABLE pvx AS SELECT DISTINCT k, depth, path, usr, vf FROM prows")
    want = dict(((k, lvl, d), n) for k, lvl, d, n in con.execute(f"""SELECT x.k, x.lvl, x.dir, count(*) FROM ({an._level_dirs('pvx')}) AS x
        SEMI JOIN near USING (k, lvl, dir) GROUP BY ALL""").fetchall())
    assert got == [
        ("q", 0, "", 6), ("q", 1, "a", 5), ("q", 1, "c", 1), ("q", 1, "z", 0), ("q", 2, "a/b", 3), ("q", 2, "a/bc", 1),
        ("r", 1, "a", 1), ("s", 0, "", 0),
    ]
    assert [(k, lvl, d, want.get((k, lvl, d), 0)) for k, lvl, d, _ in got] == got



@pytest.mark.parametrize("batch_keys, queries", [(1, 8), (2, 4), (256, 2)])
def test_start_probes_read_every_row_under_each_near_prefix_in_key_batches(tmp_path, batch_keys, queries):
    """Each near key gets every prior row whose name starts with it (nested keys `abc` / `abcd` both), once per tier,
    whatever the batching; a row between two batched keys that starts with neither is read (the batch's `s` range) but
    never joined."""
    con = duckdb.connect()
    con.execute("CREATE TABLE source(s VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf TIMESTAMP, vt TIMESTAMP, size BIGINT, n_files BIGINT)")
    rows = [("abc", "b/1", 1), ("abcd", "b/2", 2), ("abcde", "b/3", 3), ("abd", "b/4", 4), ("abx", "b/5", 5), ("xyz", "b/6", 6), ("xyzé", "b/7", 7)]
    con.executemany("INSERT INTO source VALUES (?, 3, ?, NULL, TIMESTAMP '2026-10-01', TIMESTAMP '2027-01-01', ?, 1)", rows)
    file = str(tmp_path / "source.parquet")
    con.execute(f"COPY source TO {an.q(file)} (FORMAT PARQUET)")
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file if key != "missing" else None)
    assert an.probe_start_rows(con, [tier, tier], ["abc", "abcd", "abx", "missing", "xyz"], batch_keys=batch_keys) == queries
    assert con.execute("SELECT k, size, count(*) FROM prows GROUP BY ALL ORDER BY k, size").fetchall() == [
        ("abc", 1, 2), ("abc", 2, 2), ("abc", 3, 2),
        ("abcd", 2, 2), ("abcd", 3, 2),
        ("abx", 5, 2),
        ("xyz", 6, 2), ("xyz", 7, 2),
    ]


def test_start_probe_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="^batch_keys must be positive$"):
        an.probe_start_rows(None, [], [], batch_keys=0)
