# R2 CS3 retention policy: review before enabling

Status: proposal, November 8. The 621-key historical cleanup review is a separate request; approving that list does not enable the ongoing policies below. All current serving and rollback roots stay protected. A fixed workload must stop creating redundant retained copies, while genuine historical changes remain in the interval/static stores.

## Derived scan inputs

Both profiles now use path-only staging and carry opaque versions with `path-index -V`; their version-aware interval candidates passed exact answers and a cold/warm reader benchmark before promotion. Each native retirement pass keeps two recent inputs; older raw listings remain unless the passing marker explicitly compares string `version` identities, and attribution stays unless its history and comparison both cover the scan. Old unknown identities are never filled from a later observation. This bounds future redundant raw/cache/sort storage without deleting the fixed, unproved historical raw backlog.

Primary and meta age histories append before path retirement, verify every new histogram cell before publication, and each keep two recent per-scan pyramid sets. Original pyramids retire only when both a passing cell comparison and published history coverage hold the scan; D1 age footers clear before their corresponding files. The independent age roots are `history/primary-age` and `history/meta-age`. Native carries/compaction retain their publication ordering and rollback grace. Prepare an exact historical pyramid list before the first executing pass.

Generation cleanup follows each independent lineage, every current pointer and its successors, and the fixed rollback roots. It must not treat another store's root as unused. New native age roots and version-migration candidates are tracked separately; unused candidates must also eventually retire, rather than becoming permanent extra copies.

## Access archive handoff

The incremental consumer is live; its closed checkpoint remains in GCS and the collector archive remains intact. Before trimming the producer's frozen map, publish a verified receipt of the checkpoint's source/account, per-bucket incorporated cursors, durable object revision and digest. The collector must be able to read only the intended receipt. Its existing archive binding cannot read the GCS checkpoint, so the transport/permission change needs its own concrete infrastructure plan; no publisher credential is copied into the collector by this proposal.

A compact producer-state format must explicitly identify omitted closed history. General legacy aggregation must reject that format; the incremental reader must refuse a missing, wrong-source or lagging checkpoint instead of silently returning only recent reads. The collector publishes the compact state conditionally before retiring unreachable aggregate keys, keeps a recovery grace, and never retires an active/reverification window. Receipts cannot move a cursor backwards. Failure or an unavailable receipt stops retirement, not collection. Exact historical aggregate keys and the ongoing cursor-gated policy require review before any delete.

## Nextflow work and reports

Proposed resume window: seven days after a successfully published and verified run. Retain active and unsuccessful runs until reviewed, and preserve the durable scan metadata, D1 telemetry and source/index archives outside `nf/`. Do not apply a blanket bucket lifecycle: a prefix-only rule cannot distinguish completed work from a failed run's remaining evidence. A completion-aware native planner should produce the exact first deletion list and maintain the same bounded window afterwards.

## Gates

The production age-reader release, complete primary checkpoint scan and version-aware history cutovers passed their independent verification gates; the newly pinned image is undergoing full primary/meta pipeline validation. Historical cleanup stays held until Ryan approves the concrete list. Enabling rolling cleanup is a distinct decision with the keep/proof/grace rules above; this file itself authorizes no apply, D1 mutation or deletion.
