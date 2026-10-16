"""`dt-cloud scan step`: each step's commands (a fake runner records them), its skip-if-done, its gates, and the
dispatcher's fake / fail / soft semantics."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import scan_steps as S
from dt_cloud.cli import main
from dt_cloud.scan_profile import parse


def profile(tmp_path: Path, **kw):
    d = {"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "nf"), "data": str(tmp_path / "data"),
         "layout": {"layer2": "l2/{scan}/", "snapshots": "sub"},
         "sources": [{"uri": "s3://a", "endpoint": "https://s3.example.org", "procs": 4}, "s3://b"],
         "options": {"list": {"import": True, "bulk_args": ["-a"], "threads": 8},
                     "index": {"webdata": ["python", "web.py", "{web}", "{sources}", "-a", "{date}"], "mem": "16GB", "threads": 2},
                     "static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}}}
    d.update(kw)
    return parse(d)


class Runner:
    """A fake `subprocess.run`: records each argv (tmp paths as `<tmp>`), and plays the commands' side effects."""

    def __init__(self, tmp: Path, fail: tuple[str, ...] = (), stdout: dict[str, str] | None = None):
        self.tmp, self.fail, self.stdout, self.calls = tmp, fail, stdout or {}, []

    def __call__(self, argv, env, stdout=None, text=True):
        cmd = " ".join(argv).replace(str(self.tmp), "<tmp>")
        self.calls.append(cmd)
        key = " ".join(argv[:3])
        if argv[:2] == ["disk-tree", "import"]:
            scans = Path(env["DISK_TREE_ROOT"]) / "scans"
            scans.mkdir(parents=True)
            (scans / "u1.parquet").write_text(f"l2 of {argv[argv.index('-b') + 1]}")
        if argv[:2] == ["dt-cloud", "index-write"]:
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            for n in ("path-index.parquet", "path-index-bysize.parquet", "age-pyramid-1.parquet"):
                (out / n).write_text(n)
        if argv[:2] == ["python", "web.py"]:
            Path(argv[2]).mkdir(parents=True, exist_ok=True)
            (Path(argv[2]) / "meta.json").write_text("{}")
        return subprocess.CompletedProcess(argv, 1 if key in self.fail else 0, self.stdout.get(key, ""), None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAN_RUNS_PROFILE", raising=False)
    return {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "1", "TMPDIR": str(tmp_path / "t"),
            "HOME": str(tmp_path / "home")}


def ctx(tmp_path, env, runner=None, **kw) -> S.Ctx:
    (tmp_path / "t").mkdir(exist_ok=True)
    c = S.make_ctx(profile(tmp_path, **kw), env)
    c.sh_impl = runner or Runner(tmp_path)
    return c


def test_list_imports_and_skips_when_done(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 16))
    c = ctx(tmp_path, env)
    S.list_source(c, "s3://a")
    assert c.sh_impl.calls == [
        "disk-tree bulk-list -a s3://a -E https://s3.example.org -o <tmp>/t/dt-scan/2026-10-16T1200/listing/a -P 4 -w 8 -x clear",
        "disk-tree import -e stream -j 16 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/t/dt-scan/2026-10-16T1200/listing/a/shard-*.parquet -b a",
    ]
    assert (tmp_path / "data/l2/2026-10-16T1200/a.parquet").read_text() == "l2 of a"
    again = ctx(tmp_path, env)
    S.list_source(again, "s3://a")
    assert again.sh_impl.calls == []


