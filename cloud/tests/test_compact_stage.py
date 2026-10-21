"""The Batch compaction stage (both stores' runners, `append_runner.Runner.compact_stage`) and its job
(`append_runner.run_compaction`, `r2_publish_gen`): the runners submit `compact` when the policy fires or `-K` forces
it (and `compact-r2` as the R2 account when the stages' account can't hold the R2 credentials), follow a compacted
generation's `successor.json` on their next run, and the job publishes GCS first, then R2 with the pointer last."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud import static_compact as sk
from dt_cloud import static_names as sn
from dt_cloud.blobstore import Store

from test_compact_level import _static_runner
from test_interval_append import COMMON, G1, Fake as IvFake
from test_interval_compact import DATES, LAYOUT, RG, add, build, make_world
from test_static_local import _build, _layout, _small_groups
from test_static_runner import GEN, ROOT, _py
from test_static_names import fixture, scan_ids  # noqa: F401


def _two_l0(f: IvFake) -> None:
    runs = [{"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]} for d in ("2026-08-04", "2026-08-05")]
    f.keys[f"{G1}/manifests/2026-08-05.json"] = {"date": "2026-08-05", "runs": runs}


def test_interval_compact_now():
    """`-K`: the compaction stage runs with nothing due (`compact -K`); its reason is recorded."""
    f = IvFake(["2026-08-03"], compact_level=1)
    f.keys[f"{G1}/manifests/2026-08-04.json"] = {"date": "2026-08-04", "runs": [{"key": "deltas/2026-08-04", "first": "2026-08-04",
                                                                                 "last": "2026-08-04", "level": 0, "scans": ["2026-08-04"]}]}
    r = f.runner(merge_wait=None)
    r.compact_now = True
    r.carries()
    assert (f.calls(), r.compaction) == ([("compact", f"compact -C level:1 -K -r -M 90GB -p 16 {COMMON} -m /gcs/data")], "forced (-K)")


def test_interval_follows_the_successor():
    """A generation compacted since the profile named it: the next run appends to its successor (its own ranges)."""
    f = IvFake(["2026-08-03", "2026-08-06"])
    _two_l0(f)
    f.keys[f"{G1}/successor.json"] = {"gen": "g1.c001"}
    f.keys["interval-store/g1.c001/ranges.json"] = {"k": 4}
    f.keys["interval-store/g1.c001/scans.json"] = {"scans": [{"id": d} for d in ("2026-08-03", "2026-08-04", "2026-08-05")]}
    r = f.runner(dry_run=True)
    r.run("2026-08-06")
    assert (r.p.gen, r.cfg.ranges_gen, [m for m in f.log if "compacted" in m]) == (
        "g1.c001", "g1.c001", ["g1 was compacted into g1.c001 (g1 → g1.c001): appending there"])


def test_static_compact_jobs_split_by_account():
    """The static profile's R2 account differs from the stages': `compact` (GCS only), then `compact-r2` as the R2
    account (no mount); the successor's generation is the runner's next."""
    r = _static_runner(1)
    jobs = r.compact_jobs("2026-10-10")
    assert [(spec["labels"]["stage"], spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1],
             spec["allocationPolicy"]["serviceAccount"]["email"]) for _, spec in jobs] == [
        ("compact", _py("static_merge", "compact", "-g", GEN, "-C", "level:1", "-M", "90GB", "-p", "16"), "build@proj"),
        ("compact-r2", _py("static_merge", "compact-r2", "-g", GEN, mount=False), "copy@proj"),
    ]
    assert r.with_gen(f"{GEN}.c001").gen == f"{GEN}.c001"
    assert ROOT == f"static-names/{GEN}"


# ── The job: `run_compaction` over local stand-ins of the data bucket ──────


def test_run_compaction_interval(tmp_path):
    """The interval store's job over a store dir (its own mount): not due → nothing; due → the next generation,
    published with its pointer, a rerun idempotent (the successor isn't due); the predecessor kept for the grace."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    store = Store(str(store_dir))
    c = ic.compactor(RG)
    kw = dict(threads=2, mem="1GB", log=lambda m: None, now=0.0)
    assert ar.run_compaction(store, c, "g", store_dir, tmp_path / "w1", policy=ar.parse_policy("x", "every:3s"), **kw) == {
        "gen": "g", "why": None, "compact": None, "predecessors": []}
    doc = ar.run_compaction(store, c, "g", store_dir, tmp_path / "w2", policy=ar.parse_policy("x", "every:2s"), **kw)
    assert (doc["gen"], doc["why"], doc["compact"]["gen"], doc["compact"]["current"], doc["compact"]["reads"]["source"],
            [(p["gen"], p["deleted"]) for p in doc["predecessors"]]) == ("g", "every 2s: 2 scans past the base", "g.c001", True, f"{store_dir} (mount)",
                                                                         [("g", 0)])
    assert json.loads((store_dir / ist.PREFIX / "CURRENT.json").read_text()) == {"gen": "g.c001"}
    again = ar.run_compaction(store, c, "g", store_dir, tmp_path / "w3", policy=ar.parse_policy("x", "every:2s"), **kw)
    assert (again["gen"], again["compact"]) == ("g.c001", None)


def test_r2_publish_gen_order(monkeypatch):
    """R2: the new generation's files (in parallel), then its liveness files in order, then `compaction.json`, checked;
    then the old generation's `successor.json`; then the store's `CURRENT.json`, last."""
    class Obj:
        def __init__(self, key):
            self.key, self.size = key, 1

    keys = {"p/g.c001/": ["p/g.c001/a.parquet", "p/g.c001/compaction.json", "p/g.c001/scans.json", "p/g.c001/served/b.parquet"],
            "p/g/successor.json": ["p/g/successor.json"], "p/CURRENT.json": ["p/CURRENT.json"]}
    copied: list[list[str]] = []
    monkeypatch.setattr(ar, "r2_objects", lambda bucket, prefixes: [Obj(k) for p in prefixes for k in keys[p]])
    monkeypatch.setattr(ar, "r2_copy", lambda bucket, objs, **kw: copied.append([o.key for o in objs]) or {"copied": len(objs), "bytes": len(objs)})
    monkeypatch.setattr(ar, "r2_missing", lambda objs, **kw: [])
    doc = ar.r2_publish_gen("data", "p", "g.c001", "g", log=lambda m: None)
    assert copied == [["p/g.c001/a.parquet", "p/g.c001/served/b.parquet"], ["p/g.c001/scans.json"], ["p/g.c001/compaction.json"],
                      ["p/g/successor.json"], ["p/CURRENT.json"]]
    assert {k: v for k, v in doc.items() if k != "s"} == {"objects": 4, "copied": 4, "bytes": 4}


def test_run_compaction_static(fixture, tmp_path):  # noqa: F811
    """The static store's job (`static_merge compact`'s) over a store dir: forced (`-K`) with runs, its pointer moved,
    no predecessor pruned (`Compactor.prune` off: readers pin `STATIC_GEN`); the CLI reads the same."""
    from dt_cloud import static_local as sl


    root, scans, _ = fixture
    ids = scan_ids(scans)
    store_dir = tmp_path / "store"
    with _small_groups():
        _build(root, scans, store_dir, tmp_path / "wb", through=ids[2])
        res = CliRunner().invoke(sl.add_cmd, ["-g", "g", "-L", _layout(scans), "-s", str(root), "-o", str(store_dir), "-w", str(tmp_path / "wa"),
                                              "-p", "2", "-M", "1GB", "-t", ids[3]], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        doc = ar.run_compaction(Store(str(store_dir)), sk.compactor(), "g", store_dir, tmp_path / "w1", policy=ar.NEVER, force=True, threads=2,
                                mem="1GB", log=lambda m: None)
    assert (doc["why"], doc["compact"]["gen"], doc["compact"]["current"], doc["predecessors"]) == ("forced (-K)", "g.c001", True, [])
    assert json.loads((store_dir / sn.PREFIX / "CURRENT.json").read_text()) == {"gen": "g.c001"}
    assert sorted(p.name for p in (store_dir / sn.PREFIX).iterdir()) == ["CURRENT.json", "g", "g.c001"]
