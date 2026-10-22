"""`dt_cloud.static_compact` through the shared orchestrator (`append_runner.compact_store`): a generation's base and its
runs folded into `<gen>.c<NNN>` is `static-names build` through the same scans over the base's key ranges (`build -r`),
every file md5-equal (but `catalog/meta.json`'s seconds), for every fixture flavor: two level-0 runs (compacted by `add`
when level 1 is due), one merged run (`static-names compact`), and again after a scan appended to the compacted
generation (`add -g <old>` follows `successor.json`; `add -K`)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import static_compact as sk
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn

from test_static_local import _build, _layout, _small_groups
from test_static_names import fixture, scan_ids  # noqa: F401


def _add(root: Path, scans: dict, store: Path, work: Path, through: str, *extra: str) -> dict:
    res = CliRunner().invoke(sl.add_cmd, ["-g", "g", "-L", _layout(scans), "-s", str(root), "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB",
                                          "-t", through, *extra], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _compact(store: Path, work: Path) -> dict:
    res = CliRunner().invoke(sk.compact_cmd, ["-g", "g", "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


#: What a compacted generation has that a build doesn't (its record, its manifests, the runs appended to it since, its
#: successor record), and the catalog's `meta.json`
#: (its `s`: seconds, which no two builds share either), compared apart.
OWN = ("compaction.json", "manifests/", "catalog/meta.json", "deltas/", ar.SUCCESSOR)


def _md5s(d: Path) -> dict[str, str]:
    return {k: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and not (k := p.relative_to(d).as_posix()).startswith(OWN)}


def _meta(d: Path) -> dict:
    return {k: v for k, v in json.loads((d / "catalog" / "meta.json").read_text()).items() if k != "s"}


@pytest.fixture(scope="module")
def world(fixture, tmp_path_factory):  # noqa: F811
    root, scans, _ = fixture
    ids = scan_ids(scans)
    tmp = tmp_path_factory.mktemp("compact")
    out: dict = {"root": root, "scans": scans, "ids": ids, "tmp": tmp}
    with _small_groups():
        # A: two level-0 runs (level 1: never a carry; due once two runs are at level 0) → `add` compacts into g.c001;
        # then a scan appended to g.c001 (`-g g` follows the successor) and `-K` → g.c002.
        a = tmp / "a"
        _build(root, scans, a, tmp / "wa0", through=ids[1], extra=("-u", "static-names/primary-CURRENT.json"))
        out["a"] = [_add(root, scans, a, tmp / "wa1", ids[2], "-C", "1"), _add(root, scans, a, tmp / "wa2", ids[3], "-C", "1"),
                    _add(root, scans, a, tmp / "wa3", ids[4], "-C", "1", "-K", "-u", "static-names/primary-CURRENT.json")]
        # B: one merged run (level 5: the two carry into an L1) → `static-names compact`.
        b = tmp / "b"
        _build(root, scans, b, tmp / "wb0", through=ids[1])
        out["b_add"] = _add(root, scans, b, tmp / "wb1", ids[3])
        out["b"] = _compact(b, tmp / "wb2")
        ranges = str(a / sn.PREFIX / "g" / "ranges.json")
        for gen, through in (("g.c001", ids[3]), ("g.c002", ids[4])):
            _build(root, scans, tmp / f"ref-{gen}", tmp / f"wr-{gen}", gen=gen, through=through, extra=("-r", ranges))
    out.update(a_store=a / sn.PREFIX, b_store=b / sn.PREFIX)
    return out


def test_add_compacts_when_due_then_appends_to_the_successor(world):
    ids = world["ids"]
    first, second, third = world["a"]
    assert [(o["gen"], [x["scan"] for x in o["added"]], o["compact"] and (o["compact"]["gen"], o["compact"]["why"], o["compact"]["runs"]))
            for o in (first, second, third)] == [
        ("g", [ids[2]], None),
        ("g", [ids[3]], ("g.c001", f"level 1: two runs at level 0 ({ar.run_key(ids[2], ids[2])}, {ar.run_key(ids[3], ids[3])})",
                         [ar.run_key(ids[2], ids[2]), ar.run_key(ids[3], ids[3])])),
        ("g.c001", [ids[4]], ("g.c002", "forced (-K)", [ar.run_key(ids[4], ids[4])])),
    ]
    root = world["a_store"]
    assert [json.loads((root / g / ar.SUCCESSOR).read_text()) for g in ("g", "g.c001")] == [{"gen": "g.c001"}, {"gen": "g.c002"}]
    assert json.loads((root / ar.CURRENT).read_text()) == {"gen": "g.c002"}
    # the old generations stay (readers pin STATIC_GEN; `store-gc -k` collects them)
    assert [(root / g / "scans.json").is_file() for g in ("g", "g.c001")] == [True, True]


@pytest.mark.parametrize("store, gen, ref", [("a_store", "g.c001", "g.c001"), ("a_store", "g.c002", "g.c002"), ("b_store", "g.c001", "g.c001")])
def test_compacted_generation_is_the_rebuild(world, store, gen, ref):
    got, want = world[store] / gen, world["tmp"] / f"ref-{ref}" / sn.PREFIX / ref
    g, w = _md5s(got), _md5s(want)
    assert sorted(g) == sorted(w)
    assert g == w
    assert _meta(got) == _meta(want)
    assert len(w) > 40


@pytest.mark.parametrize("store, gen", [("a_store", "g.c001"), ("a_store", "g.c002"), ("b_store", "g.c001")])
def test_compaction_record(world, store, gen):
    """`compaction.json` (uploaded last): the runs folded, the rule, and every file's md5; the runless manifest."""
    d = world[store] / gen
    rec = json.loads((d / ar.COMPACTION).read_text())
    ids = world["ids"]
    n = 4 if gen == "g.c001" else 5
    # what the compaction wrote: all but its record, and what later appends added (runs, their manifests, a successor)
    later = lambda k: k.startswith(("deltas/", ar.SUCCESSOR)) or (k.startswith("manifests/") and k != f"manifests/{ids[n - 1]}.json")  # noqa: E731
    files = {k: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*"))
             if p.is_file() and p.name != ar.COMPACTION and not later(k := p.relative_to(d).as_posix())}
    assert rec["files"] == files
    assert {k: v for k, v in rec.items() if k != "files"} == {
        "from": {"g.c001": "g", "g.c002": "g.c001"}[gen], "manifest": {("a_store", "g.c001"): f"manifests/{ids[3]}.json",
                                                                      ("a_store", "g.c002"): f"manifests/{ids[4]}.json",
                                                                      ("b_store", "g.c001"): f"manifests/{ids[3]}.m001.json"}[(store, gen)],
        "runs": {("a_store", "g.c001"): [ar.run_key(ids[2], ids[2]), ar.run_key(ids[3], ids[3])], ("a_store", "g.c002"): [ar.run_key(ids[4], ids[4])],
                 ("b_store", "g.c001"): [ar.run_key(ids[2], ids[3])]}[(store, gen)],
        "base_scans": {"g.c001": 2, "g.c002": 4}[gen], "scans": n,
        **({"level": 1} if (store, gen) == ("a_store", "g.c001") else
           {"level": 1, "rule": "level:1", "why": "forced (-K)"} if gen == "g.c002" else {"level": None, "rule": "none", "why": "forced (compact)"}),
    }
    assert json.loads((d / "manifests" / f"{ids[n - 1]}.json").read_text()) == {"gen": gen, "date": ids[n - 1], "base_scans": n, "scans": ids[:n], "runs": []}


