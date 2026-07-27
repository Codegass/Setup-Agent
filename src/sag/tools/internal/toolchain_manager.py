"""Tool executable resolution and persistence for runtime toolchains."""

import json
import posixpath
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from loguru import logger

from sag.runtime.env_overlay import EnvOverlayStore
from sag.tools.internal.build_preflight import read_build_requirements

RequirementSource = Literal[
    "tool_parameter",
    "project_metadata",
    "build_error",
    "conversation",
    "registered_state",
]
RequirementKind = Literal["exact", "range", "minimum", "maximum", "preferred"]
CandidateSource = Literal[
    "env_overlay",
    "wrapper",
    "registered",
    "standalone",
    "path",
    "system",
]

# The registry is the toolchain state a dispatch's identity is taken over
# (`sag.agent.retry_authority.TOOLCHAIN_REGISTRY_PATH`). Registering a runtime
# and dispatching a build must therefore read the same file.
TOOLCHAIN_REGISTRY_PATH = "/workspace/.setup_agent/toolchains.json"

# Which rule chose one executable out of an overlay that offered several. The
# overlay records candidates, not a decision procedure; naming the rule is what
# makes the decision auditable after the fact.
OVERLAY_RULE_ACTIVE = "overlay_active"
OVERLAY_RULE_PROJECT_REQUIREMENT = "project_required_version"
OVERLAY_RULE_HIGHEST_VERSION = "highest_registered_version"


@dataclass(frozen=True)
class ToolVersionRequirement:
    raw: str
    source: RequirementSource
    kind: RequirementKind

    @classmethod
    def from_raw(
        cls, raw: Optional[str], source: RequirementSource = "tool_parameter"
    ) -> Optional["ToolVersionRequirement"]:
        if not raw:
            return None
        cleaned = raw.strip()
        if not cleaned:
            return None
        if cleaned.startswith("[") or cleaned.startswith("("):
            kind: RequirementKind = "range"
        elif "," in cleaned and re.search(r"(?:>=|<=|>|<)", cleaned):
            kind = "range"
        elif cleaned.startswith(">="):
            kind = "minimum"
        elif cleaned.startswith("<="):
            kind = "maximum"
        elif cleaned.startswith("~"):
            kind = "preferred"
            cleaned = cleaned.removeprefix("~").strip()
        elif cleaned.lower().startswith("preferred:"):
            kind = "preferred"
            cleaned = cleaned.split(":", 1)[1].strip()
        else:
            kind = "exact"
        return cls(raw=cleaned, source=source, kind=kind)


@dataclass(frozen=True)
class ToolchainSpec:
    name: str
    executable: str
    version_requirement: Optional[ToolVersionRequirement] = None
    prefer_wrapper: bool = True


@dataclass(frozen=True)
class ToolExecutableCandidate:
    name: str
    executable: str
    path: str
    version: Optional[str]
    source: CandidateSource


@dataclass(frozen=True)
class OverlaySelection:
    """The one overlay candidate a resolution used, and the rule that chose it."""

    candidate: ToolExecutableCandidate
    rule: str
    candidate_count: int


@dataclass(frozen=True)
class ResolvedToolExecutable:
    candidate: ToolExecutableCandidate
    reason: str
    selection_rule: Optional[str] = None


