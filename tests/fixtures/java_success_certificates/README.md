# Five-project Java success metrics (D2R6 projection)

This report applies the typed Java success-certificate evaluator to five
existing D2R6 runs. It is a **legacy projection**, not a live canonical
certificate: the historical runs predate stable runtime obligation-set hashes
and direct sealed-plan-to-contract-to-receipt binding. The projection therefore
shows what the new standard would conclude without changing the existing
canonical verdict.

The checked-in input is the compact projection snapshot. The accompanying
`d2r6-five-project-source-checksums.json` binds each selected fact to the raw
D2R6 plan, receipt, metrics, verdict, and (for Ignite) candidate-surface file.
Those raw logs are intentionally gitignored: a clean clone can reproduce the
certificate JSON, while an independent raw-evidence re-audit additionally
needs the matching local artifacts.

## Decision readout

| Project | Certificate result | Build plan | Native build units | Test plan | Product-test targets | Receipt-scoped test observations | Interpretation |
|---|---|---:|---:|---:|---:|---:|---|
| Tomcat migration | `repository_green` (projected) | 1/1 success | 1/1 success | 1/1 success | 1/1 success | 52 executed, 52 passed | Repository-default build and tests are green in the reviewed legacy evidence. |
| Commons DBCP | `repository_product_test_green` (projected) | 1/1 success | 1/1 success | 1/1 success | 1/1 success | 1,605 executed, 1,596 passed, 9 skipped | Product tests are green; the broader default `clean verify` quality gate was not executed, so this is not repository-default green. |
| Ignite | `incomplete` | 1/1 success | 41/41 success | 1/1 terminal | 0/28 candidate modules closed; 1 unexpected quality-only observation | 1 diagnostic observation passed | Build is proven for the observed reactor. The 28 modules are a harness-discovered candidate surface, not a sealed model-plan denominator; `modules/checkstyle` cannot stand in for product tests. |
| Jackrabbit | `incomplete` | 1/1 success | 23/23 success | 1/1 terminal | 0/1 closed | 0 executed | Build is proven, but the terminal test receipt produced an unexpected empty result and did not close the selected JCR test target. |
| Cassandra Java driver | `build_failed` (projected) | 0 success, 1 failed, 0 missing | 3 success, 1 failed, 4 missing (4/8 closed) | 0/1 closed; 1 unexpected profile | target universe unavailable | Alternate JDK 8 diagnostic run: 4,590 executed, 4,438 passed, 89 errors, 63 skipped | The exact repository-root build failed. A narrow 2/2 retry is a different scope. The planned JDK 17 test was not executed; the JDK 8 results are diagnostic and cannot close it. |

## Metric contract

The decision uses five separately typed identity sets:

1. build plan steps;
2. native build units (Maven reactor modules or one Maven project);
3. test plan steps;
4. product-test targets;
5. evidence bindings.

Every set reports `required`, `closed`, `satisfied`, `failed`, `missing`, and
`unexpected`. `closure_fraction` records exact terminal coverage
(`satisfied + failed`) over the required identities, while
`success_fraction` records only successful identities. Fractions are never
rounded into percentages, so a partial set cannot display as 100%. A failed
obligation can therefore be fully closed without being successful.

Runtime test executions, static test declarations, source files, and compiled
classes are retained only as separate-grain diagnostics. They never share a
denominator and never close a plan step or module target. A green result also
requires a closed declared scope, receipt-bound non-empty test outcomes, and a
complete durable-evidence set.

## Reproduction

```bash
UV_CACHE_DIR=/tmp/setup-agent-java-cert-uv-cache uv run python \
  scripts/evaluate_java_success_certificates.py \
  tests/fixtures/java_success_certificates/d2r6-five-project-inputs.json \
  --output tests/fixtures/java_success_certificates/d2r6-five-project-certificates.json
```

The generated JSON SHA-256 is
`442c4052f538549f2579e8693283f7f5b40752ebdac322415a491d885e09ea34`.
The source-checksum snapshot SHA-256 is
`cbd4200664f5304a883c687dfaf5beffbe113cea54f697e7f5d3395d9b342a50`.
