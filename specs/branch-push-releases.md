# Releases by branch push: `<d>` → dev, `<d>-prod` → prod

Ryan, 2026-11-17: across the OA deployments (gcs, cw, hf, r2), a push decides what each site serves, and PRs get previews:

| Push to | Deploys | Today |
|---|---|---|
| `<d>` (`cw`, `gcs`, `hf`, `r2`) | the dev site (`dev.<host>`), after CI passes on that push | same (`oa-release.yml` on `workflow_run`) |
| `<d>-prod` | the production site (`<host>`) | `workflow_dispatch` with `sha` + `dev_deployment` |
| a PR into `<d>` | `pr-N.<project>.pages.dev` (specs/cf-pr-previews.md: author, commit-message directive, label / `/preview` overrides) | same (`pr-preview.yml`; live on hf and cw, gcs pending its token) |

Owner: the root session (`oa-release.yml`, `scripts/oa-release`, `deploy/oa/sites.json` are `cloud`'s; the stub is `front-door`'s). Per-deployment sessions only move their `<d>-prod` pointers once it's live.

## Why `<d>-prod`, not `<d>` = prod

`<d>` is where every change to a deployment lands (root's `cloud` merges, agents' standing-go pushes, PR bases, preview targets). Making it prod would ship each of those with no dev step, or move all of that to a new `<d>-dev`. `<d>-prod` already exists as "what prod runs" (`site/deploy`'s pointer, line ~121), so a push to it becomes the promotion itself: `git push o <sha>:<d>-prod`.

## Behavior

1. **Trigger.** `oa-release.yml` gains `push: branches: [gcs-prod, cw-prod, hf-prod, r2-prod]` (GitHub runs `push` workflows from the pushed branch's own copy, so either each `<d>-prod` carries the stub, or, preferably, the promotion stays a `front-door` `workflow_run` / `workflow_dispatch` that a small `push`-triggered stub on `<d>-prod` hands off to; the controller must still run `cloud`'s scripts at one resolved sha, as today). Pick whichever keeps the environments admitting only `front-door`.
2. **Gate (unchanged in substance).** `plan` for a `<d>-prod` push takes the pushed sha and finds its successful dev deployment itself (the newest `<d>-dev` GitHub deployment for that sha whose latest status is `success`, recorded by this controller). None: the run fails before any deploy, naming the reason ("`<sha>` has no successful dev release; push it to `<d>` first"), and the pointer is left where the push put it (the site keeps serving the previous build; the failure is the alert). The dispatch's `dev_deployment` input goes away; dispatch stays as the manual retry of a pointer's current sha.
3. **History.** A push that isn't a fast-forward of `<d>-prod` is a rollback or a re-point: allowed (rollbacks are the point of a pointer), subject to the same gate (the target sha had a successful dev release) and the existing seal rule that refuses a build whose D1 recipes have since been applied (the 11-14 rollback rehearsal). The run summary says "rollback" when the new sha is an ancestor of the old.
4. **Concurrency.** One release per `<d>-prod` at a time, newest push wins (`cancel-in-progress: false`, but a queued older sha is skipped when a newer one is queued behind it).
5. **`site/deploy`.** A manual prod deploy no longer pushes `<d>-prod` (that would trigger a second release of the same build). It prints the push that would promote instead, or `site/deploy` loses its prod mode on the OA deployments once this is live.
6. **Pointer truth.** Today `<d>-prod` drifts (cw's is `41ccadf45`, prod serves `d41f0bd03`): `oa-release` dispatches never move it. On cutover, each owner pushes `<d>-prod` to the sha prod's canonical deployment serves (`x-disky-build`), which is a no-op release if the gate re-checks first that prod already serves it.

## Rollout

1. Root: implement 1–5 on `cloud` + the `front-door` stub; tests for the plan's prod-from-push path (gate found / missing, rollback label, non-OA branch ignored).
2. Each deployment owner: realign `<d>-prod` to what prod serves (6), with the controller already live so the realignment is the first exercise.
3. Docs: each deployment's CLAUDE.md "ship" lines and the `oa-ci-and-release` memory switch from "dispatch `oa-release.yml` with `sha` + `dev_deployment`" to `git push o <sha>:<d>-prod`.
