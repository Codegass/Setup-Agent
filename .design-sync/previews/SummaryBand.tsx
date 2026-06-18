import { SummaryBand } from "sag-workbench"
import { detail } from "./_fixtures"

export const Partial = () => (
  <div style={{ width: 720 }}>
    <SummaryBand detail={detail} />
  </div>
)

export const Blocked = () => (
  <div style={{ width: 720 }}>
    <SummaryBand
      detail={{
        ...detail,
        status: "failed",
        outcome: "✖ FAILED — build broke before tests could run",
        build: { ...detail.build, state: "failure", time: "", note: "mvn -B verify" },
        test: { ...detail.test, state: "none", pass: 0, fail: 0, skip: 0, total: 0, passRate: null },
        evidenceStatus: "partial",
        report: "—",
        blocker: {
          code: "TOOLCHAIN_MISMATCH",
          title: "JDK version mismatch",
          detail:
            "The reactor requires JDK 17 but the container resolved JDK 11; acme-core failed to compile during the build phase.",
          hint: "Pin JDK 17 via .sdkmanrc (Temurin) and re-run mvn -B -T1C verify.",
        },
      }}
    />
  </div>
)
