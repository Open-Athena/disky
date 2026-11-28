/**
 * The Slack payloads a digest post or edit sends, built as the job's thrds `SlackClient.post` / `.edit` (the version
 * `uv.lock` pins) build them, so a Worker's converge writes exactly what the job would (specs/slack-in-workers.md;
 * goldens: `./digestWire.test.ts` over `cloud/tests/fixtures/digest/wire/payloads.json`, captured from thrds by
 * `cloud/tests/test_digest_wire_parity.py`):
 *
 * - markdown → Slack mrkdwn (`thrds.mrkdwn.to_slack`): `![:name:](…)` → `:name:`, `[text](url)` → `<url|text>`,
 *   `*italic*` → `_italic_`, `**bold**` → `*bold*`, in that order;
 * - the trailing run of `![alt](url)` lines lifted into `image` blocks under the body's `section` blocks
 *   (`split_trailing_images`, `section_blocks`), `{bust}` adding a `thrds_bust` token to the URL;
 * - a body over 4,000 characters with no image refused, as thrds refuses it;
 * - an edit with no image clears the message's blocks (`blocks: []`), so a plot dropped from the body leaves Slack too;
 * - unfurls off; the sender (name, then `icon_url` over `icon_emoji`) on a post only (Slack fixes it at post time).
 *
 * Pure.
 */

export const SLACK_MESSAGE_LIMIT = 4000
export const SLACK_SECTION_LIMIT = 3000
export const SLACK_BLOCKS_LIMIT = 50

/** Python's `\w` (Unicode letters, digits, `_`) for the emphasis guards. */
const W = String.raw`\p{L}\p{N}_`
const MD_EMOJI_IMG = /!\[:([a-z0-9_+-]+):\]\([^)]+\)/g
const MD_LINK = /\[([^\]]+)\]\(([^)]+)\)/g
const MD_ITALIC = new RegExp(String.raw`(?<![*${W}])\*(?!\*)([^*\s\n](?:[^*\n]*[^*\s\n])?)\*(?![*${W}])`, 'gu')
const MD_BOLD = /\*\*([^*]+)\*\*/g

/** `thrds.mrkdwn.to_slack`. */
export function toSlack(text: string): string {
  return text
    .replace(MD_EMOJI_IMG, ':$1:')
    .replace(MD_LINK, '<$2|$1>')
    .replace(MD_ITALIC, '_$1_')
    .replace(MD_BOLD, '*$1*')
}

const IMAGE_LINE = /^!\[([^\]\n]*)\]\(([^)\s]+)\)(\{bust\})?[ \t]*$/
const EMOJI_ALT = /^:[a-z0-9_+-]+:$/

export interface ImageRef { alt: string; url: string; bust: boolean }

/** A line that is only `![alt](url)` (optionally `{bust}`), not a custom-emoji image (`thrds.imageblock.parse_image_line`). */
export function parseImageLine(line: string): ImageRef | null {
  const m = IMAGE_LINE.exec(line)
  if (!m || EMOJI_ALT.test(m[1])) return null
  return { alt: m[1], url: m[2], bust: m[3] !== undefined }
}

/** The body and the trailing run of image lines (blank lines among them allowed), lifted (`split_trailing_images`). */
export function splitTrailingImages(content: string): { body: string; images: ImageRef[] } {
  const lines = content.split('\n')
  const refs: ImageRef[] = []
  let i = lines.length
  while (i > 0) {
    const line = lines[i - 1]
    if (!line.trim()) { i -= 1; continue }
    const ref = parseImageLine(line)
    if (!ref) break
    refs.push(ref)
    i -= 1
  }
  if (!refs.length) return { body: content, images: [] }
  return { body: lines.slice(0, i).join('\n').replace(/\n+$/, ''), images: refs.reverse() }
}

/** Characters as Python counts them (code points). */
const len = (s: string): number => [...s].length

export interface Block { type: string; [k: string]: unknown }

/** `thrds.slack.section_blocks` (paragraphs packed into sections of at most 3,000 characters; one over it is refused). */
export function sectionBlocks(text: string): Block[] {
  const chunks: string[] = []
  for (const para of text.split('\n\n')) {
    if (len(para) > SLACK_SECTION_LIMIT) {
      throw new Error(`Message body has a paragraph over Slack's ${SLACK_SECTION_LIMIT}-char section limit (${len(para)} chars); break it with a blank line or shorten it`)
    }
    if (chunks.length && len(chunks[chunks.length - 1]) + 2 + len(para) <= SLACK_SECTION_LIMIT) chunks[chunks.length - 1] += `\n\n${para}`
    else chunks.push(para)
  }
  return chunks.map(c => ({ type: 'section', text: { type: 'mrkdwn', text: c } }))
}

