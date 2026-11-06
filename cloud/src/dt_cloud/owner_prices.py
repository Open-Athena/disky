"""Owner pricing, filled by the pipeline (specs/owner-pricing-off-the-read-path.md).

The site never prices owner assignments inside a read: a prefix's bytes over a store generation's scans are rows of
D1's `owner_prefix_hist`, and a read that finds one missing answers "pricing pending". This is the job's side: a
client of the site's `POST /api/owner-prices` (its named grant's alone: the site's `OWNER_PRICES_GRANT`), which holds
the one implementation of the pricing (the site's reader over the interval store) and the table's key format.

- `fill`: every prefix priced through the store's newest scan, a bounded chunk a call (the Worker's CPU limit bounds
  a request, so the fill is many small ones): after each scan is appended (`dt-cloud owner-prices -d <scan>`, the
  `owner-prices` scan step), or by hand after a large assignment.
- `Gate`: what a compaction asks before it moves the store's `CURRENT.json` (`append_runner.compact_store`,
  `promote_gen`): the old generation filled to its end, then its rows carried to the new generation's key (the same
  scans, so the same rows). Refused (`PricingRefused`), the pointer stays: a move never leaves readers on a
  generation without pricing. A deployment without an ownership ledger has nothing to carry: the site says so, and
  the gate passes.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlencode

from . import site
from .site import SiteError

ENDPOINT = "/api/owner-prices"
#: Prefixes a call prices (one history read on the site): what its Worker handles well inside its CPU limit.
CHUNK = 64
#: The least a failing fill is cut down to (`fill` halves its chunk when a call fails as a server error): one prefix,
#: so a range only counts as refused at every size when a single prefix is.
MIN_CHUNK = 1
#: How long `fill` waits for the site's view of the store to hold the scan it was asked for (the site holds its
#: state for a minute per isolate and per colo), and how often it asks.
WAIT_S = 600
POLL_S = 15
#: A failed call (a server error, or no answer) is asked again this far apart, at half its chunk; at `MIN_CHUNK`,
#: this many more times.
RETRIES = 3
RETRY_S = 10
#: A call's timeout: one history read, a few seconds; a cold isolate's first, tens.
TIMEOUT_S = 120


class PricingRefused(Exception):
    """Owner pricing isn't in place (the site refused, didn't answer, or reports prefixes still unpriced)."""


def err(*a: object) -> None:
    print(*a, file=sys.stderr)


def call(base: str, token: str, **params: object) -> dict:
    """One `POST /api/owner-prices?<params>` (None-valued params left out): its JSON answer."""
    q = urlencode({k: v for k, v in params.items() if v is not None})
    try:
        return json.loads(site.post_json(base, token, f"{ENDPOINT}?{q}" if q else ENDPOINT, {}, timeout=TIMEOUT_S))
    except OSError as e:
        # a read timeout: urllib raises it bare (`site._fetch` words a refusal and an unreachable site)
        raise SiteError(f"{base}{ENDPOINT} didn't answer: {e}") from e


def fill(base: str, token: str, *, scan: str | None = None, gen: str | None = None, n: int = CHUNK, wait_s: float = WAIT_S,
         log: Callable[[str], None] = err, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> dict:
    """Every ledger prefix priced through the store's newest scan: calls until the site reports none remaining.

    `scan`: the scan just appended; the site answers 409 `behind` until the store it sees holds it, waited out up to
    `wait_s`. `gen`: that generation, not the site's current one. A call failing as a server error (or not answering) is
    asked again with half the chunk, down to one prefix (`RETRIES` more there). A chunk that fails holds a range too wide
    for a call, so it stays down until the fill has priced past what the largest failed call asked for, then doubles
    back to `n`. Raises `PricingRefused` on a refusal, a wait run out, a call that prices nothing while prefixes remain,
    or a prefix refused at every size. That last stops the fill, naming where, rather than going past it: the site prices
    the head of what's stale, and one unpriced prefix leaves a scan's owner-scoped views pending and a compaction's gate
    refusing, so pricing the rest would buy neither. Returns `{index, hz, prefixes, priced, calls}`, or `{ledger: False,
    calls}` on a deployment without an ownership ledger (nothing to price).
    """
    t0 = clock()
    priced = calls = failures = 0
    chunk, floor = n, min(MIN_CHUNK, n)
    past = 0
    remaining: int | None = None
    while True:
        try:
            got = call(base, token, n=n, scan=scan, gen=gen)
        except SiteError as e:
            if e.status == 409 and e.body.get("behind"):
                if clock() - t0 >= wait_s:
                    raise PricingRefused(f"{e} (waited {round(clock() - t0)}s for the site's store to hold it)") from e
                log(f"owner-prices: the site's store doesn't hold {scan} yet; asking again in {POLL_S}s")
                sleep(POLL_S)
                continue
            if e.status is not None and e.status < 500:
                raise PricingRefused(str(e)) from e
            past = max(past, priced + n)
            if n > floor:
                n = max(floor, n // 2)
                log(f"owner-prices: {e}; again in {RETRY_S}s with {n} prefixes a call")
            elif failures < RETRIES:
                failures += 1
                log(f"owner-prices: {e}; again in {RETRY_S}s with {n} prefixes a call ({failures}/{RETRIES})")
            else:
                where = "the first prefix still unpriced" if remaining is None else f"the first of the {remaining} prefixes still unpriced"
                raise PricingRefused(f"{e} (refused at every size from {chunk} to {n}: {where}, after {priced} priced by this run)") from e
            sleep(RETRY_S)
            continue
        calls += 1
        failures = 0
        if got.get("ledger") is False:
            return {"ledger": False, "calls": calls}
        priced += got["priced"]
        remaining = got["remaining"]
        if n < chunk and priced >= past:
            n = min(chunk, n * 2)
        if not got["remaining"]:
            return {"index": got["index"], "hz": got["hz"], "prefixes": got["prefixes"], "priced": priced, "calls": calls}
        if not got["priced"]:
            raise PricingRefused(f"{got['index']}: a call priced nothing with {got['remaining']} prefixes still unpriced")
        log(f"owner-prices: {got['index']}: {priced} priced, {got['remaining']} to go")


@dataclass
class Gate:
    """A compaction's owner-pricing gate over the site at `base` (its named grant's `token`)."""

    base: str
    token: str
    n: int = CHUNK
    log: Callable[[str], None] = err
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def ready(self, gen: str) -> dict:
        """Generation `gen` filled to its end (`fill`): asked before a fold spends hours, and before a carry."""
        return fill(self.base, self.token, gen=gen, n=self.n, wait_s=0, log=self.log, sleep=self.sleep, clock=self.clock)

    def before(self, old: str, new: str) -> dict:
        """Before the pointers move from `old` to `new`: `old` filled, then its rows carried to `new`'s key. Raises
        `PricingRefused` unless the site then reports nothing unpriced for `new`. Returns the carry's answer."""
        if self.ready(old).get("ledger") is False:
            return {"ledger": False}
        try:
            got = call(self.base, self.token, carry=new, **{"from": old})
        except SiteError as e:
            raise PricingRefused(str(e)) from e
        if got.get("ledger") is False:
            return {"ledger": False}
        if got["remaining"] or got["roots"]:
            lacks = " and ".join(x for x in (f"{got['remaining']} prefixes" if got["remaining"] else "", "the roots" if got["roots"] else "") if x)
            raise PricingRefused(f"{got['index']} lacks {lacks} after carrying {got['carried']} rows from {got['from']}")
        self.log(f"owner-prices: {got['carried']} rows carried {got['from']} → {got['index']}: nothing unpriced")
        return got

    def after(self, old: str, new: str) -> dict | None:
        """After the pointers moved: the carry again, for a prefix a ledger write priced on `old` meanwhile (the site
        served `old` until its pointer moved). Best effort: a failure is logged (the next fill prices it), not raised."""
        try:
            got = call(self.base, self.token, carry=new, **{"from": old})
        except (SiteError, OSError, ValueError) as e:
            self.log(f"owner-prices: the carry after the move failed ({e}): a prefix assigned during it is priced by the next fill")
            return None
        return got


def gate(env: Mapping[str, str], log: Callable[[str], None] = err) -> Gate:
    """The gate over `$SITE_URL` as `$SITE_TOKEN` (the grant the site names in `OWNER_PRICES_GRANT`). Exits when either
    is missing: a compaction asked to keep owner pricing (`-O`) must not run without the site to ask."""
    url, token = (env.get("SITE_URL") or "").strip(), (env.get("SITE_TOKEN") or "").strip()
    if not url or not token:
        missing = ", ".join(k for k, v in (("$SITE_URL", url), ("$SITE_TOKEN", token)) if not v)
        raise SystemExit(f"owner pricing: no {missing}: a compaction that keeps owner pricing asks the site (`POST {ENDPOINT}`) before it moves the pointer")
    return Gate(url.rstrip("/"), token, log=log)


def prune(base: str, token: str) -> dict:
    """The rows of generations the store no longer holds, deleted (`?prune=1`): `{kept, dropped}`."""
    try:
        return call(base, token, prune=1)
    except SiteError as e:
        raise PricingRefused(str(e)) from e
