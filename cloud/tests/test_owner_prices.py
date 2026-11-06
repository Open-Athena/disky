"""Owner pricing filled by the pipeline (specs/owner-pricing-off-the-read-path.md): `dt-cloud owner-prices` (the
`owner-prices` scan step's fill, over the site's `POST /api/owner-prices`), and the gate a compaction passes before
the interval store's `CURRENT.json` moves (`owner_prices.Gate` in `append_runner.compact_store` / `promote_gen`)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import cli, site
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud import owner_prices as op
from dt_cloud.blobstore import Store

from test_compact_stage import _candidate, _gens
from test_interval_append import COMMON, Fake as IvFake
from test_interval_compact import DATES, RG, add, build, make_world

SITE = "https://site.test"
TAG = "pq:iv:g:s"


def refusal(status: int | None, error: str, **body) -> site.SiteError:
    """The site's refusal as `site._fetch` raises it (`status` None: unreachable)."""
    where = f"{SITE}{op.ENDPOINT}"
    return site.SiteError(f"{where} → HTTP {status}: {error}" if status else f"{where} unreachable: {error}", status=status, body={"error": error, **body})


@pytest.fixture
def prices(monkeypatch):
    """The site's `POST /api/owner-prices`: `prices.answers` are its answers in turn (a dict, or a `SiteError` to
    raise), the last one repeated; `prices.calls` each call's query; `prices.logged`, `prices.slept`."""
    state = SimpleNamespace(answers=[], calls=[], logged=[], slept=[], now=0.0)

    def post_json(url: str, token: str, path: str, body: dict, timeout: int = 120) -> bytes:
        parts = urlsplit(path)
        assert (url, token, parts.path, body) == (SITE, "t", op.ENDPOINT, {})
        state.calls.append(dict(parse_qsl(parts.query)))
        answer = state.answers.pop(0) if len(state.answers) > 1 else state.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer).encode()

    def sleep(s: float) -> None:
        state.slept.append(s)
        state.now += s

    monkeypatch.setattr(site, "post_json", post_json)
    monkeypatch.setattr(cli, "err", state.logged.append)
    monkeypatch.setenv("SITE_URL", SITE)
    monkeypatch.setenv("SITE_TOKEN", "t")
    state.kw = dict(log=state.logged.append, sleep=sleep, clock=lambda: state.now)
    return state


def answer(priced: int, remaining: int, prefixes: int = 150, hz: int = 9) -> dict:
    return {"index": TAG, "hz": hz, "prefixes": prefixes, "priced": priced, "remaining": remaining, "groups": 3, "ms": 5}


# ── The fill ───────────────────────────────────────────────────────────────


def test_fill_calls_until_nothing_remains(prices) -> None:
    prices.answers = [answer(64, 86), answer(64, 22), answer(22, 0)]
    assert op.fill(SITE, "t", scan="2026-11-05", **prices.kw) == {"index": TAG, "hz": 9, "prefixes": 150, "priced": 150, "calls": 3}
    assert prices.calls == [{"n": "64", "scan": "2026-11-05"}] * 3
    assert prices.logged == [f"owner-prices: {TAG}: 64 priced, 86 to go", f"owner-prices: {TAG}: 128 priced, 22 to go"]
    assert prices.slept == []


def test_fill_with_nothing_stale_is_one_call(prices) -> None:
    prices.answers = [answer(0, 0)]
    assert op.fill(SITE, "t", gen="g.c001", n=32, **prices.kw) == {"index": TAG, "hz": 9, "prefixes": 150, "priced": 0, "calls": 1}
    assert prices.calls == [{"n": "32", "gen": "g.c001"}]


def test_fill_waits_for_the_sites_store_to_hold_the_scan(prices) -> None:
    behind = refusal(409, "the interval store doesn't hold 2026-11-05 yet", behind="2026-11-05")
    prices.answers = [behind, behind, answer(3, 0, prefixes=3)]
    assert op.fill(SITE, "t", scan="2026-11-05", **prices.kw) == {"index": TAG, "hz": 9, "prefixes": 3, "priced": 3, "calls": 1}
    assert prices.slept == [op.POLL_S, op.POLL_S]
    assert prices.logged == ["owner-prices: the site's store doesn't hold 2026-11-05 yet; asking again in 15s"] * 2


