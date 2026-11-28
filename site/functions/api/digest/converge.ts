/** The scan job's digest converge, in the site (specs/slack-in-workers.md): one step of the month's converge job
 * (`_lib/slackJobs.ts`), created or joined, per request. The job calls it until the answer says `done` or `failed`
 * (`dt-cloud digest-cfn`).
 *
 *   POST /api/digest/converge {month?: 'YYYY-MM'} → {job}     the grant `DIGEST_CFN_GRANT` names
 *
 * Where `DIGEST_CFN` is `"shadow"` (a dev stack) the converge writes nothing: it records what it would write and audits
 * the thread against what it renders. Unset, or no grant named: 404. */
import { type Ctx, json, requireViewer } from '../../_lib/auth.js'
import { digestPlot } from '../../_lib/digestPlot.js'
import { convergeConfig } from '../../_lib/digestConverge.js'
import { cfnMode, type DigestJobEnv, digestJob, jobView, step } from '../../_lib/slackJobs.js'

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const env = ctx.env as Ctx['env'] & DigestJobEnv & { DIGEST_CFN_GRANT?: string }
  if (!cfnMode(env) || !env.DIGEST_CFN_GRANT || !env.DB) return json({ error: 'no Worker digest converge on this deployment' }, 404)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (gated.via !== 'grant' || gated.grantId !== env.DIGEST_CFN_GRANT) return json({ error: 'forbidden' }, 403)
  const body = (await ctx.request.json().catch(() => ({}))) as { month?: unknown }
  if (body.month !== undefined && (typeof body.month !== 'string' || !MONTH_RE.test(body.month))) return json({ error: 'month: YYYY-MM' }, 400)
  const job = await digestJob(env, `grant:${gated.grantId}`, body.month as string | undefined)
  const site = convergeConfig(env.DIGEST_CONFIG).cfg.site_url
  const after = await step(env, job.id, { plot: digestPlot(ctx, env.DB, site), now: Date.now })
  return json({ job: jobView(after ?? job) }, 200, { 'cache-control': 'no-store' })
}
