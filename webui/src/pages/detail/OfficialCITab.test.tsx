import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { Attainment, CIComparison } from "@/api/types"

import { OfficialCITab } from "./OfficialCITab"

// Every fixture below was checked against `AttainmentResult` in
// `src/sag/metrics/attainment.py`, which is the model the API serves: a shape
// that model rejects is a shape this tab can never be handed. The five
// evaluated shapes that occur in the 681 `verdict.json` files under `logs/` are
// met (171), invalid/modules (85), invalid/conclusion (50), partial (34) and
// partial/conclusion (2); `unavailable` (108) and `no_matched_cell` (47) carry
// no attainment at all.

function attainment(overrides: Partial<Attainment> = {}): Attainment {
  return {
    verdict: "met",
    cell_id: "Apache Jenkins commons-dbutils Linux JDK 17 #455",
    cell_grade: "A",
    valid: true,
    target_usable: true,
    clean: true,
    clean_form: "ids",
    built: true,
    alpha: { numerator: 523, denominator: 523 },
    alpha_test: { numerator: 523, denominator: 523 },
    alpha_build: { numerator: 1, denominator: 1 },
    executed_observed: 523,
    executed_target: 523,
    red_observed: 0,
    red_target: 0,
    modules_matched: 1,
    modules_target: 1,
    missing_module_ids: [],
    build_form: "modules",
    modules_basis: "log",
    unmatched_observed_module_ids: [],
    lifecycle_parity: {
      status: "equivalent",
      form: "maven_phases",
      ci_command: "mvn -B -f pom.xml -V clean test",
      sag_commands: ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
      ci_reach: "test",
      sag_reach: "test",
      missing: [],
      extra: [],
    },
    unexpected_red_ids: [],
    reason_codes: [],
    ...overrides,
  }
}

function comparison(overrides: Partial<CIComparison> = {}): CIComparison {
  return {
    schema_version: 1,
    status: "evaluated",
    run_id: "r",
    repo: "apache/commons-dbutils",
    target_sha: "e1e2d9edfab8f237aab9e733c6d61e23b35d2079",
    attainment: attainment(),
    receipt_ids: ["inv-maven-1-17c8a2e62d8a-0001"],
    // In all 435 real payloads that carry a parity block, `commands` is exactly
    // `lifecycle_parity.sag_commands` — the run's own launcher, absolute path
    // and all. `acceptance_command` is the CI side.
    commands: ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
    acceptance_command: "mvn -B -f pom.xml -V clean test",
    test_identity_basis: "jenkins_single_module:commons-dbutils",
    reasons: [],
    ...overrides,
  }
}

