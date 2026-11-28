/** The digest's OP, rendered by the site (specs/slack-preview-page.md phase 2): the job posts a month's rows and its
 * digest config's display fields (`digest.cfg_json`) with the params it posts under, and gets back the OP's text and
 * its plot's `/api/digest-image` request, from the same renderer `/admin/slack` previews with (`src/digest`).
 *
 *   POST /api/digest-op {month: 'YYYY-MM', rows, cfg, params?: '<codec>', plot_url?, prev_url?, next_url?}
 *     → {text, image}
 *
 * `params` is `src/digest/codec`'s string (`''` or absent: the defaults), read strictly: a field the page's lenient
 * decoder would drop is a 400, never a silent default. A template's old name (`cw`, `gcs`) on `cfg` or `rows` reads as
 * its name (`canonicalTemplates`). Viewer-gated (the job's grant); pure, nothing stored. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { parseOpRequest, renderOp } from '../../src/digest/op.js'

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const req = parseOpRequest(await ctx.request.json().catch(() => null))
  if (typeof req === 'string') return json({ error: req }, 400)
  return json(renderOp(req), 200, { 'cache-control': 'no-store' })
}
