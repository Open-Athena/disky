// One compact row under the map's sections: the drilled node's storage-class mix as a stacked bar (its bytes and
// list-price $/mo per class in the tooltip), and the ownership share with its explainer behind an ⓘ. Both follow
// the map's drill; both hide under a name filter (`filterBlind.ts`).
import { useEffect, type ReactNode } from 'react'
import { useStore } from './store'
import { ClassMixTip, Tooltip } from './Tooltip'
import { CLASS_COLORS, fmtUsd, userBytes, type TreeNode } from './types'
import { classSegments, fracLabel } from './classStrip'

/** The drilled node's class mix (`classMix`) for `scope` (the path it names). */
export function ClassStrip({ mix, scope }: { mix: Record<string, number>; scope: ReactNode }) {
  const { segs, usd } = classSegments(mix)
  if (!segs.length) return null
  const tip = (
    <>
      <div className="classes-scope">Storage classes under <code>{scope}</code></div>
      <ClassMixTip mix={mix} note="List prices (US regions) × scanned bytes; follows the map's drill. Actual spend depends on negotiated rates and credits." />
    </>
  )
  return (
    <span className="fact classes-strip" id="storage-classes">
      <span className="lbl">classes</span>
      <Tooltip content={tip} pinnable>
        <span className="classbar" tabIndex={0} role="img" aria-label={segs.map(s => `${s.name} ${fracLabel(s.frac)}`).join(', ')}>
          {segs.map(s => <i key={s.c} style={{ flexGrow: s.frac, background: CLASS_COLORS[s.c] ?? 'var(--other)' }} />)}
        </span>
      </Tooltip>
      <span className="classes-legend">
        {segs.map(s => (
          <span key={s.c} className="li"><i className="sw" style={{ background: CLASS_COLORS[s.c] ?? 'var(--other)' }} />{s.name} <span className="pct">{fracLabel(s.frac)}</span></span>
        ))}
      </span>
      <span className="usd">≈ {fmtUsd(usd)}/mo</span>
    </span>
  )
}

/** How owners are decided, and whom to ask: the explainer that used to be the "Ownership" section. */
export function OwnershipHelp() {
  const { contact } = useStore()
  return (
    <div className="ownership-help">
      <p>
        An owner comes from one of two places. An <b>assignment</b> — someone assigning a prefix to a person
        (themselves or anyone else) from the map, the CLI, or the API — is the source of truth and always wins.
        Everything unassigned falls back to <b>inferred</b> ownership (W&amp;B run configs, <code>.executor_info</code>{' '}
        sidecars, provenance records, and a short list of manual prefix rules): “this user's runs wrote these bytes” — a
        starting point for finding your data, not a bill.
      </p>
      <p>
        Ownership has one axis: a person, or nobody. Bytes without an owner show as <i>unowned</i> (gray) until someone
        assigns them — shared corpora and infra included, because a deletion needs a person to sign off.
      </p>
      {contact && (contact.email || contact.chat) && (
        <p className="feedback">
          Questions, a wrong owner, access for a teammate:{' '}
          {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
          {contact.email && contact.chat && ' or '}
          {contact.chat && <a href={contact.chat.href} target="_blank" rel="noreferrer">{contact.chat.label}</a>}.
        </p>
      )}
    </div>
  )
}

/** The share of `tree`'s bytes that have an owner, with the explainer (pin the ⓘ to click its links). */
export function OwnershipFact({ tree }: { tree: TreeNode }) {
  // deep-linkable: the row mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])
  const pct = tree.b ? ((100 * userBytes(tree)) / tree.b).toFixed(1) : '0.0'
  return (
    <span className="fact ownership-fact" id="attribution">
      <span className="lbl">ownership</span>
      <span><b>{pct}%</b> of bytes have an owner</span>
      <Tooltip content={<OwnershipHelp />} pinnable>
        <span className="info" tabIndex={0} aria-label="how ownership works">ⓘ</span>
      </Tooltip>
    </span>
  )
}