def test_fill_gives_up_on_a_scan_the_site_never_holds(prices) -> None:
    prices.answers = [refusal(409, "the interval store doesn't hold 2026-11-05 yet", behind="2026-11-05")]
    with pytest.raises(op.PricingRefused) as e:
        op.fill(SITE, "t", scan="2026-11-05", wait_s=40, **prices.kw)
    assert str(e.value) == f"{SITE}{op.ENDPOINT} → HTTP 409: the interval store doesn't hold 2026-11-05 yet (waited 45s for the site's store to hold it)"
    assert prices.slept == [15, 15, 15]


def test_fill_halves_its_chunk_when_the_worker_fails_and_goes_on(prices) -> None:
    """A call that dies at the Worker's limit (a 5xx) is asked again for half as many prefixes: the fill is bounded
    by its chunk, so a smaller one passes."""
    over = refusal(503, "Worker exceeded resource limits")
    prices.answers = [over, answer(32, 40, prefixes=72), over, over, answer(8, 32, prefixes=72), answer(8, 24, prefixes=72), answer(24, 0, prefixes=72)]
    assert op.fill(SITE, "t", **prices.kw) == {"index": TAG, "hz": 9, "prefixes": 72, "priced": 72, "calls": 4}
    assert [c["n"] for c in prices.calls] == ["64", "32", "32", "16", "8", "8", "8"]
    assert prices.slept == [op.RETRY_S] * 3


@pytest.mark.parametrize("error,said", [
    (refusal(503, "boom"), f"{SITE}{op.ENDPOINT} → HTTP 503: boom"),
    (refusal(None, "timed out"), f"{SITE}{op.ENDPOINT} unreachable: timed out"),
])
def test_fill_refuses_once_its_retries_run_out(prices, error: site.SiteError, said: str) -> None:
    prices.answers = [error]
    with pytest.raises(op.PricingRefused) as e:
        op.fill(SITE, "t", **prices.kw)
    assert str(e.value) == said
    assert ([c["n"] for c in prices.calls], prices.slept) == (["64", "32", "16", "8"], [op.RETRY_S] * 3)


@pytest.mark.parametrize("error", [
    refusal(403, "forbidden"),
    refusal(404, "no owner-prices grant on this deployment"),
    refusal(409, "no interval store serves this deployment"),
])
def test_fill_never_retries_a_refusal(prices, error: site.SiteError) -> None:
    prices.answers = [error]
    with pytest.raises(op.PricingRefused) as e:
        op.fill(SITE, "t", **prices.kw)
    assert (str(e.value), prices.calls, prices.slept) == (str(error), [{"n": "64"}], [])


def test_fill_stops_when_a_call_prices_nothing_with_prefixes_left(prices) -> None:
    prices.answers = [answer(64, 86), answer(0, 86)]
    with pytest.raises(op.PricingRefused) as e:
        op.fill(SITE, "t", **prices.kw)
    assert (str(e.value), len(prices.calls)) == (f"{TAG}: a call priced nothing with 86 prefixes still unpriced", 2)


def test_a_read_timeout_is_a_failed_call(prices, monkeypatch) -> None:
    def post_json(*a, **kw) -> bytes:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(site, "post_json", post_json)
    with pytest.raises(site.SiteError) as e:
        op.call(SITE, "t", n=64)
    assert (str(e.value), e.value.status) == (f"{SITE}{op.ENDPOINT} didn't answer: The read operation timed out", None)


# ── `dt-cloud owner-prices` ────────────────────────────────────────────────


def run(*args: str) -> tuple[int, str | None, str | None]:
    """`dt-cloud owner-prices <args>`: its exit code, and its exit message or (exit 0) its stdout."""
    result = CliRunner().invoke(cli.main, ["owner-prices", *args])
    failed = result.exception is not None
    return result.exit_code, str(result.exception) if failed else None, None if failed else result.stdout


