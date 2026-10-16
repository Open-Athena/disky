"""OA R2's independent remote data plane and optional reviewed daily schedule."""

from base64 import b64encode
from json import dumps
from json import loads
from pathlib import Path
from subprocess import run

import pulumi
import pulumi_gcp as gcp

from gcp_jobs import Adopt, JobAccount, Secrets, labeled_provider


profile = loads((Path(__file__).resolve().parents[2] / "job/r2.json").read_text())
config = pulumi.Config("gcp")
project = config.require("project")
region = config.require("region")
if pulumi.get_stack() != "r2" or project != profile["project"] or region != profile["region"]:
    raise ValueError("Stack, project and region must match job/r2.json")
labels = {"app": "disky", "deployment": "oa-r2"}
labeled_provider("r2-labeled", project=project, region=region, labels=labels)
builder = JobAccount(
    "r2-builder",
    project=project,
    account_id=profile["builder_account"],
    display_name="OA R2 image builder",
    description="Builds only OA R2 images from its private staging bucket",
    roles=["roles/logging.logWriter"],
    acts_as_self=False,
    adopt=Adopt(False),
)
job = JobAccount(
    "r2-job",
    project=project,
    account_id=profile["job_account"],
    display_name="OA R2 metadata scan",
    description="Lists the four approved OA R2 buckets and publishes only OA R2's derived index",
    roles=["roles/batch.agentReporter", "roles/logging.logWriter"],
    acts_as_self=False,
    adopt=Adopt(False),
)
resources = []
for component, name, days in (
    ("data", profile["data_bucket"], None),
    ("scratch", profile["scratch_bucket"], 7),
):
    bucket = gcp.storage.Bucket(
        f"r2-{component}",
        project=project,
        name=name,
        location=region.upper(),
        storage_class="STANDARD",
        uniform_bucket_level_access=True,
        public_access_prevention="enforced",
        force_destroy=False,
        labels={**labels, "component": component},
        soft_delete_policy={"retention_duration_seconds": 604800 if days is None else 0},
        lifecycle_rules=[] if days is None else [{"action": {"type": "Delete"}, "condition": {"age": days}}],
        opts=pulumi.ResourceOptions(protect=True),
    )
    grant = gcp.storage.BucketIAMMember(
        f"r2-{component}-job",
        bucket=bucket.name,
        role="roles/storage.objectAdmin",
        member=job.member,
    )
    resources.extend([bucket, grant])
    build_grant = gcp.storage.BucketIAMMember(
        f"r2-{component}-builder",
        bucket=bucket.name,
        role="roles/storage.objectCreator" if component == "data" else "roles/storage.objectViewer",
        member=builder.member,
    )
    resources.append(build_grant)
repository = gcp.artifactregistry.Repository(
    "r2-images",
    project=project,
    location=region,
    repository_id=profile["image_repository"],
    format="DOCKER",
    description="Independent OA R2 scan images",
    labels={**labels, "component": "images"},
    opts=pulumi.ResourceOptions(protect=True),
)
image_reader = gcp.artifactregistry.RepositoryIamMember(
    "r2-images-job",
    project=project,
    location=repository.location,
    repository=repository.repository_id,
    role="roles/artifactregistry.reader",
    member=job.member,
)
image_writer = gcp.artifactregistry.RepositoryIamMember(
    "r2-images-builder",
    project=project,
    location=repository.location,
    repository=repository.repository_id,
    role="roles/artifactregistry.writer",
    member=builder.member,
)
secrets = Secrets(
    "r2-secrets",
    project=project,
    # the optional ones (the Slack bot, the site grant, the Pages token) too: a container without a version costs
    # nothing, and the job is handed one only once `optional_secrets_live` lists it (`job/r2-scan` `job_spec`)
    secrets={name: {**labels, "component": "secret"} for name in [*profile["secrets"].values(), *profile["optional_secrets"].values()]},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=Adopt(False),
)
pulumi.export("job_account", job.email)
pulumi.export("builder_account", builder.email)
pulumi.export("data_bucket", profile["data_bucket"])
pulumi.export("scratch_bucket", profile["scratch_bucket"])
pulumi.export("image_repository", repository.repository_id)
daily_config = pulumi.Config("r2")
daily_enabled = daily_config.get_bool("dailyEnabled") or False
if daily_enabled:
    planned = run([str(Path(__file__).resolve().parents[2] / "job/r2-scan"), "plan", daily_config.require("scanImage")], capture_output=True, text=True, check=True)
    spec = loads(planned.stdout)
    scheduler = gcp.serviceaccount.Account(
        "r2-scheduler", project=project, account_id="oa-r2-usage-scheduler",
        display_name="OA R2 daily scan submitter",
        description="Submits OA R2 Batch scans as only the OA R2 job account",
    )
    submit_role = gcp.projects.IAMCustomRole(
        "r2-batch-submit", project=project, role_id="oaR2BatchSubmit",
        title="OA R2 Batch submission", permissions=["batch.jobs.create"],
        description="Create Batch jobs; no access to existing jobs or other deployment accounts",
    )
    submit_grant = gcp.projects.IAMMember(
        "r2-scheduler-submit", project=project, role=submit_role.name,
        member=scheduler.email.apply(lambda email: f"serviceAccount:{email}"),
    )
    act_as = gcp.serviceaccount.IAMMember(
        "r2-scheduler-job", service_account_id=f"projects/{project}/serviceAccounts/{job.email_literal}",
        role="roles/iam.serviceAccountUser",
        member=scheduler.email.apply(lambda email: f"serviceAccount:{email}"),
    )
    daily = gcp.cloudscheduler.Job(
        "r2-daily", project=project, region=region, name="oa-r2-usage-daily",
        description="Metadata-only daily scan of the four approved OA R2 buckets",
        schedule="15 7 * * *", time_zone="Etc/UTC", attempt_deadline="180s",
        retry_config={"retry_count": 0, "max_retry_duration": "0s"},
        http_target={
            "uri": f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs",
            "http_method": "POST", "headers": {"Content-Type": "application/json"},
            "body": b64encode(dumps(spec).encode()).decode(),
            "oauth_token": {"service_account_email": scheduler.email, "scope": "https://www.googleapis.com/auth/cloud-platform"},
        },
        opts=pulumi.ResourceOptions(depends_on=[submit_grant, act_as]),
    )
    pulumi.export("daily_schedule", daily.schedule)
pulumi.export("scheduler_enabled", daily_enabled)