describe("OfficialCITab", () => {
  afterEach(() => {
    cleanup()
  })

  it("names the job it compared against", () => {
    render(<OfficialCITab comparison={comparison()} />)
    expect(screen.getByText(/Apache Jenkins commons-dbutils Linux JDK 17 #455/)).toBeInTheDocument()
    expect(screen.getByText(/apache\/commons-dbutils/)).toBeInTheDocument()
    expect(screen.getByText("e1e2d9e")).toHaveAttribute(
      "title",
      "e1e2d9edfab8f237aab9e733c6d61e23b35d2079",
    )
  })

  // `acceptance_command` and `lifecycle_parity.ci_command` differ on 161 of the
  // 435 real payloads that carry both — CI's `deploy` rewritten to `install`,
  // most often. Calling them both "CI command" would put one name on two
  // different strings on one screen.
  it("does not call the command this run had to match the CI command", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          acceptance_command: "./mvnw -B -U -V clean install",
          attainment: attainment({
            lifecycle_parity: {
              status: "equivalent",
              form: "maven_phases",
              ci_command: "./mvnw -B -U -V clean deploy",
              sag_commands: ["/workspace/x/mvnw -B -U -V clean install"],
              ci_reach: "deploy",
              sag_reach: "install",
              missing: [],
              extra: [],
            },
          }),
        })}
      />,
    )
    const target = screen.getByRole("region", { name: /what was compared/i })
    expect(within(target).getByText("Command this run had to match")).toBeInTheDocument()
    expect(within(target).getByText("./mvnw -B -U -V clean install")).toBeInTheDocument()
    expect(within(target).queryByText("./mvnw -B -U -V clean deploy")).not.toBeInTheDocument()
    const lifecycle = screen.getByRole("region", { name: /commands/i })
    expect(within(lifecycle).getByText("./mvnw -B -U -V clean deploy")).toBeInTheDocument()
  })

  it("says what each fraction is measured against", () => {
    render(<OfficialCITab comparison={comparison()} />)
    expect(screen.getByText(/523 of CI's 523 tests ran here/)).toBeInTheDocument()
    expect(screen.getByText(/1 of CI's 1 module matched/)).toBeInTheDocument()
    expect(screen.getByText(/the CI job log/)).toBeInTheDocument()
    expect(screen.getByText(/Overall 523\/523/)).toBeInTheDocument()
  })

  it("shows the two commands side by side", () => {
    render(<OfficialCITab comparison={comparison()} />)
    const lifecycle = screen.getByRole("region", { name: /commands/i })
    expect(within(lifecycle).getByText("mvn -B -f pom.xml -V clean test")).toBeInTheDocument()
    expect(
      within(lifecycle).getByText("/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"),
    ).toBeInTheDocument()
    expect(
      within(lifecycle).getByText("The two commands cover the same steps"),
    ).toBeInTheDocument()
  })

  // `status: "unknown"` on 216 of the 264 real payloads that carry a parity
  // block: whether the two commands match was never worked out. It reads as a
  // sentence about the comparison, not as the bare word.
  it("says so when the two commands were never compared to each other", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            lifecycle_parity: {
              status: "unknown",
              form: "none",
              ci_command: "mvn -B -f pom.xml -V clean test",
              sag_commands: ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
              ci_reach: null,
              sag_reach: null,
              missing: [],
              extra: [],
            },
          }),
        })}
      />,
    )
    expect(
      screen.getByText("Whether the two commands cover the same steps was not worked out"),
    ).toBeInTheDocument()
  })

  // A lifecycle that differs is worth surfacing on a run whose verdict is met —
  // that is exactly the case a reader would otherwise miss. Making the verdict
  // `partial` here would be an unconstructible payload: `_validate_claim`
  // rejects a partial verdict alongside the counts of a met run.
  it("names the phases a narrower run skipped", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            lifecycle_parity: {
              status: "not_equivalent",
              form: "maven_phases",
              ci_command: "mvn -V verify",
              sag_commands: ["/opt/apache-maven-3.9.16/bin/mvn -V test"],
              ci_reach: "verify",
              sag_reach: "test",
              missing: ["package", "verify"],
              extra: [],
            },
          }),
        })}
      />,
    )
    expect(screen.getByText(/package, verify/)).toBeInTheDocument()
  })

  it("lists tests that fail here and pass in CI", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "not_met",
            clean: false,
            red_observed: 2,
            unexpected_red_ids: ["a.BTest#one", "a.BTest#two"],
            reason_codes: ["NEW_RED_BEYOND_TARGET"],
          }),
        })}
      />,
    )
    expect(screen.getByText("a.BTest#one")).toBeInTheDocument()
    expect(screen.getByText("a.BTest#two")).toBeInTheDocument()
    expect(
      screen.getByRole("region", { name: "Tests that failed here and pass in CI" }),
    ).toBeInTheDocument()
    // The code's own sentence appears once, in Findings, and the section title
    // does not restate it.
    expect(screen.getByText(/tests failed here that pass in CI/)).toBeInTheDocument()
    // The record's word, spelled without its underscore.
    expect(screen.getByText("not met")).toBeInTheDocument()
  })

  it("caps a long list of failing tests and says how many it left out", () => {
    const ids = Array.from({ length: 27 }, (_, i) => `a.BTest#case${i}`)
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "not_met",
            clean: false,
            red_observed: 27,
            unexpected_red_ids: ids,
            reason_codes: ["NEW_RED_BEYOND_TARGET"],
          }),
        })}
      />,
    )
    expect(screen.getByText("a.BTest#case24")).toBeInTheDocument()
    expect(screen.queryByText("a.BTest#case25")).not.toBeInTheDocument()
    expect(screen.getByText("and 2 more")).toBeInTheDocument()
  })

  // The 34-run shape: alpha and alpha_test null, alpha_build real. The tab says
  // the score is absent and nothing about why — the cause, when the payload
  // states one, is in the findings below.
  it("states a missing overall score without naming a cause for it", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "partial",
            cell_grade: "B",
            clean_form: "counts",
            alpha: null,
            alpha_test: null,
            executed_observed: 985,
            executed_target: 0,
            reason_codes: ["TARGET_TEST_UNIVERSE_EMPTY", "CLEAN_BY_COUNTS_ONLY"],
          }),
        })}
      />,
    )
    expect(screen.getByText("No overall score")).toBeInTheDocument()
    expect(screen.getByText("No test count was compared")).toBeInTheDocument()
    expect(screen.getByText(/1 of CI's 1 module matched/)).toBeInTheDocument()
    expect(screen.getByText(/no test identities to compare against/)).toBeInTheDocument()
    // No sentence on screen invents a reason the payload declined to give.
    expect(screen.queryByText(/recorded no module list/)).not.toBeInTheDocument()
  })

  // The 85-run shape, the largest non-met case in the corpus: all three
  // fractions null. Each one says so on its own line.
  it("states every fraction as absent when none was computed", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({
            verdict: "invalid",
            cell_id: "maven-compile (ubuntu-latest, JDK-8)",
            cell_grade: "B",
            valid: false,
            built: false,
            clean: false,
            clean_form: "counts",
            alpha: null,
            alpha_test: null,
            alpha_build: null,
            executed_observed: 1328,
            executed_target: 0,
            red_observed: 1,
            modules_matched: 2,
            modules_target: 4,
            missing_module_ids: ["rocketmq-broker 5.5.1", "rocketmq-store 5.5.1"],
            lifecycle_parity: null,
            reason_codes: ["CERTIFICATE_AUTHORITY_UNAVAILABLE", "TARGET_TEST_UNIVERSE_EMPTY"],
          }),
          acceptance_command: null,
        })}
      />,
    )
    expect(screen.getByText("No overall score")).toBeInTheDocument()
    expect(screen.getByText("No test count was compared")).toBeInTheDocument()
    expect(screen.getByText("No module count was compared")).toBeInTheDocument()
    expect(screen.getByText("No commands were compared")).toBeInTheDocument()
    expect(screen.getByText("Not recorded")).toBeInTheDocument()
    expect(
      screen.getByText(/this run's own results were not bound to a receipt/),
    ).toBeInTheDocument()
  })

  // The commonest `unavailable` shape, 100 of 108: no commit, no attainment, no
  // acceptance command. Three of the four Target rows have nothing to show and
  // each says so rather than going blank.
  it("states the commit and the CI job as absent when the run recorded neither", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          status: "unavailable",
          target_sha: null,
          attainment: null,
          acceptance_command: null,
          receipt_ids: [],
          commands: [],
          test_identity_basis: null,
          reasons: ["accepted_execution_plan_unavailable"],
        })}
      />,
    )
    expect(screen.getByText(/apache\/commons-dbutils/)).toBeInTheDocument()
    expect(screen.getAllByText("Not recorded")).toHaveLength(3)
    expect(screen.getByText(/the accepted build plan was not recorded/)).toBeInTheDocument()
  })

  it("explains itself when nothing was compared", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          status: "no_matched_cell",
          attainment: null,
          reasons: ["official_ci_cell_not_matched"],
        })}
      />,
    )
    expect(screen.getByText(/was not compared/)).toBeInTheDocument()
    expect(
      screen.getAllByText(/no CI job on this commit matches the run's JDK and OS/).length,
    ).toBeGreaterThan(0)
  })

  // On an evaluated comparison the snapshot's own `reasons` list is a superset
  // of `attainment.reason_codes` — 127 of the 342 real evaluated comparisons
  // carry statements that live only there. Dropping them loses the certificate
  // blockers, which is most of what explains a comparison that went nowhere.
  it("keeps the statements the payload carries outside the attainment", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({ reason_codes: ["CLEAN_BY_COUNTS_ONLY"] }),
          reasons: ["CLEAN_BY_COUNTS_ONLY", "TEST_EXECUTION_NOT_COMPLETE"],
        })}
      />,
    )
    expect(screen.getAllByText("CLEAN_BY_COUNTS_ONLY")).toHaveLength(1)
    expect(screen.getByText("TEST_EXECUTION_NOT_COMPLETE")).toBeInTheDocument()
  })

  it("shows a code that has no sentence rather than dropping it", () => {
    render(
      <OfficialCITab
        comparison={comparison({
          attainment: attainment({ reason_codes: ["BUILD_MODULE_SCOPE_UNAVAILABLE"] }),
        })}
      />,
    )
    expect(screen.getByText("BUILD_MODULE_SCOPE_UNAVAILABLE")).toBeInTheDocument()
  })

  it("writes none of the retired words in its own copy", () => {
    const { container } = render(<OfficialCITab comparison={comparison()} />)
    const text = container.textContent ?? ""
    // Guard both directions: a tab that rendered nothing would pass the
    // absence assertions below for the wrong reason.
    expect(text).toContain("Repository")
    expect(text).toContain("523 of CI's 523 tests ran here")
    expect(text.length).toBeGreaterThan(300)
    const retired = [
      "sealed",
      "canonical",
      "claimed",
      "quarantined",
      "subject",
      "snapshot",
      "metrics-v2",
      "verdict-bearing",
      "promoting",
    ]
    expect(retired).toHaveLength(9)
    for (const word of retired) {
      expect(text.toLowerCase()).not.toContain(word)
    }
  })
})
