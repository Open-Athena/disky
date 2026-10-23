"""CW-owned, additive storage/image/credential foundation; no cutover wiring."""

import pulumi
import pulumi_gcp as gcp


def prepare_isolation(
    project: str,
    member: pulumi.Input[str],
) -> list[pulumi.CustomResource]:
    labels = {"app": "disky", "deployment": "cw-s3"}
    buckets = []
    resources: list[pulumi.CustomResource] = []
    for component, name, days in (
        ("data", "oa-cw-s3-usage-data", None),
        ("scratch", "oa-cw-s3-usage-scratch", 7),
    ):
        bucket = gcp.storage.Bucket(
            f"cw-isolation-{component}",
            project=project,
            name=name,
            location="US-EAST1",
            storage_class="STANDARD",
            uniform_bucket_level_access=True,
            public_access_prevention="enforced",
            force_destroy=False,
            labels={**labels, "component": component},
            soft_delete_policy={"retention_duration_seconds": 0},
            lifecycle_rules=[] if days is None else [{"action": {"type": "Delete"}, "condition": {"age": days}}],
            opts=pulumi.ResourceOptions(protect=True),
        )
        grant = gcp.storage.BucketIAMMember(
            f"cw-isolation-{component}-job",
            bucket=bucket.name,
            role="roles/storage.objectAdmin",
            member=member,
        )
        buckets.append(bucket)
        resources.extend([bucket, grant])
    repository = gcp.artifactregistry.Repository(
        "cw-isolation-images",
        project=project,
        location="us-central1",
        repository_id="cw-s3-usage",
        format="DOCKER",
        description="CW deployment images; no GCS image tags",
        labels={**labels, "component": "images"},
        opts=pulumi.ResourceOptions(protect=True),
    )
    reader = gcp.artifactregistry.RepositoryIamMember(
        "cw-isolation-images-job",
        project=project,
        location=repository.location,
        repository=repository.repository_id,
        role="roles/artifactregistry.reader",
        member=member,
    )
    credential = gcp.secretmanager.Secret(
        "cw-isolation-cf-token",
        project=project,
        secret_id="cw-s3-cf-pages-token",
        replication={"auto": {}},
        labels={**labels, "component": "secret"},
        opts=pulumi.ResourceOptions(protect=True),
    )
    accessor = gcp.secretmanager.SecretIamMember(
        "cw-isolation-cf-token-job",
        project=project,
        secret_id=credential.secret_id,
        role="roles/secretmanager.secretAccessor",
        member=member,
    )
    resources.extend([repository, reader, credential, accessor])
    pulumi.export("isolation_data_bucket", buckets[0].name)
    pulumi.export("isolation_scratch_bucket", buckets[1].name)
    pulumi.export("isolation_image_repository", repository.repository_id)
    pulumi.export("isolation_cf_secret", credential.secret_id)
    return resources


def prepare_credentials(
    project: str,
    member: pulumi.Input[str],
) -> list[pulumi.CustomResource]:
    """New secret identities keep existing jobs' latest-version reads unchanged."""
    resources: list[pulumi.CustomResource] = []
    for suffix in ("access-key-id", "secret-access-key"):
        secret = gcp.secretmanager.Secret(
            f"cw-isolation-r2-{suffix}",
            project=project,
            secret_id=f"cw-s3-isolated-r2-{suffix}",
            replication={"auto": {}},
            labels={"app": "disky", "deployment": "cw-s3", "component": "secret"},
            opts=pulumi.ResourceOptions(protect=True),
        )
        accessor = gcp.secretmanager.SecretIamMember(
            f"cw-isolation-r2-{suffix}-job",
            project=project,
            secret_id=secret.secret_id,
            role="roles/secretmanager.secretAccessor",
            member=member,
        )
        resources.extend([secret, accessor])
    return resources


def prepare_dispatcher(
    member: pulumi.Input[str],
) -> list[pulumi.CustomResource]:
    """The web dispatcher creates plans and reads run results, never deletes."""
    return [gcp.storage.BucketIAMMember(
        f"cw-isolation-data-dispatch-{purpose}",
        bucket="oa-cw-s3-usage-data",
        role=f"roles/storage.object{role}",
        member=member,
    ) for purpose, role in (("reader", "Viewer"), ("creator", "Creator"))]
