from pathlib import Path
from runpy import run_path

import pulumi
import pytest


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[(args.name, args.typ)] = {"type": args.typ, "inputs": args.inputs}
        return f"{args.name}-id", {
            **args.inputs,
            "email": f"{args.inputs.get('accountId', 'oa-r2-usage-job')}@oa-internal-450019.iam.gserviceaccount.com",
            "name": args.inputs.get("name", args.name),
        }

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pytest.mark.parametrize("daily_enabled", [False, True])
@pulumi.runtime.test
def test_independent_resources_and_optional_scheduler(daily_enabled: bool):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="oa-r2-usage-gcp", stack="r2", preview=False)
    image = "us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan@sha256:" + "a" * 64
    pulumi.runtime.set_all_config({"gcp:project": "oa-internal-450019", "gcp:region": "us-west1", "r2:dailyEnabled": str(daily_enabled).lower(), "r2:scanImage": image})
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
    program = run_path(str(Path(__file__).resolve().parents[1] / "gcp/__main__.py"))

    def check(_: object) -> None:
        resources = mocks.resources
        scheduler_resources = [
            ("r2-scheduler", "gcp:serviceaccount/account:Account"),
            ("r2-batch-submit", "gcp:projects/iAMCustomRole:IAMCustomRole"),
            ("r2-scheduler-submit", "gcp:projects/iAMMember:IAMMember"),
            ("r2-scheduler-job", "gcp:serviceaccount/iAMMember:IAMMember"),
            ("r2-daily", "gcp:cloudscheduler/job:Job"),
        ] if daily_enabled else []
        assert sorted(resources) == sorted([
            ("r2-labeled", "pulumi:providers:gcp"),
            ("r2-builder", "disky:gcp:JobAccount"),
            ("r2-builder", "gcp:serviceaccount/account:Account"),
            ("r2-builder-logging.logWriter", "gcp:projects/iAMMember:IAMMember"),
            ("r2-job", "disky:gcp:JobAccount"),
            ("r2-job", "gcp:serviceaccount/account:Account"),
            ("r2-job-batch.agentReporter", "gcp:projects/iAMMember:IAMMember"),
            ("r2-job-batch.jobsEditor", "gcp:projects/iAMMember:IAMMember"),
            ("r2-job-actor-oa-r2-usage-job", "gcp:serviceaccount/iAMMember:IAMMember"),
            ("r2-job-logging.logWriter", "gcp:projects/iAMMember:IAMMember"),
            ("r2-data", "gcp:storage/bucket:Bucket"),
            ("r2-scratch", "gcp:storage/bucket:Bucket"),
            ("r2-data-job", "gcp:storage/bucketIAMMember:BucketIAMMember"),
            ("r2-scratch-job", "gcp:storage/bucketIAMMember:BucketIAMMember"),
            ("r2-data-builder", "gcp:storage/bucketIAMMember:BucketIAMMember"),
            ("r2-scratch-builder", "gcp:storage/bucketIAMMember:BucketIAMMember"),
            ("r2-images", "gcp:artifactregistry/repository:Repository"),
            ("r2-images-job", "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember"),
            ("r2-images-builder", "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember"),
            ("r2-secrets", "disky:gcp:Secrets"),
            *[(name, "gcp:secretmanager/secret:Secret") for name in (
                "oa-r2-source-access-key-id", "oa-r2-source-secret-access-key", "oa-r2-index-access-key-id", "oa-r2-index-secret-access-key", "oa-r2-d1-token",
                "oa-r2-slack-bot-token", "oa-r2-site-grant", "oa-r2-cf-pages-token",
            )],
            *[(name + "-accessor", "gcp:secretmanager/secretIamMember:SecretIamMember") for name in (
                "oa-r2-source-access-key-id", "oa-r2-source-secret-access-key", "oa-r2-index-access-key-id", "oa-r2-index-secret-access-key", "oa-r2-d1-token",
                "oa-r2-slack-bot-token", "oa-r2-site-grant", "oa-r2-cf-pages-token",
            )],
        ] + scheduler_resources)
        grants = sorted(
            (value["inputs"]["bucket"], value["inputs"]["role"], value["inputs"]["member"])
            for value in resources.values() if value["type"] == "gcp:storage/bucketIAMMember:BucketIAMMember"
        )
        assert grants == sorted([
            ("oa-r2-usage-data", "roles/storage.objectCreator", "serviceAccount:oa-r2-usage-builder@oa-internal-450019.iam.gserviceaccount.com"),
            ("oa-r2-usage-data", "roles/storage.objectAdmin", "serviceAccount:oa-r2-usage-job@oa-internal-450019.iam.gserviceaccount.com"),
            ("oa-r2-usage-scratch", "roles/storage.objectViewer", "serviceAccount:oa-r2-usage-builder@oa-internal-450019.iam.gserviceaccount.com"),
            ("oa-r2-usage-scratch", "roles/storage.objectAdmin", "serviceAccount:oa-r2-usage-job@oa-internal-450019.iam.gserviceaccount.com"),
        ])
        assert sorted(value["inputs"]["secretId"] for value in resources.values() if value["type"] == "gcp:secretmanager/secret:Secret") == sorted([
            "oa-r2-source-access-key-id", "oa-r2-source-secret-access-key", "oa-r2-index-access-key-id", "oa-r2-index-secret-access-key", "oa-r2-d1-token",
            "oa-r2-slack-bot-token", "oa-r2-site-grant", "oa-r2-cf-pages-token",
        ])
        if daily_enabled:
            from base64 import b64decode
            from json import loads

            role = resources[("r2-batch-submit", "gcp:projects/iAMCustomRole:IAMCustomRole")]["inputs"]
            assert role["permissions"] == ["batch.jobs.create"]
            act_as = resources[("r2-scheduler-job", "gcp:serviceaccount/iAMMember:IAMMember")]["inputs"]
            assert act_as == {
                "serviceAccountId": "projects/oa-internal-450019/serviceAccounts/oa-r2-usage-job@oa-internal-450019.iam.gserviceaccount.com",
                "role": "roles/iam.serviceAccountUser", "member": "serviceAccount:oa-r2-usage-scheduler@oa-internal-450019.iam.gserviceaccount.com",
            }
            daily = resources[("r2-daily", "gcp:cloudscheduler/job:Job")]["inputs"]
            assert (daily["schedule"], daily["timeZone"], daily["retryConfig"]) == ("15 7 * * *", "Etc/UTC", {"retryCount": 0, "maxRetryDuration": "0s"})
            target = daily["httpTarget"]
            assert target["oauthToken"] == {"serviceAccountEmail": "oa-r2-usage-scheduler@oa-internal-450019.iam.gserviceaccount.com", "scope": "https://www.googleapis.com/auth/cloud-platform"}
            planned = loads(b64decode(target["body"]))
            assert planned["allocationPolicy"]["serviceAccount"] == {"email": "oa-r2-usage-job@oa-internal-450019.iam.gserviceaccount.com"}
            assert planned["taskGroups"][0]["taskSpec"]["runnables"] == [{"container": {"imageUri": image, "volumes": ["/mnt/disks/r2-stage:/stage"]}}]

    outputs = [resource.id for resource in program["resources"]]
    outputs += [program["image_reader"].id, *[resource.id for resource in program["secrets"].secrets.values()]]
    if daily_enabled:
        outputs.append(program["daily"].id)
    return pulumi.Output.all(*outputs).apply(check)
