# cf — Cloudflare resources as code (shared toolkit)

`cfn_dashboard.py` is the shared `CfnDashboard` Pulumi component: the Cloudflare surface of a `site/` deployment that `wrangler pages deploy` doesn't own. That's the Pages project shell, its custom domain + CNAME, preview-branch aliases, the D1 database, an optional `CACHE_KV` namespace, an optional R2 serving bucket, and an optional Zero Trust Access gate. It carries no account ids, zone ids or store literals. Wrangler still fills each deploy's bindings and vars from that branch's `site/wrangler.toml`; Pulumi owns the container.

`cloud` carries only the component and its environment (`../pyproject.toml`, `../uv.lock`). Each deployment branch adds its own instance wiring next to it: a `__main__.py` (its `Store`s + account/zone config), `Pulumi.yaml` and `Pulumi.<stack>.yaml`. Those are gcs (gcs.oa.dev), cw-s3 (cw-s3.oa.dev) and m3 (disk.rbw.sh).

```bash
cd infra/cf
pulumi preview -s <stack>   # read-only; `up` is a human's call
```

## hf stack

The `hf` stack (hf.oa.dev) runs the shared program through `hf_stack.py` (`main:` in `Pulumi.yaml`), with this deployment's values in `Pulumi.hf.yaml`. The account and zone ids are not in that file: `hf_stack.py` takes them from `$CLOUDFLARE_ACCOUNT_ID` / `$CF_ZONE_ID`. It declares:

- the `oa-hf-usage` Pages project shell and the `hf.oa.dev` custom domain + CNAME (no preview-branch alias yet);
- the `oa-hf-usage-db` D1 database (its schema stays with the app, `site/schema.sql`);
- the `CACHE_KV` namespace `oa-hf-usage-cache`;
- the R2 index bucket `oa-hf-usage-index` and its read/write token (`r2_s3_*` outputs: the job's keys);
- from `hf_stack.py`: a read-only token on that bucket (`site_r2_*`: the site's `STORE_*`) and the job's D1 token (`job_d1_token`).

`hf-secrets` delivers those outputs (`-P`: Pages secrets, `-g`: Secret Manager versions) without printing a value; `run` runs `pulumi` with `CF_IAC_TOKEN`. The ordered apply: `../../specs/hf-deployment.md`.
