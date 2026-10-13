"""CW's additive CF credential/assets phase; live deployments stay unchanged."""

from hashlib import sha256
import json

import pulumi
import pulumi_cloudflare as cloudflare

from cfn_dashboard import _only_permission_group

GROUPS = (
    "Workers R2 Storage Bucket Item Read",
    "Workers R2 Storage Bucket Item Write",
    "D1 Read", "D1 Write", "Pages Read", "Pages Write",
)


def prepare_isolation(
    account_id: str,
    bucket: str,
    group_ids: dict[str, pulumi.Input[str]] | None = None,
) -> list[pulumi.CustomResource]:
    if bucket != "oa-cw-s3-usage-index":
        raise ValueError("CW isolation credentials must target the CW serving bucket")
    ids = group_ids if group_ids is not None else {
        name: cloudflare.get_account_api_token_permission_groups_list_output(account_id=account_id, name=name).apply(
            lambda result, name=name: _only_permission_group(result, name),
        ) for name in GROUPS
    }
    resources: list[pulumi.CustomResource] = []
    for purpose, names, scope in (
        ("reader", GROUPS[:1], {f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}": "*"}),
        ("publisher", GROUPS[:2], {f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}": "*"}),
        ("pages-token", GROUPS[2:], {f"com.cloudflare.api.account.{account_id}": "*"}),
    ):
        token = cloudflare.AccountToken(
            f"cw-isolation-{purpose}",
            account_id=account_id,
            name=f"cw-s3 isolation {purpose}",
            policies=[cloudflare.AccountTokenPolicyArgs(
                effect="allow",
                permission_groups=[cloudflare.AccountTokenPolicyPermissionGroupArgs(id=ids[name]) for name in names],
                resources=json.dumps(scope, sort_keys=True),
            )],
            # Policy readback includes expanded permission records. These are
            # new immutable policy identities, not edits to the live token.
            opts=pulumi.ResourceOptions(protect=True, ignore_changes=["policies"]),
        )
        resources.append(token)
        if purpose == "pages-token":
            pulumi.export("isolation_pages_token", pulumi.Output.secret(token.value))
            pulumi.export("isolation_pages_token_id", pulumi.Output.secret(token.id))
        else:
            pulumi.export(f"isolation_r2_{purpose}_key_id", pulumi.Output.secret(token.id))
            pulumi.export(f"isolation_r2_{purpose}_secret", pulumi.Output.secret(token.value.apply(lambda value: sha256(value.encode()).hexdigest())))
    icons = cloudflare.PagesProject(
        "cw-isolation-icons",
        account_id=account_id,
        name="oa-cw-s3-usage-icons",
        production_branch="main",
        build_config=cloudflare.PagesProjectBuildConfigArgs(destination_dir="."),
        opts=pulumi.ResourceOptions(protect=True, ignore_changes=["deployment_configs"]),
    )
    resources.append(icons)
    pulumi.export("isolation_icons_project", icons.name)
    return resources