def record_registered_runtime(
    orchestrator,
    tool: str,
    executable: str,
    *,
    version: Optional[str] = None,
    source: CandidateSource = "registered",
) -> bool:
    """Record one registered runtime in the toolchain registry.

    Live polaris (`logs/session_20260727_065557_97847`): Java 21 was installed,
    registered and activated, and the compile that was meant to inherit it was
    refused three times as an identical retry — the same `retry_key` before and
    after the registration. The retry identity's toolchain component hashes the
    REGISTRY; every real registration wrote only the env OVERLAY, and no session
    of the 23-project campaign produced a registry file at all. The two stores
    were disconnected, so the registered runtime could not reach a dispatch.

    Registration writes both from here: the overlay stays the execution
    consumer (it is what the dispatch shell sources) and the registry states
    that the toolchain changed. Best effort by construction — a registry the
    container will not accept must never fail the registration the model asked
    for.
    """
    if orchestrator is None or not hasattr(orchestrator, "execute_command"):
        return False
    path = str(executable or "").strip()
    if not path:
        return False
    try:
        ToolchainManager(orchestrator).register(
            ToolExecutableCandidate(
                name=str(tool),
                executable=posixpath.basename(path) or str(tool),
                path=path,
                version=str(version) if version is not None else None,
                source=source,
            )
        )
        return True
    except Exception as exc:
        logger.warning(f"Toolchain registry record failed for {path}: {exc}")
        return False


