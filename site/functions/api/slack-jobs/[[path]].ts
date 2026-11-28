/** Slack jobs on `/admin/slack` (specs/slack-in-workers.md, `_lib/slackJobs.ts`): the recent ones, one in full, a new
 * digest converge, and a step of one (the page drives a job it started, or resumes one whose driver went away).
 *
 *   GET  /api/slack-jobs                                 → {jobs}                                       admin
 *   GET  /api/slack-jobs/<id>                            → {job}                                        admin
 *   POST /api/slack-jobs {kind: 'digest-converge', month?} → {job}   (created or joined, then one step) admin
 *   POST /api/slack-jobs/<id>/step                       → {job}                                        admin
 *
 * Where `DIGEST_CFN` is unset the POSTs answer 404; `"shadow"` (a dev stack): a converge records its writes and audits
 * the thread, writing nothing. A restricted link is refused (`RESTRICTED_BLOCKED`): a job names the digest's channel and
 * message ids. */
import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { convergeConfig } from '../../_lib/digestConverge.js'
import { digestPlot } from '../../_lib/digestPlot.js'
import { cfnMode, type DigestJobEnv, digestJob, getJob, jobView, recentJobs, step } from '../../_lib/slackJobs.js'

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
const ID_RE = /^[a-z-]+-\d{8}t\d{9}z-[0-9a-f]{8}$/
const NO_STORE = { 'cache-control': 'no-store' }

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as Ctx['env'] & DigestJobEnv
  if (!env.DB) return json({ error: 'no D1' }, 503)
  const segs = new URL(ctx.request.url).pathname.split('/').filter(Boolean).slice(2)
  const method = ctx.request.method
  if (method === 'GET' && segs.length === 0) return json({ jobs: (await recentJobs(env.DB)).map(jobView) }, 200, NO_STORE)
  if (method === 'GET' && segs.length === 1 && ID_RE.test(segs[0])) {
    const j = await getJob(env.DB, segs[0])
    return j ? json({ job: jobView(j) }, 200, NO_STORE) : json({ error: 'no such job' }, 404)
  }
  if (method !== 'POST') return json({ error: 'not found' }, 404)
  if (!cfnMode(env)) return json({ error: 'no Worker digest converge on this deployment (DIGEST_CFN)' }, 404)
  const by = id.email ?? `grant:${id.grantId}`
  const deps = () => ({ plot: digestPlot(ctx, env.DB!, convergeConfig(env.DIGEST_CONFIG).cfg.site_url), now: Date.now })
  if (segs.length === 0) {
    const body = (await ctx.request.json().catch(() => ({}))) as { kind?: unknown; month?: unknown }
    if (body.kind !== 'digest-converge') return json({ error: "kind: 'digest-converge'" }, 400)
    if (body.month !== undefined && (typeof body.month !== 'string' || !MONTH_RE.test(body.month))) return json({ error: 'month: YYYY-MM' }, 400)
    const job = await digestJob(env, by, body.month as string | undefined)
    return json({ job: jobView((await step(env, job.id, deps())) ?? job) }, 200, NO_STORE)
  }
  if (segs.length === 2 && ID_RE.test(segs[0]) && segs[1] === 'step') {
    const j = await getJob(env.DB, segs[0])
    if (!j) return json({ error: 'no such job' }, 404)
    return json({ job: jobView((await step(env, j.id, deps())) ?? j) }, 200, NO_STORE)
  }
  return json({ error: 'not found' }, 404)
}
