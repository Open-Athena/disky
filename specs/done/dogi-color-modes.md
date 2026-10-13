# dOGI color-mode parity

Status: 2026-10-13. Shared `cloud` fix implemented and verified; live rollout belongs to each deployment. No deployment bindings, permissions or infrastructure are changed by this checkpoint.

The map-card renderer previously retained `c` in URLs/cache identities but ignored it, drawing every card with owner colors. It now shares the interactive map's URL codec and available-axis fallback. `c=t` / `tree` uses the existing size-ranked directory palette and child hue fan; `c=w` / `written` / `date` / `age` uses the existing date gradient over the card's drawn leaves; `c=r` / `read` uses the scan metadata's access-log observation window, with brick-red cells for missing observations; `c=u` / `user` retains owner colors. Folded remainders stay neutral. Tree legends name directories; date/read cards do not show a misleading owner legend. Subtitles identify the selected effective axis, including provider-aware created/modified terminology.

Absent/invalid color selections default to owner on attributed scans and tree otherwise. A deployment whose frontend config sets `Store.defaultColor` must also set `OG_DEFAULT_COLOR` to match (`w` for OA R2's written default). Explicit URL selections win. Unsupported user/read axes fall back to owner if available, otherwise tree, using the same helper as the interactive dashboard.

The render version advances to `r=3` so new stamped URLs and internal caches cannot reuse the old palettes. External card consumers may still retain previously fetched unfurls; re-fetch the page metadata to obtain the new image URL. The [preview privacy contract] is unchanged: private generic cards never reach the map renderer; GCS must retain its explicit `OG_ANONYMOUS=shape` setting during rollout if its approved geometry/totals policy should remain.

Validation: nine exact regression cases failed before the fix; aliases, metadata defaults, missing read coverage, configured defaults, axis-specific legends and URL/cache propagation pass afterward. The real SVG/resvg-WASM pipeline generates four distinct PNGs from identical synthetic geometry. Full site suite: 1,931 passed / one skipped; client and Functions typechecks plus production build pass. Chrome verification: [local color-mode comparison]. Production verification remains the deployment owner's responsibility.

[preview privacy contract]: dogi-privacy.md
[local color-mode comparison]: http://127.0.0.1:3269/tmp/og-color-preview.html
