"""hf's two stacks under Pulumi's mocks, from their real stack config (`Pulumi.hf.yaml`): every resource by type and
name, the tokens' scopes, and the cron's Batch spec against the secrets the stack creates and the job's profile."""

import base64
import json
import re
import runpy
import sys
from pathlib import Path

import pulumi
import pytest
import yaml

INFRA = Path(__file__).resolve().parents[1]
REPO = INFRA.parent
CF_PROGRAM = INFRA / "cf" / "hf_stack.py"
GCP_PROGRAM = INFRA / "gcp" / "__main__.py"


def stack_config(d: Path, project: str) -> dict[str, str]:
    cfg = yaml.safe_load((d / "Pulumi.hf.yaml").read_text())["config"]
    return {k if ":" in k else f"{project}:{k}": v if isinstance(v, str) else json.dumps(v) for k, v in cfg.items()}


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[tuple[str, str], dict] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.typ, args.name] = args.inputs
        extra = {"value": "fixture-token"}
        if args.typ == "gcp:serviceaccount/account:Account":
            extra["email"] = f"{args.inputs['accountId']}@{args.inputs['project']}.iam.gserviceaccount.com"
        return f"{args.name}-id", {**args.inputs, **extra}

    def call(self, args: pulumi.runtime.MockCallArgs):
        if args.token.endswith("getAccountApiTokenPermissionGroupsList"):
            return {"results": [{"id": f"pg:{args.args['name']}", "name": args.args["name"]}]}
        return {}

    def created(self) -> list[tuple[str, str]]:
        return sorted(self.resources)

    def inputs(self, name: str) -> dict:
        """The one custom (provider) resource named `name`'s inputs."""
        [inputs] = [v for (t, n), v in self.resources.items() if n == name and t.split(":")[0] in ("cloudflare", "gcp")]
        return inputs


