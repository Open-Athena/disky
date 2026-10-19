# Retire `hashSpy` → `use-prms/sections`

**Status:** proposed. **Source:** use-prms now ships the section spy upstream (new `use-prms/sections` subpath), ported from this repo's [`hashSpy.ts`](../site/src/hashSpy.ts). disky owns adopting it and deleting its bespoke copy. The upstream spec (with the full API and rationale) is `use-prms/specs/done/section-spy.md`.

## Why

`site/src/hashSpy.ts` (~300 lines) is in production here (home page scroll-spy, staged-deletions page), and was lifted into use-prms verbatim-then-generalized. Keeping a second copy is pure drift risk. The upstream version adds **param mode** (`?s=diff` instead of `#diff`), which the fragment can't give a dOGI — directly relevant to this repo's [`section-aware-dogi`](section-aware-dogi.md) work (the Worker already reads `sp.get('section')`).

## Dependency

`site/package.json` pins `use-prms: ^0.5.0`, which predates `use-prms/sections`. Bump to the use-prms build that includes it:

- Prefer a dist build while it's unreleased: `pds gh use-prms` (pick the SHA at/after the section-spy commits), or
- `pds n use-prms <version>` once a release ≥ that ships.

Verify `import { useSectionSpy } from 'use-prms/sections'` resolves before starting.

Minor behavior note (applies to all use-prms consumers in this bump): the debounce "discard pending on external URL change" check is now **per-key** — an external write to a *different* key no longer discards a pending debounced value. disky has no debounced `useUrlState` that this changes, but worth knowing.

## Phase 1 — fragment mode, no behavior change

Land this first; the URL behavior is identical to today's `hashSpy`.

- **Delete** `site/src/hashSpy.ts` and `site/src/hashSpy.test.ts` (the tests live upstream now).
- **`SectionAnchor.tsx`** — becomes a thin wrapper, or is deleted:
  - Keep it: re-export upstream `SectionLink` with the `sec-anchor` class —
    ```tsx
    import { SectionLink } from 'use-prms/sections'
    export const SectionAnchor = ({ id, children }: { id: string; children: ReactNode }) =>
      <SectionLink id={id} className="sec-anchor">{children}</SectionLink>
    ```
  - Or delete it and update the `App.tsx` / `SizeOverTime.tsx` imports to `SectionLink` directly (pass `className="sec-anchor"`).
- **`App.tsx:669`** — replace the `useHashSpy` call:
  ```tsx
  import { useSectionSpy } from 'use-prms/sections'
  useSectionSpy({
    sections: SECTION_IDS,
    nav: { location, navType },
    deps: [mapTree, meta, scans],
    aliases: LEGACY_ANCHORS,                 // was `legacy`
    reference: { offset: topbarH },          // was `offset`
    write: spy === 'on' ? 'replace' : 'off', // was `spy: spy === 'on'`
    mode: { kind: 'fragment' },
  })
  ```
  Option renames vs `useHashSpy`: `ids`→`sections`, `legacy`→`aliases`, `offset`→`reference.offset`, `spy: boolean`→`write: 'replace' | 'off'`. `location`/`navType`/`deps`/`pursuitMs` are unchanged (now under `nav` for the first two).
- **`StagedPage.tsx:344`** — same shape: `sections: sessionAnchors`, `aliases: memberAnchors`, `deps: [...]`, `mode: { kind: 'fragment' }`.
- **`prefs.tsx`** — keep the `scroll-spy` pref and `SiteNav` item; it maps to `write` (`'on'` → `'replace'`, else `'off'`).
- Saved positions under the old `hashSpy.pos` sessionStorage key are lost once per tab (the upstream key is `use-prms:sections:pos`, and the per-entry key in `history.state` renamed `hsKey`→`upKey`). Acceptable.

Verify: deep links (`#diff`, `#tbl`, legacy `#changes`/`#size-over-time`/`#created-date`, StagedPage `#batch-123`) still land; scroll-spy names the hash; back/forward restore; the `scroll-spy` pref toggles writing.

## Phase 2 — param mode (default `key: 's'`)

Flip the section into the query so the dOGI can see it. The use-prms default key is `s` (shorter); the Worker currently reads `section`, so make it accept both.

- **`App.tsx` / `StagedPage.tsx`** — `mode: { kind: 'param' }` (default key `s`). Default `adoptFragment: true` converts an incoming `#diff` naming a known section into `?s=diff`, then drops the fragment; `aliases` still map the legacy ids first. Under this repo's query-param setup the section lives in the query by default — exactly what the dOGI needs.
- Existing `#diff`/`#tbl`/legacy/`#batch-123` links keep working via `adoptFragment` + `aliases`: they land, then the URL becomes `?s=diff`.
- **dOGI (`og/routes.ts` `pageView`)** — change `sp.get('section') === 'diff'` to `(sp.get('s') ?? sp.get('section')) === 'diff'`, so new `?s=` links and any already-shared `?section=` links both render their card.
- **`drillTo` (~App.tsx:746)** — `withoutSection(search)` alongside its `q.delete('open')`. Audit every `navigate({ search })` built from router state: the `?path=` / legacy-param rewrites (~L169 / ~L333) pass `hash: window.location.hash` (still correct); their `search` comes from the router and gets the stale-carry rule, but call `withoutSection` where they carry a section.
- **`app.scss`** — `.stage-batch:target` → `.stage-batch[data-section-target]` (param mode never sets `:target`; the spy marks the parked element with `data-section-target`, cleared on the reader's next scroll). Audit other `:target` selectors similarly.
- **dOGI cards** — extend `pageView` beyond `diff` (`over-time`, `mtime`, `tbl` cards) as a follow-up; out of scope here (tracked in [`section-aware-dogi`](section-aware-dogi.md)).

## Done when

- `hashSpy.ts` / `hashSpy.test.ts` deleted; nothing imports them.
- Home + staged pages run on `useSectionSpy`; deep links, scroll-spy, restore, and the `scroll-spy` pref all behave as before.
- Param mode writes `?s=…`; the dOGI renders the `diff` card for `?s=diff` (and still for already-shared `?section=diff`).
- `specs/use-prms-sections.md` updated to final state and moved to `specs/done/`.
