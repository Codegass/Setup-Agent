"""Typed, deterministic composition of project analysis into planner guidance."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections import defaultdict
from copy import deepcopy
from enum import Enum
from typing import Any, Iterable, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .output_storage import atomic_write_container_text

PROJECT_BRIEF_PATH = "/workspace/.setup_agent/project_brief.json"
PROJECT_BRIEF_VERSION = 1
PROJECT_BRIEF_COMPOSER_VERSION = "project-brief-v1"
PROJECT_BRIEF_PROJECTION_CHARS = 1200


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _dedupe_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        sorted({str(value).strip() for value in values if value is not None and str(value).strip()})
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return normalized or "input"


def _display(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=True, default=str)


class InputRole(str, Enum):
    POLICY = "policy"
    REQUIREMENT = "requirement"
    EVIDENCE = "evidence"
    DEFAULT = "default"


FragmentRole = InputRole


class ProjectBriefInputs(BaseModel):
    """The complete dependency set for one cached brief fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: dict[str, Any] = Field(default_factory=dict)
    detected_toolchain: dict[str, Any] = Field(default_factory=dict)
    submodule_state: tuple[Any, ...] = ()
    build_roots: tuple[Any, ...] = ()
    repo_docs: dict[str, Any] = Field(default_factory=dict)
    analyzer_version: str
    composer_version: str = PROJECT_BRIEF_COMPOSER_VERSION

    def component_fingerprints(self) -> dict[str, str]:
        return {
            "manifest": _digest(self.manifest),
            "detected_toolchain": _digest(self.detected_toolchain),
            "submodule_state": _digest(self.submodule_state),
            "build_roots": _digest(self.build_roots),
            "repo_docs": _digest(self.repo_docs),
            "analyzer_version": _digest(self.analyzer_version),
            "composer_version": _digest(self.composer_version),
        }

    def fingerprint(self) -> str:
        return _digest(self.component_fingerprints())


class BriefFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction_id: str
    subject: str
    role: InputRole
    value: Any = None
    text: str
    source: str
    refs: tuple[str, ...] = ()

    @field_validator("instruction_id", "subject", "text", "source")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("brief fragment fields must be non-empty")
        return normalized

    @field_validator("refs", mode="before")
    @classmethod
    def _normalize_refs(cls, value) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        return _dedupe_strings(value or ())


class BriefInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction_id: str
    subject: str
    role: str
    text: str
    refs: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()


class BuildStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str
    system: str
    goal: str
    depends_on: tuple[str, ...] = ()


class BriefSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str
    instructions: tuple[BriefInstruction, ...] = ()
    build_steps: tuple[BuildStep, ...] = ()
    refs: tuple[str, ...] = ()


class ProjectBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = PROJECT_BRIEF_VERSION
    input_fingerprint: str
    sections: tuple[BriefSection, ...]

    def section(self, section_id: str) -> BriefSection:
        for section in self.sections:
            if section.section_id == section_id:
                return section
        raise KeyError(section_id)

    @staticmethod
    def _instruction_line(item: BriefInstruction) -> str:
        labels: tuple[str, ...]
        if "assumption" in item.markers:
            labels = ("assumption",)
        else:
            labels = (item.role, *item.markers)
        marker = "; ".join(dict.fromkeys(label for label in labels if label))
        refs = ",".join(item.refs) or "none"
        return f"- [{marker}] {item.instruction_id}: {item.text} refs={refs}"

    def _projection_body_lines(self) -> list[str]:
        lines = []
        titles = {
            "actions": "REQUIRED ACTIONS:",
            "requirements": "REQUIREMENTS/POLICY:",
            "current-state": "CURRENT EVIDENCE:",
            "assumptions": "ASSUMPTIONS:",
            "recommended-build": "RECOMMENDED BUILD:",
        }
        for section in self.sections:
            if not section.instructions and not section.build_steps:
                continue
            lines.append(titles.get(section.section_id, section.section_id.upper() + ":"))
            lines.extend(self._instruction_line(item) for item in section.instructions)
            for step in section.build_steps:
                dependencies = ",".join(step.depends_on) or "none"
                lines.append(
                    f"- root={step.root} system={step.system} goal={step.goal} "
                    f"depends_on={dependencies}"
                )
        return lines

    def to_planner_projection(
        self,
        *,
        max_chars: int = PROJECT_BRIEF_PROJECTION_CHARS,
        full_ref: str = PROJECT_BRIEF_PATH,
    ) -> str:
        if max_chars < 240:
            raise ValueError("project brief projection budget must be at least 240 chars")
        prefix = [
            f"=== PROJECT BRIEF v{self.version} ===",
            f"fingerprint={self.input_fingerprint[:16]}",
            "[BEGIN UNTRUSTED TOOL/PROJECT EVIDENCE]",
        ]
        suffix = [
            "[END UNTRUSTED TOOL/PROJECT EVIDENCE]",
            f"full brief: {full_ref}",
        ]
        body = self._projection_body_lines()
        selected: list[str] = []
        for index, line in enumerate(body):
            remaining = len(body) - index - 1
            tail = [f"... omitted={remaining} line(s)"] if remaining else []
            candidate = "\n".join([*prefix, *selected, line, *tail, *suffix])
            if len(candidate) > max_chars:
                break
            selected.append(line)

        omitted = len(body) - len(selected)
        omitted_line = [f"... omitted={omitted} line(s)"] if omitted else []
        rendered = "\n".join([*prefix, *selected, *omitted_line, *suffix])
        if len(rendered) > max_chars:
            raise ValueError("project brief fixed projection fields exceed the budget")
        return rendered

    def to_prompt_text(self, *, max_chars: int = PROJECT_BRIEF_PROJECTION_CHARS) -> str:
        return self.to_planner_projection(max_chars=max_chars)


