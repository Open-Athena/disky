/** `/api/digest-op` without the site: a request on stdin (`digest.site_render`'s body) → `{text, image}` on stdout, by
 * the same renderer (`src/digest/op`). `dt-cloud digest -n` runs it (bundled by the checkout's esbuild) when it has no
 * site token, so a dry run renders the OP a checkout has. A refused request: its reason on stderr, exit 2. */
import { readFileSync } from 'node:fs'
import { parseOpRequest, renderOp } from '../src/digest/op'

const req = parseOpRequest(JSON.parse(readFileSync(0, 'utf8')))
if (typeof req === 'string') {
  process.stderr.write(`digest-op: ${req}\n`)
  process.exit(2)
}
process.stdout.write(JSON.stringify(renderOp(req)))
