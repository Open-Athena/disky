/** A digest OP request (`/api/digest-op`'s body, `site/scripts/digest-op.ts`'s stdin) read strictly, and rendered:
 * the month's rows and the digest config's display fields (`digest.cfg_json`) under the params the job posts with,
 * → the OP's text and its plot's `/api/digest-image` request. One renderer for the site, the job and a dry run. */
import { parseParams } from './codec'
import { type DigestCfg, type DigestRows, imageRequest, opText } from './index'
import { canonicalTemplates, TEMPLATES } from './templates'

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const optStr = (v: unknown): v is string | null | undefined => v === undefined || v === null || typeof v === 'string'

/** The request, or why it's refused. */
export function parseOpRequest(b: unknown) {
  if (!isObj(b)) return 'body: a JSON object'
  const { month, rows, cfg, params, plot_url, prev_url, next_url } = canonicalTemplates(b)
  if (typeof month !== 'string' || !MONTH_RE.test(month)) return 'month: YYYY-MM'
  if (!isObj(cfg) || !(TEMPLATES as readonly unknown[]).includes(cfg.template)) return `cfg.template: ${TEMPLATES.map(t => `'${t}'`).join(' | ')}`
  if (!isObj(rows) || rows.template !== cfg.template || !Array.isArray(rows.rows) || !rows.rows.length) {
    return `rows: the ${cfg.template} template's rows, at least one scan`
  }
  if (!optStr(params) || !optStr(plot_url) || !optStr(prev_url) || !optStr(next_url)) return 'params, plot_url, prev_url, next_url: strings'
  let p
  try {
    p = parseParams(params ?? '')
  } catch (e) {
    return `params: ${(e as Error).message}`
  }
  return { month, rows: rows as unknown as DigestRows, cfg: cfg as unknown as DigestCfg, params: p, links: { plotUrl: plot_url ?? null, prevUrl: prev_url ?? null, nextUrl: next_url ?? null } }
}

/** The OP's text and its plot's image request, for a request `parseOpRequest` accepted. */
export function renderOp({ month, rows, cfg, params, links }: Exclude<ReturnType<typeof parseOpRequest>, string>) {
  return { text: opText(rows, month, cfg, links, params), image: imageRequest(rows, month, cfg, params) }
}
