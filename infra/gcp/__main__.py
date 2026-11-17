"""OA R2's independent remote data plane and optional reviewed daily schedule."""

from base64 import b64encode
from json import dumps
from json import loads
from pathlib import Path
from subprocess import run

import pulumi
import pulumi_gcp as gcp

from gcp_jobs import Adopt, BatchSubmitCron, JobAccount, Secrets, labeled_provider


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
# "Run a scan" on /scans (`/api/scan-runs/dispatch`) runs the primary head's submitter job as this account (its key is
# the Pages secret `GCP_SA_KEY`, made by hand: keys stay out of state); it holds nothing else
dispatch = JobAccount(
    "r2-dispatch",
    project=project,
    account_id="oa-r2-usage-dispatch",
    display_name="OA R2 scan dispatch",
    description="Runs only the OA R2 primary scan's submitter job (oa-r2-usage-submit) from the site's /scans",
    roles=[],
    acts_as_self=False,
    adopt=Adopt(False),
)
job = JobAccount(
    "r2-job",
    project=project,
    account_id=profile["job_account"],
    display_name="OA R2 metadata scan",
    description="Lists the four approved OA R2 buckets and publishes only OA R2's derived index",
    # batch.jobsEditor + acting as itself: a Nextflow head (`dt-cloud scan head`, specs/pipelines-everywhere.md) submits,
    # polls and cancels its steps' Batch jobs, each running as this account
    # `gcp-cost` (`dt-cloud cost`): the buckets' `total_bytes` from Cloud Monitoring
    roles=["roles/batch.agentReporter", "roles/batch.jobsEditor", "roles/logging.logWriter", "roles/monitoring.viewer"],
    acts_as_self=True,
    # the site's "Run a scan" account: runs the primary head's submitter (below), which submits as this account
    actors=[f"oa-r2-usage-dispatch@{project}.iam.gserviceaccount.com"],
    actor_deps=[dispatch.account],
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
    # `gcp-cost` (`dt-cloud cost`) reads the bucket's lifecycle and soft-delete settings (`storage.buckets.get`)
    viewer = gcp.storage.BucketIAMMember(
        f"r2-{component}-job-viewer",
        bucket=bucket.name,
        role="roles/storage.bucketViewer",
        member=job.member,
    )
    resources.extend([bucket, grant, viewer])
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
# The disky.oa.dev fleet dashboard's dedicated `oa-r2:read` grant (`dt-cloud job-token`, specs/fleet-read-grants.md):
# root installs it as `FLEET_TOKEN_R2`; the job never reads it, so no accessor
fleet_secrets = Secrets(
    "r2-fleet-secrets",
    project=project,
    secrets={"oa-r2-fleet-token": {**labels, "component": "secret"}},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=Adopt(False),
    grant_only=set(),
)
pulumi.export("job_account", job.email)
pulumi.export("builder_account", builder.email)
pulumi.export("data_bucket", profile["data_bucket"])
pulumi.export("scratch_bucket", profile["scratch_bucket"])
pulumi.export("image_repository", repository.repository_id)
daily_config = pulumi.Config("r2")
daily_enabled = daily_config.get_bool("dailyEnabled") or False
if daily_enabled:
    # `r2:heads` ([{profile, schedule}], `job/profiles/` names): one scheduler job per Nextflow head (each its own Batch
    # job, so its own run record), the first keeping `r2-daily`; unset, `r2-daily` submits the one-VM scan.
    heads = daily_config.get_object("heads") or [{"profile": None, "schedule": "15 7 * * *"}]

    def plan(profile: str | None) -> dict:
        head = ["-H", f"job/profiles/{profile}"] if profile else []
        planned = run([str(Path(__file__).resolve().parents[2] / "job/r2-scan"), "plan", *head, daily_config.require("scanImage")],
                      capture_output=True, text=True, check=True)
        return loads(planned.stdout)
    # Each head's cron runs a submitter job (`BatchSubmitCron`, `job/scan-submit.sh`): the day's Batch job named for the
    # day, transient failures retried, an alert if it still can't submit. The first head's is also "Run a scan"'s.
    for i, head in enumerate(heads):
        stem = (head["profile"] or "").removesuffix(".yml")
        suffix = "" if i == 0 else f"-{stem.removeprefix('r2-')}"
        submit_job = f"oa-r2-usage-submit{suffix}"
        daily = BatchSubmitCron(
            f"r2-daily{suffix}",
            project=project,
            region=region,
            job=submit_job,
            trigger=f"oa-r2-usage-daily{suffix}",
            schedule=head["schedule"],
            submitter=plan(head["profile"]),
            script=Path(__file__).resolve().parents[2] / "job/scan-submit.sh",
            sa_email=job.email_literal,
            image=daily_config.require("scanImage"),
            env={
                "PROJECT": project,
                "REGION": region,
                "SUBMIT_JOB": submit_job,
                "JOB_PREFIX": f"oa-r2-scan-daily{suffix}",
                "SCAN_NAME": f"r2 {'daily' if i == 0 else stem.removeprefix('r2-')} scan",
                "SLACK_ALERT_CHANNEL": profile["slack_alert_channel"],
                "DISKY_LABELS": ",".join(f"{k}={v}" for k, v in labels.items()),
            },
            secret_env={"SLACK_BOT_TOKEN": profile["optional_secrets"]["SLACK_BOT_TOKEN"]},
            overridable=["DT_TRIGGER", "DT_TRIGGER_BY"],
            dispatchers={"site": dispatch.member} if i == 0 else None,
            labels={**labels, "component": "scan-submit"},
            description=f"Daily Nextflow head over job/profiles/{head['profile']}: runs the submitter job, which names the day's Batch job, retries, and alerts",
            cron_aliases=[f"urn:pulumi:r2::oa-r2-usage-gcp::gcp:cloudscheduler/job:Job::r2-daily{suffix}"],
            adopt=Adopt(False),
            opts=pulumi.ResourceOptions(depends_on=[secrets]),
        )
        pulumi.export(f"daily_schedule{suffix}", daily.cron.schedule)
        pulumi.export(f"submit_job{suffix}", daily.job.id)
pulumi.export("scheduler_enabled", daily_enabled)