@pulumi.runtime.test
def test_cf_stack(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CF_ZONE_ID", "zone")
    m = Mocks()
    pulumi.runtime.set_mocks(m, project="gcs-usage-cf", stack="hf", preview=False)
    pulumi.runtime.set_all_config(stack_config(INFRA / "cf", "gcs-usage-cf"))
    g = runpy.run_path(str(CF_PROGRAM))

    def check(_: object) -> None:
        assert m.created() == sorted([
            ("oa:cfn:CfnDashboard", "hf"),
            ("cloudflare:index/pagesProject:PagesProject", "hf-pages"),
            ("cloudflare:index/dnsRecord:DnsRecord", "hf-cname"),
            ("cloudflare:index/pagesDomain:PagesDomain", "hf-domain"),
            ("cloudflare:index/dnsRecord:DnsRecord", "hf-dev-cname"),
            ("cloudflare:index/pagesDomain:PagesDomain", "hf-dev-domain"),
            ("cloudflare:index/d1Database:D1Database", "hf-d1"),
            ("cloudflare:index/workersKvNamespace:WorkersKvNamespace", "hf-cache-kv"),
            ("cloudflare:index/r2Bucket:R2Bucket", "hf-r2"),
            ("cloudflare:index/accountToken:AccountToken", "hf-r2-token"),
            ("cloudflare:index/accountToken:AccountToken", "hf-site-r2-reader"),
            ("cloudflare:index/accountToken:AccountToken", "hf-job-d1"),
            ("cloudflare:index/pagesProject:PagesProject", "hf-icons"),
            ("cloudflare:index/accountToken:AccountToken", "hf-job-pages"),
        ])
        res = {n: m.inputs(n) for _, n in m.created() if not n == "hf"}
        assert {k: res["hf-pages"][k] for k in ("accountId", "name")} == {"accountId": "acct", "name": "oa-hf-usage"}
        assert {k: res["hf-domain"][k] for k in ("accountId", "name")} == {"accountId": "acct", "name": "hf.oa.dev"}
        assert {k: res["hf-cname"][k] for k in ("zoneId", "name", "type", "content")} == {
            "zoneId": "zone", "name": "hf.oa.dev", "type": "CNAME", "content": "oa-hf-usage.pages.dev",
        }
        # The dev stack: `site/deploy --dev`'s branch alias, under its own name.
        assert {k: res["hf-dev-domain"][k] for k in ("accountId", "name")} == {"accountId": "acct", "name": "dev.hf.oa.dev"}
        assert {k: res["hf-dev-cname"][k] for k in ("zoneId", "name", "type", "content")} == {
            "zoneId": "zone", "name": "dev.hf.oa.dev", "type": "CNAME", "content": "dev.oa-hf-usage.pages.dev",
        }
        assert res["hf-d1"]["name"] == "oa-hf-usage-db"
        assert res["hf-cache-kv"]["title"] == "oa-hf-usage-cache"
        assert res["hf-r2"]["name"] == "oa-hf-usage-index"
        bucket = {"com.cloudflare.edge.r2.bucket.acct_default_oa-hf-usage-index": "*"}
        for name, title, groups, scope in (
            ("hf-site-r2-reader", "oa-hf-usage r2 read (site)", ["Workers R2 Storage Bucket Item Read"], bucket),
            ("hf-job-d1", "oa-hf-usage d1 (job)", ["D1 Read", "D1 Write"], {"com.cloudflare.api.account.acct": "*"}),
            ("hf-job-pages", "oa-hf-usage pages (job: digest plot)", ["Pages Write"], {"com.cloudflare.api.account.acct": "*"}),
        ):
            assert res[name] == {
                "accountId": "acct", "name": title,
                "policies": [{"effect": "allow", "permissionGroups": [{"id": f"pg:{n}"} for n in groups], "resources": json.dumps(scope, sort_keys=True)}],
            }
        # The digest's icons project: avatars on production, the plot on its `plot` branch (job/digest.yml).
        assert {k: res["hf-icons"][k] for k in ("accountId", "name", "productionBranch")} == {"accountId": "acct", "name": "oa-hf-usage-icons", "productionBranch": "main"}
        r2_policy = res["hf-r2-token"]["policies"][0]
        assert json.loads(r2_policy["resources"]) == bucket

    # Every resource's id: the mocks register them asynchronously, so a check gated on a few ran before the rest had
    # registered (a flaky `created()` short of the dev aliases or the KV).
    dash = g["program"]["dash"]
    every = [
        dash.pages, dash.domain, dash.cname, dash.d1, dash.kv, dash.r2, dash.r2_token,
        *dash.branch_cnames.values(), *dash.branch_domains.values(), g["reader"], g["d1"], g["icons"], g["pages"],
    ]
    return pulumi.Output.all(*[r.id for r in every]).apply(check)


def test_cf_stack_needs_the_ids(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CF_ZONE_ID", "zone")
    pulumi.runtime.set_mocks(Mocks(), project="gcs-usage-cf", stack="hf", preview=False)
    pulumi.runtime.set_all_config(stack_config(INFRA / "cf", "gcs-usage-cf"))
    with pytest.raises(pulumi.RunError, match=r"^accountId: set \$CLOUDFLARE_ACCOUNT_ID"):
        runpy.run_path(str(CF_PROGRAM))


@pulumi.runtime.test
def test_gcp_stack(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=hf")
    monkeypatch.syspath_prepend(str(INFRA / "gcp"))
    m = Mocks()
    pulumi.runtime.set_mocks(m, project="gcs-usage-gcp", stack="hf", preview=False)
    pulumi.runtime.set_all_config(stack_config(INFRA / "gcp", "gcs-usage-gcp"))
    g = runpy.run_path(str(GCP_PROGRAM))
    secrets = g["HF_SECRETS"]
    project = "oa-internal-450019"
    sa = f"hf-usage-job@{project}.iam.gserviceaccount.com"
    submitter = f"hf-usage-scheduler@{project}.iam.gserviceaccount.com"

    def check(_: object) -> None:
        assert m.created() == sorted([
            ("pulumi:providers:gcp", "hf-labeled"),
            ("disky:gcp:JobAccount", "hf-usage-job"),
            ("gcp:serviceaccount/account:Account", "hf-usage-job"),
            *[("gcp:projects/iAMMember:IAMMember", f"hf-usage-job-{r}") for r in ("batch.agentReporter", "batch.jobsEditor", "logging.logWriter")],
            *[("gcp:serviceaccount/iAMMember:IAMMember", f"hf-usage-job-actor-{a}") for a in ("hf-usage-job", "hf-usage-scheduler")],
            ("gcp:storage/bucket:Bucket", "hf-usage-data"),
            ("gcp:storage/bucketIAMMember:BucketIAMMember", "hf-usage-data-job"),
            ("disky:gcp:JobAccount", "hf-usage-scheduler"),
            ("gcp:serviceaccount/account:Account", "hf-usage-scheduler"),
            ("gcp:projects/iAMCustomRole:IAMCustomRole", "hf-usage-batch-submit"),
            ("gcp:projects/iAMMember:IAMMember", "hf-usage-scheduler-submit"),
            ("gcp:artifactregistry/repository:Repository", "hf-usage-images"),
            ("gcp:artifactregistry/repositoryIamMember:RepositoryIamMember", "hf-usage-images-job"),
            ("disky:gcp:Secrets", "hf-secrets"),
            *[("gcp:secretmanager/secret:Secret", s) for s in secrets],
            *[("gcp:secretmanager/secretIamMember:SecretIamMember", f"{s}-accessor") for s in secrets],
            ("disky:gcp:BatchCron", "hf-usage-snapshot"),
            ("gcp:cloudscheduler/job:Job", "hf-usage-snapshot"),
        ])
        assert m.inputs("hf-usage-images")["repositoryId"] == "hf-usage"
        # the cron's caller may only create jobs, and act as the job account (which submits its step tasks as itself)
        assert m.inputs("hf-usage-batch-submit")["permissions"] == ["batch.jobs.create"]
        assert m.inputs("hf-usage-scheduler-submit")["member"] == f"serviceAccount:{submitter}"
        assert {k: m.inputs("hf-usage-job-actor-hf-usage-scheduler")[k] for k in ("role", "member")} == {
            "role": "roles/iam.serviceAccountUser", "member": f"serviceAccount:{submitter}",
        }
        assert m.inputs("hf-usage-images-job")["member"] == f"serviceAccount:{sa}"
        assert {k: m.inputs("hf-usage-job-actor-hf-usage-job")[k] for k in ("role", "member")} == {
            "role": "roles/iam.serviceAccountUser", "member": f"serviceAccount:{sa}",
        }
        bucket = m.inputs("hf-usage-data")
        assert {k: bucket[k] for k in ("name", "location", "uniformBucketLevelAccess", "publicAccessPrevention")} == {
            "name": "oa-hf-usage-data", "location": "US-CENTRAL1", "uniformBucketLevelAccess": True, "publicAccessPrevention": "enforced",
        }
        assert [(r["condition"]["age"], r["condition"]["matchesPrefixes"], r["action"]["type"]) for r in bucket["lifecycleRules"]] == [
            (30, ["raw/"], "Delete"), (14, ["nf/work/", "nf/cache/"], "Delete"), (7, ["scratch/"], "Delete"),
        ]
        assert {k: m.inputs("hf-usage-data-job")[k] for k in ("bucket", "role", "member")} == {
            "bucket": "oa-hf-usage-data", "role": "roles/storage.objectAdmin", "member": f"serviceAccount:{sa}",
        }
        cron = m.inputs("hf-usage-snapshot")
        assert (cron["schedule"], cron["timeZone"], cron["httpTarget"]["oauthToken"]["serviceAccountEmail"]) == ("0 8 * * *", "Etc/UTC", submitter)
        spec = json.loads(base64.b64decode(cron["httpTarget"]["body"]))
        task = spec["taskGroups"][0]["taskSpec"]
        assert task["runnables"][0]["container"] == {
            "imageUri": f"us-central1-docker.pkg.dev/{project}/hf-usage/scan:hf",
            "entrypoint": "dt-cloud",
            "commands": ["scan", "head", "/app/job/profiles/hf.yml"],
        }
        assert (task["computeResource"], task["maxRunDuration"]) == ({"cpuMilli": 2000, "memoryMib": 7000}, "21600s")
        assert task["environment"]["variables"] == {
            "DISKY_LABELS": "app=disky,deployment=hf", "HF_URI": "hf://open-athena", "R2_BUCKET": "oa-hf-usage-index",
            "WORK_DIR": "/work", "SLACK_CHANNEL": "C0C9G547AP7", "SLACK_ALERT_CHANNEL": "C0C9J9VDPV5",
            "DT_TRIGGER": "schedule",  # the cron's scans are the digest's reply scans
        }
        refs = task["environment"]["secretVariables"]
        # The head reads only its own: the run record's D1 token + account id and the alert's Slack token (each step
        # task fetches the profile's); each a container this stack creates (and grants), and the profile redacts it.
        assert {k: r.split("/")[3] for k, r in refs.items()} == {
            "CLOUDFLARE_API_TOKEN": "hf-usage-cf-token", "CLOUDFLARE_ACCOUNT_ID": "hf-usage-cf-account-id", "SLACK_BOT_TOKEN": "hf-usage-slack-bot-token",
        }
        assert set(refs.values()) <= {f"projects/{project}/secrets/{s}/versions/latest" for s in secrets}
        profile = json.loads((REPO / "job" / "hf-scan-runs.json").read_text())
        assert set(refs) <= set(profile["secrets"])
        policy = spec["allocationPolicy"]
        assert (policy["serviceAccount"]["email"], policy["instances"][0]["policy"]["machineType"], policy["instances"][0]["policy"]["provisioningModel"]) == (sa, "e2-standard-2", "STANDARD")
        assert policy["labels"] == spec["labels"] == {"app": "disky", "deployment": "hf", "component": "scan", "trigger": "schedule"}

    return pulumi.Output.all(g["scan"].job.id, g["images_reader"].id, g["data_admin"].id).apply(check)


def test_scan_profile_matches_the_site_and_the_stack():
    """The DAG's profile (job/profiles/hf.yml) names what the site and the GCP stack do: the site's D1 and static-names
    generation (`site/wrangler.toml`), only secrets this stack creates, each redacted by the scan-run profile, and the
    stack's data bucket and job account."""
    import tomllib

    from dt_cloud.scan_profile import load

    p = load(str(REPO / "job" / "profiles" / "hf.yml"))
    w = tomllib.loads((REPO / "site" / "wrangler.toml").read_text())
    [db] = w["d1_databases"]
    assert (p.env["D1_DB_ID"], p.env["D1_DB_NAME"], p.opts("static-names")["gen"]) == (db["database_id"], db["database_name"], w["vars"]["STATIC_GEN"])
    block = re.search(r"^HF_SECRETS = \[\n(.*?)^\]", (INFRA / "gcp" / "__main__.py").read_text(), re.M | re.S).group(1)
    stack_secrets = re.findall(r'^    "([a-z0-9-]+)",', block, re.M)
    assert len(stack_secrets) == 9
    assert set(p.secrets.values()) <= set(stack_secrets)
    redacted = json.loads((REPO / "job" / "hf-scan-runs.json").read_text())["secrets"]
    assert set(p.secrets) <= set(redacted)
    assert (p.data, p.work, p.executor.service_account) == (
        "gs://oa-hf-usage-data", "gs://oa-hf-usage-data/nf", "hf-usage-job@oa-internal-450019.iam.gserviceaccount.com")