export const BUST_PARAM = 'thrds_bust'

/** Python's `quote_plus`: unreserved characters kept, a space as `+`, everything else percent-encoded (UTF-8). */
const quotePlus = (s: string): string =>
  encodeURIComponent(s).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`).replace(/%20/g, '+')

/** ``url`` with `?thrds_bust=<token>` (any earlier one replaced): `thrds.imageblock.bust_url`, its query re-encoded as
 *  Python's `urlencode(parse_qsl(query))` does. */
export function bustUrl(url: string, token: string): string {
  const m = /^([^?#]*)(?:\?([^#]*))?(#.*)?$/.exec(url)!
  const params: [string, string][] = []
  for (const part of (m[2] ?? '').split('&')) {
    if (!part) continue
    const at = part.indexOf('=')
    const [k, v] = at < 0 ? [part, ''] : [part.slice(0, at), part.slice(at + 1)]
    const dec = (x: string) => decodeURIComponent(x.replace(/\+/g, ' '))
    if (dec(k) !== BUST_PARAM) params.push([dec(k), dec(v)])
  }
  params.push([BUST_PARAM, token])
  return `${m[1]}?${params.map(([k, v]) => `${quotePlus(k)}=${quotePlus(v)}`).join('&')}${m[3] ?? ''}`
}

/** thrds's cache-bust token: the Unix minute. */
export const bustToken = (now = Date.now()): string => String(Math.floor(now / 60_000))

const imageBlock = (ref: ImageRef, token: string): Block =>
  ({ type: 'image', image_url: ref.bust ? bustUrl(ref.url, token) : ref.url, alt_text: ref.alt })

/** The body's wire text and (with images) its blocks. */
function wire(content: string, token: string): { text: string; blocks?: Block[] } {
  const { body, images } = splitTrailingImages(content)
  if (!images.length && len(content) > SLACK_MESSAGE_LIMIT) {
    throw new Error(`Message exceeds Slack's ${SLACK_MESSAGE_LIMIT} char limit (${len(content)} chars)`)
  }
  const text = toSlack(body)
  if (!images.length) return { text }
  if (!text.trim()) throw new Error('Message is only image lines; Slack needs body text alongside image blocks — add at least one line')
  const blocks = [...sectionBlocks(text), ...images.map(r => imageBlock(r, token))]
  if (blocks.length > SLACK_BLOCKS_LIMIT) throw new Error(`Message has ${blocks.length} blocks, over Slack's ${SLACK_BLOCKS_LIMIT}`)
  return { text, blocks }
}

export interface Sender { username?: string | null; icon_url?: string | null; icon_emoji?: string | null }

export interface PostOptions {
  /** The thread's parent; absent: a top-level message. */
  threadTs?: string
  sender?: Sender
  /** Also in the channel (`reply_broadcast`); a reply only. */
  broadcast?: boolean
  metadata?: { event_type: string; event_payload: Record<string, unknown> }
  bust?: string
}

/** A `chat.postMessage` body for ``content`` (markdown) in ``channel``. */
export function postPayload(channel: string, content: string, o: PostOptions = {}): Record<string, unknown> {
  if (o.broadcast && o.threadTs === undefined) throw new Error('broadcast needs a thread: Slack applies reply_broadcast to a reply only')
  const w = wire(content, o.bust ?? bustToken())
  const data: Record<string, unknown> = { channel, text: w.text, unfurl_links: false, unfurl_media: false }
  const s = o.sender ?? {}
  if (s.username != null) data.username = s.username
  if (s.icon_url != null) data.icon_url = s.icon_url
  else if (s.icon_emoji != null) data.icon_emoji = s.icon_emoji
  if (w.blocks) data.blocks = w.blocks
  if (o.threadTs !== undefined) {
    data.thread_ts = o.threadTs
    if (o.broadcast) data.reply_broadcast = true
  }
  if (o.metadata) data.metadata = o.metadata
  return data
}

/** A `chat.update` body setting message ``ts`` in ``channel`` to ``content`` (markdown); no image clears its blocks. */
export function editPayload(channel: string, ts: string, content: string, o: { bust?: string } = {}): Record<string, unknown> {
  const w = wire(content, o.bust ?? bustToken())
  return { channel, ts, text: w.text, unfurl_links: false, unfurl_media: false, blocks: w.blocks ?? [] }
}
