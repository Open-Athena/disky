# The site's default scan is the newest one search covers

## Why

Ryan (via gcs, 2026-10-24): "search won't work for ~an hour around each scan" is not acceptable. A scan publishes its data (path store, D1 rows) before its static-names run is appended (about 1 h today: catalog ~30 m, drill ~45 m, anchors ~20 m, longer when a compaction holds the lease). Meanwhile the site opens on the new scan, and every `?f=` search there is refused (`scan-not-indexed`).

## Rule

With the static filter on, the site's default scan, the one a view without `?d=` opens on, is the newest scan the static name index covers. A newer published scan is reachable explicitly (`?d=`, /scans) but isn't the default until search covers it. With the static filter off, the default stays the newest published scan.

## What's there

- `/api/filter-scans` lists the scans the static name index covers (`staticFilterStore(...).scans()`), the list `indexedGate` checks.
- A filtered view with no `?d=` already opens on the newest of them. This spec makes that rule the default for every view, not just filtered ones.
- Union views: `filter-scans`' cutoffs, the scans at which every member's resolved scan is indexed.

## Shape

1. **Server:** wherever the site resolves "latest" for a view without `?d=` (the Map, the table, digests' links, OG cards), resolve it as `min(newest published, newest indexed)` when the static filter is on. It's one shared resolver: per [[generic-code-no-deploy-defaults]], one exported scan-id resolver.
2. **Client:** the scan picker marks a published scan newer than the default as "indexing" (title: "search catches up when the static name index appends this scan"), selectable.
3. **/scans:** the run's chip is "search behind" until its scan is indexed (already the case after 6e42080d when the probe ran before the append).
4. **Mutable state:** none new. The covered list comes from the static store's manifests, as `filter-scans` reads it. No pointer to move, so a deployment whose static-names step is skipped or lags keeps a consistent (older) default rather than a stuck pointer.

## Not this spec

Driving the append itself from about 1 h to minutes: the catalog / drill / anchors fan-outs, following the compaction's. The gate makes the lag invisible; that track makes it short.

## Decision needed (Ryan)

This changes what every deployment shows as current: the default can trail the newest published scan by the append's duration (more while a compaction holds the lease). Go / no-go, and whether digests (Slack) should link the newest published scan or the default.
