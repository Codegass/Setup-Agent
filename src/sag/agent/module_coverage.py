"""Island/module coverage: ONE computation, two consumers.

The verdict finalizer folds these conflicts at evidence-close; the phase gates
render the checklist MID-RUN so the agent can see what remains (live 2026-07-18
probes: one bigtop run gave up with islands unattempted because nothing named
them; another fixated on a broken island for 7 calls while three healthy ones
sat untouched). Both consumers call the same function — if the in-run guidance
and the sealed verdict computed coverage differently, they would eventually
disagree, which is the split-brain this campaign just cured.

Python projects are exempt (July rule: packages-as-modules is future work).
Never raises: coverage is guidance and honesty, not a failure mode.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from loguru import logger

from sag.runtime.container_io import command_did_not_run

# Denominator authority, in order (Plan 8 spec §3.5). p7d polaris graded a
# build against a denominator NOTHING derived — the survey could not read
# `settings.gradle.kts`, so "100% of expected classes" was 100% of zero — while
# the scan that had walked 26 subprojects only decorated the sentence. Which
# computation owns the denominator is now a stated ladder, and the reason names
# the rung it stood on.
BASIS_RECEIPT = "receipt"
BASIS_SCAN = "scan"
BASIS_SURVEY = "survey"


@dataclass(frozen=True)
class ModuleBasis:
    """Which computation set the coverage denominator, and what it stated.

    ``built`` is stated only by the scan rung: a receipt states which modules the
    build ATTEMPTED (the class-weighted coverage is measured against those,
    unchanged since #17/d5dc330), and the survey states an expectation list, not
    a module tally. ``total`` on the receipt rung is the number of MODULES the
    denominator holds, counted on `module_key` exactly as the denominator is
    keyed (`_module_count`) — not the number of labels the receipts printed.
    """

    authority: str
    provenance: str
    total: int = 0
    built: int = 0

    @property
    def states_a_shortfall(self) -> bool:
        """True only when the rung that OWNS the denominator counted a
        minority. A scan under a receipt-stated denominator counts modules the
        build never tried, which is untried, not unbuilt.

        "Owns" means the denominator the run actually measured against — the
        caller passes the scoping outcome's own answer. When the narrowing was
        refused, the authority is this scan even though receipts exist, and the
        cap fires: that is what makes "a passing coverage verdict beside a
        minority module scan" unconstructible (spec §6 acceptance 1).
        """
        return self.authority == BASIS_SCAN and self.built < self.total

    def phrase(self) -> str:
        if self.authority == BASIS_RECEIPT:
            # Named by id only when ONE promoted receipt stated exactly this
            # module set (§3.6); otherwise the receipts collectively, which is
            # still the receipt rung and says so.
            stated_by = (
                f"receipt {self.provenance}"
                if self.provenance
                else "the receipts' module outcomes"
            )
            return f"denominator: {stated_by} ({self.total} module(s) attempted)"
        if self.authority == BASIS_SCAN:
            return (
                f"denominator: the module scan on disk "
                f"({self.built}/{self.total} modules built)"
            )
        return "denominator: the survey's expectations"


def _receipt_that_stated(structure: Mapping[str, Any] | None, modules: Sequence[str]) -> str:
    """The receipt id to NAME for this denominator, or "" when none stated it.

    The denominator is the union of every receipt's `module_outcomes`, while the
    promoted structure fact (§3.6) is ONE receipt's statement. Naming that id
    over a union it never made attributes a denominator to a receipt that did
    not claim it — a Category-3 falsehood in one word. So the id is named only
    when that receipt stated exactly the module set being counted; otherwise the
    sentence credits the receipts collectively, which is true and is still the
    receipt rung.
    """
    from sag.agent.receipt_structure import module_key

    provenance = str((structure or {}).get("provenance") or "").strip()
    if not provenance:
        return ""
    stated = {module_key(name) for name in (structure or {}).get("modules") or ()} - {""}
    counted = {module_key(name) for name in modules} - {""}
    return provenance if stated and stated == counted else ""


def _module_count(modules: Sequence[str]) -> int:
    """How many MODULES a label list names, counted the way the denominator is.

    The denominator is keyed on `module_key` — that is why `Apache Camel :: Core`
    can match the directory `core` at all — so counting labels instead of keys
    tells the model more modules were attempted than the denominator contains
    (two receipts spelling one module two ways, a reactor summary and a Gradle
    task list naming the same subproject). A label that normalizes to nothing is
    still a label the build printed and stays counted as itself; deduplicating on
    a key that does not exist would be a guess.
    """
    from sag.agent.receipt_structure import module_key

    counted = set()
    for name in modules:
        key = module_key(name)
        counted.add(key if key else f"\0{name}")
    return len(counted)


def module_basis(
    coverage: dict[str, Any] | None,
    *,
    denominator_modules: Sequence[str] | None = None,
    structure: Mapping[str, Any] | None = None,
) -> ModuleBasis:
    """The ladder: a terminal receipt, else the scan on disk, else the survey.

    ``denominator_modules`` is the modules that ACTUALLY SET this pass's
    denominator — the answer the scoping outcome already computed
    (`_ExpectationScope.denominator_modules`), not "the modules some receipt
    mentioned". The distinction is the whole of round three's blocker: a receipt
    naming `build-logic` against a lone `/build/libs` expectation cannot be
    mapped, so scoping keeps the survey's wide list and records
    `build_coverage_scope_unverified` — that receipt set no denominator, and
    reading authority off its mere existence disarmed the §3.5 scan cap in the
    exact p7d polaris state. Deciding it here a second time is the parallel
    computation P3 forbids, so this function decides nothing: it reports.

    ``structure`` is the promoted receipt-proven structure, consulted only to
    name the receipt that stated this denominator.
    """
    modules = tuple(str(name) for name in (denominator_modules or ()) if str(name).strip())
    if modules:
        return ModuleBasis(
            BASIS_RECEIPT,
            _receipt_that_stated(structure, modules),
            total=_module_count(modules),
        )
    summary = (coverage or {}).get("summary") or {}
    total = int(summary.get("modules_total") or 0)
    # The scan earns the denominator by enumerating a STRUCTURE the expectation
    # walk did not have — polaris's 26 subprojects against a survey that parsed
    # none. A one-module scan is not a structure: it is the same module the
    # expectation check already measured, with a coarser instrument (a
    # directory probe, not a source-weighted class count). Letting it decide
    # there would re-create the split-brain P3 exists to prevent, pointing the
    # other way.
    if total > 1:
        return ModuleBasis(
            BASIS_SCAN,
            "the module scan on disk",
            total=total,
            built=int(summary.get("modules_built") or 0),
        )
    return ModuleBasis(BASIS_SURVEY, "the survey's expectations")


def shared_module_scan(validator, project_name) -> dict[str, Any] | None:
    """The ONE scan of this gate pass (spec §3.5 / P3).

    ``validate_build_status`` performs the scan and holds its result; the
    checklist reads that same object rather than walking the tree a second
    time. A validator without the hook (fakes, older callers) falls back to
    scanning here, which is exactly the pre-Plan-8 behaviour.
    """
    method = getattr(validator, "module_scan", None)
    if callable(method):
        try:
            return method(project_name)
        except Exception as exc:  # pragma: no cover - guidance never raises
            logger.debug(f"shared module scan unavailable: {exc}")
            return None
    return module_coverage(validator, project_name)


def _record_richness(module: dict[str, Any]) -> int:
    return (
        int(module.get("class_count") or 0)
        + int(module.get("jar_count") or 0)
        + len(module.get("report_dirs") or [])
        + (1 if module.get("has_test_sources") else 0)
    )


def module_coverage(validator, project_name) -> dict[str, Any] | None:
    """Scan both JVM systems, merge per path (richer record wins — the July
    mixed-layout semantics), parse per-module test reports, and roll up.

    Returns ``{"summary": ..., "modules": ..., "project_dir": ...}`` or None
    for non-JVM projects / unavailable validators.
    """
    if validator is None:
        return None
    try:
        from sag.tools.module_metrics import assemble_module_metrics

        project_path = str(getattr(validator, "project_path", "/workspace") or "/workspace")
        project_dir = f"{project_path}/{project_name}" if project_name else project_path
        # §6.8 fence 3 / §3.8: one canary probe tells "could not look" apart
        # from "not a JVM tree". Without it, a transient read failure made
        # every detection probe "fail", the scan returned None exactly as a
        # Python project does, and the checklist — the model's only way to
        # discover it under-built — vanished without a trace.
        orchestrator = getattr(validator, "docker_orchestrator", None)
        if orchestrator is not None:
            try:
                canary = orchestrator.execute_command(f"test -d {shlex.quote(project_dir)}")
            except Exception:
                return {"unreadable": True, "project_dir": project_dir}
            if command_did_not_run(canary):
                return {"unreadable": True, "project_dir": project_dir}
        primary = str(validator._detect_build_system(project_dir) or "").strip().lower()
        if primary not in ("maven", "gradle"):
            return None

        merged: dict[str, dict[str, Any]] = {}
        for system in ("maven", "gradle"):
            try:
                modules = validator.scan_modules(project_dir, system) or []
            except Exception:
                continue
            for module in modules:
                if not isinstance(module, dict):
                    continue
                path = str(module.get("path") or ".")
                current = merged.get(path)
                if current is None or _record_richness(module) > _record_richness(current):
                    merged[path] = module
        if not merged:
            return None

        tests: dict[str, Any] = {}
        for path, module in merged.items():
            report_dirs = module.get("report_dirs") or []
            if not report_dirs:
                continue
            module_dir = f"{project_dir}/{path}" if path != "." else project_dir
            try:
                parsed = validator.parse_module_test_reports(module_dir, report_dirs)
            except Exception:
                parsed = {}
            if parsed:
                tests[path] = parsed

        metrics = assemble_module_metrics(
            modules=list(merged.values()),
            reactor_status={},
            tests=tests,
            build_systems=[primary],
            build_error_samples={},
            generated_at="coverage",
        )
        return {
            "summary": metrics.get("module_summary") or {},
            "modules": metrics.get("modules") or [],
            "project_dir": project_dir,
        }
    except Exception as exc:
        logger.debug(f"module coverage unavailable: {exc}")
        return None


def _island_checklist_line(
    coverage: dict[str, Any],
    islands: list[dict[str, Any]],
    *,
    limit: int = 6,
) -> str | None:
    """Key the checklist to the recommended islands: an island counts as built
    when any scanned module under its root has build output."""
    project_dir = str(coverage.get("project_dir") or "").rstrip("/")
    modules = coverage.get("modules") or []
    built_paths = [
        str(m.get("path") or "") for m in modules if m.get("build_status") == "success"
    ]

    def _island_rel(root: str) -> str:
        root = root.rstrip("/")
        if project_dir and root.startswith(project_dir):
            return root[len(project_dir):].strip("/") or "."
        return root

    built_islands: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for island in islands:
        root = str(island.get("root") or "")
        if not root:
            continue
        rel = _island_rel(root)
        covered = any(
            path == rel or path.startswith(f"{rel}/") for path in built_paths if path
        )
        (built_islands if covered else remaining).append(island)

    total = len(built_islands) + len(remaining)
    if not total:
        return None
    line = f"Recommended islands: {len(built_islands)}/{total} built"
    if remaining:
        items = "; ".join(
            (
                f"{isl.get('system') or 'build'} '{isl['goal']}' in {isl['root']}"
                if isl.get("goal")
                else f"{isl.get('system') or 'build'} in {isl['root']}"
            )
            for isl in remaining[:limit]
        )
        line += f" · remaining: {items}"
    return line


def coverage_conflicts(coverage: dict[str, Any] | None) -> tuple[str, ...]:
    """The two July coverage caps, from a coverage rollup."""
    if not coverage:
        return ()
    if coverage.get("unreadable"):
        # §6.8 fence 3, sealed at the verdict too: the finalizer folds these
        # at evidence-close, and an inability to read must reach the verdict
        # as a conflict, not vanish as an empty rollup.
        return ("module_scan_unreadable",)
    summary = coverage.get("summary") or {}
    conflicts: list[str] = []
    total = int(summary.get("modules_total") or 0)
    built = int(summary.get("modules_built") or 0)
    if total and built < total:
        conflicts.append("build_modules_incomplete")
    bearing = int(summary.get("modules_test_bearing") or 0)
    tested = summary.get("modules_tested")
    if bearing and tested is not None and 0 < int(tested) < bearing:
        conflicts.append("reactor_scope_narrowed")
    return tuple(conflicts)


def coverage_checklist_line(
    coverage: dict[str, Any] | None,
    *,
    islands: list[dict[str, Any]] | None = None,
    limit: int = 6,
) -> str | None:
    """A one-line, agent-facing checklist: what built, what has no output yet.

    The agent's window is seven steps; a ratio alone ("1/4") tells it nothing
    actionable. With ISLANDS known, key the line to them — full root AND goal
    per remaining island (live bigtop 2026-07-18: the raw 15-module basename
    dump was half noise with no coordinates, and the agent kept building at
    the root for 86 calls). Without islands, fall back to module names.
    """
    if not coverage:
        return None
    if coverage.get("unreadable"):
        # §3.8 fence b: a failed read caps and SAYS SO — never "no unbuilt
        # modules", and never a silently missing line.
        return "module scan could not read the tree — coverage unknown"
    if islands and len(islands) > 1:
        island_line = _island_checklist_line(coverage, islands, limit=limit)
        if island_line:
            return island_line
    modules = coverage.get("modules") or []
    summary = coverage.get("summary") or {}
    total = int(summary.get("modules_total") or 0)
    if not total:
        return None
    built = [str(m.get("path")) for m in modules if m.get("build_status") == "success"]
    unbuilt = [str(m.get("path")) for m in modules if m.get("build_status") != "success"]

    def _names(paths: list[str]) -> str:
        shown = [p.rsplit("/", 1)[-1] or p for p in paths[:limit]]
        suffix = f" +{len(paths) - limit} more" if len(paths) > limit else ""
        return ", ".join(shown) + suffix

    line = f"Module coverage: {len(built)}/{total} built"
    if built:
        line += f" [{_names(built)}]"
    if unbuilt:
        line += f" · no output yet: [{_names(unbuilt)}]"
    tested = summary.get("modules_tested")
    bearing = summary.get("modules_test_bearing")
    if bearing:
        line += f" · tests ran in {int(tested or 0)}/{int(bearing)} test-bearing modules"
    return line
