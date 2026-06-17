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
 */
export function useScrollSpy(ids: string[], offset = 170) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [active, setActive] = useState<string | null>(ids[0] ?? null)

  // Reset to the first section when the set of ids changes (e.g. session switch).
  useEffect(() => {
    setActive(ids[0] ?? null)
    if (containerRef.current) {
      containerRef.current.scrollTop = 0
    }
  }, [ids.join("|")])

  function recompute() {
    const container = containerRef.current
    if (!container) {
      return
    }
    const positions: SectionPosition[] = ids.map((id) => {
      const el = document.getElementById(`facet-${id}`)
      return { id, top: el ? el.offsetTop : 0 }
    })
    setActive(pickActiveSection(positions, container.scrollTop, offset))
  }

  function jump(id: string) {
    setActive(id)
    const el = document.getElementById(`facet-${id}`)
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" })
  }

  return { containerRef, active, onScroll: recompute, jump }
}
