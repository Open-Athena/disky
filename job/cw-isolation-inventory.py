#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click", "google-cloud-storage", "requests"]
# ///
"""Read-only CW migration inventory: metadata/checksums, D1 references, no bodies."""

import json
import os
import re
import sys
import tomllib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from click import ClickException, command, option

ROOT = Path(__file__).resolve().parents[1]


def prefixes(gen: str) -> list[str]:
    if not gen or "/" in gen or gen in (".", ".."):
        raise ValueError("static generation must be one nonempty path segment")
    return ["cw-l2/", "snapshots/cw/", "digest/cw/", "probes/cw/", "cw-sweep/", "sweep/cw/",
            "meta-l2/", "snapshots/meta/", f"static-names/{gen}/"]


def object_record(blob: object, bucket: str) -> dict:
    if blob.generation is None or blob.size is None or not (blob.md5_hash or blob.crc32c):
        raise ValueError(f"object metadata lacks generation/size/checksum: {blob.name}")
    return {"bucket": bucket, "key": blob.name, "generation": str(blob.generation), "bytes": int(blob.size), "storage_class": blob.storage_class,
            "md5": blob.md5_hash, "crc32c": blob.crc32c}


def index_key(row: dict) -> str:
    variant = row["variant"].removeprefix("meta:")
    if variant in ("age", "over-time"):
        filename = "age-index" if variant == "age" else "over-time"
    elif re.fullmatch(r"age-pyramid-\d+(?:min|h|d|mo|y)", variant):
        filename = variant
    elif variant == "path":
        filename = "path-index"
    elif variant == "user":
        filename = "path-index-by-user"
    elif re.fullmatch(r"(?:coarse\d+|bysize)(?:-user)?", variant):
        filename = "path-index-" + variant.replace("-user", "-by-user")
    else:
        raise ValueError(f"unsupported index variant: {row['variant']}")
    return row["dir"].rstrip("/") + "/" + filename + ".parquet"


def old_references(value: object, bucket: str, path: str = "") -> list[dict]:
    if isinstance(value, dict):
        return [ref for key, child in value.items() for ref in old_references(child, bucket, path + "/" + key)]
    if isinstance(value, list):
        return [ref for i, child in enumerate(value) for ref in old_references(child, bucket, path + "/" + str(i))]
    if isinstance(value, str) and bucket in value:
        return [{"field": path, "value": value}]
    return []


def controls(
    client: object,
    records: list[dict],
    prefix: str,
    bucket: str,
) -> list[dict]:
    out = []
    for record in records:
        key = record["key"]
        if not key.startswith(prefix) or not (key.endswith("/scans.json") or key.startswith(prefix + "manifests/") and key.endswith(".json")):
            continue
        if record["bytes"] > 2097152:
            raise ValueError(f"control JSON exceeds 2 MiB cap: {key}")
        blob = client.bucket(bucket).blob(key, generation=int(record["generation"]))
        doc = json.loads(blob.download_as_bytes(if_generation_match=int(record["generation"])))
        out.append({"key": key, "generation": record["generation"], "old_bucket_references": old_references(doc, bucket)})
    return out


def summarize(records: Iterable[dict], roots: list[str]) -> dict:
    totals = {root: {"objects": 0, "bytes": 0} for root in roots}
    for record in records:
        matches = [root for root in roots if record["key"].startswith(root)]
        if len(matches) != 1:
            raise ValueError(f"object must match exactly one allowed prefix: {record['key']}")
        total = totals[matches[0]]
        total["objects"] += 1
        total["bytes"] += record["bytes"]
    return totals


