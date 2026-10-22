# gcs: root map card under-counts ledger-assigned owners

Branch / worktree: `gcs` (`~/c/disky/wt/gcs`); shared code in `site/functions/_lib/og/data.ts`.

## Repro (full-tier share links, scan 2026-10-16T0701)

| view | Michael | unowned |
|---|---|---|
| dashboard root legend (gcs.oa.dev, signed in) | 283 T (2nd largest) | 2099 T |
| `/users` card (`gcs-users.png`, `ownerTotals`) | 262 TiB | 1.86 PiB |
| `/marin-us-central2` card (`gcs-drill-central2.png`) | 112 TiB, `documents` (84 TiB) drawn purple | 337 TiB |
| `/` root card (`gcs-root.png`) | **absent from legend**; `marin-us-central2/documents` drawn grey | 2.13 PiB |

Expected: the root card's colours and legend agree with the dashboard root (and with the drill card for the same nodes).

## Pointers / hypotheses

- `mapCard` applies the live ledger with `` applyLedger(tree, idx, site.scheme + (path ? `${path}/` : ''), …) `` only `if (!lens && env.DB)`. At the root the prefix is `gs://`; check that `applyLedger`'s `rec` builds `gs://marin-us-central2/documents` for depth-2 nodes of a `maxDepth: 2` view and that `touchedPrefixes` matches (`src/ledgerOverlay.ts`).
- Or the root `buildView` (`maxDepth: 2`) returns depth-2 leaves without `us`, while the dashboard root (deeper fetch) has them.
- Or the colo cache: the card cache key includes `ledgerHead`; confirm a fresh render (bump `r` locally) shows the same.
- Repro locally with the root view at the same scan; compare `tree.us` before/after `applyLedger` and the dashboard's `/api/subtree` + client overlay for the same scan.

## Resolution (2026-10-22)

None of the hypotheses above: the ledger rows (12, one owner) sit *under* `marin-us-central2/documents/`, and the root card draws only two levels (`OG_FLOOR_DEPTH`), so `documents` came back childless and `applyLedger` repainted only drawn paths. Fixed in `e2e3d4ea` (`[cloud]`, cloud `0f3794b0`): `applyLedger` takes the ledger rows' subtree sizes by owner (`ownerAssignments`) and folds them exactly into undrawn tiles; `mapCard` passes them for whole-subtree views. Verified on dev's root card (Michael 213 TiB in the legend, `documents` purple), deployed to prod.