def test_store_gc_keeps_the_pointer_and_forwarding_records(world, tmp_path):
    """`store-gc -t static-names`: the pointer's generation is live without `-k`; a collected compacted generation keeps
    its `successor.json`, so `add -g` still follows it."""
    import shutil

    from dt_cloud import store_gen_gc as gc
    from dt_cloud.blobstore import Store

    shutil.copytree(world["a_store"].parent, tmp_path / "a")
    store = Store(str(tmp_path / "a"))
    recs = gc.run(store, "static-names", ["g"], grace_s=0, now=4e9, execute=True)
    assert [(r["gen"], r["action"], r["reason"]) for r in recs] == [("g", "keep", "kept (-k)"), ("g.c001", "delete", None), ("g.c002", "keep", "current")]
    root = tmp_path / "a" / sn.PREFIX
    assert sorted(p.relative_to(root).as_posix() for p in (root / "g.c001").rglob("*") if p.is_file()) == [f"g.c001/{ar.SUCCESSOR}"]
    assert ar.chain(store, sn.PREFIX, "g") == ["g", "g.c001", "g.c002"]


def test_independent_static_pointers_and_gc_follow_both_lineages(world, tmp_path):
    import shutil

    from dt_cloud import store_gen_gc as gc
    from dt_cloud.blobstore import Store

    shutil.copytree(world["a_store"].parent, tmp_path / "a")
    store = Store(str(tmp_path / "a"))
    store.write_text("static-names/meta/scans.json", "{}\n")
    store.write_text("static-names/meta/successor.json", '{"gen":"meta.c001"}\n')
    store.write_text("static-names/meta.c001/scans.json", "{}\n")
    sl.publish_current(store, "meta", "static-names/meta-CURRENT.json")
    assert [store.read_json(k) for k in (
        "static-names/CURRENT.json", "static-names/primary-CURRENT.json", "static-names/meta-CURRENT.json",
    )] == [{"gen": "g.c002"}, {"gen": "g.c002"}, {"gen": "meta.c001"}]
    plan = gc.plan(store, "static-names", ["g", "meta"], lineage_only=False, grace_s=0, now=4e9)
    assert [(r["gen"], r["action"]) for r in plan] == [
        ("g", "keep"), ("g.c001", "delete"), ("g.c002", "keep"), ("meta", "keep"), ("meta.c001", "keep"),
    ]
