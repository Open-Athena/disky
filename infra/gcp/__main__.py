"""hf's GCP stack: the Hugging Face scan job's account, image repository, secrets and daily cron.

Thin wiring over `gcp_jobs.py` (the shared components, from `cloud`). The cron's Batch spec comes from this
branch's `job/hf-batch-submit.sh` under `PIN=1 DRY=1`. Everything is new (no adoption). The job writes R2 only, so
there is no GCS bucket here: its scratch is the VM's boot disk.

Secret values are never managed: the stack creates the containers and the job's `secretAccessor` on each; the values
go in with `gcloud secrets versions add` (specs/hf-deployment.md).

CI and agents run `pulumi preview` only; `up` is a human's call.
"""

from pathlib import Path

import pulumi
import pulumi_gcp as gcp

from gcp_jobs import Adopt, BatchCron, JobAccount, Secrets, cost_labels, labeled_provider

cfg = pulumi.Config()
gcp_cfg = pulumi.Config("gcp")
project = gcp_cfg.require("project")
region = gcp_cfg.require("region")
adopt = Adopt(cfg.get_bool("adopting") or False)
job_dir = Path(__file__).resolve().parents[2] / "job"

STACK = "hf"
if pulumi.get_stack() != STACK:
    raise ValueError(f"this branch's gcp/ manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")

# Cost attribution (specs/done/cost-labels.md): `$DISKY_LABELS` (app=disky,deployment=hf, from the shell's `.envrc`)
# becomes every labelable resource's default labels.
labeled_provider("hf-labeled", project=project, region=region, labels=cost_labels())

# Least privilege, as the `r2` branch: the cron calls Batch as a submit-only account (one custom role holding
# `batch.jobs.create`, and `serviceAccountUser` on the job account, which the job then runs as), so the job account
# needs no rights over other Batch jobs.
scheduler = JobAccount(
    "hf-usage-scheduler",
    project=project,
    account_id="hf-usage-scheduler",
    display_name="hf daily scan submitter",
    description="Cloud Scheduler's caller: creates the hf.oa.dev scan's Batch job, as hf-usage-job, and nothing else",
    roles=[],
    acts_as_self=False,
    adopt=adopt,
)
submit_role = gcp.projects.IAMCustomRole(
    "hf-usage-batch-submit",
    project=project,
    role_id="hfUsageBatchSubmit",
    title="hf Batch submission",
    permissions=["batch.jobs.create"],
    description="Create Batch jobs; no access to existing jobs",
)
submit_grant = gcp.projects.IAMMember(
    "hf-usage-scheduler-submit",
    project=project,
    role=submit_role.name,
    member=scheduler.member,
)

job = JobAccount(
    "hf-usage-job",
    project=project,
    account_id="hf-usage-job",
    display_name="hf Hugging Face scan job",
    description="Runs the hf.oa.dev daily scan on Batch (job/hf-run.sh); hf-usage-scheduler submits it as this account",
    roles=[
        "roles/batch.agentReporter",
        "roles/batch.jobsViewer",   # `scan-run start -B` reads its own job back
        "roles/logging.logWriter",
    ],
    actors=[scheduler.email_literal],
    actor_deps=[scheduler],
    acts_as_self=False,
    adopt=adopt,
)

images = gcp.artifactregistry.Repository(
    "hf-usage-images",
    project=project,
    location=region,
    repository_id="hf-usage",
    format="DOCKER",
    description="hf deployment images (job/build.sh)",
    labels={"component": "images"},
    opts=pulumi.ResourceOptions(protect=True),
)
images_reader = gcp.artifactregistry.RepositoryIamMember(
    "hf-usage-images-job",
    project=project,
    location=images.location,
    repository=images.repository_id,
    role="roles/artifactregistry.reader",
    member=job.member,
)

# The job's secrets (job/hf-batch-submit.sh `secretVariables`). The CF account id and the R2 endpoint (which holds
# it) are secrets so that no account id is in git.
HF_SECRETS = [
    "hf-usage-hf-token",              # HF_TOKEN: a Hub read token that sees the org's private repos and buckets
    "hf-usage-cf-token",              # CLOUDFLARE_API_TOKEN: D1 edit (index-sync, scan runs); the `job_d1_token` CF output
    "hf-usage-cf-pages-token",        # CF_PAGES_TOKEN: Pages edit, the digest's plot upload; the `job_pages_token` CF output
    "hf-usage-cf-account-id",         # CLOUDFLARE_ACCOUNT_ID
    "hf-usage-r2-endpoint",           # R2_ENDPOINT: https://<account id>.r2.cloudflarestorage.com
    "hf-usage-r2-access-key-id",      # R2_ACCESS_KEY_ID: the `r2_s3_access_key_id` CF output (read/write, the bucket only)
    "hf-usage-r2-secret-access-key",  # R2_SECRET_ACCESS_KEY: the `r2_s3_secret_access_key` CF output
    "hf-usage-slack-bot-token",       # SLACK_BOT_TOKEN: the HF Usage Bot (job/slack/): the digest + failure alerts
    "hf-usage-job-grant",             # SITE_TOKEN: a read-only grant on hf.oa.dev (minted from /admin), for warm-up + probe
]
secrets = Secrets(
    "hf-secrets",
    project=project,
    secrets={sid: {"component": "secret"} for sid in HF_SECRETS},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
)

scan = BatchCron(
    "hf-usage-snapshot",
    project=project,
    region=region,
    schedule="0 8 * * *",
    submitter=job_dir / "hf-batch-submit.sh",
    sa_email=scheduler.email,
    description="Daily Hugging Face org scan (job/hf-batch-submit.sh PIN=1 DRY=1 body) -> hf.oa.dev",
    adopt=adopt,
    depends_on=[job, secrets, images_reader, submit_grant],
)

pulumi.export("job_account", job.email)
pulumi.export("scheduler_account", scheduler.email)
pulumi.export("image_repository", images.repository_id)
pulumi.export("secrets", HF_SECRETS)
pulumi.export("cron", scan.job.name)
