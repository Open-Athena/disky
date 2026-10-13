#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click", "google-cloud-storage"]
# ///
"""Pinned CW artifact migration; no source writes, deletion or live cutover."""

from base64 import b64decode
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from itertools import batched
from pathlib import Path

from click import ClickException, IntRange, Path as ClickPath, group, option

SOURCE = "oa-gcs-usage-dvx"
DESTINATION = "oa-cw-s3-usage-data"
CONTROL_LIMIT = 2 * 1024 * 1024
SCRATCH_SOURCE = "oa-gcs-usage-scratch"
SCRATCH_DESTINATION = "oa-cw-s3-usage-scratch"


def controls_key(
    key: str,
    generation: str,
) -> bool:
    prefix = f"static-names/{generation}/"
    return key.startswith(prefix) and (key.endswith("/scans.json") or key.startswith(prefix + "manifests/") and key.endswith(".json"))


def load_inventory(directory: Path) -> tuple[list[dict], str]:
    success = json.loads((directory / "_SUCCESS.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    generation = summary["static_gen"]
    if not generation or "/" in generation or generation in (".", ".."):
        raise ValueError("invalid static generation")
    roots = ["cw-l2/", "snapshots/cw/", "digest/cw/", "probes/cw/", "cw-sweep/", "sweep/cw/",
             "meta-l2/", "snapshots/meta/", f"static-names/{generation}/"]
    records = [json.loads(line) for line in (directory / "data.jsonl").read_text().splitlines()]
    if len(records) != success["data_objects"] or summary["missing_index_files"]:
        raise ValueError("inventory is incomplete")
    keys = [r["key"] for r in records]
    if keys != sorted(set(keys)):
        raise ValueError("inventory keys must be unique and sorted")
    totals = {root: {"objects": 0, "bytes": 0} for root in roots}
    for r in records:
        matches = [root for root in roots if r["key"].startswith(root)]
        if r["bucket"] != SOURCE or len(matches) != 1:
            raise ValueError(f"outside approved CW source/scope: {r['key']}")
        if not str(r["generation"]).isdigit() or int(r["generation"]) <= 0 or r["bytes"] < 0 or not (r["md5"] or r["crc32c"]):
            raise ValueError(f"incomplete pinned metadata: {r['key']}")
        if r["storage_class"] != "STANDARD":
            raise ValueError(f"unreviewed storage class: {r['key']}")
        total = totals[matches[0]]
        total["objects"] += 1
        total["bytes"] += r["bytes"]
    if totals != summary["data"]:
        raise ValueError("inventory totals do not match summary")
    return records, generation


def fingerprint(blob: object) -> dict:
    return {"bytes": blob.size, "md5": blob.md5_hash, "crc32c": blob.crc32c, "storage_class": blob.storage_class}


def scratch_state(
    records: list[dict],
    generation: str,
    scan: str,
    ranges: int,
) -> list[dict]:
    """Only the newest published open state is resumable; never copy a lease."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}", scan) or isinstance(ranges, bool) or not isinstance(ranges, int) or ranges < 1:
        raise ValueError("invalid published scratch state identity/range count")
    prefix = f"static-names/{generation}/"
    if any(r["bucket"] != SCRATCH_SOURCE or not r["key"].startswith(prefix) for r in records):
        raise ValueError("scratch records outside CW source generation")
    if any(r["key"] == prefix + "merge.lease.json" for r in records):
        raise ValueError("merge lease exists; drain/review it before state preservation")
    root = prefix + f"state/{scan}/"
    state = sorted((r for r in records if r["key"].startswith(root)), key=lambda r: r["key"])
    expected = sorted(root + f"{kind}/r{i:04d}.{extension}" for kind, extension in (("copen", "parquet"), ("done", "json")) for i in range(ranges))
    if [r["key"] for r in state] != expected:
        raise ValueError("published scratch state is incomplete or contains unexpected objects")
    for record in state:
        if record["storage_class"] != "STANDARD" or record["bytes"] < 0 or int(record["generation"]) <= 0 or not (record["md5"] or record["crc32c"]):
            raise ValueError("scratch state has incomplete pinned metadata")
    return state


def pinned_json(
    client: object,
    record: dict,
) -> dict:
    if record["bytes"] > CONTROL_LIMIT:
        raise ValueError("pinned JSON exceeds bounded read limit")
    blob = client.bucket(record["bucket"]).blob(record["key"], generation=int(record["generation"]))
    blob.reload(if_generation_match=int(record["generation"]))
    verify_blob(blob, record)
    raw = blob.download_as_bytes(if_generation_match=int(record["generation"]))
    if len(raw) != record["bytes"]:
        raise ValueError("pinned JSON size differs from inventory")
    return json.loads(raw)


def audit_scan_pins(
    documents: list[dict],
    records: list[dict],
) -> list[dict]:
    """A control's exact input revision must be the revision the manifest will copy."""
    by_key = {r["key"]: r for r in records}
    problems = []
    for control in documents:
        doc = control["document"]
        if "bucket" not in doc:
            continue
        if doc["bucket"] != SOURCE:
            problems.append({"control": control["key"], "field": "/bucket", "error": "unexpected bucket"})
            continue
        for i, scan in enumerate(doc["scans"]):
            record = by_key.get(scan["src"])
            at = {"control": control["key"], "field": f"/scans/{i}", "source": scan["src"]}
            if record is None:
                problems.append({**at, "error": "source outside inventory"})
                continue
            expected = {"generation": int(record["generation"]), "size": record["bytes"],
                        "md5": b64decode(record["md5"]).hex() if record["md5"] else None, "crc32c": record["crc32c"]}
            actual = {k: scan.get(k) for k in expected}
            if actual != expected:
                problems.append({**at, "error": "source pin differs from current inventory", "control_pin": actual, "inventory_pin": expected})
    return problems


def remap_control(
    document: dict,
    destination_generations: dict[str, str],
) -> dict:
    """Only the reviewed bucket and scan-input generations may change."""
    refs = old_bucket_fields(document)
    if refs != (["/bucket"] if "bucket" in document else []):
        raise ValueError("unreviewed embedded source-bucket reference")
    result = deepcopy(document)
    if "bucket" not in result:
        return result
    if result["bucket"] != SOURCE:
        raise ValueError("unreviewed control bucket")
    result["bucket"] = DESTINATION
    for scan in result["scans"]:
        generation = destination_generations[scan["src"]]
        if not generation.isdigit() or int(generation) <= 0:
            raise ValueError("invalid destination generation")
        scan["generation"] = int(generation)
    return result


def old_bucket_fields(
    value: object,
    path: str = "",
) -> list[str]:
    if isinstance(value, dict):
        return [p for key, child in value.items() for p in old_bucket_fields(child, path + "/" + key)]
    if isinstance(value, list):
        return [p for i, child in enumerate(value) for p in old_bucket_fields(child, path + "/" + str(i))]
    return [path] if isinstance(value, str) and SOURCE in value else []


def verified_copies(
    journal: Path,
    inventory: Path,
    records: list[dict],
    generation: str,
) -> dict[str, str]:
    summary = json.loads((journal / "summary.json").read_text())
    success = json.loads((journal / "_SUCCESS.json").read_text())
    expected = {r["key"]: r["generation"] for r in records if not controls_key(r["key"], generation)}
    if summary["inventory_sha256"] != sha256((inventory / "data.jsonl").read_bytes()).hexdigest() or summary["destination"] != DESTINATION:
        raise ValueError("copy journal belongs to a different inventory/destination")
    copies = [json.loads(line) for line in (journal / "objects.jsonl").read_text().splitlines()]
    actual = {c["key"]: c["source_generation"] for c in copies}
    if actual != expected or len(copies) != len(expected) or success["payloads_checked"] != len(expected):
        raise ValueError("copy journal is incomplete")
    for c in copies:
        if c["action"] not in ("copied", "verified") or not str(c.get("destination_generation", "")).isdigit() or int(c["destination_generation"]) <= 0:
            raise ValueError(f"payload not verified: {c['key']}")
    return {c["key"]: c["destination_generation"] for c in copies}


def verify_blob(
    blob: object,
    record: dict,
) -> None:
    actual = fingerprint(blob)
    expected = {k: record[k] for k in actual}
    if actual != expected or blob.generation is None:
        raise ValueError(f"destination checksum/size/class mismatch: {record['key']}")


def copy_one(
    client: object,
    record: dict,
    apply: bool,
    destination_bucket: str = DESTINATION,
) -> dict:
    pairs = {DESTINATION: SOURCE, SCRATCH_DESTINATION: SCRATCH_SOURCE}
    if destination_bucket not in pairs or record["bucket"] != pairs[destination_bucket]:
        raise ValueError("unreviewed source/destination pair")
    destination = client.bucket(destination_bucket)
    existing = destination.get_blob(record["key"])
    if existing is not None:
        verify_blob(existing, record)
        return {"key": record["key"], "source_generation": record["generation"], "destination_generation": str(existing.generation), "action": "verified"}
    if not apply:
        return {"key": record["key"], "source_generation": record["generation"], "action": "missing"}
    source = client.bucket(record["bucket"]).blob(record["key"], generation=int(record["generation"]))
    target = destination.blob(record["key"])
    token = None
    while True:
        token, _, _ = target.rewrite(source, token=token, if_generation_match=0, if_source_generation_match=int(record["generation"]))
        if token is None:
            break
    # Reload exactly the new revision, never a concurrent replacement.
    if target.generation is None:
        raise ValueError(f"rewrite did not return a destination generation: {record['key']}")
    target.reload(if_generation_match=int(target.generation))
    verify_blob(target, record)
    return {"key": record["key"], "source_generation": record["generation"], "destination_generation": str(target.generation), "action": "copied"}


def write_json(
    directory: Path,
    filename: str,
    value: object,
) -> None:
    with (directory / filename).open("x") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")


@group()
def main() -> None:
    """Inspect controls and plan/seed checksum-verified server-side copies."""


@main.command("controls")
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private directory; refuses existing paths")
def controls(
    directory: Path,
    output: Path,
) -> None:
    """Read only pinned control JSON, bounded to 2 MiB per object; no writes to cloud."""
    from google.cloud.storage import Client

    records, generation = load_inventory(directory)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    client = Client(project="oa-internal-450019")
    documents = []
    for r in records:
        if not controls_key(r["key"], generation):
            continue
        if r["bytes"] > CONTROL_LIMIT:
            raise ClickException(f"control JSON exceeds 2 MiB: {r['key']}")
        blob = client.bucket(SOURCE).blob(r["key"], generation=int(r["generation"]))
        raw = blob.download_as_bytes(if_generation_match=int(r["generation"]))
        if len(raw) != r["bytes"] or len(raw) > CONTROL_LIMIT:
            raise ClickException(f"control JSON size differs from inventory: {r['key']}")
        documents.append({"key": r["key"], "generation": r["generation"], "sha256": sha256(raw).hexdigest(), "document": json.loads(raw)})
    write_json(output, "controls.json", documents)
    problems = audit_scan_pins(documents, records)
    write_json(output, "pin-problems.json", problems)
    if problems:
        raise ClickException(f"{len(problems)} control source pins differ; do not copy or cut over (see pin-problems.json)")
    write_json(output, "_SUCCESS.json", {"documents": len(documents)})
    print(json.dumps({"documents": len(documents), "output": str(output)}))


@main.command("copy")
@option("-a", "--apply", is_flag=True, help="Copy missing payloads; absent = metadata-only verification/plan")
@option("-D", "--writers-drained", is_flag=True, help="Attest the CW writers/schedules are drained; required with --apply")
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-j", "--jobs", type=IntRange(1, 16), default=8, show_default=True)
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private journal directory; refuses existing paths")
def copy(
    apply: bool,
    writers_drained: bool,
    directory: Path,
    jobs: int,
    output: Path,
) -> None:
    """Plan or copy payloads only. Control JSON is deferred, never blindly published."""
    from google.cloud.storage import Client

    if apply and not writers_drained:
        raise ClickException("--apply requires --writers-drained and a fresh drained inventory")
    records, generation = load_inventory(directory)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deferred = [r["key"] for r in records if controls_key(r["key"], generation)]
    payloads = [r for r in records if not controls_key(r["key"], generation)]
    # Separate clients per thread: credentials/HTTP sessions are not shared.
    from threading import local

    clients = local()

    def work(record: dict) -> dict:
        if not hasattr(clients, "client"):
            clients.client = Client(project="oa-internal-450019")
        return copy_one(clients.client, record, apply)

    counts = {"copied": 0, "verified": 0, "missing": 0}
    checked = 0
    with (output / "objects.jsonl").open("x") as stream, ThreadPoolExecutor(max_workers=jobs) as pool:
        # At most one batch in flight; a failure cannot leave thousands of
        # queued writes running while the command unwinds.
        for batch in batched(payloads, jobs):
            for result in pool.map(work, batch):
                stream.write(json.dumps(result, separators=(",", ":")) + "\n")
                stream.flush()
                counts[result["action"]] += 1
                checked += 1
            if checked % 100 < jobs:
                print(f"checked {checked}/{len(payloads)} payloads", file=sys.stderr, flush=True)
    summary = {"source": SOURCE, "destination": DESTINATION, "apply": apply,
               "inventory_sha256": sha256((directory / "data.jsonl").read_bytes()).hexdigest(),
               "payloads": len(payloads), "actions": counts, "deferred_controls": deferred,
               "ready_for_cutover": False}
    write_json(output, "summary.json", summary)
    write_json(output, "_SUCCESS.json", {"payloads_checked": len(payloads), "controls_published": False})
    print(json.dumps(summary, indent=2))


@main.command("audit-controls")
@option("-c", "--controls", "control_file", type=ClickPath(path_type=Path, exists=True, dir_okay=False), required=True)
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private directory; local audit only")
def audit_controls(
    control_file: Path,
    directory: Path,
    output: Path,
) -> None:
    """Validate previously downloaded control pins without re-fetching any cloud data."""
    records, generation = load_inventory(directory)
    documents = json.loads(control_file.read_text())
    expected = {r["key"]: r["generation"] for r in records if controls_key(r["key"], generation)}
    actual = {c["key"]: c["generation"] for c in documents}
    if actual != expected or len(documents) != len(expected):
        raise ClickException("control documents do not exactly match inventory")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    problems = audit_scan_pins(documents, records)
    write_json(output, "pin-problems.json", problems)
    summary = {"documents": len(documents), "pin_problems": len(problems), "source_pins_match": not problems}
    write_json(output, "summary.json", summary)
    print(json.dumps(summary))
    if problems:
        raise ClickException("control source pins differ; do not copy or cut over (see pin-problems.json)")


@main.command("publish-controls")
@option("-a", "--apply", is_flag=True, help="Publish to the new bucket; absent = construct/verify local plan only")
@option("-c", "--controls", "control_file", type=ClickPath(path_type=Path, exists=True, dir_okay=False), required=True)
@option("-D", "--writers-drained", is_flag=True, help="Attest all CW writers/schedules remain drained; required with --apply")
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-j", "--journal", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True, help="Successful verified payload-copy journal")
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private output directory")
def publish_controls(
    apply: bool,
    control_file: Path,
    writers_drained: bool,
    directory: Path,
    journal: Path,
    output: Path,
) -> None:
    """Publish remapped scan controls then manifests; never overwrite or switch readers."""
    from google.cloud.storage import Client

    if apply and not writers_drained:
        raise ClickException("--apply requires --writers-drained and a fresh drained inventory")
    records, generation = load_inventory(directory)
    documents = json.loads(control_file.read_text())
    by_key = {r["key"]: r for r in records}
    expected = {r["key"]: r["generation"] for r in records if controls_key(r["key"], generation)}
    if {c["key"]: c["generation"] for c in documents} != expected or len(documents) != len(expected):
        raise ClickException("control documents do not exactly match inventory")
    if audit_scan_pins(documents, records):
        raise ClickException("control source pins differ; publication refused")
    copies = verified_copies(journal, directory, records, generation)
    transformed_documents = {c["key"]: remap_control(c["document"], copies) for c in documents}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    client = Client(project="oa-internal-450019")
    destination = client.bucket(DESTINATION)
    # A saved journal is not authority to reference a replaced destination.
    current_payloads = {blob.name: blob for blob in client.list_blobs(DESTINATION) if blob.name in copies}
    for key, pin in copies.items():
        current = current_payloads.get(key)
        if current is None or str(current.generation) != pin:
            raise ClickException(f"copied payload generation changed or missing: {key}")
        verify_blob(current, by_key[key])
    results = []
    for control in sorted(documents, key=lambda c: ("/manifests/" in c["key"], c["key"])):
        record = by_key[control["key"]]
        if record["bytes"] > CONTROL_LIMIT:
            raise ClickException(f"control JSON exceeds 2 MiB: {record['key']}")
        source = client.bucket(SOURCE).blob(record["key"], generation=int(record["generation"]))
        raw = source.download_as_bytes(if_generation_match=int(record["generation"]))
        if len(raw) != record["bytes"] or sha256(raw).hexdigest() != control["sha256"] or json.loads(raw) != control["document"]:
            raise ClickException(f"control differs from audited revision: {record['key']}")
        transformed = transformed_documents[control["key"]]
        # Untouched manifests retain byte-identical payloads, not reserialized JSON.
        payload = raw if transformed == control["document"] else (json.dumps(transformed, indent=2) + "\n").encode()
        digest = sha256(payload).hexdigest()
        target = destination.get_blob(record["key"])
        action = "planned"
        if target is not None:
            if target.size != len(payload) or target.size > CONTROL_LIMIT:
                raise ClickException(f"conflicting destination control: {record['key']}")
            downloaded = target.download_as_bytes(if_generation_match=int(target.generation))
            if sha256(downloaded).hexdigest() != digest:
                raise ClickException(f"conflicting destination control: {record['key']}")
            action = "verified"
        elif apply:
            target = destination.blob(record["key"])
            target.upload_from_string(payload, content_type="application/json", if_generation_match=0)
            target.reload(if_generation_match=int(target.generation))
            if target.size != len(payload) or sha256(target.download_as_bytes(if_generation_match=int(target.generation))).hexdigest() != digest:
                raise ClickException(f"published control verification failed: {record['key']}")
            action = "published"
        results.append({"key": record["key"], "source_generation": record["generation"], "source_sha256": control["sha256"],
                        "destination_generation": str(target.generation) if target is not None else None,
                        "destination_sha256": digest, "transformed": transformed != control["document"], "action": action})
        write_json(output, f"control-{len(results):04d}.json", transformed)
    write_json(output, "controls.json", results)
    summary = {"controls": len(results), "apply": apply, "ready_for_reader_checks": all(r["action"] in ("published", "verified") for r in results), "ready_for_cutover": False}
    write_json(output, "_SUCCESS.json", summary)
    print(json.dumps(summary))


@main.command("audit-scratch")
@option("-c", "--controls", "control_file", type=ClickPath(path_type=Path, exists=True, dir_okay=False), required=True)
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private read-only state preservation plan")
def audit_scratch(
    control_file: Path,
    directory: Path,
    output: Path,
) -> None:
    """Check published state shape/markers; do not copy shuffle, state or leases."""
    from google.cloud.storage import Client

    records, generation = load_inventory(directory)
    documents = json.loads(control_file.read_text())
    expected = {r["key"]: r["generation"] for r in records if controls_key(r["key"], generation)}
    if {c["key"]: c["generation"] for c in documents} != expected or len(documents) != len(expected) or audit_scan_pins(documents, records):
        raise ClickException("control documents do not exactly match pinned inventory")
    manifests = [c["document"] for c in documents if "/manifests/" in c["key"]]
    latest = max(manifests, key=lambda doc: (doc["date"], len(doc["scans"])))
    scan = latest["date"]
    if latest["gen"] != generation or latest["scans"][-1] != scan:
        raise ClickException("unexpected newest published manifest identity")
    [range_record] = [r for r in records if r["key"] == f"static-names/{generation}/ranges.json"]
    client = Client(project="oa-internal-450019")
    ranges = pinned_json(client, range_record)["k"]
    scratch = [json.loads(line) for line in (directory / "scratch.jsonl").read_text().splitlines()]
    success = json.loads((directory / "_SUCCESS.json").read_text())
    if len(scratch) != success["scratch_objects"]:
        raise ClickException("scratch inventory is incomplete")
    state = scratch_state(scratch, generation, scan, ranges)
    for record in state:
        if "/done/" in record["key"]:
            doc = pinned_json(client, record)
            name = record["key"].rsplit("/", 1)[1].removesuffix(".json")
            if (doc["range"], doc["scan"]) != (name, scan):
                raise ClickException("scratch completion marker identity mismatch")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(output, "state.json", state)
    summary = {"source": SCRATCH_SOURCE, "destination": SCRATCH_DESTINATION, "generation": generation,
               "scan": scan, "ranges": ranges, "state_objects": len(state), "state_bytes": sum(r["bytes"] for r in state),
               "excluded_objects": len(scratch) - len(state), "ready_for_cutover": False,
               "inventory_sha256": sha256((directory / "scratch.jsonl").read_bytes()).hexdigest()}
    write_json(output, "summary.json", summary)
    write_json(output, "_SUCCESS.json", {"state_markers_verified": ranges, "state_copied": False})
    print(json.dumps(summary, indent=2))


@main.command("copy-scratch")
@option("-a", "--apply", is_flag=True, help="Preserve only the audited newest published state")
@option("-c", "--controls", "control_file", type=ClickPath(path_type=Path, exists=True, dir_okay=False), required=True)
@option("-D", "--writers-drained", is_flag=True, help="Attest all CW writers remain drained; required with --apply")
@option("-i", "--inventory", "directory", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
@option("-o", "--output", type=ClickPath(path_type=Path), required=True)
@option("-p", "--plan", type=ClickPath(path_type=Path, exists=True, file_okay=False), required=True)
def copy_scratch(
    apply: bool,
    control_file: Path,
    writers_drained: bool,
    directory: Path,
    output: Path,
    plan: Path,
) -> None:
    """Revalidate the preservation plan, copy pinned state and verify checksums."""
    from google.cloud.storage import Client

    if apply and not writers_drained:
        raise ClickException("--apply requires --writers-drained and a fresh drained inventory")
    records, generation = load_inventory(directory)
    documents = json.loads(control_file.read_text())
    expected = {r["key"]: r["generation"] for r in records if controls_key(r["key"], generation)}
    if {c["key"]: c["generation"] for c in documents} != expected or len(documents) != len(expected) or audit_scan_pins(documents, records):
        raise ClickException("control documents do not exactly match pinned inventory")
    latest = max((c["document"] for c in documents if "/manifests/" in c["key"]), key=lambda doc: (doc["date"], len(doc["scans"])))
    if latest["gen"] != generation or latest["scans"][-1] != latest["date"]:
        raise ClickException("unexpected newest published manifest identity")
    client = Client(project="oa-internal-450019")
    [range_record] = [r for r in records if r["key"] == f"static-names/{generation}/ranges.json"]
    ranges = pinned_json(client, range_record)["k"]
    scratch = [json.loads(line) for line in (directory / "scratch.jsonl").read_text().splitlines()]
    if len(scratch) != json.loads((directory / "_SUCCESS.json").read_text())["scratch_objects"]:
        raise ClickException("scratch inventory is incomplete")
    state = scratch_state(scratch, generation, latest["date"], ranges)
    audit = json.loads((plan / "summary.json").read_text())
    if json.loads((plan / "state.json").read_text()) != state or audit["inventory_sha256"] != sha256((directory / "scratch.jsonl").read_bytes()).hexdigest() or json.loads((plan / "_SUCCESS.json").read_text()) != {"state_markers_verified": ranges, "state_copied": False}:
        raise ClickException("scratch plan differs from the pinned latest state")
    # Marker identities are checked again before any cloud write.
    for record in state:
        if "/done/" in record["key"]:
            marker = pinned_json(client, record)
            name = record["key"].rsplit("/", 1)[1].removesuffix(".json")
            if (marker["range"], marker["scan"]) != (name, latest["date"]):
                raise ClickException("scratch completion marker identity mismatch")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    counts = {"copied": 0, "verified": 0, "missing": 0}
    with (output / "objects.jsonl").open("x") as stream:
        for record in state:
            result = copy_one(client, record, apply, SCRATCH_DESTINATION)
            stream.write(json.dumps(result, separators=(",", ":")) + "\n")
            stream.flush()
            counts[result["action"]] += 1
    summary = {"source": SCRATCH_SOURCE, "destination": SCRATCH_DESTINATION,
               "scan": latest["date"], "state_objects": len(state), "actions": counts,
               "inventory_sha256": audit["inventory_sha256"], "ready_for_cutover": False}
    write_json(output, "summary.json", summary)
    write_json(output, "_SUCCESS.json", {"state_checked": len(state), "state_copied": counts["missing"] == 0})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