def test_cli_fills_through_the_scan_then_prunes(prices) -> None:
    prices.answers = [answer(64, 6, prefixes=70), answer(6, 0, prefixes=70), {"kept": [TAG], "dropped": [{"index": "pq:iv:old:s", "rows": 71}], "ms": 4}]
    code, exc, out = run("-d", "2026-11-05", "-p")
    assert (code, exc) == (0, None)
    assert json.loads(out) == {"index": TAG, "hz": 9, "prefixes": 70, "priced": 70, "calls": 2,
                               "prune": {"kept": [TAG], "dropped": [{"index": "pq:iv:old:s", "rows": 71}], "ms": 4}}
    assert prices.calls == [{"n": "64", "scan": "2026-11-05"}, {"n": "64", "scan": "2026-11-05"}, {"prune": "1"}]
    assert prices.logged == [f"owner-prices: {TAG}: 64 priced, 6 to go",
                             f"owner-prices: {TAG}: 70 prefixes priced through 2026-11-05 (70 in 2 calls)",
                             "owner-prices: pruned pq:iv:old:s (71 rows)"]


def test_cli_on_a_deployment_without_a_ledger_has_nothing_to_price(prices) -> None:
    prices.answers = [{"ledger": False, "priced": 0, "remaining": 0}]
    code, exc, out = run("-p")
    assert (code, exc, json.loads(out)) == (0, None, {"ledger": False, "calls": 1})
    assert (prices.calls, prices.logged) == ([{"n": "64"}], ["owner-prices: no ownership ledger on this deployment: nothing to price"])


def test_cli_flags(prices) -> None:
    prices.answers = [answer(0, 0)]
    assert run("-g", "g.c001", "-n", "16", "-w", "5")[:2] == (0, None)
    assert prices.calls == [{"n": "16", "gen": "g.c001"}]
    assert prices.logged == [f"owner-prices: {TAG}: 150 prefixes priced through the newest scan (0 in 1 calls)"]


@pytest.mark.parametrize("error", [refusal(403, "forbidden"), refusal(404, "no owner-prices grant on this deployment")])
def test_cli_exits_1_with_the_sites_refusal(prices, error: site.SiteError) -> None:
    prices.answers = [error]
    assert run() == (1, f"owner-prices: {error}", None)


def test_cli_without_a_token_exits_1_before_any_call(prices, monkeypatch) -> None:
    monkeypatch.delenv("SITE_TOKEN")
    monkeypatch.delenv("GCS_USAGE_TOKEN", raising=False)  # (`site_token`'s deprecated fallback)
    assert run() == (1, "owner-prices: no token (-t or $SITE_TOKEN)", None)
    assert prices.calls == []


# ── The gate: a compaction moves the pointer only with the new generation's pricing ───


def carry(remaining: int = 0, roots: bool = False, carried: int = 71) -> dict:
    return {"from": "pq:iv:g:s", "index": "pq:iv:g.c001:s", "hz": 9, "carried": carried, "remaining": remaining, "roots": roots, "ms": 2}


def test_gate_fills_the_old_generation_then_carries_it(prices) -> None:
    prices.answers = [answer(64, 6, prefixes=70), answer(6, 0, prefixes=70), carry()]
    assert op.gate({"SITE_URL": f"{SITE}/", "SITE_TOKEN": "t"}, log=prices.logged.append).before("g", "g.c001") == carry()
    assert prices.calls == [{"n": "64", "gen": "g"}, {"n": "64", "gen": "g"}, {"carry": "g.c001", "from": "g"}]
    assert prices.logged[-1] == "owner-prices: 71 rows carried pq:iv:g:s → pq:iv:g.c001:s: nothing unpriced"