def d1_references(config: dict) -> dict:
    from requests import Session

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("OA_CF_ACCT", "")).strip()
    if not token or not account:
        raise ClickException("Need CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID/OA_CF_ACCT; values are never printed")
    database = config["d1_databases"][0]["database_id"]
    sqls = {
        "index": "SELECT store,date,variant,gen,dir FROM index_schema ORDER BY store,date,variant",
        "over_time": "SELECT dataset,key,scans FROM pyramid_multiscans ORDER BY dataset,key",
        "deletions": "SELECT manifest,log_dir FROM deletion_runs ORDER BY manifest,log_dir",
    }
    out = {"database": database}
    with Session() as session:
        session.headers.update({"Authorization": f"Bearer {token}"})
        for name, sql in sqls.items():
            response = session.post(f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query",
                                    json={"sql": sql}, timeout=60)
            response.raise_for_status()
            body = response.json()
            if body.get("success") is not True or len(body["result"]) != 1 or body["result"][0].get("success") is not True:
                raise ClickException(f"D1 metadata SELECT failed: {name}")
            out[name] = body["result"][0]["results"]
    return out


def inventory(
    client: object,
    bucket: str,
    roots: list[str],
) -> list[dict]:
    records = []
    for root in roots:
        print(f"listing metadata: gs://{bucket}/{root}", file=sys.stderr, flush=True)
        records.extend(object_record(blob, bucket) for blob in client.list_blobs(bucket, prefix=root))
    records.sort(key=lambda record: record["key"])
    summarize(records, roots)
    if len({record["key"] for record in records}) != len(records):
        raise ValueError("duplicate object key in inventory")
    return records


@command()
@option("-c", "--site-config", type=Path, default=ROOT / "site/wrangler.toml", help="CW site configuration, selects its D1")
@option("-o", "--output", type=Path, required=True, help="New private output directory; refuses existing paths")
@option("-p", "--profile", type=Path, default=ROOT / "job/static-names.json", help="CW static-index profile")
@option("-s", "--source", is_flag=True, help="Inventory the retained shared source buckets regardless of prepared profile wiring")
def main(
    site_config: Path,
    output: Path,
    profile: Path,
    source: bool,
) -> None:
    from google.cloud.storage import Client

    config = tomllib.loads(site_config.read_text())
    definition = json.loads(profile.read_text())
    if config["vars"]["STORE"] != "cw" or definition["name"] != "cw":
        raise ClickException("This inventory is for CW only")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    references = d1_references(config)
    roots = prefixes(definition["gen"])
    client = Client(project=definition["project"])
    data_bucket = "oa-gcs-usage-dvx" if source else definition["bucket"]
    scratch_bucket = "oa-gcs-usage-scratch" if source else definition["scratch"]
    data = inventory(client, data_bucket, roots)
    scratch_roots = [f"static-names/{definition['gen']}/"]
    scratch = inventory(client, scratch_bucket, scratch_roots)
    keys = {record["key"] for record in data}
    missing = sorted({index_key(row) for row in references["index"]} - keys)
    control_refs = controls(client, data, f"static-names/{definition['gen']}/", data_bucket)
    classes = {}
    for record in data:
        total = classes.setdefault(record["storage_class"], {"objects": 0, "bytes": 0})
        total["objects"] += 1
        total["bytes"] += record["bytes"]
    summary = {"captured_at": datetime.now(timezone.utc).isoformat(), "static_gen": definition["gen"],
               "data": summarize(data, roots), "scratch": summarize(scratch, scratch_roots),
               "missing_index_files": missing, "storage_classes": classes,
               "scope": "candidate CW history; metadata inventory is not copy authorization; meta ownership still requires review"}
    for filename, value in (("references.json", references), ("summary.json", summary), ("control-references.json", control_refs)):
        with (output / filename).open("x") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
    for filename, records in (("data.jsonl", data), ("scratch.jsonl", scratch)):
        with (output / filename).open("x") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    if missing:
        raise ClickException("D1 references missing index files; inventory is incomplete (see summary.json)")
    with (output / "_SUCCESS.json").open("x") as stream:
        stream.write(json.dumps({"data_objects": len(data), "scratch_objects": len(scratch)}) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
