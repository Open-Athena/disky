"""CW entrypoint over the shared stack; optional additive isolation only."""

from pathlib import Path
import runpy
import sys

import pulumi

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from cw_cf_isolation import prepare_isolation

if pulumi.get_stack() != "cw":
    raise ValueError("CW entrypoint manages only the cw stack")
runpy.run_path(str(ROOT / "stack" / "__main__.py"))
config = pulumi.Config()
if config.get_bool("prepareIsolation"):
    prepare_isolation(config.require("accountId"), config.require("r2Bucket"))
