// A scan run's pipeline DAG (specs/pipelines-everywhere.md "Run reports"): the head's Nextflow `-with-dag` Mermaid,
// reduced to its processes, laid out in columns, and each process joined to the phases its tasks recorded
// (`scan_run_phases.task` = `<process> (<tag>)`, from the task's `$NF_PROCESS` / `$NF_TAG`).
import { phaseStatus, type ScanRunPhase } from './scanRunsModel'

export interface Dag { nodes: string[]; edges: [string, string][] }

// `v2(["list_source"])` is a process; `v0["channel.fromList"]`, `v3(( ))` (an operator) and `v15[" "]` are not.
const NODE = /^\s*(v\d+)\s*(\(\[|\(\(|\[|\()\s*"?(.*?)"?\s*(\]\)|\)\)|\]|\))\s*$/
const EDGE = /^\s*(v\d+)\s*-->\s*(?:\|[^|]*\|\s*)?(v\d+)\s*$/

/** The processes and the edges between them: a path through channels and operators is one edge. */
export function parseDag(mermaid: string): Dag {
  const proc = new Map<string, string>()
  const out = new Map<string, string[]>()
  for (const line of mermaid.split('\n')) {
    const n = NODE.exec(line)
    if (n) {
      if (n[2] === '([') proc.set(n[1], n[3])
      continue
    }
    const e = EDGE.exec(line)
    if (e) out.set(e[1], [...(out.get(e[1]) ?? []), e[2]])
  }
  const edges: [string, string][] = []
  for (const [id, name] of proc) {
    const seen = new Set<string>(), next = [...(out.get(id) ?? [])], to = new Set<string>()
    while (next.length) {
      const v = next.shift()!
      if (seen.has(v)) continue
      seen.add(v)
      if (proc.has(v)) to.add(proc.get(v)!)
      else next.push(...(out.get(v) ?? []))
    }
    for (const t of [...to].sort()) edges.push([name, t])
  }
  return { nodes: [...proc.values()], edges }
}

/** Each process's column: its longest path from a source (a process with no inputs). */
export function dagLayers(dag: Dag): Map<string, number> {
  const ins = new Map(dag.nodes.map(n => [n, dag.edges.filter(([, t]) => t === n).map(([f]) => f)]))
  const layer = new Map<string, number>()
  const at = (n: string, stack: Set<string>): number => {
    if (layer.has(n)) return layer.get(n)!
    if (stack.has(n)) return 0  // a cycle: Nextflow's DAGs have none
    stack.add(n)
    const l = Math.max(-1, ...(ins.get(n) ?? []).map(f => at(f, stack))) + 1
    stack.delete(n)
    layer.set(n, l)
    return l
  }
  for (const n of dag.nodes) at(n, new Set())
  return layer
}

export type NodeStatus = ScanRunPhase['status'] | 'pending' | 'unrecorded'

/** One task of a process (a fan-out's member, or the process's only task) and the phases it recorded. */
export interface DagTask { task: string; tag: string; job_name: string | null; machine: string | null; phases: string[]; status: ScanRunPhase['status']; start: number | null; end: number | null }

export interface DagNode { name: string; layer: number; row: number; status: NodeStatus; tasks: DagTask[]; start: number | null; end: number | null }

const processOf = (task: string): string => task.replace(/ \(.*\)$/, '')
const tagOf = (task: string): string => /\((.*)\)$/.exec(task)?.[1] ?? ''

function worst(ss: readonly ScanRunPhase['status'][]): ScanRunPhase['status'] {
  return ss.includes('failed') ? 'failed' : ss.includes('running') ? 'running' : 'done'
}

/** The DAG's processes with their tasks' phases and a status: no phases yet is `pending` while the run runs, else
 * `unrecorded` (a task that recorded nothing, or a run before phases named their task). */
export function dagNodes(dag: Dag, phases: readonly ScanRunPhase[], running: boolean): DagNode[] {
  const layers = dagLayers(dag)
  const rows = new Map<number, number>()
  return dag.nodes.map(name => {
    const by = new Map<string, DagTask>()
    for (const p of [...phases].sort((a, b) => (a.started_ts ?? Infinity) - (b.started_ts ?? Infinity) || a.seq - b.seq)) {
      if (!p.task || processOf(p.task) !== name) continue
      const t = by.get(p.task) ?? { task: p.task, tag: tagOf(p.task), job_name: p.job_name ?? null, machine: p.machine ?? null, phases: [], status: 'done' as const, start: null, end: null }
      t.phases.push(p.phase)
      t.status = worst([t.status, phaseStatus(p)])
      if (p.started_ts != null) t.start = t.start == null ? p.started_ts : Math.min(t.start, p.started_ts)
      if (p.finished_ts != null) t.end = t.end == null ? p.finished_ts : Math.max(t.end, p.finished_ts)
      by.set(p.task, t)
    }
    const tasks = [...by.values()]
    const layer = layers.get(name) ?? 0
    const row = rows.get(layer) ?? 0
    rows.set(layer, row + 1)
    const starts = tasks.flatMap(t => t.start ?? []), ends = tasks.flatMap(t => t.end ?? [])
    return {
      name, layer, row, tasks,
      status: tasks.length ? worst(tasks.map(t => t.status)) : running ? 'pending' : 'unrecorded',
      start: starts.length ? Math.min(...starts) : null,
      end: ends.length && !tasks.some(t => t.status === 'running') ? Math.max(...ends) : null,
    }
  })
}
