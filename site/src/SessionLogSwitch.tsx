import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { isNoEnd, lastDay, type SwitchState } from '../functions/_lib/sessionLogShape'

// `/admin`'s session-log switch (specs/session-log.md): on through a UTC date (default 7 days, at most 31), on with no
// end date (specs/done/activity-feed.md), or off. With a date it ends by itself after it; every change is recorded in
// `admin_edits`.

const ENDPOINT = '/api/session-log/switch'
const KEY = ['session-log', 'switch'] as const

const day = (epochS: number): string => new Date(epochS * 1000).toISOString().slice(0, 10)

/** The switch's one-line status. */
export function switchStatus(s: SwitchState): string {
  const by = s.who && s.ts !== null ? ` Set by ${s.who} on ${day(s.ts)}.` : ''
  if (s.reason === 'not set up') return 'Not set up on this deployment (no session_log tables): apply the session-log migration.'
  if (s.enabled && isNoEnd(s.until)) return `On, no end date.${by}`
  if (s.enabled && s.until !== null) return `On through ${lastDay(s.until)} (UTC).${by}`
  if (s.reason === 'expired' && s.until !== null) return `Off: ended after ${lastDay(s.until)} (UTC).${by}`
  return `Off.${by}`
}

export interface SwitchViewProps {
  state: SwitchState
  /** Today's UTC date: the earliest a new end may be. */
  today: string
  /** The date field's value (`YYYY-MM-DD`), or `none` (no end date). */
  date: string
  onDate: (d: string) => void
  /** Turn on through `date` (also: change the date while on), or off. */
  onSet: (enabled: boolean) => void
  busy: boolean
  error: string | null
}

/** The switch's end as the date field holds it: `none`, or its last UTC day. */
export const untilField = (until: number): string => isNoEnd(until) ? 'none' : lastDay(until)

/** The switch, as markup (pure: `SessionLogSwitch` wires it to the API). */
export function SessionLogSwitchView({ state, today, date, onDate, onSet, busy, error }: SwitchViewProps) {
  const setUp = state.reason !== 'not set up'
  const noEnd = date === 'none'
  const changed = state.enabled && state.until !== null && date !== untilField(state.until)
  return (
    <section className="session-log-switch">
      <h2>Session log</h2>
      <p>
        Records what viewers do (page views, clicks on controls, the path filter, requests and errors), to replay a
        reported bug (<Link to="/admin/sessions">/admin/sessions</Link>) and for the activity feed
        (<Link to="/admin/activity">/admin/activity</Link>). While it's on, <Link to="/privacy">/privacy</Link> says
        what is recorded. It turns itself off after the chosen day, unless it has no end date.
      </p>
      <p className="status">{switchStatus(state)}</p>
      {setUp && (
        <form onSubmit={e => { e.preventDefault(); onSet(true) }}>
          <label className="switch">
            <input type="checkbox" role="switch" checked={state.enabled} disabled={busy} onChange={e => onSet(e.target.checked)} />
            {' '}Logging
          </label>
          <label>
            {' '}through{' '}
            <input type="date" value={noEnd ? '' : date} min={today} max={lastDay(state.maxUntil)} disabled={busy || noEnd} onChange={e => onDate(e.target.value)} />
            {' '}(UTC)
          </label>
          <label>
            {' '}<input type="checkbox" checked={noEnd} disabled={busy} onChange={e => onDate(e.target.checked ? 'none' : lastDay(state.defaultUntil))} />
            {' '}no end date
          </label>
          {changed && <button type="submit" disabled={busy}>Set date</button>}
        </form>
      )}
      {error && <p className="err">{error}</p>}
    </section>
  )
}

async function call(init?: RequestInit): Promise<SwitchState> {
  const r = await fetch(ENDPOINT, { credentials: 'include', cache: 'no-store', ...init })
  const body = await r.json().catch(() => null) as (SwitchState & { error?: string }) | null
  if (!r.ok) throw new Error(body?.error ?? `session-log switch: ${r.status}`)
  return body as SwitchState
}

export function SessionLogSwitch() {
  const qc = useQueryClient()
  const q = useQuery<SwitchState, Error>({ queryKey: KEY, queryFn: () => call() })
  const [draft, setDraft] = useState<string | null>(null)
  const set = useMutation<SwitchState, Error, { enabled: boolean; until: string }>({
    mutationFn: ({ enabled, until }) => call({
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(enabled ? { enabled, until } : { enabled }),
    }),
    onSuccess: s => { qc.setQueryData(KEY, s); setDraft(null) },
  })
  if (q.error) return <section className="session-log-switch"><h2>Session log</h2><p className="err">{q.error.message}</p></section>
  if (!q.data) return null
  const s = q.data
  const date = draft ?? untilField(s.enabled && s.until !== null ? s.until : s.defaultUntil)
  return (
    <SessionLogSwitchView
      state={s}
      today={new Date().toISOString().slice(0, 10)}
      date={date}
      onDate={setDraft}
      onSet={enabled => set.mutate({ enabled, until: date })}
      busy={set.isPending}
      error={set.error?.message ?? null}
    />
  )
}