class ToolchainManager:
    """Resolve tool executables from requirements, registry, and container state."""

    registry_path = TOOLCHAIN_REGISTRY_PATH

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.env_overlay = EnvOverlayStore(orchestrator) if orchestrator is not None else None

    def resolve(
        self, spec: ToolchainSpec, working_directory: str = "/workspace"
    ) -> Optional[ResolvedToolExecutable]:
        overlay = self._env_overlay_snapshot()
        observed_requirements = self._observed_requirements_from_overlay(
            spec.name,
            overlay,
            working_directory=working_directory,
        )
        requirements = []
        for requirement in [spec.version_requirement, *observed_requirements]:
            if requirement is not None and requirement.raw not in {
                item.raw for item in requirements
            }:
                requirements.append(requirement)
        candidates, overlay_selection = self._discover(
            spec,
            working_directory,
            overlay,
            requirements,
        )
        compatible = [
            candidate
            for candidate in candidates
            if all(
                self._matches_requirement(candidate.version, requirement)
                for requirement in requirements
            )
        ]
        if not compatible:
            logger.info(
                "No compatible %s executable found for requirement %s",
                spec.executable,
                " and ".join(requirement.raw for requirement in requirements) or "<none>",
            )
            return None

        selected = sorted(
            compatible,
            key=lambda candidate: self._rank_candidate(candidate, spec),
        )[0]
        reason = self._resolution_reason(
            selected,
            spec.version_requirement
            or (observed_requirements[0] if observed_requirements else None),
        )
        selection_rule = None
        if overlay_selection and overlay_selection.candidate.path == selected.path:
            selection_rule = overlay_selection.rule
            if selection_rule != OVERLAY_RULE_ACTIVE:
                reason = (
                    f"{reason}; overlay resolution rule: {selection_rule} among "
                    f"{overlay_selection.candidate_count} registered candidates"
                )
                logger.info(f"[toolchain] {reason}")
        return ResolvedToolExecutable(
            candidate=selected,
            reason=reason,
            selection_rule=selection_rule,
        )

    def matches_requirement(
        self,
        version: Optional[str],
        requirement: Optional[ToolVersionRequirement],
    ) -> bool:
        """Public compatibility check shared by discovery and env registration."""
        return self._matches_requirement(version, requirement)

    def observed_requirements(
        self,
        name: str,
        *,
        working_directory: Optional[str] = None,
    ) -> List[ToolVersionRequirement]:
        """Return all applicable persisted constraints for one build root."""
        return self._observed_requirements_from_overlay(
            name,
            self._env_overlay_snapshot(),
            working_directory=working_directory,
        )

    def observed_requirement(
        self,
        name: str,
        *,
        working_directory: Optional[str] = None,
    ) -> Optional[ToolVersionRequirement]:
        """Backward-compatible first applicable persisted constraint."""
        requirements = self.observed_requirements(
            name,
            working_directory=working_directory,
        )
        return requirements[0] if requirements else None

    def register(self, candidate: ToolExecutableCandidate) -> None:
        registry = self._load_registry()
        by_tool = registry.setdefault(candidate.name, {})
        entries = by_tool.setdefault(candidate.executable, [])
        serialized = {
            **asdict(candidate),
            "registered_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        # A registry the dispatch identity is hashed over must state the
        # toolchain, not how many times a model asked for it: re-registering
        # the same executable with the same facts leaves the file byte-stable,
        # so only a genuinely new registration is material progress. The head
        # entry is the only one that can be re-stated without moving a byte —
        # promoting a candidate from further down the list DOES change the
        # file, and is therefore written like any other change.
        if (
            entries
            and entries[0].get("path") == candidate.path
            and {key: value for key, value in entries[0].items() if key != "registered_at"}
            == asdict(candidate)
        ):
            return
        entries[:] = [entry for entry in entries if entry.get("path") != candidate.path]
        entries.insert(0, serialized)
        self._save_registry(registry)

    def discover(
        self, spec: ToolchainSpec, working_directory: str = "/workspace"
    ) -> List[ToolExecutableCandidate]:
        overlay = self._env_overlay_snapshot()
        return self._discover_with_overlay(spec, working_directory, overlay)

    def _discover_with_overlay(
        self,
        spec: ToolchainSpec,
        working_directory: str,
        overlay: Optional[Dict[str, Any]],
    ) -> List[ToolExecutableCandidate]:
        candidates, _selection = self._discover(spec, working_directory, overlay, ())
        return candidates

    def _discover(
        self,
        spec: ToolchainSpec,
        working_directory: str,
        overlay: Optional[Dict[str, Any]],
        requirements: Sequence[ToolVersionRequirement],
    ) -> Tuple[List[ToolExecutableCandidate], Optional[OverlaySelection]]:
        candidates: List[ToolExecutableCandidate] = []
        overlay_selection = self._env_overlay_selection(spec, overlay, requirements)
        if overlay_selection:
            candidates.append(overlay_selection.candidate)

        if spec.prefer_wrapper:
            if spec.executable == "mvn":
                wrapper = f"{working_directory.rstrip('/')}/mvnw"
                if self._is_executable(wrapper):
                    candidates.append(
                        self._candidate_from_path(spec, wrapper, source="wrapper")
                    )
            elif spec.executable == "gradle":
                gradle_wrapper = self._checkout_gradle_wrapper(working_directory)
                if gradle_wrapper:
                    candidates.append(
                        self._candidate_from_path(
                            spec,
                            gradle_wrapper,
                            source="wrapper",
                        )
                    )

        candidates.extend(self._registered_candidates(spec))

        if spec.executable == "mvn":
            candidates.extend(self._discover_standalone_maven(spec))

        path_candidate = self._path_candidate(spec)
        if path_candidate:
            candidates.append(path_candidate)

        filtered = self._filter_blocked_candidates(
            self._dedupe_candidates(candidates), spec, overlay
        )
        if overlay_selection and all(
            candidate.path != overlay_selection.candidate.path for candidate in filtered
        ):
            overlay_selection = None
        return filtered, overlay_selection

    def _observed_requirements_from_overlay(
        self,
        name: str,
        overlay: Optional[Dict[str, Any]],
        *,
        working_directory: Optional[str],
    ) -> List[ToolVersionRequirement]:
        if self.env_overlay is None or overlay is None:
            return []
        tool_name = self.env_overlay._normalize_tool(name)
        raw_records = overlay.get("tools", {}).get(tool_name, {}).get("requirements", [])
        requirements = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict) or not self.env_overlay._requirement_applies(
                raw_record,
                working_directory,
            ):
                continue
            requirement = ToolVersionRequirement.from_raw(
                raw_record.get("raw"),
                source="registered_state",
            )
            if requirement and requirement.raw not in {item.raw for item in requirements}:
                requirements.append(requirement)
        return requirements

    def ensure_path(self, candidate: ToolExecutableCandidate) -> None:
        directory = candidate.path.rsplit("/", 1)[0]
        block = (
            "# SAG_TOOLCHAIN_PATH_BEGIN\n"
            f'export PATH="{directory}:$PATH"\n'
            "# SAG_TOOLCHAIN_PATH_END"
        )
        commands = [
            f"mkdir -p {shlex.quote('/etc/profile.d')}",
            f"cat > /etc/profile.d/sag_toolchain_path.sh << 'SAG_TOOLCHAIN_PATH_EOF'\n{block}\nSAG_TOOLCHAIN_PATH_EOF",
            "chmod +x /etc/profile.d/sag_toolchain_path.sh",
        ]
        for command in commands:
            self.orchestrator.execute_command(command)

    def _registered_candidates(self, spec: ToolchainSpec) -> List[ToolExecutableCandidate]:
        registry = self._load_registry()
        entries = registry.get(spec.name, {}).get(spec.executable, [])
        candidates = []
        for entry in entries:
            path = entry.get("path")
            if not path or not self._is_executable(path):
                continue
            candidates.append(
                ToolExecutableCandidate(
                    name=entry.get("name", spec.name),
                    executable=entry.get("executable", spec.executable),
                    path=path,
                    version=entry.get("version") or self._probe_version(path),
                    source="registered",
                )
            )
        return candidates

    def _env_overlay_snapshot(self) -> Optional[Dict[str, Any]]:
        if self.env_overlay is None:
            return None
        overlay, _warnings = self.env_overlay._load_overlay()
        return overlay

    def _env_overlay_selection(
        self,
        spec: ToolchainSpec,
        overlay: Optional[Dict[str, Any]],
        requirements: Sequence[ToolVersionRequirement] = (),
    ) -> Optional[OverlaySelection]:
        """The one overlay candidate this resolution uses, and the rule that chose it.

        Live camel-quarkus (`logs/session_20260727_063915_96714`): the overlay
        listed two Mavens for one tool with no active candidate, and the test
        phase closed with nothing to run because the harness offered a question
        instead of an answer. An overlay that names more than one executable
        now resolves in a fixed order — the version the project requires, then
        the highest registered version — and names the rule that decided.

        An ACTIVE candidate is not a tie to break: activation is a recorded
        decision, and the dispatch shell exports that candidate's PATH, so
        overruling it here would make the resolver and the environment the
        build actually runs in disagree.

        Ambiguity is the ONLY thing this resolves. An overlay that lists one
        executable states no question, so it keeps its existing answer exactly:
        activated means selected, not activated means the overlay contributes
        nothing and ordinary discovery decides.
        """
        if self.env_overlay is None or overlay is None:
            return None
        tool_name = self.env_overlay._normalize_tool(spec.name)
        entry = overlay.get("tools", {}).get(tool_name, {})
        registered = entry.get("candidates", {}) or {}

        active_path = entry.get("active")
        if active_path:
            active = registered.get(active_path)
            if active and self._is_executable(active_path):
                return OverlaySelection(
                    candidate=self._overlay_candidate(spec, active_path, active),
                    rule=OVERLAY_RULE_ACTIVE,
                    candidate_count=len(registered),
                )
            return None

        if len(registered) < 2:
            return None

        usable: List[ToolExecutableCandidate] = []
        for path, record in sorted(registered.items()):
            if not self._is_executable(path):
                continue
            candidate = self._overlay_candidate(spec, path, record)
            if self._is_blocked_by_overlay(candidate, spec, overlay):
                continue
            usable.append(candidate)
        if not usable:
            return None

        required = [
            candidate
            for candidate in usable
            if requirements
            and all(
                self._matches_requirement(candidate.version, requirement)
                for requirement in requirements
            )
        ]
        rule = OVERLAY_RULE_PROJECT_REQUIREMENT if required else OVERLAY_RULE_HIGHEST_VERSION
        pool = required or usable
        selected = sorted(
            pool,
            key=lambda candidate: (
                self._negative_version_tuple(candidate.version),
                candidate.path,
            ),
        )[0]
        return OverlaySelection(
            candidate=selected,
            rule=rule,
            candidate_count=len(registered),
        )

    def _overlay_candidate(
        self,
        spec: ToolchainSpec,
        path: str,
        record: Dict[str, Any],
    ) -> ToolExecutableCandidate:
        return ToolExecutableCandidate(
            name=spec.name,
            executable=spec.executable,
            path=path,
            version=record.get("version") or self._probe_version(path),
            source="env_overlay",
        )

    def _discover_standalone_maven(self, spec: ToolchainSpec) -> List[ToolExecutableCandidate]:
        result = self.orchestrator.execute_command(
            "find /workspace /tmp /opt /usr/local -path '*/apache-maven-*/bin/mvn' -type f 2>/dev/null"
        )
        if result.get("exit_code") != 0:
            return []
        candidates = []
        for path in (result.get("output") or "").splitlines():
            path = path.strip()
            if path and self._is_executable(path):
                candidates.append(self._candidate_from_path(spec, path, source="standalone"))
        return candidates

    def _path_candidate(self, spec: ToolchainSpec) -> Optional[ToolExecutableCandidate]:
        result = self.orchestrator.execute_command(f"command -v {shlex.quote(spec.executable)}")
        path = (result.get("output") or "").strip()
        if result.get("exit_code") != 0 or not path:
            return None
        source: CandidateSource = "system" if path.startswith("/usr/bin/") else "path"
        if not self._is_executable(path):
            return None
        return self._candidate_from_path(spec, path, source=source)

    def _candidate_from_path(
        self, spec: ToolchainSpec, path: str, source: CandidateSource
    ) -> ToolExecutableCandidate:
        return ToolExecutableCandidate(
            name=spec.name,
            executable=spec.executable,
            path=path,
            version=self._probe_version(path),
            source=source,
        )

    def _is_executable(self, path: str) -> bool:
        result = self.orchestrator.execute_command(
            f"test -x {shlex.quote(path)} && echo EXISTS || echo MISSING"
        )
        return result.get("exit_code") == 0 and "EXISTS" in (result.get("output") or "")

    def _is_file(self, path: str) -> bool:
        result = self.orchestrator.execute_command(
            f"test -f {shlex.quote(path)} && echo EXISTS || echo MISSING"
        )
        return result.get("exit_code") == 0 and "EXISTS" in (result.get("output") or "")

    def _realpath(self, path: str) -> Optional[str]:
        result = self.orchestrator.execute_command(
            f"realpath -e -- {shlex.quote(path)}"
        )
        resolved = (result.get("output") or "").strip().splitlines()
        if result.get("exit_code") != 0 or not resolved:
            return None
        candidate = resolved[-1].strip()
        return candidate if candidate.startswith("/") else None

    @staticmethod
    def _path_within(path: str, root: str) -> bool:
        try:
            return posixpath.commonpath((path, root)) == root
        except ValueError:
            return False

    def _checkout_gradle_wrapper(self, working_directory: str) -> Optional[str]:
        """Find the nearest ancestor gradlew without crossing the surveyed checkout.

        Both the working directory and every wrapper target are realpath
        checked. This rejects a lexically in-tree symlink that escapes the
        checkout and never searches above the survey's persisted project root.
        Without a survey stamp, discovery is intentionally limited to the
        working directory itself.
        """
        manifest = read_build_requirements(self.orchestrator) or {}
        survey_root = str((manifest.get("survey") or {}).get("project_path") or "").strip()
        lexical_root = survey_root or working_directory
        root = self._realpath(lexical_root)
        current = self._realpath(working_directory)
        if not root or not current or not self._path_within(current, root):
            return None

        while True:
            wrapper = posixpath.join(current, "gradlew")
            if self._is_file(wrapper):
                wrapper_target = self._realpath(wrapper)
                if wrapper_target and self._path_within(wrapper_target, root):
                    return wrapper
            if current == root:
                break
            parent = posixpath.dirname(current)
            if parent == current or not self._path_within(parent, root):
                break
            current = parent
        return None

    def _probe_version(self, path: str) -> Optional[str]:
        result = self.orchestrator.execute_command(f"{shlex.quote(path)} -version")
        if result.get("exit_code") != 0:
            return None
        return self._extract_version(result.get("output") or "")

    def _load_registry(self) -> Dict[str, Any]:
        result = self.orchestrator.execute_command(
            f"cat {self.registry_path} 2>/dev/null || echo '{{}}'"
        )
        if result.get("exit_code") != 0:
            return {}
        try:
            return json.loads(result.get("output") or "{}")
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid toolchain registry JSON")
            return {}

    def _save_registry(self, registry: Dict[str, Any]) -> None:
        payload = json.dumps(registry, indent=2, sort_keys=True)
        self.orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        self.orchestrator.execute_command(
            "cat > /workspace/.setup_agent/toolchains.json << 'SAG_TOOLCHAINS_EOF'\n"
            f"{payload}\n"
            "SAG_TOOLCHAINS_EOF"
        )

    def _dedupe_candidates(
        self, candidates: List[ToolExecutableCandidate]
    ) -> List[ToolExecutableCandidate]:
        deduped: Dict[str, ToolExecutableCandidate] = {}
        for candidate in candidates:
            existing = deduped.get(candidate.path)
            if not existing or self._source_priority(candidate.source) < self._source_priority(
                existing.source
            ):
                deduped[candidate.path] = candidate
        return list(deduped.values())

    def _filter_blocked_candidates(
        self,
        candidates: List[ToolExecutableCandidate],
        spec: ToolchainSpec,
        overlay: Optional[Dict[str, Any]],
    ) -> List[ToolExecutableCandidate]:
        if self.env_overlay is None or overlay is None:
            return candidates

        filtered = []
        for candidate in candidates:
            if self._is_blocked_by_overlay(candidate, spec, overlay):
                logger.debug(
                    "Excluding %s candidate %s from %s due to env overlay blocker",
                    candidate.name,
                    candidate.path,
                    candidate.source,
                )
                continue
            filtered.append(candidate)
        return filtered

    def _is_blocked_by_overlay(
        self,
        candidate: ToolExecutableCandidate,
        spec: ToolchainSpec,
        overlay: Optional[Dict[str, Any]],
    ) -> bool:
        if self.env_overlay is None or overlay is None:
            return False

        tool_name = self.env_overlay._normalize_tool(spec.name)
        executable = self.env_overlay._normalize_executable(candidate.path)
        requirement = spec.version_requirement.raw if spec.version_requirement else None
        has_evidence = False
        if candidate.version is not None:
            has_evidence = True
            if self.env_overlay._is_blocked_in_overlay(
                overlay,
                tool_name,
                executable,
                version=candidate.version,
            ):
                return True
        if requirement is not None:
            has_evidence = True
            if self.env_overlay._is_blocked_in_overlay(
                overlay,
                tool_name,
                executable,
                requirement=requirement,
            ):
                return True
        if not has_evidence:
            return self.env_overlay._is_blocked_in_overlay(overlay, tool_name, executable)
        return False

    def _rank_candidate(
        self, candidate: ToolExecutableCandidate, spec: ToolchainSpec
    ) -> Tuple[int, int, Tuple[int, ...], str]:
        requirement = spec.version_requirement
        preferred_penalty = 0
        if requirement and requirement.kind == "preferred":
            preferred_penalty = 0 if self._same_version(candidate.version, requirement.raw) else 1
        return (
            preferred_penalty,
            self._source_priority(candidate.source),
            self._negative_version_tuple(candidate.version),
            candidate.path,
        )

    def _source_priority(self, source: CandidateSource) -> int:
        priorities = {
            "env_overlay": 0,
            "wrapper": 1,
            "registered": 2,
            "path": 3,
            "standalone": 4,
            "system": 5,
        }
        return priorities[source]

    def _matches_requirement(
        self, version: Optional[str], requirement: Optional[ToolVersionRequirement]
    ) -> bool:
        if requirement is None:
            return True
        if requirement.kind == "preferred":
            return True
        if version is None:
            return False
        if requirement.kind == "exact":
            return self._same_version(version, requirement.raw)
        if requirement.kind == "minimum":
            return self._compare_versions(version, requirement.raw.lstrip(">=")) >= 0
        if requirement.kind == "maximum":
            return self._compare_versions(version, requirement.raw.lstrip("<=")) <= 0
        if requirement.kind == "range":
            return self._matches_range(version, requirement.raw)
        return False

    def _matches_range(self, version: str, raw_range: str) -> bool:
        if not raw_range.startswith(("[", "(")):
            return self._matches_relational_range(version, raw_range)

        match = re.match(r"^([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])$", raw_range)
        if not match:
            return False
        lower_inclusive = match.group(1) == "["
        upper_inclusive = match.group(4) == "]"
        lower = match.group(2).strip()
        upper = match.group(3).strip()
        if lower:
            cmp_lower = self._compare_versions(version, lower)
            if cmp_lower < 0 or (cmp_lower == 0 and not lower_inclusive):
                return False
        if upper:
            cmp_upper = self._compare_versions(version, upper)
            if cmp_upper > 0 or (cmp_upper == 0 and not upper_inclusive):
                return False
        return True

    def _matches_relational_range(self, version: str, raw_range: str) -> bool:
        clauses = [clause.strip() for clause in raw_range.split(",") if clause.strip()]
        if not clauses:
            return False

        for clause in clauses:
            if clause.startswith(">="):
                if self._compare_versions(version, clause[2:].strip()) < 0:
                    return False
            elif clause.startswith(">"):
                if self._compare_versions(version, clause[1:].strip()) <= 0:
                    return False
            elif clause.startswith("<="):
                if self._compare_versions(version, clause[2:].strip()) > 0:
                    return False
            elif clause.startswith("<"):
                if self._compare_versions(version, clause[1:].strip()) >= 0:
                    return False
            elif not self._same_version(version, clause):
                return False

        return True

    def _same_version(self, left: Optional[str], right: str) -> bool:
        if left is None:
            return False
        return self._version_tuple(left) == self._version_tuple(right)

    def _compare_versions(self, left: str, right: str) -> int:
        left_tuple = self._version_tuple(left)
        right_tuple = self._version_tuple(right)
        width = max(len(left_tuple), len(right_tuple))
        left_tuple = left_tuple + (0,) * (width - len(left_tuple))
        right_tuple = right_tuple + (0,) * (width - len(right_tuple))
        if left_tuple < right_tuple:
            return -1
        if left_tuple > right_tuple:
            return 1
        return 0

    def _negative_version_tuple(self, version: Optional[str]) -> Tuple[int, ...]:
        return tuple(-part for part in self._version_tuple(version or "0"))

    def _version_tuple(self, version: str) -> Tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", version)[:4]) or (0,)

    def _extract_version(self, output: str) -> Optional[str]:
        match = re.search(r"Apache Maven\s+([0-9]+(?:\.[0-9]+){0,3})", output)
        if match:
            return match.group(1)
        match = re.search(r"\b([0-9]+(?:\.[0-9]+){1,3})\b", output)
        return match.group(1) if match else None

    def _resolution_reason(
        self,
        candidate: ToolExecutableCandidate,
        requirement: Optional[ToolVersionRequirement],
    ) -> str:
        if requirement:
            return (
                f"selected {candidate.path} from {candidate.source} because version "
                f"{candidate.version or 'unknown'} satisfies {requirement.raw}"
            )
        return f"selected {candidate.path} from {candidate.source} by default priority"
