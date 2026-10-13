from pathlib import Path
import json
import sys

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cf"))
# GCP has another deployment-specific module of the same basename.
from importlib.util import module_from_spec, spec_from_file_location

spec = spec_from_file_location("cw_cf_isolation", Path(__file__).resolve().parents[1] / "cf/cw_cf_isolation.py")
isolation = module_from_spec(spec)
spec.loader.exec_module(isolation)


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources = {}
        self.types = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args.inputs
        self.types[args.name] = args.typ
        return args.name + "-id", args.inputs | {"value": "fixture-token"}

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pulumi.runtime.test
def test_new_credentials_are_additive_and_minimally_scoped() -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="cw-s3", preview=False)
    groups = {name: f"group-{i}" for i, name in enumerate(isolation.GROUPS)}
    resources = isolation.prepare_isolation("acct", "oa-cw-s3-usage-index", groups)

    def check(_: object) -> None:
        assert mocks.types == {
            "cw-isolation-reader": "cloudflare:index/accountToken:AccountToken",
            "cw-isolation-publisher": "cloudflare:index/accountToken:AccountToken",
            "cw-isolation-pages-token": "cloudflare:index/accountToken:AccountToken",
            "cw-isolation-icons": "cloudflare:index/pagesProject:PagesProject",
        }
        for purpose, ids, scope in (
            ("reader", ["group-0"], {"com.cloudflare.edge.r2.bucket.acct_default_oa-cw-s3-usage-index": "*"}),
            ("publisher", ["group-0", "group-1"], {"com.cloudflare.edge.r2.bucket.acct_default_oa-cw-s3-usage-index": "*"}),
            ("pages-token", ["group-2", "group-3", "group-4", "group-5"], {"com.cloudflare.api.account.acct": "*"}),
        ):
            token = mocks.resources[f"cw-isolation-{purpose}"]
            assert token == {
                "accountId": "acct", "name": f"cw-s3 isolation {purpose}",
                "policies": [{"effect": "allow", "permissionGroups": [{"id": id} for id in ids], "resources": json.dumps(scope, sort_keys=True)}],
            }
        assert mocks.resources["cw-isolation-icons"] == {
            "accountId": "acct", "name": "oa-cw-s3-usage-icons", "productionBranch": "main", "buildConfig": {"destinationDir": "."},
        }

    return pulumi.Output.all(*[r.id for r in resources]).apply(check)


def test_cannot_mint_credentials_for_another_deployments_bucket() -> None:
    with pytest.raises(ValueError, match="^CW isolation credentials must target the CW serving bucket$"):
        isolation.prepare_isolation("acct", "gcs-index", {})
