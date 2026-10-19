import type { SectionMode } from 'use-prms/sections'

/** Where a page's section (`useSectionSpy`) lives in the URL. One module constant: the spy re-resolves its mode, and
 * re-binds its scroll listener, whenever this object's identity changes. */
export const SECTION_MODE: SectionMode = { kind: 'fragment' }
