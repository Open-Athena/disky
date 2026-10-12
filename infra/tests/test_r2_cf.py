from json import dumps, loads
from pathlib import Path
from runpy import run_path

import pulumi


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.tokens: dict[str, list] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        if args.typ == "cloudflare:index/accountToken:AccountToken":
            self.tokens[args.name] = [dict(policy) for policy in args.inputs["policies"]]
        return f"{args.name}-id", {**args.inputs, "value": "test-secret"}

    def call(self, args: pulumi.runtime.MockCallArgs):
        name = args.args["name"]
        return {"results": [{"id": name + "-id", "name": name}]}


@pulumi.runtime.test
def test_r2_token_roles_have_exact_scopes():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="oa-r2-usage-cf", stack="r2", preview=False)
    config = {
        "accountId": "account", "zoneId": "zone", "pagesProject": "oa-r2-usage", "domain": "r2.oa.dev",
        "d1Name": "oa-r2-usage-db", "r2Bucket": "oa-r2-usage-index", "d1Publisher": True,
        "sourceBuckets": ["marin-na", "marin-discord", "oa-cw-s3-usage-index", "oa-gcs-usage-index"],
    }
    pulumi.runtime.set_all_config({f"oa-r2-usage-cf:{key}": value if isinstance(value, str) else dumps(value) for key, value in config.items()})
    program = run_path(str(Path(__file__).resolve().parents[1] / "cf/stack/__main__.py"))

    def check(_: object) -> None:
        tokens = mocks.tokens
        for policies in tokens.values():
            for policy in policies:
                policy["resources"] = loads(policy["resources"])
        read = [{"id": "Workers R2 Storage Bucket Item Read-id"}]
        assert tokens == {
            "r2-r2-token": [{
                "effect": "allow",
                "permissionGroups": [*read, {"id": "Workers R2 Storage Bucket Item Write-id"}],
                "resources": {"com.cloudflare.edge.r2.bucket.account_default_oa-r2-usage-index": "*"},
            }],
            "index-reader": [{
                "effect": "allow", "permissionGroups": read,
                "resources": {"com.cloudflare.edge.r2.bucket.account_default_oa-r2-usage-index": "*"},
            }],
            "source-reader": [{
                "effect": "allow", "permissionGroups": read,
                "resources": {
                    "com.cloudflare.edge.r2.bucket.account_default_marin-na": "*",
                    "com.cloudflare.edge.r2.bucket.account_default_marin-discord": "*",
                    "com.cloudflare.edge.r2.bucket.account_default_oa-cw-s3-usage-index": "*",
                    "com.cloudflare.edge.r2.bucket.account_default_oa-gcs-usage-index": "*",
                },
            }],
            "d1-publisher": [{
                "effect": "allow", "permissionGroups": [{"id": "D1 Write-id"}],
                "resources": {"com.cloudflare.api.account.account": "*"},
            }],
        }

    return pulumi.Output.all(
        program["dash"].r2_token.id, program["source_reader"].id, program["index_reader"].id, program["d1_publisher"].id,
    ).apply(check)
