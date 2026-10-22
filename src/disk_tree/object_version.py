"""Opaque provider object identities, independent of index-format versions."""
from collections.abc import Collection
from pathlib import Path

VERSION = "version"


def version_sql(columns: Collection[str]) -> str:
    """The shared token, or an exact decimal legacy GCS generation; missing stays NULL."""
    if VERSION in columns:
        return "coalesce(version::VARCHAR, generation::VARCHAR)" if "generation" in columns else "version::VARCHAR"
    if "generation" in columns:
        return "generation::VARCHAR"
    return "NULL::VARCHAR"


def source_versions(con, source: str, bucket: str) -> str | None:
    """One unambiguous identity per exact source name; unsupported listings have none."""
    columns = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    expr = version_sql(columns)
    con.execute("SET VARIABLE object_version_bucket = ?", [bucket])
    sql = f"SELECT name, {expr} AS version FROM {source} WHERE bucket = getvariable('object_version_bucket')"
    if con.execute(f"SELECT count(*) FROM ({sql}) WHERE version IS NOT NULL").fetchone()[0] == 0:
        return None
    if con.execute(f"SELECT count(*) FROM (SELECT name FROM ({sql}) GROUP BY name HAVING count(DISTINCT version) > 1)").fetchone()[0]:
        raise ValueError(f"{bucket}: listing contains conflicting object versions for the same name")
    return f"SELECT name, any_value(version) AS version FROM ({sql}) GROUP BY name"


def attach_parquet(path: str, source: str, bucket: str, con) -> None:
    """Preserve the listing's identities on file rows, with bounded batches and the original format metadata."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from disk_tree.listing_format import pyarrow_codec
    from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE

    versions = source_versions(con, source, bucket)
    if versions is None:
        return
    schema = pq.read_schema(path)
    if VERSION in schema.names:
        raise ValueError(f"{path}: object version is already attached")
    con.execute("SET VARIABLE object_version_l2 = ?", [path])
    cursor = con.execute(f"""SELECT l.* EXCLUDE (position), CASE WHEN l.kind = 'file' THEN v.version END AS version
        FROM (SELECT *, row_number() OVER () AS position FROM read_parquet(getvariable('object_version_l2'))) l
        LEFT JOIN ({versions}) v ON l.path = v.name ORDER BY position""")
    schema = schema.append(pa.field(VERSION, pa.string()))
    tmp = Path(path + ".object-version.tmp")
    try:
        with pq.ParquetWriter(tmp, schema, **pyarrow_codec()) as writer:
            for batch in cursor.to_arrow_reader(BLOB_ROW_GROUP_SIZE):
                writer.write_batch(batch.cast(schema), row_group_size=BLOB_ROW_GROUP_SIZE)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
