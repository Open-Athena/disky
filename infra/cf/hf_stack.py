"""hf's Cloudflare entrypoint: the shared `stack/` program, plus the two tokens only this deployment mints.

- **Ids from the environment.** The account and zone ids stay out of `Pulumi.hf.yaml` (the repo is public): unless
  the stack config sets them, they come from `$CLOUDFLARE_ACCOUNT_ID` and `$CF_ZONE_ID` (the worktree's `.envrc`).
- **The shared program** (`stack/__main__.py`) makes the Pages project, `hf.oa.dev` + its CNAME, the D1, the cache KV,
  the R2 index bucket and its read/write token (`r2_s3_*` outputs: the job's publisher keys).
- **The site's reader** (`site_r2_*`): R2 item read on the index bucket only, for the site's `STORE_*` seam, so the
  public-facing deployment holds no write key.
- **The job's D1 token** (`job_d1_token`): D1 read + write on the account, for `index-sync` and the scan-run records.
  D1 tokens can't be scoped to one database.
- **The digest's icons** (`oa-hf-usage-icons`): a Pages project serving the Slack digest's reply avatars (its
  production deployment of `job/icons-hf/`) and the OP's plot (the `plot` branch, deployed by `dt-cloud digest`), and
  the job's Pages token (`job_pages_token`, Pages edit on the account: Pages tokens can't be scoped to one project).

`pulumi preview` is read-only; `up` is a human's call.
"""

import hashlib
import json
import os
import runpy
import sys
from pathlib import Path

import pulumi
import pulumi_cloudflare as cloudflare

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from cfn_dashboard import _only_permission_group  # noqa: E402

STACK = "hf"
ID_ENV = {"accountId": "CLOUDFLARE_ACCOUNT_ID", "zoneId": "CF_ZONE_ID"}
READ = "Workers R2 Storage Bucket Item Read"
D1 = ("D1 Read", "D1 Write")
PAGES = ("Pages Write",)
ICONS = "oa-hf-usage-icons"


def ids_from_env(config: pulumi.Config, environ: dict[str, str]) -> None:
    """Fill the stack config's `accountId` / `zoneId` from the environment, unless the config sets them."""
    for key, var in ID_ENV.items():
        if config.get(key) is not None:
            continue
        value = environ.get(var, "").strip()
        if not value:
            raise pulumi.RunError(f"{key}: set ${var} (kept out of the public repo), or `pulumi config set {key}`")
        pulumi.runtime.set_config(f"{pulumi.get_project()}:{key}", value)


def group_ids(account_id: str, names: tuple[str, ...]) -> list[pulumi.Output[str]]:
    return [
        cloudflare.get_account_api_token_permission_groups_list_output(account_id=account_id, name=n).apply(
            lambda r, n=n: _only_permission_group(r, n),
        )
        for n in names
    ]


def token(name: str, *, account_id: str, title: str, groups: list[pulumi.Input[str]], resources: dict[str, str]) -> cloudflare.AccountToken:
    return cloudflare.AccountToken(
        name,
        account_id=account_id,
        name=title,
        policies=[cloudflare.AccountTokenPolicyArgs(
            effect="allow",
            permission_groups=[cloudflare.AccountTokenPolicyPermissionGroupArgs(id=g) for g in groups],
            resources=json.dumps(resources, sort_keys=True),
        )],
        # The provider reads `policies` back in a shape it never matches to the inputs (as `cfn_dashboard`'s token):
        # re-key by replacing the token, not by editing this.
        opts=pulumi.ResourceOptions(ignore_changes=["policies"]),
    )


if pulumi.get_stack() != STACK:
    raise ValueError(f"this branch's cf/ manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")
config = pulumi.Config()
ids_from_env(config, dict(os.environ))
program = runpy.run_path(str(ROOT / "stack" / "__main__.py"))

account_id = config.require("accountId")
bucket = config.require("r2Bucket")
project = config.require("pagesProject")
reader = token(
    "hf-site-r2-reader",
    account_id=account_id,
    title=f"{project} r2 read (site)",
    groups=group_ids(account_id, (READ,)),
    resources={f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}": "*"},
)
d1 = token(
    "hf-job-d1",
    account_id=account_id,
    title=f"{project} d1 (job)",
    groups=group_ids(account_id, D1),
    resources={f"com.cloudflare.api.account.{account_id}": "*"},
)
pulumi.export("site_r2_access_key_id", pulumi.Output.secret(reader.id))
# R2's S3 secret is the SHA-256 of the token value.
pulumi.export("site_r2_secret_access_key", pulumi.Output.secret(reader.value.apply(lambda v: hashlib.sha256(v.encode()).hexdigest())))
pulumi.export("job_d1_token", pulumi.Output.secret(d1.value))
icons = cloudflare.PagesProject(
    "hf-icons",
    account_id=account_id,
    name=ICONS,
    production_branch="main",
    build_config=cloudflare.PagesProjectBuildConfigArgs(destination_dir="."),
    # direct uploads (`wrangler pages deploy`) record their deployments' configs on the project
    opts=pulumi.ResourceOptions(ignore_changes=["deployment_configs"]),
)
pages = token(
    "hf-job-pages",
    account_id=account_id,
    title=f"{project} pages (job: digest plot)",
    groups=group_ids(account_id, PAGES),
    resources={f"com.cloudflare.api.account.{account_id}": "*"},
)
pulumi.export("icons_project", icons.name)
pulumi.export("job_pages_token", pulumi.Output.secret(pages.value))