@pytest.mark.parametrize("got,said", [
    (carry(remaining=4), "pq:iv:g.c001:s lacks 4 prefixes after carrying 71 rows from pq:iv:g:s"),
    (carry(roots=True, carried=0), "pq:iv:g.c001:s lacks the roots after carrying 0 rows from pq:iv:g:s"),
    (refusal(409, "no interval store serves this deployment"), f"{SITE}{op.ENDPOINT} → HTTP 409: no interval store serves this deployment"),
])
def test_gate_refuses_unless_the_new_generation_is_priced(prices, got, said: str) -> None:
    prices.answers = [answer(0, 0), got]
    with pytest.raises(op.PricingRefused) as e:
        op.Gate(SITE, "t", **prices.kw).before("g", "g.c001")
    assert str(e.value) == said


def test_gate_has_nothing_to_carry_without_a_ledger_and_asks_no_more(prices) -> None:
    prices.answers = [{"ledger": False, "priced": 0, "remaining": 0}]
    assert op.Gate(SITE, "t", **prices.kw).before("g", "g.c001") == {"ledger": False}
    assert prices.calls == [{"n": "64", "gen": "g"}]


def test_gate_after_is_best_effort(prices) -> None:
    g = op.Gate(SITE, "t", **prices.kw)
    prices.answers = [carry()]
    assert g.after("g", "g.c001") == carry()
    prices.answers = [refusal(503, "boom")]
    assert g.after("g", "g.c001") is None
    assert prices.logged == [f"owner-prices: the carry after the move failed ({SITE}{op.ENDPOINT} → HTTP 503: boom): a prefix assigned during it is priced by the next fill"]


@pytest.mark.parametrize("env,missing", [({}, "$SITE_URL, $SITE_TOKEN"), ({"SITE_URL": SITE}, "$SITE_TOKEN"), ({"SITE_TOKEN": "t"}, "$SITE_URL")])
def test_a_gated_compaction_needs_the_site(env: dict, missing: str) -> None:
    with pytest.raises(SystemExit) as e:
        op.gate(env)
    assert str(e.value) == (f"owner pricing: no {missing}: a compaction that keeps owner pricing asks the site (`POST /api/owner-prices`) "
                            "before it moves the pointer")


class FakeGate:
    """A gate that records what it's asked, refusing at `refuse` (`ready` / `before`)."""

    def __init__(self, refuse: str | None = None):
        self.refuse, self.asked = refuse, []

    def ready(self, gen: str) -> dict:
        self.asked.append(f"ready {gen}")
        if self.refuse == "ready":
            raise op.PricingRefused("the site said no")
        return {"priced": 0}

    def before(self, old: str, new: str) -> dict:
        self.asked.append(f"before {old} {new}")
        if self.refuse == "before":
            raise op.PricingRefused("pq:iv:g.c001:s lacks 4 prefixes")
        return {"carried": 71, "remaining": 0, "roots": False}

    def after(self, old: str, new: str) -> dict:
        self.asked.append(f"after {old} {new}")
        return {"carried": 72}