def test_index_layer2_webdata_tiers_then_skip(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 16))
    l2 = tmp_path / "data/l2/2026-10-16T1200"
    l2.mkdir(parents=True)
    for b in ("a", "b"):
        (l2 / f"{b}.parquet").write_text(f"l2 of {b}")
    c = ctx(tmp_path, env)
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [
        f"python web.py {w}/web a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet -a 2026-10-16",
        f"dt-cloud index-write -m 16GB -t 2 -o {w}/index a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet",
    ]
    assert sorted(p.relative_to(tmp_path / "data").as_posix() for p in (tmp_path / "data").rglob("*") if p.is_file()) == [
        "l2/2026-10-16T1200/a.parquet",
        "l2/2026-10-16T1200/b.parquet",
        "l2/2026-10-16T1200/index/G1/age-pyramid-1.parquet",
        "l2/2026-10-16T1200/index/G1/path-index-bysize.parquet",
        "l2/2026-10-16T1200/index/G1/path-index.parquet",
        "snapshots/sub/2026-10-16T1200/meta.json",
    ]
    again = ctx(tmp_path, env)
    S.index(again, None)
    assert again.sh_impl.calls == [f"python web.py {w}/web a={w}/l2-in/a.parquet b={w}/l2-in/b.parquet -a 2026-10-16"]


def test_index_names_a_lost_input(tmp_path, env):
    c = ctx(tmp_path, env)
    with pytest.raises(S.StepFailed) as e:
        S.index(c, None)
    assert str(e.value) == (f"missing input {tmp_path}/data/l2/2026-10-16T1200/a.parquet (from `list`): a cached task's output "
                            "was lost; rerun the head with -F")


def test_index_imports_listings_when_list_does_not(tmp_path, env, monkeypatch):
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 2))
    for b in ("a", "b"):
        (tmp_path / f"data/lst/2026-10-16T1200/{b}").mkdir(parents=True)
    c = ctx(tmp_path, env, layout={"layer2": "l2/{scan}/", "snapshots": "sub", "listing": "lst/{scan}/"},
            options={"static-names": {"profile": "sn.json"}, "digest": {"template": "t", "config": "c.yml"}})
    S.index(c, None)
    w = "<tmp>/t/dt-scan/2026-10-16T1200"
    assert c.sh_impl.calls == [
        "disk-tree import -e stream -j 2 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/data/lst/2026-10-16T1200/a/shard-*.parquet -b a",
        "disk-tree import -e stream -j 2 -m -s s3 -t 2026-10-16T00:00:00+00:00 -l <tmp>/data/lst/2026-10-16T1200/b/shard-*.parquet -b b",
        f"dt-cloud index-write -t 2 -o {w}/index a={w}/l2/a/scans/u1.parquet b={w}/l2/b/scans/u1.parquet",
    ]


def test_serving_steps_gate_on_credentials(tmp_path, env):
    c = ctx(tmp_path, env)
    for f in (S.publish_r2, S.index_rows, S.index_sync, S.over_time, S.index_gc, S.warm_cache, S.probe, S.digest):
        f(c, None)
    assert c.sh_impl.calls == []


def test_serving_steps_commands(tmp_path, env):
    env.update(CLOUDFLARE_API_TOKEN=" tok\n", CLOUDFLARE_ACCOUNT_ID="acct", R2_ENDPOINT="https://r2.example.org",
               R2_BUCKET="r2b", R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s", SITE_TOKEN="st", SITE_URL="https://site.example.org",
               SLACK_BOT_TOKEN="x", SLACK_CHANNEL="C1")
    c = ctx(tmp_path, env, secrets={"CLOUDFLARE_API_TOKEN": "cf-token"})
    assert env["CLOUDFLARE_API_TOKEN"] == "tok"
    for f in (S.static_names, S.publish_r2, S.index_rows, S.index_sync, S.over_time, S.index_gc, S.warm_cache, S.probe, S.digest):
        f(c, None)
    k = "l2/2026-10-16T1200/index/G1"
    assert c.sh_impl.calls == [
        "dt-cloud static-names runs add -c 2026-10-16T1200",
        "dt-cloud publish-r2 -a 2026-10-16T1200",
        f"dt-cloud index-sync -r -d <tmp>/data/{k} -g G1 -k {k} 2026-10-16T1200",
        f"dt-cloud index-sync -p -d <tmp>/data/{k} -g G1 -k {k} -M <tmp>/data/snapshots/sub 2026-10-16T1200",
        "dt-cloud index-gc 2026-10-16T1200",
        "dt-cloud over-time-groups -g G1 -o <tmp>/t/dt-scan/2026-10-16T1200/over-time -p r2://r2b",
        "dt-cloud index-gc -R -m 2d -F <tmp>/data -F r2",
        "dt-cloud warm-cache -d 2026-10-16T1200 -r r2://r2b/snapshots/sub",
        "dt-cloud probe -c -u https://site.example.org -o <tmp>/data/probes/sub/",
        "dt-cloud digest -T t -C c.yml -r <tmp>/data/snapshots/sub",
    ]
    assert (tmp_path / "home/.config/disk-tree/buckets.yml").read_text() == (
        "buckets:\n  - uri: r2://r2b\n    endpoint_url: https://r2.example.org\n    profile: r2\n")
    assert (Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text(), env["LAYER2_PREFIX"], env["SCAN_ID"]) == (
        "[r2]\naws_access_key_id = k\naws_secret_access_key = s\n", "l2/{scan}/", "2026-10-16T1200")


