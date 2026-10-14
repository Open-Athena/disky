import { type Ctx, json, requireScope } from '../_lib/auth.js'
import { check, MONITOR_END_MS, MONITOR_SCOPE } from '../_lib/sessionMonitor.js'
import type { SlackEnv } from '../_lib/slack.js'

type C = Ctx & { env: Ctx['env'] & SlackEnv }

export const onRequestPost = async (ctx: C): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const dry = url.searchParams.get('dry') === '1'
  // Expired scheduler calls are quiet no-ops, before auth (the grant expires too), with no DB access.
  if (Date.now() >= MONITOR_END_MS) return json({ status: 'expired' }, 200, { 'cache-control': 'no-store' })
  const id = await requireScope(ctx, MONITOR_SCOPE)
  if (id instanceof Response) return id
  if (!dry && (ctx.env.SESSION_MONITOR !== '1' || url.hostname !== ctx.env.PROD_HOST)) return json({ error: 'monitor notifications disabled on this host' }, 403)
  if (!ctx.env.DB) return json({ error: 'no D1 bound' }, 503)
  try {
    return json(await check(ctx.env.DB, ctx.env, Math.floor(Date.now() / 1000), dry), 200, { 'cache-control': 'no-store' })
  } catch {
    // Do not leak a D1 query, raw events or a transport error into scheduler logs.
    console.error('session monitor check failed; no automatic notification retry')
    return json({ error: 'monitor check failed' }, 503, { 'cache-control': 'no-store' })
  }
}
