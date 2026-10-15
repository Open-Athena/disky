import { type ReactNode, useMemo } from 'react'
import { FederatedScanPicker } from './FederatedScanPicker'
import type { PickerSource } from './federatedPickerModel'
import { cutoffOfScan, scanAtCutoff, sourcesBefore } from './unionSources'

/** The page's scan picker on a union view: `FederatedScanPicker` over the members' observations, speaking scan
 *  ids (`?d=`) at its edges. A picked cutoff reads as the newest observation through it among the page's own
 *  scans (`scanAtCutoff`); `before` (a scan id) limits the cutoffs to those strictly earlier (a diff's start).
 *  A `value` no member observes (the union's list not yet refreshed) shows `fallback`. */
export function UnionScanPicker({ sources, value, scans, onChange, label, before, compact, fallback }: {
  sources: readonly PickerSource[]
  value: string
  /** The page's scan list: what a `?d=` can name. */
  scans: readonly string[]
  onChange: (id: string) => void
  label: string
  before?: string | null
  compact?: boolean
  fallback?: ReactNode
}) {
  const limit = before ? cutoffOfScan(sources, before) : null
  const shown = useMemo(() => (limit ? sourcesBefore(sources, limit) : [...sources]), [sources, limit])
  const pickable = useMemo(() => new Set(scans), [scans])
  const at = cutoffOfScan(sources, value)
  if (!at) return <>{fallback}</>
  return (
    <FederatedScanPicker sources={shown} value={at} label={label} compact={compact}
      onChange={t => { const id = scanAtCutoff(shown, t, pickable); if (id && id !== value) onChange(id) }} />
  )
}