def test_reproc_skips_side_effects(tmp_path, env):
    env.update(REPROC="1", SITE_TOKEN="st", SITE_URL="https://site.example.org", R2_ARCHIVE="r2://x")
    c = ctx(tmp_path, env)
    for f in (S.static_names, S.interval_store, S.warm_cache, S.probe):
        f(c, None)
    assert (c.sh_impl.calls, env["DT_RECORD"]) == ([], "off")


def test_interval_store_without_a_generation(tmp_path, env):
    env["R2_ARCHIVE"] = "r2://x"
    c = ctx(tmp_path, env, runner=Runner(tmp_path, stdout={"dt-cloud interval-store current": "\n"}))
    S.interval_store(c, None)
    assert c.sh_impl.calls == ["dt-cloud interval-store current -o r2://x"]


class FakeS3:
    def __init__(self, buckets: dict[str, int | Exception]):
        self.buckets = buckets

    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.buckets]}

    def list_objects_v2(self, Bucket, MaxKeys):
        v = self.buckets[Bucket]
        if isinstance(v, Exception):
            raise v
        return {"KeyCount": v}


def test_unscanned():
    s3 = FakeS3({"scanned": 5, "empty": 0, "new": 3, "denied": PermissionError("no")})
    assert S.unscanned(s3, {"scanned"}) == ["denied (check failed: ListObjectsV2: PermissionError: no)", "new"]


def test_run_step_fake_fail_unknown(tmp_path, monkeypatch):
    marks = tmp_path / "marks"
    env = {S.FAKE_ENV: str(marks), S.FAIL_ENV: "index"}
    assert [S.run_step("list", "s3://a", env), S.run_step("post-it", None, env), S.run_step("index", None, env),
            S.run_step("list", None, env), S.run_step("index-sync", None, env)] == [0, 2, 1, 2, 0]
    assert marks.read_text().split("\n") == ["list s3://a", "index-sync", ""]


def test_soft_step_failure_is_a_warning(tmp_path, env, monkeypatch):
    p = tmp_path / "p.yml"
    p.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                 "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\nskip: [static-names, digest]\n"
                 "options: {list: {import: true}}\n")

    def boom(c, a):
        raise RuntimeError("D1 down")

    monkeypatch.setitem(S.STEPS, "index-sync", S.Step("index-sync", boom, soft=True))
    monkeypatch.setitem(S.STEPS, "index", S.Step("index", boom))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "t").mkdir()
    soft = CliRunner().invoke(main, ["scan", "step", "-p", str(p), "index-sync"])
    assert (soft.exit_code, soft.stderr.rstrip().split("\n")) == (0, [
        "scan step index-sync: RuntimeError: D1 down",
        "WARN: index-sync failed (exit 1); soft step, the scan goes on",
    ])
    hard = CliRunner().invoke(main, ["scan", "step", "-p", str(p), "index"])
    assert (hard.exit_code, type(hard.exception)) == (1, RuntimeError)


def test_identity_is_required(tmp_path):
    p = profile(tmp_path)
    with pytest.raises(S.StepFailed) as e:
        S.make_ctx(p, {"SNAP_ID": "2026-10-16"})
    assert str(e.value) == "the scan's identity is stamped by the head: $GEN, $DT_JOB_START unset"