def _fragment_key(fragment: BriefFragment) -> tuple[str, ...]:
    return (
        fragment.instruction_id,
        fragment.subject,
        fragment.role.value,
        _canonical_json(fragment.value),
        fragment.text,
        fragment.source,
    )


def _dedupe_fragments(fragments: Iterable[BriefFragment]) -> tuple[BriefFragment, ...]:
    grouped: dict[str, list[BriefFragment]] = defaultdict(list)
    for fragment in fragments:
        if not isinstance(fragment, BriefFragment):
            fragment = BriefFragment.model_validate(fragment)
        grouped[fragment.instruction_id].append(fragment)

    deduped = []
    for instruction_id in sorted(grouped):
        candidates = sorted(grouped[instruction_id], key=_fragment_key)
        winner = candidates[0]
        refs = _dedupe_strings(ref for item in candidates for ref in item.refs)
        deduped.append(winner.model_copy(update={"refs": refs}))
    return tuple(deduped)


def _markers(fragment: BriefFragment, *extra: str) -> tuple[str, ...]:
    markers = list(extra)
    source = fragment.source.lower()
    if fragment.role is InputRole.REQUIREMENT and ("repo" in source or "doc" in source):
        markers.append("UNTRUSTED project input")
    return tuple(dict.fromkeys(markers))


def _instruction(
    fragment: BriefFragment,
    *,
    role: str | None = None,
    markers: tuple[str, ...] = (),
) -> BriefInstruction:
    return BriefInstruction(
        instruction_id=fragment.instruction_id,
        subject=fragment.subject,
        role=role or fragment.role.value,
        text=fragment.text,
        refs=fragment.refs,
        markers=_markers(fragment, *markers),
    )


def _values(fragments: Iterable[BriefFragment]) -> tuple[Any, ...]:
    by_value = {_canonical_json(fragment.value): fragment.value for fragment in fragments}
    return tuple(by_value[key] for key in sorted(by_value))


def _action(
    instruction_id: str,
    subject: str,
    text: str,
    fragments: Iterable[BriefFragment],
) -> BriefInstruction:
    return BriefInstruction(
        instruction_id=instruction_id,
        subject=subject,
        role="action",
        text=text,
        refs=_dedupe_strings(ref for fragment in fragments for ref in fragment.refs),
        markers=("required-action",),
    )


def _mismatch_action(
    subject: str,
    needed: tuple[Any, ...],
    current: tuple[Any, ...],
    fragments: Iterable[BriefFragment],
) -> BriefInstruction:
    required_text = ", ".join(_display(value) for value in needed)
    current_text = ", ".join(_display(value) for value in current)
    if subject in {"java", "java.version", "jdk", "jdk.version"}:
        return _action(
            "provision-jdk",
            subject,
            f"Provision JDK {required_text}; current environment reports JDK {current_text}.",
            fragments,
        )
    return _action(
        f"reconcile-{_slug(subject)}",
        subject,
        f"Reconcile {subject}: required {required_text}; current evidence is {current_text}.",
        fragments,
    )


