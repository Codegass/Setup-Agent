/**
 * The cadences the workbench follows a live run at.
 *
 * They live here rather than in `App.tsx` because more than one view follows
 * the same run: the dashboard poll and the timeline's `since=` poll are the
 * same heartbeat, and two copies of the number would drift the first time one
 * of them was tuned.
 */

/** Dashboard, launch queue, and the trajectory timeline's delta poll. */
export const DASHBOARD_POLL_MS = 5000

/** The open session's detail, which moves faster than the dashboard. */
export const SESSION_DETAIL_POLL_MS = 3000
