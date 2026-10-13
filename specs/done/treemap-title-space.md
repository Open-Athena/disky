# Reuse empty branch title space

Implemented and deployed to OA R2 dev and production on October 13, 2026. The core remains opt-in for other consumers.

Ryan's OA R2 screenshot shows wide parent bars whose label/size occupy only the left side. The deployed DOM and canvas layouts both reserve a uniform 20 px strip. No prior implementation was found in this repository's shared-core or older UI history; root has no concurrent layout edits.

Add an opt-in `reclaimTitleSpace` core prop and enable it on the R2 site. Measure the rendered title font/padding once per map; conservatively reserve enough width for both DOM and canvas text. A branch with right-aligned size or arbitrary subtitle content retains its full-width title strip. Truncated long labels also retain the space they actually require.

Find a complete vertical partition to the right of the title: no child rectangle may cross it. Stretch all rectangles to its right together into the unused strip, preserving their relative heights and shared boundaries. Keep the parent/child outer bounds, nesting and hit tests exact. Left-side tiles remain below the title. If there is no complete partition, keep the existing geometry. Shared and gaps modes respect their own outer insets. Shrink the parent's title hover area when columns are raised.

DOM placement, canvas placement, outlines and exports must use the same reclamation helper. Verify exact geometry, hit testing, no crossing/overlap, collapsed-chain labels, long/right-aligned titles, both renderers and real OA R2 rendering. Deploy after tests and browser verification. Other deployments retain the core default until their owners opt in.

The shared `layoutChildren` helper now uses an O(n log n) complete-partition search. Font probes reproduce the cell's CSS ancestry and reserve tabular numeral widths as well as both renderer fonts. Right-column tiles gain the reclaimed strip's area; their relative heights stay proportional, while left-column geometry stays unchanged. Right-aligned sizes and arbitrary subtitle content conservatively retain full-width titles.

Validation: 180 core tests pass, including exact column bounds, crossing rejection, shared/gaps insets, collapsed labels, reclaimed hit tests and component DOM/canvas equivalence. The site production build/typechecks and 39 focused OG tests pass. Chrome verified both local renderers and production child targets inside the reclaimed strip. On the latest production scan, `normalized` starts at y=148.25; `nemotron_v1` remains at y=168.25, while `nemotron_cc_v2`, `nemotron_cc_v2_1` and `cp` rise to y=149.75 without clipping the title. Proof: `tmp/r2-title-production.png` and `tmp/r2-title-normalized-production.png`; preview deployment `c2596364`, production `d8820a6d`.