def _policy_action(
    subject: str,
    policies: tuple[BriefFragment, ...],
    conflicts: tuple[BriefFragment, ...],
) -> BriefInstruction:
    allowed = ", ".join(_display(value) for value in _values(policies))
    requested = ", ".join(_display(value) for value in _values(conflicts))
    return _action(
        f"policy-conflict-{_slug(subject)}",
        subject,
        f"Follow policy for {subject} ({allowed}); conflicting input requested {requested}.",
        (*policies, *conflicts),
    )


def _normalize_dependencies(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    return _dedupe_strings(value)


def _build_steps(recommendation: Mapping[str, Any] | None) -> tuple[BuildStep, ...]:
    rec = dict(recommendation or {})
    islands = list(rec.get("build_islands") or [])
    if islands:
        raw_steps = islands
    elif rec.get("build_root") or rec.get("build_system"):
        raw_steps = [rec]
    else:
        return ()

    by_root: dict[str, dict[str, Any]] = {}
    for raw in raw_steps:
        root = str(raw.get("root") or raw.get("build_root") or ".").strip() or "."
        candidate = {
            "root": root,
            "system": str(raw.get("system") or raw.get("build_system") or "unknown"),
            "goal": str(raw.get("goal") or "build"),
            "depends_on": _normalize_dependencies(raw.get("depends_on")),
        }
        existing = by_root.get(root)
        if existing is None or _canonical_json(candidate) < _canonical_json(existing):
            by_root[root] = candidate

    publishers = sorted(
        root
        for root, item in by_root.items()
        if item["goal"].lower() in {"install", "publish", "publishtomavenlocal"}
    )
    for root, item in by_root.items():
        if len(by_root) > 1 and not item["depends_on"] and root not in publishers:
            item["depends_on"] = tuple(publisher for publisher in publishers if publisher != root)

    ordered_roots = []
    pending = set(by_root)
    while pending:
        ready = sorted(
            root
            for root in pending
            if all(
                dependency not in by_root or dependency in ordered_roots
                for dependency in by_root[root]["depends_on"]
            )
        )
        if not ready:
            ready = sorted(pending)
        for root in ready:
            ordered_roots.append(root)
            pending.remove(root)

    return tuple(BuildStep(**by_root[root]) for root in ordered_roots)


class ProjectBriefComposer:
    def __init__(self, *, version: int = PROJECT_BRIEF_VERSION) -> None:
        self.version = int(version)
        self.composition_count = 0

    def compose(
        self,
        inputs: ProjectBriefInputs,
        fragments: Iterable[BriefFragment],
        *,
        build_recommendation: Mapping[str, Any] | None = None,
        cached_brief: ProjectBrief | None = None,
    ) -> ProjectBrief:
        if not isinstance(inputs, ProjectBriefInputs):
            inputs = ProjectBriefInputs.model_validate(inputs)
        fingerprint = inputs.fingerprint()
        if cached_brief is not None and cached_brief.input_fingerprint == fingerprint:
            return cached_brief

        self.composition_count += 1
        normalized = _dedupe_fragments(fragments)
        grouped: dict[str, list[BriefFragment]] = defaultdict(list)
        for fragment in normalized:
            grouped[fragment.subject].append(fragment)

        requirements: list[BriefInstruction] = []
        evidence: list[BriefInstruction] = []
        assumptions: list[BriefInstruction] = []
        actions: list[BriefInstruction] = []

        for subject in sorted(grouped):
            subject_fragments = tuple(sorted(grouped[subject], key=_fragment_key))
            by_role = {
                role: tuple(item for item in subject_fragments if item.role is role)
                for role in InputRole
            }
            policies = by_role[InputRole.POLICY]
            needed = by_role[InputRole.REQUIREMENT]
            current = by_role[InputRole.EVIDENCE]
            defaults = by_role[InputRole.DEFAULT]

            evidence.extend(_instruction(item) for item in current)
            if policies:
                requirements.extend(
                    _instruction(item, markers=("policy-wins",)) for item in policies
                )
                policy_values = {_canonical_json(value) for value in _values(policies)}
                conflicts = tuple(
                    item
                    for item in (*needed, *current, *defaults)
                    if _canonical_json(item.value) not in policy_values
                )
                if conflicts:
                    actions.append(_policy_action(subject, policies, conflicts))
                continue

            if needed:
                requirements.extend(_instruction(item) for item in needed)
                required_values = _values(needed)
                if len(required_values) > 1:
                    actions.append(
                        _action(
                            f"reconcile-requirements-{_slug(subject)}",
                            subject,
                            f"Resolve conflicting requirements for {subject}: "
                            + ", ".join(_display(value) for value in required_values),
                            needed,
                        )
                    )
                current_values = _values(current)
                required_keys = {_canonical_json(value) for value in required_values}
                if current_values and any(
                    _canonical_json(value) not in required_keys for value in current_values
                ):
                    actions.append(
                        _mismatch_action(
                            subject,
                            required_values,
                            current_values,
                            (*needed, *current),
                        )
                    )
                continue

            if defaults:
                selected = sorted(defaults, key=_fragment_key)[0]
                assumptions.append(
                    _instruction(selected, role="assumption", markers=("assumption",))
                )
                current_values = _values(current)
                if current_values and _canonical_json(selected.value) not in {
                    _canonical_json(value) for value in current_values
                }:
                    actions.append(
                        _mismatch_action(
                            subject,
                            (selected.value,),
                            current_values,
                            (selected, *current),
                        )
                    )

        def unique(items: Iterable[BriefInstruction]) -> tuple[BriefInstruction, ...]:
            by_id: dict[str, BriefInstruction] = {}
            priority = {"native-first": 0, "install-deps": 1, "test-command": 2}
            for item in sorted(
                items,
                key=lambda value: (
                    priority.get(value.instruction_id, 10),
                    value.instruction_id,
                ),
            ):
                by_id.setdefault(item.instruction_id, item)
            return tuple(by_id.values())

        steps = _build_steps(build_recommendation)
        sections = (
            BriefSection(section_id="actions", instructions=unique(actions)),
            BriefSection(section_id="requirements", instructions=unique(requirements)),
            BriefSection(section_id="current-state", instructions=unique(evidence)),
            BriefSection(section_id="assumptions", instructions=unique(assumptions)),
            BriefSection(
                section_id="recommended-build",
                build_steps=steps,
                refs=("analysis://build-recommendation",) if steps else (),
            ),
        )
        return ProjectBrief(
            version=self.version,
            input_fingerprint=fingerprint,
            sections=sections,
        )


class ProjectBriefStore:
    """Validated complete reads and temp-file/rename atomic publication."""

    def __init__(self, orchestrator, *, path: str = PROJECT_BRIEF_PATH) -> None:
        self.orchestrator = orchestrator
        self.path = str(path)

    @staticmethod
    def _ok(result: Mapping[str, Any]) -> bool:
        return bool(result.get("exit_code") == 0 or result.get("success"))

    def load(self) -> ProjectBrief | None:
        try:
            result = self.orchestrator.execute_command(f"cat {self.path}")
            if not self._ok(result):
                return None
            return cast(
                ProjectBrief,
                ProjectBrief.model_validate_json(result.get("output") or ""),
            )
        except Exception:
            return None

    def write(self, brief: ProjectBrief) -> None:
        if not isinstance(brief, ProjectBrief):
            brief = ProjectBrief.model_validate(brief)
        directory = posixpath.dirname(self.path)
        mkdir = self.orchestrator.execute_command(f"mkdir -p {directory}")
        if not self._ok(mkdir):
            raise OSError(f"failed to create project brief directory {directory}")
        payload = json.dumps(
            brief.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        atomic_write_container_text(self.orchestrator, self.path, payload)


class ProjectBriefArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brief: ProjectBrief
    planner_projection: str
    artifact_ref: str
    cache_hit: bool


_MANIFEST_NAMES = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
)


def _command_ok(result: Mapping[str, Any]) -> bool:
    return bool(result.get("exit_code") == 0 or result.get("success"))


def _relative_root(value: Any, project_path: str) -> str:
    source = str(value or ".").strip() or "."
    if not source.startswith("/"):
        return posixpath.normpath(source)
    project_root = posixpath.normpath(project_path)
    normalized = posixpath.normpath(source)
    relative = posixpath.relpath(normalized, project_root)
    if relative == "." or not relative.startswith("../"):
        return relative
    return f"<external>/{posixpath.basename(normalized)}"


def _normalized_recommendation(
    recommendation: Mapping[str, Any] | None,
    project_path: str,
) -> dict[str, Any]:
    rec = deepcopy(dict(recommendation or {}))
    for field_name in ("build_root", "test_root", "root"):
        if rec.get(field_name):
            rec[field_name] = _relative_root(rec[field_name], project_path)
    for list_name in ("build_islands", "test_islands"):
        normalized = []
        for item in rec.get(list_name) or []:
            candidate = deepcopy(dict(item))
            if candidate.get("root"):
                candidate["root"] = _relative_root(candidate["root"], project_path)
            candidate["depends_on"] = tuple(
                _relative_root(dependency, project_path)
                for dependency in _normalize_dependencies(candidate.get("depends_on"))
            )
            normalized.append(candidate)
        if normalized:
            rec[list_name] = normalized
    return rec


def _read_text(orchestrator, path: str) -> str | None:
    try:
        try:
            result = orchestrator.execute_command(
                f"cat {shlex.quote(path)}",
                truncate_output=False,
            )
        except TypeError:
            result = orchestrator.execute_command(f"cat {shlex.quote(path)}")
    except Exception:
        return None
    if not _command_ok(result):
        return None
    return str(result.get("output") or "")


def _manifest_snapshot(
    orchestrator,
    analysis: Mapping[str, Any],
    project_path: str,
) -> dict[str, str]:
    candidates = set()
    for name in analysis.get("existing_files") or ():
        if name in _MANIFEST_NAMES:
            candidates.add(posixpath.join(project_path, name))
    recommendation = analysis.get("build_recommendation") or {}
    roots = [
        (
            recommendation.get("build_root"),
            recommendation.get("build_system"),
        )
    ]
    roots.extend(
        (item.get("root"), item.get("system")) for item in recommendation.get("build_islands") or []
    )
    by_system = {
        "maven": ("pom.xml",),
        "gradle": (
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradlew",
        ),
        "python": (
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "CMakeLists.txt",
        ),
    }
    for root, system in roots:
        if not root:
            continue
        names = by_system.get(str(system or "").strip().lower(), _MANIFEST_NAMES)
        for name in names:
            candidates.add(posixpath.join(str(root), name))

    snapshot = {}
    for path in sorted(candidates):
        content = _read_text(orchestrator, path)
        if content is not None:
            snapshot[_relative_root(path, project_path)] = content
    return snapshot


def _submodule_snapshot(orchestrator, project_path: str) -> tuple[str, ...]:
    try:
        command = f"git -C {shlex.quote(project_path)} submodule status --recursive " "2>/dev/null"
        result = orchestrator.execute_command(command)
    except Exception:
        return ()
    if not _command_ok(result):
        return ()
    lines = []
    for line in str(result.get("output") or "").splitlines():
        normalized = " ".join(line.replace(project_path, "<project>").split())
        if normalized:
            lines.append(normalized)
    return tuple(sorted(set(lines)))


def _overlay_toolchain(overlay: Mapping[str, Any]) -> dict[str, Any]:
    detected = {}
    for tool_name, tool in sorted((overlay.get("tools") or {}).items()):
        active = tool.get("active")
        candidate = (tool.get("candidates") or {}).get(active, {}) if active else {}
        detected[str(tool_name)] = {
            "active": bool(active),
            "version": candidate.get("version"),
            "source": candidate.get("source"),
        }
    return detected


def _toolchain_snapshot(
    analysis: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    python_config = analysis.get("python_config") or {}
    return {
        "project_type": analysis.get("project_type"),
        "build_system": analysis.get("build_system"),
        "java_version": analysis.get("java_version"),
        "java_version_source": analysis.get("java_version_source"),
        "python_version": python_config.get("python_version"),
        "python_installer": python_config.get("python_installer"),
        "has_native_build": bool(python_config.get("has_native_build")),
        "overlay": _overlay_toolchain(overlay),
    }


def _repo_docs_snapshot(analysis: Mapping[str, Any]) -> dict[str, str]:
    documentation = analysis.get("documentation") or {}
    content = str(documentation.get("readme_content") or "")
    if not content:
        return {}
    source_path = str(documentation.get("source_path") or "README").strip()
    return {source_path: content}


def project_brief_inputs_from_analysis(
    analysis: Mapping[str, Any],
    *,
    orchestrator,
    project_path: str,
    analyzer_version: str,
    composer_version: str = PROJECT_BRIEF_COMPOSER_VERSION,
    overlay: Mapping[str, Any] | None = None,
) -> ProjectBriefInputs:
    if overlay is None:
        try:
            from sag.runtime.env_overlay import EnvOverlayStore

            overlay = EnvOverlayStore(orchestrator).inspect()
        except Exception:
            overlay = {"version": 1, "tools": {}}
    recommendation = _normalized_recommendation(analysis.get("build_recommendation"), project_path)
    roots = [step.model_dump(mode="json") for step in _build_steps(recommendation)]
    roots.append(
        {
            "test_root": recommendation.get("test_root"),
            "test_system": recommendation.get("test_system"),
            "has_native_build": bool(recommendation.get("has_native_build")),
        }
    )
    repo_docs = _repo_docs_snapshot(analysis)
    for source_path in tuple(repo_docs):
        full_content = _read_text(
            orchestrator,
            posixpath.join(project_path, source_path),
        )
        if full_content is not None:
            repo_docs[source_path] = full_content
    return ProjectBriefInputs(
        manifest=_manifest_snapshot(orchestrator, analysis, project_path),
        detected_toolchain=_toolchain_snapshot(analysis, overlay),
        submodule_state=_submodule_snapshot(orchestrator, project_path),
        build_roots=tuple(roots),
        repo_docs=repo_docs,
        analyzer_version=str(analyzer_version),
        composer_version=str(composer_version),
    )


def _active_overlay_version(overlay: Mapping[str, Any], tool_name: str) -> Any:
    tool = (overlay.get("tools") or {}).get(tool_name, {})
    active = tool.get("active")
    if not active:
        return None
    return (tool.get("candidates") or {}).get(active, {}).get("version")


def project_brief_fragments_from_analysis(
    analysis: Mapping[str, Any],
    *,
    overlay: Mapping[str, Any] | None = None,
    project_path: str,
) -> tuple[BriefFragment, ...]:
    overlay = dict(overlay or {"version": 1, "tools": {}})
    documentation = analysis.get("documentation") or {}
    recommendation = _normalized_recommendation(analysis.get("build_recommendation"), project_path)
    fragments = []

    required_java = analysis.get("java_version")
    docs_java = documentation.get("java_version_requirement")
    if required_java:
        manifest_name = (
            "pom.xml"
            if str(analysis.get("build_system", "")).lower() == "maven"
            else "build.gradle"
        )
        fragments.append(
            BriefFragment(
                instruction_id="java-required",
                subject="java.version",
                role=InputRole.REQUIREMENT,
                value=str(required_java),
                text=f"Project manifests require JDK {required_java}.",
                source="manifest",
                refs=(f"manifest://{manifest_name}#java-version",),
            )
        )
    if docs_java and (not required_java or str(docs_java) != str(required_java)):
        source_path = documentation.get("source_path") or "README"
        fragments.append(
            BriefFragment(
                instruction_id=("java-doc-required" if required_java else "java-required"),
                subject="java.version",
                role=InputRole.REQUIREMENT,
                value=str(docs_java),
                text=f"Repository documentation requires JDK {docs_java}.",
                source="repo-doc",
                refs=(f"repo-doc://{source_path}#java-version",),
            )
        )

    current_java = _active_overlay_version(overlay, "java")
    if current_java:
        fragments.append(
            BriefFragment(
                instruction_id="java-current",
                subject="java.version",
                role=InputRole.EVIDENCE,
                value=str(current_java),
                text=f"Current environment overlay provides JDK {current_java}.",
                source="env-overlay",
                refs=("env-overlay://java",),
            )
        )
    if str(analysis.get("project_type", "")).lower() == "java":
        fragments.append(
            BriefFragment(
                instruction_id="java-default",
                subject="java.version",
                role=InputRole.DEFAULT,
                value="17",
                text="Assume JDK 17 only because no project requirement was detected.",
                source="runtime-default",
                refs=("default://java",),
            )
        )

    build_system = str(recommendation.get("build_system") or analysis.get("build_system") or "")
    if build_system:
        if build_system.strip().lower() == "python":
            dependency_text = (
                "Python project: use build(action='deps') to create the project venv, "
                "then build(action='compile') to verify byte-compilation. A missing Java "
                "target is not a blocker; never run pip through bash."
            )
        else:
            dependency_text = (
                "Use build(action='deps') only when compile evidence reports missing "
                "declared dependencies."
            )
        fragments.append(
            BriefFragment(
                instruction_id="install-deps",
                subject="dependencies.install",
                role=InputRole.REQUIREMENT,
                value="build:deps",
                text=dependency_text,
                source="analyzer",
                refs=("analysis://dependency-plan",),
            )
        )

    python_config = analysis.get("python_config") or {}
    if recommendation.get("has_native_build") or python_config.get("has_native_build"):
        python_root = recommendation.get("build_root") or python_config.get("python_root") or "."
        fragments.append(
            BriefFragment(
                instruction_id="native-first",
                subject="build.order.native",
                role=InputRole.REQUIREMENT,
                value="native-before-python",
                text=(
                    "This package has a NATIVE core (CMakeLists.txt at the repo root). "
                    "Build the native library FIRST; the Python package will not import "
                    "without it. Initialize empty submodules, then install the package "
                    f"from {python_root}. Long native builds detach; poll with search."
                ),
                source="analyzer",
                refs=("analysis://native-build",),
            )
        )

    test_root = recommendation.get("test_root")
    test_system = recommendation.get("test_system")
    if test_root or test_system:
        if str(test_system).lower() == "pytest":
            test_text = (
                "Run pytest with build(action='test'); a partial pass above threshold is "
                "a valid, honest outcome."
            )
        else:
            test_text = (
                f"Run tests with {test_system or 'the detected test system'} "
                f"at {test_root or '.'}."
            )
        fragments.append(
            BriefFragment(
                instruction_id="test-command",
                subject="test.command",
                role=InputRole.REQUIREMENT,
                value={"root": test_root or ".", "system": test_system or "unknown"},
                text=test_text,
                source="analyzer",
                refs=("analysis://test-recommendation",),
            )
        )
    elif documentation.get("test_commands"):
        source_path = documentation.get("source_path") or "README"
        command = str(documentation["test_commands"][0])
        fragments.append(
            BriefFragment(
                instruction_id="test-command",
                subject="test.command",
                role=InputRole.REQUIREMENT,
                value=command,
                text=f"Repository documentation proposes: {command}",
                source="repo-doc",
                refs=(f"repo-doc://{source_path}#tests",),
            )
        )
    return tuple(sorted(fragments, key=_fragment_key))


class ProjectBriefAdapter:
    """Compose one analyzer result with current overlay evidence and cache it."""

    def __init__(
        self,
        orchestrator,
        *,
        analyzer_version: str,
        composer_version: str = PROJECT_BRIEF_COMPOSER_VERSION,
        path: str = PROJECT_BRIEF_PATH,
    ) -> None:
        self.orchestrator = orchestrator
        self.analyzer_version = str(analyzer_version)
        self.composer_version = str(composer_version)
        self.store = ProjectBriefStore(orchestrator, path=path)
        self.composer = ProjectBriefComposer()

    def compose(
        self,
        analysis: Mapping[str, Any],
        *,
        project_path: str,
    ) -> ProjectBriefArtifact:
        try:
            from sag.runtime.env_overlay import EnvOverlayStore

            overlay = EnvOverlayStore(self.orchestrator).inspect()
        except Exception:
            overlay = {"version": 1, "tools": {}}
        inputs = project_brief_inputs_from_analysis(
            analysis,
            orchestrator=self.orchestrator,
            project_path=project_path,
            analyzer_version=str(analysis.get("analyzer_version") or self.analyzer_version),
            composer_version=self.composer_version,
            overlay=overlay,
        )
        fragments = project_brief_fragments_from_analysis(
            analysis,
            overlay=overlay,
            project_path=project_path,
        )
        recommendation = _normalized_recommendation(
            analysis.get("build_recommendation"), project_path
        )
        cached = self.store.load()
        cache_hit = bool(cached is not None and cached.input_fingerprint == inputs.fingerprint())
        brief = self.composer.compose(
            inputs,
            fragments,
            build_recommendation=recommendation,
            cached_brief=cached,
        )
        if not cache_hit:
            self.store.write(brief)
        return ProjectBriefArtifact(
            brief=brief,
            planner_projection=brief.to_planner_projection(full_ref=self.store.path),
            artifact_ref=self.store.path,
            cache_hit=cache_hit,
        )
