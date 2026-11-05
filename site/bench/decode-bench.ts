#!/usr/bin/env node
/**
 * The site's own decoder over local parquet bytes, under Node: what a served sort's writer settings cost a read, as a
 * ratio between settings (Node isn't a Worker; its absolute times aren't a Worker's). Reads go through the reader's own
 * calls (`readGroupRaw`'s and `readChangedPages`' `parquetReadObjects`, `compressors`, `reviveRowGroup`, `chunkSpan`,
 * `parsePageIx`, `runsSlice`) with the bytes already in memory, so no I/O is timed.
 *
 *   node decode-bench.mjs -f A=a.parquet -f D=d.parquet -f E=e.parquet -m meta.json -g 500 -p 7 -o out.json
 *   node decode-bench.mjs -s published=old.groups.parquet -s new=new.groups.parquet -o out.json
 *
 * Files (`-f LABEL=PATH`, the same rows in the same groups; `dt-cloud interval-store settings-sample` writes them and
 * `-m`, the schema and version the site would read): per group, interleaved across labels so drift hits every label alike,
 * and each pass in a new order:
 * - `map`: the group whole, the plain projection (`rowColumns`), a Map read;
 * - `whole`: the group whole with the churn columns (`withChurn`), a diff read of a group not read by page;
 * - `ix`: the group's page index parsed (`parsePageIx`), where it has one;
 * - `page<R>`: R rows by page index, the diff projection, for each R of `-r` the file's page size divides.
 * The first pass warms the JIT and is not reported. A group's time is its median over the passes; per kind, the median and
 * p90 over groups, each pass's median (the spread), and the paired ratio to `-b`'s label over the same groups.
 *
 * Sidecars (`-s LABEL=PATH`): a `.groups.parquet`'s footer parsed, footer groups decoded (`IV_FOOTER_COLS`), their colo
 * doc's JSON and its parse, and `reviveRowGroup`.
 *
 * Pin it (`taskset`) on an idle machine. Bundle it for a machine with no checkout (`esbuild --bundle --platform=node --format=esm`).
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { cpus } from 'node:os'
import { parseArgs } from 'node:util'
import { type FileMetaData, parquetMetadata, parquetReadObjects } from 'hyparquet'
import { chunkSpan, IV_CHURN_COLS, IV_FOOTER_COLS, type IndexedChunk, pageIxSpans, parsePageIx, reviveRowGroup, rowColumns, runsSlice, bufferSlice } from '../functions/_lib/index.js'
import { compressors } from '../functions/_lib/zstd.js'

const { floor, min, max } = Math
const now = () => performance.now()
const err = (...a: unknown[]) => console.error(...a)

type Leaf = { name: string; type?: string }
type Meta = { schema: Leaf[]; version: number }
type RevivedChunk = IndexedChunk & { meta_data: IndexedChunk['meta_data'] & { path_in_schema: string[]; total_compressed_size: bigint } }
type Revived = { num_rows: bigint; columns: RevivedChunk[] }

interface File { label: string; buf: ArrayBuffer; footerBytes: number; groups: string[] }

function loadBytes(path: string): ArrayBuffer {
  const b = readFileSync(path)
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
}

function footerOf(buf: ArrayBuffer): { footer: ArrayBuffer; bytes: number } {
  const n = new DataView(buf).getUint32(buf.byteLength - 8, true)
  return { footer: buf.slice(buf.byteLength - n - 8), bytes: n }
}

/** A row group's compact metadata as the sidecar stores it (`interval_store.write_groups`), from the file's own footer. */
function compact(rg: FileMetaData['row_groups'][number]): string {
  const cols = rg.columns.map(c => {
    const m = c.meta_data!
    const head = [Number(m.data_page_offset), Number(m.total_compressed_size), Number(m.dictionary_page_offset ?? 0)]
    return c.offset_index_length ? [...head, Number(c.column_index_offset), c.column_index_length, Number(c.offset_index_offset), c.offset_index_length] : head
  })
  return JSON.stringify([Number(rg.num_rows), rg.columns[0].meta_data!.codec, cols])
}

function loadFile(label: string, path: string): File {
  const buf = loadBytes(path)
  const { footer, bytes } = footerOf(buf)
  const md = parquetMetadata(footer)
  return { label, buf, footerBytes: bytes, groups: md.row_groups.map(compact) }
}

