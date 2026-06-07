export interface LaunchRowDraft {
  repoUrl: string
  name: string
  ref: string
  goal: string
  record: boolean
}

export function emptyLaunchRow(): LaunchRowDraft {
  return { repoUrl: "", name: "", ref: "", goal: "", record: false }
}

/**
 * Parse multi-line paste input. Supported quick format per line:
 *   repo_url
 *   repo_url ref
 */
export function parsePastedRepoLines(
  text: string,
): Array<{ repoUrl: string; ref: string }> {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [repoUrl, ref = ""] = line.split(/\s+/)
      return { repoUrl, ref }
    })
}
