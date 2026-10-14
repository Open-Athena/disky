"""The GCS job must not expose a scan whose required R2 copies are unavailable."""
from pathlib import Path
from subprocess import run

import pytest


@pytest.mark.parametrize("failure, credentials, expected_code, expected", [
    ("", True, 0, [
        "COPY -S -a -R snapshots/s/meta.json -R snapshots/s/age.json -R listing/s/index/g/attr.tsv -R listing/s/index/g/attr.tsv.idx.json s",
        "COPY -a -p snapshots/rules.json -R snapshots/rules.json s",
        "PHASE publish-r2",
        "POINTER",
    ]),
    ("scan", True, 7, [
        "COPY -S -a -R snapshots/s/meta.json -R snapshots/s/age.json -R listing/s/index/g/attr.tsv -R listing/s/index/g/attr.tsv.idx.json s",
        "ALERT",
        "PHASE publish-r2 failed (exit 7)",
    ]),
    ("rules", True, 7, [
        "COPY -S -a -R snapshots/s/meta.json -R snapshots/s/age.json -R listing/s/index/g/attr.tsv -R listing/s/index/g/attr.tsv.idx.json s",
        "COPY -a -p snapshots/rules.json -R snapshots/rules.json s",
        "ALERT",
        "PHASE publish-r2 failed (exit 7)",
    ]),
    ("", False, 1, ["PHASE publish-r2 failed (missing credentials)"]),
])
def test_r2_publication_is_a_pointer_barrier(failure, credentials, expected_code, expected):
    script = (Path(__file__).resolve().parents[1] / "job/run.sh").read_text()
    block = script.split("# Serving off GCS", 1)[1].split('if [ "${SCAN_COMPUTE_ONLY:-0}"', 1)[0]
    preamble = r'''
set -eu
SNAP_ID=s SNAP_PATH=snapshots/s INDEX_DIR=listing/s/index/g
phase() { printf 'PHASE %s\n' "$*"; }
slack_post() { printf 'ALERT\n'; }
dt-cloud() {
  shift
  printf 'COPY %s\n' "$*"
  if [ "$FAIL" = scan ] && [ "$1" = -S ]; then return 7; fi
  if [ "$FAIL" = rules ] && [ "$1" = -a ]; then return 7; fi
}
'''
    env = {"PATH": "/usr/bin:/bin", "FAIL": failure, "CLOUDFLARE_ACCOUNT_ID": "account"}
    if credentials:
        env.update(R2_ACCESS_KEY_ID="key-id", R2_SECRET_ACCESS_KEY="secret-not-for-logs")
    result = run(["bash", "-c", preamble + "# Serving off GCS" + block + "printf 'POINTER\\n'"], env=env, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines()) == (expected_code, expected)
    assert "secret-not-for-logs" not in result.stderr
