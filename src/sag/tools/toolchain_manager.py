"""Tool executable resolution and persistence for runtime toolchains."""

import json
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from loguru import logger

from sag.runtime.env_overlay import EnvOverlayStore

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
class ResolvedToolExecutable:
    candidate: ToolExecutableCandidate
    reason: str


class ToolchainManager:
    """Resolve tool executables from requirements, registry, and container state."""

    registry_path = "/workspace/.setup_agent/toolchains.json"

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.env_overlay = EnvOverlayStore(orchestrator) if orchestrator is not None else None

    def resolve(
        self, spec: ToolchainSpec, working_directory: str = "/workspace"
    ) -> Optional[ResolvedToolExecutable]:
        candidates = self.discover(spec, working_directory)
        compatible = [
            candidate
            for candidate in candidates
            if self._matches_requirement(candidate.version, spec.version_requirement)
        ]
        if not compatible:
            logger.info(
                "No compatible %s executable found for requirement %s",
                spec.executable,
                spec.version_requirement.raw if spec.version_requirement else "<none>",
            )
            return None

        selected = sorted(
            compatible,
            key=lambda candidate: self._rank_candidate(candidate, spec),
        )[0]
        return ResolvedToolExecutable(
            candidate=selected,
            reason=self._resolution_reason(selected, spec.version_requirement),
        )

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
        entries[:] = [entry for entry in entries if entry.get("path") != candidate.path]
        entries.insert(0, serialized)
        self._save_registry(registry)

    def discover(
        self, spec: ToolchainSpec, working_directory: str = "/workspace"
    ) -> List[ToolExecutableCandidate]:
        candidates: List[ToolExecutableCandidate] = []

        overlay_candidate = self._env_overlay_candidate(spec)
        if overlay_candidate:
            candidates.append(overlay_candidate)

        if spec.prefer_wrapper:
            wrapper = f"{working_directory.rstrip('/')}/mvnw"
            if spec.executable == "mvn" and self._is_executable(wrapper):
                candidates.append(self._candidate_from_path(spec, wrapper, source="wrapper"))

        candidates.extend(self._registered_candidates(spec))

        if spec.executable == "mvn":
            candidates.extend(self._discover_standalone_maven(spec))

        path_candidate = self._path_candidate(spec)
        if path_candidate:
            candidates.append(path_candidate)

        return self._filter_blocked_candidates(self._dedupe_candidates(candidates), spec)

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

    def _env_overlay_candidate(
        self, spec: ToolchainSpec
    ) -> Optional[ToolExecutableCandidate]:
        if self.env_overlay is None:
            return None
        active = self.env_overlay.active_candidate(spec.name)
        if not active:
            return None
        path = active.get("executable")
        if not path or not self._is_executable(path):
            return None
        return ToolExecutableCandidate(
            name=spec.name,
            executable=spec.executable,
            path=path,
            version=active.get("version") or self._probe_version(path),
            source="env_overlay",
        )

    def _discover_standalone_maven(self, spec: ToolchainSpec) -> List[ToolExecutableCandidate]:
        result = self.orchestrator.execute_command(
            "find /tmp /opt /usr/local -path '*/apache-maven-*/bin/mvn' -type f 2>/dev/null"
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
        self, candidates: List[ToolExecutableCandidate], spec: ToolchainSpec
    ) -> List[ToolExecutableCandidate]:
        if self.env_overlay is None:
            return candidates

        filtered = []
        for candidate in candidates:
            if self._is_blocked_by_overlay(candidate, spec):
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
        self, candidate: ToolExecutableCandidate, spec: ToolchainSpec
    ) -> bool:
        if self.env_overlay is None:
            return False

        requirement = spec.version_requirement.raw if spec.version_requirement else None
        has_evidence = False
        if candidate.version is not None:
            has_evidence = True
            if self.env_overlay.is_blocked(
                spec.name,
                candidate.path,
                version=candidate.version,
            ):
                return True
        if requirement is not None:
            has_evidence = True
            if self.env_overlay.is_blocked(
                spec.name,
                candidate.path,
                requirement=requirement,
            ):
                return True
        if not has_evidence:
            return self.env_overlay.is_blocked(spec.name, candidate.path)
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