const pct = (xs: number[], q: number): number => {
  const a = [...xs].sort((x, y) => x - y)
  return a.length ? a[min(a.length - 1, floor(q * a.length))] : NaN
}
const med = (xs: number[]): number => pct(xs, 0.5)
const r3 = (x: number): number => Math.round(x * 1000) / 1000

function shuffled(n: number, seed: number): number[] {
  const a = Array.from({ length: n }, (_, i) => i)
  let s = seed >>> 0
  for (let i = n - 1; i > 0; i--) {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    const j = s % (i + 1)
    ;[a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

async function benchFiles(paths: Record<string, string>, meta: Meta, nGroups: number, passes: number, rowsList: number[], baseLabel: string): Promise<object> {
  const files = Object.entries(paths).map(([l, p]) => loadFile(l, p))
  const total = files[0].groups.length
  for (const f of files) if (f.groups.length !== total) throw new Error(`${f.label}: ${f.groups.length} groups, ${files[0].label} ${total}`)
  const pick = Array.from({ length: min(nGroups, total) }, (_, i) => floor(i * total / min(nGroups, total)))
  const plain = rowColumns(meta.version, meta.schema as never)
  if (!plain) throw new Error(`version ${meta.version}: no projection`)
  const leaves = meta.schema.slice(1)
  const names = new Set(leaves.map(l => l.name))
  const diff = [...plain, ...IV_CHURN_COLS.filter(c => names.has(c))]
  err(`decode-bench: ${pick.length} of ${total} groups, ${passes} passes, labels ${files.map(f => f.label)}, map ${plain.length} columns, diff ${diff.length}`)
  // Each group's fetched bytes, as a read holds them, cut once outside any timer.
  const prep = new Map<string, { rg: Revived; start: number; data: ArrayBuffer; n: number; ixSpans: ReturnType<typeof pageIxSpans>; ixBuf: ArrayBuffer | null; pageRows: number | null }>()
  for (const f of files) {
    for (const g of pick) {
      const rg = reviveRowGroup(f.groups[g], meta.schema as never) as unknown as Revived
      const [s, e] = chunkSpan(rg as never, diff)
      const hasIx = !!rg.columns[0].offset_index_length
      const ixSpans = pageIxSpans(rg.columns, leaves as never)
      const ixBuf = hasIx ? concat(ixSpans.runs.map(r => f.buf.slice(r.start, r.end))) : null
      prep.set(`${f.label}|${g}`, { rg, start: s, data: f.buf.slice(s, e), n: Number(rg.num_rows), ixSpans, ixBuf, pageRows: null })
    }
  }
  // Samples: `${label}|${kind}` → group → per-pass ms.
  const samples = new Map<string, Map<number, number[]>>()
  const note = (label: string, kind: string, g: number, ms: number) => {
    const k = `${label}|${kind}`
    let m = samples.get(k)
    if (!m) samples.set(k, m = new Map())
    let l = m.get(g)
    if (!l) m.set(g, l = [])
    l.push(ms)
  }
  const passMed = new Map<string, number[]>()
  for (let pass = 0; pass < passes; pass++) {
    const t0 = now()
    const order = shuffled(pick.length, 2654435761 + pass)
    const inPass = new Map<string, number[]>()
    const tally = (label: string, kind: string, ms: number) => {
      const k = `${label}|${kind}`
      let l = inPass.get(k)
      if (!l) inPass.set(k, l = [])
      l.push(ms)
    }
    for (let at = 0; at < order.length; at++) {
      const g = pick[order[at]]
      const rot = (at + pass) % files.length
      for (let fi = 0; fi < files.length; fi++) {
        const f = files[(fi + rot) % files.length]
        const p = prep.get(`${f.label}|${g}`)!
        const metadata = { version: meta.version, schema: meta.schema, num_rows: p.rg.num_rows, row_groups: [p.rg], metadata_length: 0 } as unknown as FileMetaData
        const timeWhole = async (kind: string, columns: string[]) => {
          const file = bufferSlice(p.data, p.start)
          const t = now()
          const rows = await parquetReadObjects({ file, metadata, columns, compressors }) as unknown[]
          const ms = now() - t
          if (rows.length !== p.n) throw new Error(`${f.label} group ${g}: ${rows.length} rows of ${p.n}`)
          if (pass) { note(f.label, kind, g, ms); tally(f.label, kind, ms) }
          return rows as Record<string, unknown>[]
        }
        await timeWhole('map', plain)
        const whole = await timeWhole('whole', diff)
        if (!p.ixBuf) continue
        const t = now()
        const ix = parsePageIx(p.rg.columns, leaves as never, p.ixSpans.runs, p.ixBuf)
        const ixMs = now() - t
        if (pass) { note(f.label, 'ix', g, ixMs); tally(f.label, 'ix', ixMs) }
        const pageRows = Number(ix.locs.depth[1]?.first_row_index ?? p.n)
        for (const rows of rowsList) {
          if (rows % pageRows || p.n < rows) continue
          // The same rows under every label: a window of `rows` at a position fixed by the group and the pass.
          const slots = floor(p.n / rows)
          const a = ((g * 31 + pass * 17) % slots) * rows
          const pageLocationsByGroup = [Object.fromEntries(diff.map(c => [c, ix.locs[c]]))]
          const opts = { file: runsSlice([{ start: p.start, buf: p.data }]), metadata, columns: diff, compressors, pageRangesByGroup: [[[a, a + rows]]], pageLocationsByGroup }
          const t1 = now()
          const out = await parquetReadObjects(opts as Parameters<typeof parquetReadObjects>[0]) as unknown[]
          const ms = now() - t1
          if (out.length !== rows) throw new Error(`${f.label} group ${g}: page read of [${a}, ${a + rows}) returned ${out.length} rows`)
          if (!pass && (out as Record<string, unknown>[]).some((r, i) => r.path !== whole[a + i].path || r.vf !== whole[a + i].vf)) throw new Error(`${f.label} group ${g}: page read of [${a}, ${a + rows}) differs from the group's rows`)
          if (pass) { note(f.label, `page${rows}`, g, ms); tally(f.label, `page${rows}`, ms) }
        }
      }
    }
    for (const [k, l] of inPass) (passMed.get(k) ?? passMed.set(k, []).get(k)!).push(med(l))
    err(`pass ${pass}${pass ? '' : ' (warm-up, not reported)'}: ${((now() - t0) / 1000).toFixed(1)}s`)
  }
  const base = (kind: string) => samples.get(`${baseLabel}|${kind}`)
  const out: Record<string, object> = {}
  for (const [k, m] of samples) {
    const [label, kind] = k.split('|')
    const perGroup = [...m.entries()].map(([g, l]) => [g, med(l)] as [number, number])
    const ms = perGroup.map(([, v]) => v)
    const pm = passMed.get(k) ?? []
    const b = base(kind)
    const ratios = b ? perGroup.flatMap(([g, v]) => { const x = b.get(g); return x ? [v / med(x)] : [] }) : []
    out[k] = {
      label, kind, groups: ms.length, median_ms: r3(med(ms)), p90_ms: r3(pct(ms, 0.9)),
      pass_medians_ms: pm.map(r3), pass_spread: pm.length ? r3((max(...pm) - min(...pm)) / med(pm)) : null,
      ...(ratios.length ? { ratio_to_base: { median: r3(med(ratios)), p10: r3(pct(ratios, 0.1)), p90: r3(pct(ratios, 0.9)) } } : {}),
    }
  }
  return {
    env: { node: process.version, v8: process.versions.v8, cpu: cpus()[0]?.model, cpus: cpus().length }, base: baseLabel, picked: pick.length, of: total, passes,
    footers: Object.fromEntries(files.map(f => [f.label, f.footerBytes])), bytes: Object.fromEntries(files.map(f => [f.label, f.buf.byteLength])),
    kinds: out,
  }
}

function concat(parts: ArrayBuffer[]): ArrayBuffer {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0))
  let at = 0
  for (const p of parts) {
    out.set(new Uint8Array(p), at)
    at += p.byteLength
  }
  return out.buffer
}

async function benchSidecar(path: string, passes: number, nGroups: number): Promise<object> {
  const buf = loadBytes(path)
  const { footer, bytes } = footerOf(buf)
  const footerMs: number[] = []
  let md = parquetMetadata(footer)
  for (let i = 0; i < passes + 1; i++) {
    const t = now()
    md = parquetMetadata(footer)
    if (i) footerMs.push(now() - t)
  }
  const kv = new Map((md.key_value_metadata ?? []).map(e => [e.key, e.value]))
  const schema = JSON.parse(kv.get('schema') ?? '[]') as Leaf[]
  const n = md.row_groups.length
  const picks = Array.from({ length: min(nGroups, n) }, (_, i) => floor(i * n / min(nGroups, n)))
  const decodeMs: number[] = []
  const parseMs: number[] = []
  const docBytes: number[] = []
  const reviveUs: number[] = []
  const fgRows: number[] = []
  const fgBytes: number[] = []
  const rgJsonChars: number[] = []
  const heldBytes: number[] = []
  for (let pass = 0; pass <= passes; pass++) {
    for (const i of picks) {
      const rg = md.row_groups[i]
      const metadata = { ...md, row_groups: [rg], num_rows: rg.num_rows }
      const t = now()
      const rows = await parquetReadObjects({ file: { byteLength: buf.byteLength, slice: async (s: number, e?: number) => buf.slice(s, e) }, metadata, columns: IV_FOOTER_COLS, compressors }) as Record<string, unknown>[]
      const dec = now() - t
      const doc = Object.fromEntries(IV_FOOTER_COLS.map(c => [c, rows.map(r => r[c] == null ? null : typeof r[c] === 'string' ? r[c] : Number(r[c]))]))
      const text = JSON.stringify(doc)
      const t2 = now()
      JSON.parse(text)
      const par = now() - t2
      if (!pass) continue
      decodeMs.push(dec)
      parseMs.push(par)
      docBytes.push(text.length)
      fgRows.push(rows.length)
      fgBytes.push(rg.columns.reduce((m, c) => m + Number(c.meta_data!.total_compressed_size), 0))
      const js = rows.map(r => r.rg_json as string)
      rgJsonChars.push(...js.map(j => j.length))
      // `jsonBytes` + `groupsBytes`: what a footer group charges the isolate's footer LRU.
      heldBytes.push(js.reduce((m, j) => m + 32 + 2 * j.length, 0) + rows.reduce((m, r) => m + 96 + 2 * (String(r.p_min).length + String(r.p_max).length), 0))
      const t3 = now()
      for (const j of js) reviveRowGroup(j, schema as never)
      reviveUs.push((now() - t3) * 1000 / js.length)
    }
  }
  return {
    file_bytes: buf.byteLength, footer_bytes: bytes, row_groups: n, footer_parse_ms: r3(med(footerMs)),
    fg_rows_median: med(fgRows), fg_compressed_bytes_median: med(fgBytes), fg_decode_ms: { median: r3(med(decodeMs)), p90: r3(pct(decodeMs, 0.9)) }, fg_doc_json_bytes_median: med(docBytes),
    fg_doc_parse_ms: { median: r3(med(parseMs)), p90: r3(pct(parseMs, 0.9)) }, rg_json_chars_median: med(rgJsonChars), revive_us_median: r3(med(reviveUs)), fg_lru_charge_bytes_median: med(heldBytes),
  }
}

const pairs = (l: string[] | undefined): Record<string, string> => Object.fromEntries((l ?? []).map(s => {
  const at = s.indexOf('=')
  if (at < 1) throw new Error(`${s}: expected LABEL=PATH`)
  return [s.slice(0, at), s.slice(at + 1)]
}))

async function main(): Promise<void> {
  const { values: v } = parseArgs({
    options: {
      base: { type: 'string', short: 'b' },
      file: { type: 'string', short: 'f', multiple: true },
      groups: { type: 'string', short: 'g', default: '500' },
      meta: { type: 'string', short: 'm' },
      out: { type: 'string', short: 'o' },
      passes: { type: 'string', short: 'p', default: '7' },
      rows: { type: 'string', short: 'r', default: '256,1024' },
      sidecar: { type: 'string', short: 's', multiple: true },
    },
  })
  const passes = Number(v.passes)
  const report: Record<string, unknown> = {}
  const files = pairs(v.file)
  if (Object.keys(files).length) {
    if (!v.meta) throw new Error('-f needs -m')
    const meta = JSON.parse(readFileSync(v.meta, 'utf8')) as Meta
    report.files = await benchFiles(files, meta, Number(v.groups), passes, v.rows!.split(',').map(Number), v.base ?? Object.keys(files)[0])
  }
  const cars = pairs(v.sidecar)
  if (Object.keys(cars).length) {
    const got: Record<string, object> = {}
    for (const [l, p] of Object.entries(cars)) got[l] = await benchSidecar(p, passes, Number(v.groups))
    report.sidecars = got
  }
  const text = JSON.stringify(report, null, 1)
  if (v.out) writeFileSync(v.out, `${text}\n`)
  else console.log(text)
}

await main()
