// The DAG view's model (src/runDag.ts) over Nextflow 26's own `-with-dag` output for job/nf/scan.nf.
import { describe, expect, it } from 'vitest'
import { dagLayers, dagNodes, parseDag } from './runDag'
import type { ScanRunPhase } from './scanRunsModel'

// `nextflow run job/nf/scan.nf -preview -with-dag` over cw's profile (bucket-check + index-sync folded)
const MERMAID = `flowchart TB
    subgraph " "
    v0["channel.empty"]
    v1["channel.fromList"]
    end
    v2(["list_source"])
    v6(["index"])
    v7(["static_names"])
    v8(["publish_r2"])
    v9(["interval_store"])
    v10(["index_rows"])
    v14(["post"])
    subgraph " "
    v15[" "]
    end
    v3(( ))
    v11(( ))
    v0 --> v3
    v1 --> v2
    v2 --> v3
    v3 --> v6
    v6 --> v7
    v6 --> v8
    v6 --> v10
    v7 --> v11
    v8 --> v9
    v9 --> v11
    v10 --> v11
    v11 --> v14
    v14 --> v15
`

const ph = (phase: string, seq: number, task: string, start: number, end: number | null, o: Partial<ScanRunPhase> = {}): ScanRunPhase => ({
  run_id: 'r', phase, seq, started_ts: start, finished_ts: end, status: end == null ? 'running' : 'done', note: null, task, ...o,
})

describe('parseDag', () => {
  it('keeps the processes and joins them through channels and operators', () => {
    expect(parseDag(MERMAID)).toEqual({
      nodes: ['list_source', 'index', 'static_names', 'publish_r2', 'interval_store', 'index_rows', 'post'],
      edges: [
        ['list_source', 'index'],
        ['index', 'index_rows'], ['index', 'publish_r2'], ['index', 'static_names'],
        ['static_names', 'post'],
        ['publish_r2', 'interval_store'],
        ['interval_store', 'post'],
        ['index_rows', 'post'],
      ],
    })
  })
  it('lays each process out at its longest path from a source', () => {
    expect(Object.fromEntries(dagLayers(parseDag(MERMAID)))).toEqual({
      list_source: 0, index: 1, static_names: 2, publish_r2: 2, interval_store: 3, index_rows: 2, post: 4,
    })
  })
})

describe('dagNodes', () => {
  const phases = [
    ph('list b1', 0, 'list_source (s3://b1)', 100, 400, { job_name: 'nf-1', machine: 'n2-standard-16' }),
    ph('import b1', 1, 'list_source (s3://b1)', 400, 500, { job_name: 'nf-1', machine: 'n2-standard-16' }),
    ph('bucket-check', 2, 'list_source (s3://b2)', 110, 115, { job_name: 'nf-2' }),
    ph('list b2', 3, 'list_source (s3://b2)', 115, 200, { job_name: 'nf-2' }),
    ph('layer-2', 4, 'index', 600, 620),
    ph('index-write', 5, 'index', 620, 900),
    ph('static-names', 6, 'static_names', 950, null),
    ph('legacy', 7, '', 0, 10),
  ]
  it('joins each process to its tasks\' phases', () => {
    const nodes = dagNodes(parseDag(MERMAID), phases, true)
    expect(nodes.map(n => [n.name, n.layer, n.row, n.status, n.start, n.end, n.tasks.map(t => [t.tag, t.job_name, t.phases, t.status])])).toEqual([
      ['list_source', 0, 0, 'done', 100, 500, [['s3://b1', 'nf-1', ['list b1', 'import b1'], 'done'], ['s3://b2', 'nf-2', ['bucket-check', 'list b2'], 'done']]],
      ['index', 1, 0, 'done', 600, 900, [['', null, ['layer-2', 'index-write'], 'done']]],
      ['static_names', 2, 0, 'running', 950, null, [['', null, ['static-names'], 'running']]],
      ['publish_r2', 2, 1, 'pending', null, null, []],
      ['interval_store', 3, 0, 'pending', null, null, []],
      ['index_rows', 2, 2, 'pending', null, null, []],
      ['post', 4, 0, 'pending', null, null, []],
    ])
  })
  it('calls a finished run\'s phaseless process unrecorded, and a failed task fails its process', () => {
    const failed = [...phases.slice(0, 6), ph('static-names', 6, 'static_names', 950, 990, { status: 'failed' })]
    expect(dagNodes(parseDag(MERMAID), failed, false).map(n => [n.name, n.status])).toEqual([
      ['list_source', 'done'], ['index', 'done'], ['static_names', 'failed'], ['publish_r2', 'unrecorded'],
      ['interval_store', 'unrecorded'], ['index_rows', 'unrecorded'], ['post', 'unrecorded'],
    ])
  })
})
