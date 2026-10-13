# Reuse empty branch title space

Ryan's OA R2 screenshot shows wide parent bars whose label/size occupy only the left side. The deployed DOM and canvas layouts both reserve a uniform 20 px strip. No prior implementation was found in this repository's shared-core or older UI history; root has no concurrent layout edits.

Add an opt-in `reclaimTitleSpace` core prop and enable it on the R2 site. Measure the rendered title font/padding once per map; conservatively reserve enough width for both DOM and canvas text. A branch with right-aligned size or arbitrary subtitle content retains its full-width title strip. Truncated long labels also retain the space they actually require.

Find a complete vertical partition to the right of the title: no child rectangle may cross it. Stretch all rectangles to its right together into the unused strip, preserving their relative heights and shared boundaries. Keep the parent/child outer bounds, nesting and hit tests exact. Left-side tiles remain below the title. If there is no complete partition, keep the existing geometry. Shared and gaps modes respect their own outer insets. Shrink the parent's title hover area when columns are raised.

DOM placement, canvas placement, outlines and exports must use the same reclamation helper. Verify exact geometry, hit testing, no crossing/overlap, collapsed-chain labels, long/right-aligned titles, both renderers and real OA R2 rendering. Deploy after tests and browser verification. Other deployments retain the core default until their owners opt in.
