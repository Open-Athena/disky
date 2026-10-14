"""The GCS job must not expose a scan whose required R2 copies are unavailable."""
from pathlib import Path
from subprocess import run

import pytest


COPY_SCAN = "COPY -S -a -R snapshots/s/meta.json -R snapshots/s/age.json -R listing/s/index/g/attr.tsv -R listing/s/index/g/attr.tsv.idx.json s"
COPY_RULES = "COPY -a -p snapshots/rules.json -R snapshots/rules.json s"
KEPT = "D1 pointers were not advanced, so the previous scan remains visible. Repair the R2 copy before resuming index-sync."


@pytest.mark.parametrize("failure, credentials, expected_code, expected", [
    ("", True, 0, [COPY_SCAN, COPY_RULES, "PHASE-END publish-r2", "POINTER"]),
    ("scan", True, 7, [COPY_SCAN, f"PHASE-END publish-r2 -x 7 -m {KEPT}"]),
    ("rules", True, 7, [COPY_SCAN, COPY_RULES, f"PHASE-END publish-r2 -x 7 -m {KEPT}"]),
    ("", False, 1, ["PHASE-END publish-r2 -x 1 -q missing credentials -m no R2 credentials: publication blocked; D1 pointers were not advanced, so the previous scan remains visible."]),
])
def test_r2_publication_is_a_pointer_barrier(failure, credentials, expected_code, expected):
    script = (Path(__file__).resolve().parents[2] / "job/run.sh").read_text()
    block = script.split("# Serving off GCS", 1)[1].split('if [ "${SCAN_COMPUTE_ONLY:-0}"', 1)[0]
    # `sr` is run.sh's `dt-cloud scan-run` helper; a fatal `phase-end -x RC` exits RC (it alerts, then fails the job).
    preamble = r'''
set -eu
SNAP_ID=s SNAP_PATH=snapshots/s INDEX_DIR=listing/s/index/g
sr() { dt-cloud scan-run "$@"; }
dt-cloud() {
  case "$1" in
    publish-r2)
      shift
      printf 'COPY %s\n' "$*"
      if [ "$FAIL" = scan ] && [ "$1" = -S ]; then return 7; fi
      if [ "$FAIL" = rules ] && [ "$1" = -a ]; then return 7; fi ;;
    scan-run)
      shift 2
      local name=$1 rc=0; shift
      printf 'PHASE-END %s\n' "$name${*:+ $*}"
      while [ $# -gt 0 ]; do if [ "$1" = -x ]; then rc=$2; fi; shift; done
      return "$rc" ;;
  esac
}
'''
    env = {"PATH": "/usr/bin:/bin", "FAIL": failure, "CLOUDFLARE_ACCOUNT_ID": "account"}
    if credentials:
        env.update(R2_ACCESS_KEY_ID="key-id", R2_SECRET_ACCESS_KEY="secret-not-for-logs")
    result = run(["bash", "-c", preamble + "# Serving off GCS" + block + "printf 'POINTER\\n'"], env=env, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines()) == (expected_code, expected)
    assert "secret-not-for-logs" not in result.stderr