def _due(tmp_path: Path):
    """`g` (a base through DATES[1], DATES[2:4] appended), current, a compaction of it forced."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    store = Store(str(store_dir))
    ar.set_current(store, ist.PREFIX, "g")
    return store_dir, store, dict(threads=2, mem="1GB", log=lambda m: None, now=0.0, force=True, policy=ar.NEVER)


def _pointers(store_dir: Path, store: Store) -> tuple:
    return _gens(store_dir), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists(), ar.current(store, ist.PREFIX)


def test_a_compaction_asks_before_the_fold_and_before_the_pointer_then_moves_it(tmp_path) -> None:
    store_dir, store, kw = _due(tmp_path)
    gate = FakeGate()
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", pricing=gate, **kw)
    assert gate.asked == ["ready g", "before g g.c001", "after g g.c001"]
    assert (doc["compact"]["gen"], doc["compact"]["current"], doc["compact"]["pricing"]) == (
        "g.c001", True, {"carried": {"carried": 71, "remaining": 0, "roots": False}, "after": {"carried": 72}})
    assert _pointers(store_dir, store) == (["g", "g.c001"], True, "g.c001")


def test_a_compaction_refused_before_its_fold_writes_nothing(tmp_path) -> None:
    store_dir, store, kw = _due(tmp_path)
    gate = FakeGate(refuse="ready")
    with pytest.raises(SystemExit) as e:
        ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", pricing=gate, **kw)
    assert str(e.value) == "compact g: its owner pricing can't be filled (the site said no): not compacting (the pointer would move to a generation without it)"
    assert (gate.asked, _pointers(store_dir, store)) == (["ready g"], (["g"], False, "g"))


def test_a_compaction_refused_at_the_pointer_leaves_the_old_generation_current(tmp_path) -> None:
    """Pricing for the new generation isn't in place: no `successor.json`, `CURRENT.json` on the old generation (appends
    and readers stay there), the new one unpublished. The next compaction, its pricing in place, replaces it and moves."""
    store_dir, store, kw = _due(tmp_path)
    gate = FakeGate(refuse="before")
    with pytest.raises(SystemExit) as e:
        ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", pricing=gate, **kw)
    assert str(e.value) == ("compact g → g.c001: owner pricing for g.c001 isn't in place (pq:iv:g.c001:s lacks 4 prefixes): no pointer moved; "
                            "g.c001 stays unpublished (the next compaction replaces it)")
    assert (gate.asked, _pointers(store_dir, store)) == (["ready g", "before g g.c001"], (["g", "g.c001"], False, "g"))
    # still appendable, on the old generation
    assert ar.resolve_gen(store, ist.PREFIX, "g", lambda m: None) == "g"
    again = FakeGate()
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w2", pricing=again, **kw)
    assert (doc["compact"]["gen"], again.asked, _pointers(store_dir, store)) == (
        "g.c001", ["ready g", "before g g.c001", "after g g.c001"], (["g", "g.c001"], True, "g.c001"))


def test_a_compaction_that_moves_no_pointer_carries_nothing(tmp_path) -> None:
    """`CURRENT.json` names another generation: the compaction records its successor and moves nothing, so nothing is
    asked at the pointer."""
    store_dir, store, kw = _due(tmp_path)
    ar.set_current(store, ist.PREFIX, "elsewhere")
    gate = FakeGate(refuse="before")
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", pricing=gate, **kw)
    assert (gate.asked, doc["compact"]["current"], "pricing" in doc["compact"], ar.current(store, ist.PREFIX)) == (["ready g"], False, False, "elsewhere")


def test_a_deployment_without_assignments_is_not_blocked(tmp_path, prices) -> None:
    """The real gate over a site with no ownership ledger (`{ledger: false}`: nothing to fill or carry), then over one
    whose ledger holds no assignment (nothing stale; the roots' rows carried): the pointer moves either way."""
    store_dir, store, kw = _due(tmp_path)
    prices.answers = [{"ledger": False, "priced": 0, "remaining": 0}]
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", pricing=op.Gate(SITE, "t", **prices.kw), **kw)
    assert (doc["compact"]["gen"], doc["compact"]["pricing"], _pointers(store_dir, store)) == (
        "g.c001", {"carried": {"ledger": False}, "after": None}, (["g", "g.c001"], True, "g.c001"))
    # asked before the fold and at the pointer; nothing to carry, before or after
    assert prices.calls == [{"n": "64", "gen": "g"}, {"n": "64", "gen": "g"}]

    store_dir, store, kw = _due(tmp_path / "empty")
    prices.calls.clear()
    prices.answers = [answer(0, 0, prefixes=0), answer(0, 0, prefixes=0), carry(carried=2), carry(carried=2)]
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w2", pricing=op.Gate(SITE, "t", **prices.kw), **kw)
    assert (doc["compact"]["pricing"], _pointers(store_dir, store)) == ({"carried": carry(carried=2), "after": carry(carried=2)}, (["g", "g.c001"], True, "g.c001"))
    assert prices.calls == [{"n": "64", "gen": "g"}, {"n": "64", "gen": "g"}, {"carry": "g.c001", "from": "g"}, {"carry": "g.c001", "from": "g"}]


def test_promote_refuses_without_pricing_and_moves_with_it(tmp_path) -> None:
    _, store_dir, store, _ = _candidate(tmp_path)
    gate = FakeGate(refuse="before")
    with pytest.raises(SystemExit) as e:
        ar.promote_gen(store, ist.PREFIX, "g.c001", pricing=gate, log=lambda m: None)
    assert str(e.value) == "promote g.c001: owner pricing for it isn't in place (pq:iv:g.c001:s lacks 4 prefixes): nothing moved, CURRENT.json stays at g"
    assert (gate.asked, (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists(), ar.current(store, ist.PREFIX)) == (["before g g.c001"], False, "g")
    ok = FakeGate()
    assert ar.promote_gen(store, ist.PREFIX, "g.c001", pricing=ok, log=lambda m: None) == {
        "from": "g", "gen": "g.c001", "current": True, "pricing": {"carried": {"carried": 71, "remaining": 0, "roots": False}, "after": {"carried": 72}}}
    assert (ok.asked, ar.current(store, ist.PREFIX)) == (["before g g.c001", "after g g.c001"], "g.c001")


# ── The staged compaction's job: `-O`, and what its gate asks the site with ──


def test_a_priced_profiles_compact_job_gets_the_flag_the_site_and_its_token() -> None:
    f = IvFake(["2026-08-03"], compact_level=1, owner_prices=True, site_secrets={"SITE_TOKEN": "site-tok"})
    r = f.runner(merge_wait=None, environ={"SITE_URL": SITE, "SITE_TOKEN": "never-copied"})
    (name, spec), = r.compact_jobs("2026-08-04")
    task = spec["taskGroups"][0]["taskSpec"]
    plain = IvFake(["2026-08-03"], compact_level=1).runner(merge_wait=None).compact_jobs("2026-08-04")[0][1]["taskGroups"][0]["taskSpec"]
    assert task["runnables"][0]["container"]["commands"][-1].split(" compact ")[1] == f"-C level:1 -r -j 1 -O -M 90GB -p 16 {COMMON} -m /gcs/data"
    assert plain["runnables"][0]["container"]["commands"][-1].split(" compact ")[1] == f"-C level:1 -r -j 1 -M 90GB -p 16 {COMMON} -m /gcs/data"
    assert task["environment"] == {
        **plain["environment"],
        "variables": {**plain["environment"]["variables"], "SITE_URL": SITE},
        "secretVariables": {**plain["environment"].get("secretVariables", {}), "SITE_TOKEN": "projects/proj/secrets/site-tok/versions/latest"},
    }


@pytest.mark.parametrize("cfg,environ,missing", [
    ({"site_secrets": {"SITE_TOKEN": "site-tok"}}, {}, "$SITE_URL in the submitter's environment"),
    ({}, {"SITE_URL": SITE}, "`site_secrets.SITE_TOKEN` in the profile"),
    ({}, {}, "$SITE_URL in the submitter's environment and `site_secrets.SITE_TOKEN` in the profile"),
])
def test_a_priced_profiles_compaction_is_not_submitted_without_the_site(cfg: dict, environ: dict, missing: str) -> None:
    r = IvFake(["2026-08-03"], compact_level=1, owner_prices=True, **cfg).runner(merge_wait=None, environ=environ)
    with pytest.raises(SystemExit) as e:
        r.compact_jobs("2026-08-04")
    assert str(e.value).split(": ", 1)[1] == f"the profile keeps owner pricing across a compaction (`owner_prices`), which asks the site: no {missing}"


def test_a_candidate_is_not_gated_until_its_promote() -> None:
    r = IvFake(["2026-08-03"], compact_level=1, owner_prices=True).runner(merge_wait=None, environ={})
    r.candidate = True
    (_, spec), = r.compact_jobs("2026-08-04")
    assert spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][-1].split(" compact ")[1] == f"-C level:1 -r -j 1 -N -M 90GB -p 16 {COMMON} -m /gcs/data"
