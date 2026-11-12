"""r2.oa.dev's Cloudflare stack: the shared program (`stack/__main__.py`, the dashboard every deployment has), then
what only r2 holds: read-only R2 tokens for its source buckets and its derived index, a D1-only job credential, and the
Slack digest's icons project with the job's Pages token. Resource names are unchanged from when these lived in the shared
file, so moving them here replaces nothing. `pulumi preview` is read-only; `up` is a human's call.
"""
import hashlib
import json
import runpy
from pathlib import Path

import pulumi
import pulumi_cloudflare as cloudflare

shared = runpy.run_path(str(Path(__file__).resolve().parents[1] / "stack/__main__.py"))
from cfn_dashboard import _only_permission_group  # noqa: E402  (on `sys.path` once the shared program ran)

cfg: pulumi.Config = shared["cfg"]
account_id: str = shared["account_id"]
dash = shared["dash"]

# Deployment-owned RO credentials: source listings and the site's derived
# reads are distinct from CfnDashboard's derived-index publisher credential.
source_buckets = cfg.get_object("sourceBuckets")
if source_buckets is not None and source_buckets != ["marin-na", "marin-discord", "oa-cw-s3-usage-index", "oa-gcs-usage-index"]:
    raise pulumi.RunError("Review the explicit OA source-bucket inventory before changing sourceBuckets")
if source_buckets is not None:
    read_group = cloudflare.get_account_api_token_permission_groups_list_output(
        account_id=account_id, name="Workers R2 Storage Bucket Item Read",
    ).apply(lambda result: _only_permission_group(result, "Workers R2 Storage Bucket Item Read"))

    def readonly_token(name: str, buckets: list[str]) -> cloudflare.AccountToken:
        return cloudflare.AccountToken(
            name,
            account_id=account_id,
            name=f"oa-r2-usage {name}",
            policies=[{
                "effect": "allow", "permission_groups": [{"id": read_group}],
                "resources": json.dumps({f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}": "*" for bucket in buckets}),
            }],
            opts=pulumi.ResourceOptions(ignore_changes=["policies"]),
        )

    source_reader = readonly_token("source-reader", source_buckets)
    index_reader = readonly_token("index-reader", [cfg.require("r2Bucket")])
    private_index = cloudflare.R2ManagedDomain(
        "index-private", account_id=account_id, bucket_name=dash.r2.name, enabled=False,
    )
    for role, token in [("source", source_reader), ("index", index_reader)]:
        pulumi.export(f"{role}_s3_access_key_id", pulumi.Output.secret(token.id))
        pulumi.export(f"{role}_s3_secret_access_key", pulumi.Output.secret(token.value.apply(lambda value: hashlib.sha256(value.encode()).hexdigest())))

# Separate D1-only job credential. Cloudflare's D1 API permissions are account-
# scoped; the job's reviewed profile pins writes to this deployment's database.
if cfg.get_bool("d1Publisher"):
    d1_group = cloudflare.get_account_api_token_permission_groups_list_output(
        account_id=account_id, name="D1 Write",
    ).apply(lambda result: _only_permission_group(result, "D1 Write"))
    d1_publisher = cloudflare.AccountToken(
        "d1-publisher",
        account_id=account_id,
        name="oa-r2-usage D1 publisher",
        policies=[{
            "effect": "allow", "permission_groups": [{"id": d1_group}],
            "resources": json.dumps({f"com.cloudflare.api.account.{account_id}": "*"}),
        }],
        opts=pulumi.ResourceOptions(ignore_changes=["policies"]),
    )
    pulumi.export("d1_publish_token", pulumi.Output.secret(d1_publisher.value))

# The Slack digest's icons: a Pages project serving the reply avatars (its production deployment of `job/icons-r2/`)
# and the OP's plot (the `plot` branch, deployed by `dt-cloud digest`), and the job's Pages token. Pages tokens can't be
# scoped to one project, so it is Pages Write on the account, held only by the job.
if icons_project := cfg.get("iconsProject"):
    icons = cloudflare.PagesProject(
        "icons",
        account_id=account_id,
        name=icons_project,
        production_branch="main",
        build_config=cloudflare.PagesProjectBuildConfigArgs(destination_dir="."),
        # direct uploads (`wrangler pages deploy`) record their deployments' configs on the project
        opts=pulumi.ResourceOptions(ignore_changes=["deployment_configs"]),
    )
    pages_group = cloudflare.get_account_api_token_permission_groups_list_output(
        account_id=account_id, name="Pages Write",
    ).apply(lambda result: _only_permission_group(result, "Pages Write"))
    pages_publisher = cloudflare.AccountToken(
        "pages-publisher",
        account_id=account_id,
        name="oa-r2-usage Pages publisher (job: digest plot)",
        policies=[{
            "effect": "allow", "permission_groups": [{"id": pages_group}],
            "resources": json.dumps({f"com.cloudflare.api.account.{account_id}": "*"}),
        }],
        opts=pulumi.ResourceOptions(ignore_changes=["policies"]),
    )
    pulumi.export("icons_project", icons.name)
    pulumi.export("job_pages_token", pulumi.Output.secret(pages_publisher.value))
