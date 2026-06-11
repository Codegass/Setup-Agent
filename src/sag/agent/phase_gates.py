"""Evidence gates at phase boundaries (spec §3.1, rule set §2.4/§2.5).

Every gate: fails OPEN on probe errors; suggestions enumerate options with
evidence; only maven/gradle builds are judged on .class/JAR presence
(the round-3 over-block lesson)."""

from typing import Any, Dict, List, Optional

from loguru import logger


def _verdict(ok: bool, reason: str = "", suggestions: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"ok": ok, "reason": reason, "suggestions": suggestions or []}


def check_phase_done(phase: str, validator, orchestrator, project_name: Optional[str]) -> Dict[str, Any]:
    try:
        if phase == "provision":
            return _check_provision(orchestrator, project_name)
        if phase == "build":
            return _check_build(validator, project_name)
        if phase == "test":
            return _check_test(validator, project_name)
        if phase == "report":
            return _check_report(orchestrator)
        return _verdict(True)  # analyze: advisory, never traps
    except Exception as exc:
        logger.warning(f"Phase gate '{phase}' failed open (probe error): {exc}")
        return _verdict(True)


def _check_provision(orchestrator, project_name) -> Dict[str, Any]:
    workdir = f"/workspace/{project_name}" if project_name else "/workspace"
    probe = orchestrator.execute_command(
        f"test -d {workdir} && echo exists || echo missing", workdir=None, timeout=30
    )
    if "exists" not in (probe.get("output") or ""):
        return _verdict(
            False,
            f"workspace {workdir} does not exist — repository not cloned",
            [
                "Clone first: project(action='clone', repo_url=...)",
                "If the repo cloned elsewhere, verify with bash ls /workspace",
            ],
        )
    return _verdict(True)


def _check_build(validator, project_name) -> Dict[str, Any]:
    status = validator.validate_build_status(project_name)
    system = (status.get("evidence") or {}).get("build_system")
    if system not in ("maven", "gradle"):
        return _verdict(True)  # non-JVM builds have no .class/JAR evidence
    if not status.get("success"):
        reason = status.get("reason") or "no build artifacts found"
        return _verdict(
            False,
            f"build claims done but physical evidence is missing ({reason})",
            [
                "Run build(action='compile') and re-claim with phase(action='done')",
                "If this project genuinely cannot build here, use phase(action='blocked', "
                "reason=..., evidence=[refs]) — the run will be recorded as partial/failed honestly",
            ],
        )
    return _verdict(True)


def _check_test(validator, project_name) -> Dict[str, Any]:
    status = validator.validate_test_status(project_name)
    if not status.get("has_test_reports"):
        return _verdict(
            False,
            "test phase claims done but no test reports exist on disk",
            [
                "Run build(action='test') so reports are produced, then re-claim",
                "If tests cannot run (missing runtime, environmental), use "
                "phase(action='blocked', reason=..., evidence=[refs])",
            ],
        )
    return _verdict(True)


def _check_report(orchestrator) -> Dict[str, Any]:
    probe = orchestrator.execute_command(
        "find /workspace -maxdepth 1 -name 'setup-report-*.md' | head -1", workdir=None, timeout=30
    )
    if not (probe.get("output") or "").strip():
        return _verdict(
            False,
            "report phase claims done but no setup-report-*.md exists",
            ["Generate it with the report tool, then re-claim"],
        )
    return _verdict(True)
