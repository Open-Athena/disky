#!/usr/bin/env python
"""Copy an interval-store generation's base files (no runs, manifests or verify output) from one store to another,
server-side (`job/cw-interval-submit.sh` `SEED_GEN`): a scratch store at the base's scans, for an `add` to re-append
from, e.g. to size a compaction off the live store.

    job/iv-seed.py SRC DST GEN    # SRC, DST: store roots on the same endpoint (`r2://bucket[/prefix]`)
"""
import sys

from disk_tree.blobfs import fs_for

RUN_DIRS = ("deltas", "manifests", "verify")

src, dst, gen = sys.argv[1:]
fs, _ = fs_for(src)
s = f"{src.split('://', 1)[1]}/interval-store/{gen}"
d = f"{dst.split('://', 1)[1]}/interval-store/{gen}"
if fs.exists(f"{d}/scans.json"):
    sys.exit(f"{dst}: interval-store/{gen} exists; not seeding over it")
keys = [k for k in fs.find(s) if k[len(s) + 1:].split("/")[0] not in RUN_DIRS]
for k in sorted(keys, key=lambda k: k.endswith("/scans.json")):  # scans.json last: the generation exists once it lands
    t = d + k[len(s):]
    if "file" in fs.protocol:  # a local store (the tests); object stores have no dirs
        fs.makedirs(t.rsplit("/", 1)[0], exist_ok=True)
    fs.copy(k, t)
print(f"seeded {len(keys)} files: {s} -> {d}", flush=True)
