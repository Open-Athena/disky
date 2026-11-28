"""The month loader's goldens (specs/slack-in-workers.md): `tests/fixtures/digest/load/months.json` holds a snapshots
root's listing, its scans' `meta.json`s and `scan_runs` rows, and per case the month's rows as `load_month` →
`QuotaTemplate.rows_json` reads them and each scan's trigger (`scan_triggers`); `DT_UPDATE_GOLDEN=1` rewrites it.
`site/src/digest/load.test.ts` must answer it exactly, so a Worker's converge reads the month the job would."""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path

from dt_cloud import digest as E
from dt_cloud import digest_quota as DC

from digest_examples import CW_EXAMPLE

GOLDEN = Path(__file__).parent / "fixtures" / "digest" / "load" / "months.json"
TIB = 1024**4
TB = 10**12
NOW = dt.datetime(2026, 12, 3, 7, 30, tzinfo=dt.timezone.utc)

#: Mid-September (the window's look-back reaches before October) to December's 3rd: a scan a day at 07Z, date-only ids
#: on 10/12 (alone on its day) and 11/9 (beside a timed scan, so its `started` keys it), four a day on 11/20–21, no
#: scans 11/24–26.
def _scans() -> list[str]:
    out = []
    d = dt.date(2026, 9, 20)
    while d <= dt.date(2026, 12, 3):
        if dt.date(2026, 11, 24) <= d <= dt.date(2026, 11, 26):
            pass
        elif d in (dt.date(2026, 10, 12), dt.date(2026, 11, 9)):
            out.append(d.isoformat())
        elif d in (dt.date(2026, 11, 20), dt.date(2026, 11, 21)):
            out += [f"{d}T{h}03" for h in ("01", "07", "13", "19")]
        else:
            out.append(f"{d}T0702")
        d += dt.timedelta(days=1)
    return out


SCANS = _scans()


def _meta(i: int, scan: str) -> dict:
    """``main`` grows with a dip on 11/20; ``side`` shrinks; ``late`` joins in November; classes on the primary."""
    main = 250 + 0.37 * i - (3.3 if scan.startswith("2026-11-20") else 0)
    sizes = {"main": main, "side": max(0.0, 40 - 0.55 * i)}
    if scan >= "2026-11":
        sizes["late"] = 1.05 + 0.01 * i
    buckets = {b: {"total_bytes": round(v * TB), "total_objects": 1000 + 7 * i} for b, v in sizes.items()}
    buckets["main"]["class_bytes"] = {"1": round(main * 0.8 * TB), "5": round(main * 0.2 * TB)}
    m = {"total_bytes": sum(b["total_bytes"] for b in buckets.values()), "total_objects": 3, "buckets": buckets}
    if len(scan) == 10:
        m["started"] = f"{scan}T06:41:09Z"
    return m


METAS = {s: _meta(i, s) for i, s in enumerate(SCANS)}
#: 11/9's second scan, timed: the date-only one beside it needs its start (`times_needed`)
METAS["2026-11-09T1900"] = _meta(80, "2026-11-09T1900")
SCANS = sorted([*SCANS, "2026-11-09T1900"])
#: `scan_runs` rows: 11/2 and 11/21's 13Z by hand; 11/5 first by cron then resumed by hand (stays `schedule`); 11/6's
#: first run has no trigger (absent), a later one does (ignored: the first run decides).
RUNS = [
    {"scan": "2026-11-02T0702", "trigger": "manual", "started_ts": 1793603000},
    {"scan": "2026-11-05T0702", "trigger": "schedule", "started_ts": 1793862000},
    {"scan": "2026-11-05T0702", "trigger": "manual", "started_ts": 1793870000},
    {"scan": "2026-11-06T0702", "trigger": None, "started_ts": 1793948000},
    {"scan": "2026-11-06T0702", "trigger": "manual", "started_ts": 1793950000},
    {"scan": "2026-11-21T1303", "trigger": "manual", "started_ts": None},
    {"scan": "2026-11-21T0703", "trigger": "schedule", "started_ts": 1795331000},
]
#: The listing's keys: each scan's `meta.json` and `tree.json`, an unpublished scan (no meta), a stray file.
KEYS = sorted([*(f"snapshots/r2/{s}/meta.json" for s in SCANS), *(f"snapshots/r2/{s}/tree.json" for s in SCANS),
               "snapshots/r2/2026-12-03T1300/tree.json", "snapshots/r2/README.md"])
#: (name, month, primary, unit, as_of)
CASES = [
    ("october-first", "2026-10", "main", TB, None),
    ("november-tb", "2026-11", "main", TB, None),
    ("november-tib", "2026-11", "main", TIB, None),
    ("november-as-of-10th", "2026-11", "main", TB, "2026-11-10"),
    ("november-side", "2026-11", "side", TB, None),
    ("december-open", "2026-12", "main", TB, None),
    ("january-none", "2027-01", "main", TB, None),
]


def _expected(root: Path) -> dict:
    triggers = DC.scan_triggers(RUNS)
    cfg = replace(CW_EXAMPLE, primary="main")
    out = []
    for name, month, primary, unit, as_of in CASES:
        m = DC.load_month(str(root), dt.date.fromisoformat(f"{month}-01"), primary, unit=unit, triggers=triggers,
                          as_of=None if as_of is None else dt.date.fromisoformat(as_of), now=NOW)
        out.append({"name": name, "month": month, "primary": primary, "unit": unit, "as_of": as_of,
                    "rows": None if m is None else DC.QuotaTemplate(replace(cfg, primary=primary)).rows_json(m)})
    return {"now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "keys": KEYS, "metas": METAS, "runs": RUNS, "triggers": triggers,
            "scans": E.list_scans(str(root)), "cases": out}


def build(tmp: Path) -> dict:
    root = tmp / "snapshots" / "r2"
    for k in KEYS:
        p = tmp / k
        p.parent.mkdir(parents=True, exist_ok=True)
        s = k.split("/")[2]
        p.write_text(json.dumps(METAS[s]) if k.endswith("meta.json") else "{}")
    return _expected(root)


def test_load_goldens(tmp_path):
    got = build(tmp_path)
    if os.environ.get("DT_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1) + "\n")
    assert json.loads(GOLDEN.read_text()) == got
