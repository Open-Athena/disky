/**
 * The digest OP's plot for a Worker's converge (specs/slack-in-workers.md): drawn in-process as `/api/digest-image`
 * draws it, stored by the SHA-256 of its bytes in `public_avatars` and served at `<site>/a/<sha256>.png`
 * (`_lib/publicAvatar.ts`): public, as the icons project's `plot` branch it replaces was, named by its bytes alone. A new
 * plot is a new URL, so Slack's image cache never shows a stale one. Refused, never cut, past what one D1 row holds.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { renderDigestImage } from '../api/digest-image.js'
import type { Env } from './auth.js'
import { withPathStore } from './index.js'
import { parseDigestRequest } from './og/digestRequest.js'
import type { OgEnv } from './og/serve.js'
import { publishAvatar } from './publicAvatar.js'

/** The largest PNG a `public_avatars` row stores: D1 caps a value near 2 MB, and the row holds it as base64 (4/3). */
export const MAX_PLOT_BYTES = 1_400_000

/** A plot function for ``ctx``'s deployment: the image request's PNG, published under ``site`` (the deployment's own
 *  origin, the one Slack fetches from); null when there's nothing to draw. */
export function digestPlot(ctx: { request: Request; env: Env }, db: D1Database, site: string) {
  return async (req: Record<string, unknown>): Promise<string | null> => {
    const parsed = parseDigestRequest(req)
    if (typeof parsed === 'string') throw new Error(`digest image request: ${parsed}`)
    const c2 = await withPathStore(ctx)
    const png = await renderDigestImage(c2.env as unknown as OgEnv, new URL(ctx.request.url).origin, parsed)
    if (png instanceof Response) throw new Error(`digest image: ${png.status} ${await png.text()}`)
    if (png === null) return null
    if (png.length > MAX_PLOT_BYTES) throw new Error(`digest plot is ${png.length} bytes, over the ${MAX_PLOT_BYTES} a public_avatars row holds`)
    return publishAvatar(db, site.replace(/\/+$/, ''), { type: 'image/png', bytes: png }, Math.floor(Date.now() / 1000))
  }
}
