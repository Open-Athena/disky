"""The scan profile (specs/pipelines-everywhere.md §3): validated on load, every problem named by its key path."""
from __future__ import annotations

from pathlib import Path

import pytest

from dt_cloud.scan_profile import Disk, Machine, ProfileError, Source, load, parse

FIXTURE = Path(__file__).parent / "fixtures" / "scan_profiles" / "cw-s3.yml"


def minimal(**kw) -> dict:
    d = {"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
         "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["s3://a"],
         "options": {"list": {"import": True}, "static-names": {"profile": "s.json"}, "digest": {"template": "t", "config": "c"}}}
    d.update(kw)
    return d


def problems(d: dict) -> list[str]:
    with pytest.raises(ProfileError) as e:
        parse(d)
    return e.value.problems


def test_fixture_loads():
    p = load(FIXTURE)
    assert p.enabled() == ("bucket-check", "list", "index", "static-names", "publish-r2", "interval-store", "index-rows",
                           "index-sync", "over-time", "index-gc", "warm-cache", "probe", "digest")
    assert p.sources == (
        Source("s3://bucket-big", machine="n2-standard-8", procs=8),
        Source("s3://bucket-b"),
        Source("s3://bucket-c", disk=Disk(375, "local-ssd")),
    )
    assert [(s.kind, s.bucket) for s in p.sources] == [("s3", "bucket-big"), ("s3", "bucket-b"), ("s3", "bucket-c")]
    assert p.machines["big"] == Machine("n2-standard-8", Disk(200))
    assert p.fold == ("bucket-check", "index-sync")
    assert p.data_bucket == "example-data"


def test_required_keys_named():
    assert problems({"deployment": "x"}) == [
        "executor: required", "work: required", "data: required", "layout: required", "sources: required",
    ]


def test_unknown_keys_refused():
    d = minimal(colour="red", executor={"kind": "local", "zone": "z"}, sources=[{"uri": "s3://a", "size": 1}],
                options={"list": {"import": True, "turbo": True}, "static-names": {"profile": "s"},
                         "digest": {"template": "t", "config": "c"}, "nope": {}})
    assert problems(d) == [
        "colour: unknown key",
        "executor.zone: unknown key",
        "sources[0].size: unknown key",
        "options.list.turbo: unknown key (known: import, bulk_args, import_args, threads, weights)",
        "options.nope: unknown step",
    ]


def test_google_batch_requirements():
    d = minimal(executor={"kind": "google-batch", "project": "p"}, work="gs://w/nf", data="gs://other")
    assert problems(d) == [
        "executor.region: required for google-batch",
        "executor.image: required for google-batch",
        "data: must be in work's bucket (w): google-batch tasks mount only the work dir's bucket",
    ]


def test_local_executor_needs_a_local_work_dir():
    assert problems(minimal(work="gs://w/nf")) == [
        "work: a local dir for the local executor (it refuses a cloud work dir without Fusion)",
    ]


def test_sources_and_disks():
    d = minimal(sources=[{"uri": "ftp://x"}, {"uri": "s3://a", "disk": {"request": 400, "type": "local-ssd"}, "procs": 0},
                         "s3://a", {"uri": "gs://g", "region": "europe-west4"}],
                machines={"big": {"type": "n2", "disk": {"request": 10, "type": "nvme"}}})
    assert problems(d) == [
        "sources[0].uri: <scheme>://<bucket> with scheme one of gs, gcs, s3, r2, hf (got 'ftp://x')",
        "sources[1].procs: a positive integer",
        "sources[1].disk.request: a multiple of 375 for local-ssd (got 400)",
        "sources[2].uri: bucket 'a' listed twice",
        "sources[3].region: 'europe-west4' ≠ the executor's (None); nf-google 1.29.1 has no per-task location (spec §4 'Per-source region')",
        "machines.big.disk.type: one of local-ssd, pd-balanced, pd-ssd, pd-standard",
    ]


def test_steps_skip_fold():
    d = minimal(steps=["index", "warp"], skip=["list", "probe"], fold=["index"])
    assert problems(d) == [
        "steps: 'index' is a core step (always on unless in `skip`)",
        "steps: unknown step 'warp'",
        "skip: 'list' can't be skipped (one of bucket-check, static-names, publish-r2, interval-store, over-time, index-gc, warm-cache, probe, digest)",
        "fold: 'index' can't be folded (one of bucket-check, index-sync)",
    ]


def test_skip_and_fold_apply():
    p = parse(minimal(skip=["probe", "digest", "static-names"], fold=[], options={"list": {"import": True}}))
    assert (p.enabled(), p.fold) == (
        ("bucket-check", "list", "index", "publish-r2", "interval-store", "index-rows", "index-sync", "over-time", "index-gc", "warm-cache"),
        (),
    )


def test_step_requirements():
    d = minimal(layout={"layer2": "l2/", "snapshots": None}, options={"index": {"mode": "path-index", "access": "a"}})
    assert problems(d) == [
        "layout.layer2: a template with `{scan}`",
        "layout.snapshots: required (\"\" for `snapshots/` itself)",
        "layout.listing: required unless options.list.import is true (index reads the listings from the data store)",
        "data: a gs:// bucket with options.index.mode path-index (its inputs are staged from GCS)",
        "options.static-names.profile: required (the deployment's static-names profile; or env STATIC_NAMES_PROFILE, or gen: the one-machine path, or skip: [static-names])",
        "options.digest.template: required (or skip: [digest])",
        "options.digest.config: required (or skip: [digest])",
    ]


def test_option_types():
    d = minimal(options={"list": {"import": "yes", "bulk_args": "-a"}, "static-names": {"profile": "s"},
                         "digest": {"template": "t", "config": "c"}, "index-gc": {"grace": 2}})
    assert problems(d) == [
        "options.list.import: true or false",
        "options.list.bulk_args: a list of strings",
        "options.index-gc.grace: a string",
    ]


def test_machine_labels_are_groovy_identifiers():
    """A label goes bare into the executor config (`withLabel: over_time { … }`): `_` yes, `-` never."""
    assert sorted(parse(minimal(machines={"over_time": "n2-standard-8", "post": "e2-highmem-4"})).machines) == ["over_time", "post"]
    assert problems(minimal(machines={"over-time": "n2-standard-8", "Big": "n2-standard-8"})) == [
        "machines.over-time: a label of lowercase letters, digits and '_' (it is written bare into the Nextflow config)",
        "machines.Big: a label of lowercase letters, digits and '_' (it is written bare into the Nextflow config)",
    ]
