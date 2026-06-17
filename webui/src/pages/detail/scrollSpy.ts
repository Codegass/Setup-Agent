import { useEffect, useRef, useState } from "react"

export interface SectionPosition {
  id: string
  top: number
}

/** Pick the section whose top (minus a sticky-header offset) is the last one at/above the scroll position. */
export function pickActiveSection(
  positions: SectionPosition[],
  scrollTop: number,
  offset: number,
): string | null {
  if (positions.length === 0) {
    return null
  }
  let active = positions[0].id
  for (const pos of positions) {
    if (pos.top - offset <= scrollTop) {
      active = pos.id
    }
  }
  return active
}

/**
 * Tracks which facet section is in view as the container scrolls.
 * Returns the active id plus a `jump(id)` that smooth-scrolls a section into view.
 * `offset` accounts for the sticky header + nav height.
 *
 * `sessionId` keys the reset: switching sessions scrolls back to the top and
 * resets the active highlight (the facet ids are identical across sessions, so
 * the id set cannot be used as the reset signal).
 */
export function useScrollSpy(
  ids: string[],
  sessionId: string,
  options?: { offset?: number; initialFacet?: string },
) {
  // Default offset must cover a jumped section's scroll-mt so the section you
  // jumped to is the one the next recompute marks active (see DetailPane's
  // section scroll-mt). A too-small offset lags the active pill by a section.
  const offset = options?.offset ?? 150
  const initialFacet = options?.initialFacet
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [active, setActive] = useState<string | null>(ids[0] ?? null)

  function jump(id: string) {
    setActive(id)
    const el = document.getElementById(`facet-${id}`)
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" })
  }

  // Reset / position on session switch (and on first mount). When an initialFacet
  // is requested (deep-link), jump to it; otherwise scroll back to the top.
  useEffect(() => {
    if (initialFacet && ids.includes(initialFacet)) {
      jump(initialFacet)
      return
    }
    setActive(ids[0] ?? null)
    if (containerRef.current) {
      containerRef.current.scrollTop = 0
    }
    // Keyed on sessionId only: the facet ids are identical across sessions, so a
    // same-content id array on re-render must not retrigger this reset.
  }, [sessionId])

  function recompute() {
    const container = containerRef.current
    if (!container) {
      return
    }
    // Use getBoundingClientRect deltas so section tops are measured relative to
    // the scroll container regardless of which element is the offsetParent.
    // (The scroll container is not guaranteed to be a positioning context, so
    // el.offsetTop would be measured against <body> and mismatch scrollTop.)
    const containerTop = container.getBoundingClientRect().top
    const scrollTop = container.scrollTop
    const positions: SectionPosition[] = ids.map((id) => {
      const el = document.getElementById(`facet-${id}`)
      const top = el ? el.getBoundingClientRect().top - containerTop + scrollTop : 0
      return { id, top }
    })
    setActive(pickActiveSection(positions, scrollTop, offset))
  }

  return { containerRef, active, onScroll: recompute, jump }
}
