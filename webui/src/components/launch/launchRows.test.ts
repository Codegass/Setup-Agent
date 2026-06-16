import { describe, expect, it } from "vitest"

import { emptyLaunchRow, parsePastedRepoLines } from "./launchRows"

describe("emptyLaunchRow", () => {
  it("creates a blank row with record and coverage off", () => {
    expect(emptyLaunchRow()).toEqual({
      repoUrl: "",
      name: "",
      ref: "",
      goal: "",
      record: false,
      coverage: false,
    })
  })
})

describe("parsePastedRepoLines", () => {
  it("parses one repo url per line", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git\nhttps://github.com/apache/dubbo.git",
    )

    expect(parsed).toEqual([
      { repoUrl: "https://github.com/apache/commons-cli.git", ref: "" },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "" },
    ])
  })

  it("parses the quick repo_url ref format", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0\n" +
        "https://github.com/apache/dubbo.git dubbo-3.2.19",
    )

    expect(parsed).toEqual([
      {
        repoUrl: "https://github.com/apache/commons-cli.git",
        ref: "rel/commons-cli-1.11.0",
      },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "dubbo-3.2.19" },
    ])
  })

  it("ignores blank lines and trims whitespace", () => {
    const parsed = parsePastedRepoLines(
      "\n  https://github.com/a/b.git   v1.0  \r\n\n",
    )

    expect(parsed).toEqual([{ repoUrl: "https://github.com/a/b.git", ref: "v1.0" }])
  })
})
