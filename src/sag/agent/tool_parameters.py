"""Representation-preserving parameter normalization for public tool calls.

The normalizer is deliberately not a repair engine.  It may translate a
retired public spelling into its current spelling, rename an unambiguous field
alias, coerce a JSON value to the declared schema type, or apply an explicit
schema default.  It must never choose an action, working directory, module,
version, command-line flag, retry, or fallback from runtime state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Dict, Optional

from loguru import logger as default_logger

from sag.agent.tool_orchestration import ParameterFix, ParameterFixSource
from sag.tools.base import BaseTool

_MAVEN_COMMAND_TO_BUILD_ACTION = {
    "deps": "deps",
    "dependency:resolve": "deps",
    "compile": "compile",
    "test": "test",
    "package": "package",
    "install": "install",
    # MavenBackend materializes the public test verb as Maven verify, so this
    # legacy spelling is representation-equivalent to build(action='test').
    "verify": "test",
}
_BUILD_ACTIONS = frozenset({"deps", "compile", "test", "package", "install"})


def _string_sequence(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item is not None).strip()
    return str(value or "").strip()


def _map_legacy_maven_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate the retired Maven facade without inventing a lifecycle.

    A caller-supplied current ``action``/``args`` field wins.  Known Maven
    lifecycle spellings have an exact public-build equivalent.  Unknown or
    missing lifecycle values remain invalid build actions so schema validation
    refuses them; the old ``compile + raw args`` fallback was a harness-authored
    replacement, not normalization.
    """

    raw = dict(params or {})
    mapped = {
        key: value
        for key, value in raw.items()
        if key not in {"command", "goals", "extra_args", "properties"}
    }

    if "action" not in mapped:
        selected = raw.get("command") if "command" in raw else raw.get("goals")
        command = _string_sequence(selected)
        mapped["action"] = _MAVEN_COMMAND_TO_BUILD_ACTION.get(command.lower(), command)

    if "args" not in mapped:
        arg_parts: list[str] = []
        if raw.get("extra_args"):
            arg_parts.append(str(raw["extra_args"]))
        properties = raw.get("properties")
        if isinstance(properties, Mapping):
            raise ValueError(
                "legacy Maven properties object has no exact public build args representation"
            )
        if isinstance(properties, (list, tuple)):
            arg_parts.extend(str(item) for item in properties if item)
        elif properties:
            arg_parts.append(str(properties))
        if arg_parts:
            mapped["args"] = " ".join(arg_parts)
    return mapped


def _map_legacy_gradle_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an exactly equivalent Gradle facade call or leave it invalid."""

    raw = dict(params or {})
    mapped = {key: value for key, value in raw.items() if key != "tasks"}
    if "action" not in mapped:
        task = _string_sequence(raw.get("tasks"))
        # Known facade verbs are exact aliases.  Unknown task strings are not
        # forced through compile; preserving them makes enum validation fail.
        mapped["action"] = task if task in _BUILD_ACTIONS else task
    return mapped


def _map_web_search_params(params: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(params or {})
    mapped = {key: value for key, value in raw.items() if key != "query"}
    if "target" not in mapped:
        query = str(raw.get("query") or "").strip()
        if not query:
            raise ValueError("legacy web_search requires query")
        mapped["target"] = f"web:{query}"
    return mapped


def _map_output_search_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Map only legacy retrieve/grep calls expressible by the Search facade."""

    raw = dict(params or {})
    action = str(raw.get("action") or "").strip().lower()
    if action and action not in {"retrieve", "grep"}:
        raise ValueError(f"legacy output_search action {action!r} has no exact search equivalent")
    unsupported = {
        "head_lines",
        "tail_lines",
        "task_id",
        "tool_name",
        "show_line_numbers",
        "extreme",
    }.intersection(raw)
    if unsupported:
        raise ValueError(
            "legacy output_search parameters have no exact search equivalent: "
            + ", ".join(sorted(unsupported))
        )
    mapped = {
        key: value
        for key, value in raw.items()
        if key not in {"action", "ref_id", "grep_pattern", "limit"}
    }
    if "target" not in mapped:
        target = str(raw.get("ref_id") or "").strip()
        if not target:
            raise ValueError("legacy output_search requires ref_id")
        mapped["target"] = target
    if "pattern" not in mapped and raw.get("grep_pattern") is not None:
        mapped["pattern"] = raw["grep_pattern"]
    if "max_results" not in mapped and raw.get("limit") is not None:
        mapped["max_results"] = raw["limit"]
    return mapped


def _map_project_setup_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve a legacy project-setup call in the Project facade spelling."""

    raw = dict(params or {})
    mapped = dict(raw)
    if "repo_url" not in mapped:
        for alias in ("repository_url", "url", "repo", "repository", "git_url"):
            if alias in raw:
                mapped["repo_url"] = raw[alias]
                break
    for alias in ("repository_url", "url", "repo", "repository", "git_url"):
        if alias != "repo_url":
            mapped.pop(alias, None)
    if "ref" not in mapped:
        for alias in ("branch", "tag", "release", "commit", "commit_hash", "version_ref"):
            if alias in raw:
                mapped["ref"] = raw[alias]
                break
    for alias in ("branch", "tag", "release", "commit", "commit_hash", "version_ref"):
        mapped.pop(alias, None)
    return mapped


def _map_project_analyzer_params(params: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(params or {})
    action = str(raw.get("action") or "analyze").strip().lower()
    if action != "analyze":
        raise ValueError(f"legacy project_analyzer action {action!r} is not analyze")
    mapped = {key: value for key, value in raw.items() if key != "action"}
    mapped["action"] = "analyze"
    if "project_path" not in mapped:
        for alias in ("working_directory", "path"):
            if alias in raw:
                mapped["project_path"] = raw[alias]
                break
    mapped.pop("working_directory", None)
    mapped.pop("path", None)
    return mapped


def _map_system_params(params: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(params or {})
    legacy_action = str(raw.get("action") or "").strip().lower()
    if legacy_action not in {"install", "install_java", "provision"}:
        raise ValueError(
            f"legacy system action {legacy_action or '<missing>'!r} has no exact project equivalent"
        )
    if legacy_action == "install" and (
        not raw.get("packages") or raw.get("java_version") not in (None, "")
    ):
        raise ValueError(
            "legacy system install maps exactly only with packages and without java_version"
        )
    if legacy_action == "install_java" and not str(raw.get("java_version") or "").strip():
        raise ValueError("legacy system install_java requires java_version")
    mapped = {key: value for key, value in raw.items() if key != "action"}
    mapped["action"] = "provision"
    return mapped


def _map_env_params(params: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(params or {})
    legacy_action = str(raw.get("action") or "").strip().lower()
    if legacy_action not in {"register", "env"}:
        raise ValueError(
            f"legacy env action {legacy_action or '<missing>'!r} has no exact project equivalent"
        )
    if legacy_action == "register" and raw.get("activate") is not True:
        raise ValueError(
            "legacy env register maps exactly only when activate=true; "
            "the public env action is atomic"
        )
    mapped = {key: value for key, value in raw.items() if key != "action"}
    mapped["action"] = "env"
    return mapped


class ToolParameterNormalizer:
    """Normalize only static representations, never model intent."""

    # Applied only when the retired name is not directly registered.  Each
    # mapper is deterministic from the submitted call alone; none consults
    # analyzer output, prior executions, repository identity, or filesystem
    # state.
    LEGACY_TOOL_ALIASES: Dict[
        str,
        tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]],
    ] = {
        "maven": ("build", _map_legacy_maven_params),
        "gradle": ("build", _map_legacy_gradle_params),
        "web_search": ("search", _map_web_search_params),
        "output_search": ("search", _map_output_search_params),
        # ProjectSetupTool had several verbs.  Preserve the submitted action;
        # an omitted or unsupported verb must fail instead of becoming clone.
        "project_setup": ("project", _map_project_setup_params),
        "project_analyzer": ("project", _map_project_analyzer_params),
        "system": ("project", _map_system_params),
        "env": ("project", _map_env_params),
    }

    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        repository_ref: Optional[str] = None,
        logger: Any = None,
    ) -> None:
        self.tools = tools
        # Retained as constructor compatibility only.  Reading any of these to
        # alter params would make a model-owned ActionIntent falsely describe a
        # controller-selected action.
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.repository_ref = repository_ref
        self.logger = logger or default_logger

    @staticmethod
    def _add_parameter_fix(
        fixes: list[ParameterFix],
        *,
        field: str,
        before: Any,
        after: Any,
        reason: str,
        source: ParameterFixSource,
    ) -> None:
        if before != after:
            fixes.append(
                ParameterFix(
                    field=field,
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            )

    def resolve_legacy_alias(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Map a retired public spelling to a statically equivalent facade."""

        if tool_name in self.tools or tool_name not in self.LEGACY_TOOL_ALIASES:
            return tool_name, dict(params or {})

        raw_params = dict(params or {})
        new_name, mapper = self.LEGACY_TOOL_ALIASES[tool_name]
        mapped = mapper(raw_params)
        fixes = parameter_fixes
        if fixes is not None:
            self._add_parameter_fix(
                fixes,
                field="tool",
                before=tool_name,
                after=new_name,
                reason=f"Mapped legacy tool '{tool_name}' to '{new_name}'",
                source="schema_alias",
            )
            if tool_name == "maven" and "goals" in raw_params:
                self._add_parameter_fix(
                    fixes,
                    field="goals",
                    before=raw_params.get("goals"),
                    after=None,
                    reason=(
                        "Removed legacy Maven goals because canonical action or command won"
                        if "action" in raw_params or "command" in raw_params
                        else "Renamed legacy Maven goals to build action"
                    ),
                    source="schema_alias",
                )
        return new_name, mapped

    def validate_and_fix(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Return the same action in canonical schema representation.

        Schema-invalid model data raises ``ValueError``.  The orchestrator
        converts that into a non-dispatched validation result, so no guessed
        replacement can be minted as ``source=model``.
        """

        fixes = parameter_fixes if parameter_fixes is not None else []
        if tool_name not in self.tools:
            tool_name, params = self.resolve_legacy_alias(
                tool_name,
                params,
                parameter_fixes=fixes,
            )
        if tool_name not in self.tools:
            raise ValueError(f"unknown tool: {tool_name}")

        raw = dict(params or {})
        tool = self.tools[tool_name]
        if hasattr(tool, "get_parameter_schema"):
            schema = tool.get_parameter_schema()
        elif hasattr(tool, "_get_parameters_schema"):
            schema = tool._get_parameters_schema()
        else:
            return raw
        if not isinstance(schema, Mapping):
            raise ValueError(f"invalid parameter schema for {tool_name}")
        return self._normalize_against_schema(raw, dict(schema), tool_name, fixes)

    def _normalize_against_schema(
        self,
        params: Dict[str, Any],
        schema: Dict[str, Any],
        tool_name: str,
        fixes: list[ParameterFix],
    ) -> Dict[str, Any]:
        properties = dict(schema.get("properties") or {})
        required = tuple(schema.get("required") or ())
        normalized = self._fix_parameter_names(params, properties, tool_name, fixes)

        # Every non-null default explicitly declared by the public schema is
        # part of the canonical call and is materialized before ActionIntent
        # freeze. Action-conditional defaults must be expressed by a static
        # JSON-schema if/then branch; no parameter/tool/state heuristic is
        # allowed here.
        default_properties = [properties]
        for clause in schema.get("allOf") or ():
            if not isinstance(clause, Mapping):
                continue
            condition = clause.get("if")
            branch = clause.get("then")
            if not isinstance(condition, Mapping) or not isinstance(branch, Mapping):
                continue
            if self._schema_condition_matches(normalized, condition):
                branch_properties = branch.get("properties")
                if isinstance(branch_properties, Mapping):
                    default_properties.append(dict(branch_properties))

        for default_set in default_properties:
            self._materialize_schema_defaults(normalized, default_set, fixes)

        for name, value in list(normalized.items()):
            prop = properties.get(name)
            if not isinstance(prop, Mapping) or value is None:
                continue
            expected_type = prop.get("type")
            if expected_type:
                converted = self._convert_parameter_type(value, str(expected_type), name)
                if converted != value:
                    normalized[name] = converted
                    self._add_parameter_fix(
                        fixes,
                        field=name,
                        before=value,
                        after=converted,
                        reason=f"Converted parameter '{name}' to {expected_type}",
                        source="safety_fix",
                    )

        missing = [
            name
            for name in required
            if name not in normalized
            or normalized[name] is None
            or (isinstance(normalized[name], str) and not normalized[name].strip())
        ]
        if missing:
            raise ValueError("missing required parameters: " + ", ".join(missing))

        if not schema.get("additionalProperties"):
            unexpected = sorted(set(normalized) - set(properties))
            if unexpected:
                raise ValueError("unexpected parameters: " + ", ".join(unexpected))

        for name, prop in properties.items():
            if name not in normalized or not isinstance(prop, Mapping):
                continue
            allowed = prop.get("enum")
            if allowed is not None and normalized[name] not in allowed:
                raise ValueError(
                    f"invalid value for {name}: {normalized[name]!r}; "
                    f"expected one of {list(allowed)!r}"
                )
        return normalized

    def _materialize_schema_defaults(
        self,
        normalized: Dict[str, Any],
        properties: Mapping[str, Any],
        fixes: list[ParameterFix],
    ) -> None:
        """Materialize only explicit, non-null schema defaults."""

        for name, prop in properties.items():
            if name in normalized and normalized[name] is not None:
                continue
            if not isinstance(prop, Mapping) or "default" not in prop or prop["default"] is None:
                continue
            before = normalized.get(name)
            normalized[name] = prop["default"]
            self._add_parameter_fix(
                fixes,
                field=name,
                before=before,
                after=prop["default"],
                reason=f"Applied schema default for parameter '{name}'",
                source="default",
            )

    @staticmethod
    def _schema_condition_matches(params: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
        """Evaluate the small static if-shape used for schema defaults.

        Unsupported predicates do not match. This deliberately cannot infer a
        condition from repository or execution state.
        """

        supported = {"properties", "required"}
        if set(condition) - supported:
            return False
        required = condition.get("required") or ()
        if not isinstance(required, (list, tuple)) or any(name not in params for name in required):
            return False
        predicates = condition.get("properties") or {}
        if not isinstance(predicates, Mapping):
            return False
        for name, predicate in predicates.items():
            if not isinstance(predicate, Mapping) or name not in params:
                return False
            if set(predicate) == {"const"}:
                if params[name] != predicate["const"]:
                    return False
                continue
            if set(predicate) == {"enum"} and isinstance(predicate["enum"], (list, tuple)):
                if params[name] not in predicate["enum"]:
                    return False
                continue
            return False
        return True

    @staticmethod
    def _convert_parameter_type(value: Any, expected_type: str, param_name: str) -> Any:
        """Perform only deterministic, representation-equivalent coercions."""

        if expected_type == "string":
            if isinstance(value, dict):
                converted = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            elif isinstance(value, list):
                converted = " ".join(str(item) for item in value)
            else:
                converted = str(value)
            if param_name == "key_results":
                return " ".join(converted.split())
            return converted

        if expected_type == "integer":
            if isinstance(value, bool):
                raise ValueError(f"parameter '{param_name}' is not an integer")
            return int(value)

        if expected_type == "number":
            if isinstance(value, bool):
                raise ValueError(f"parameter '{param_name}' is not a number")
            return float(value)

        if expected_type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "1", "yes", "on"}:
                    return True
                if lowered in {"false", "0", "no", "off"}:
                    return False
            if value in {0, 1}:
                return bool(value)
            raise ValueError(f"parameter '{param_name}' is not a boolean")

        if expected_type == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("["):
                    parsed = json.loads(text)
                    if not isinstance(parsed, list):
                        raise ValueError(f"parameter '{param_name}' is not an array")
                    return parsed
                return [item.strip() for item in value.split(",") if item.strip()]
            return [value]

        if expected_type == "object":
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            raise ValueError(f"parameter '{param_name}' is not an object")
        return value

    def _rename_aliases(
        self,
        params: Dict[str, Any],
        properties: Dict[str, Any],
        mappings: Mapping[str, str],
        fixes: list[ParameterFix],
    ) -> Dict[str, Any]:
        normalized = dict(params)
        for alias, canonical in mappings.items():
            if alias not in normalized or canonical not in properties or alias == canonical:
                continue
            alias_value = normalized.pop(alias)
            if canonical in normalized:
                self._add_parameter_fix(
                    fixes,
                    field=alias,
                    before=alias_value,
                    after=None,
                    reason=(
                        f"Removed alias '{alias}' because canonical field "
                        f"'{canonical}' was supplied"
                    ),
                    source="schema_alias",
                )
                continue
            normalized[canonical] = alias_value
            self._add_parameter_fix(
                fixes,
                field=canonical,
                before=None,
                after=alias_value,
                reason=f"Renamed parameter '{alias}' to '{canonical}'",
                source="schema_alias",
            )
        return normalized

    def _fix_parameter_names(
        self,
        params: Dict[str, Any],
        properties: Dict[str, Any],
        tool_name: str,
        fixes: list[ParameterFix],
    ) -> Dict[str, Any]:
        """Rename documented-equivalent fields; canonical fields always win."""

        general = {
            "op": "action",
            "operation": "action",
            "method": "action",
            "type": "action",
            "search": "query",
            "q": "query",
            "term": "query",
            "search_term": "query",
            "keywords": "query",
            "url": "repository_url",
            "repo_url": "repository_url",
            "git_url": "repository_url",
            "repository": "repository_url",
            "repo": "repository_url",
            "git_repo": "repository_url",
            "destination": "target_directory",
            "dest": "target_directory",
            "target_dir": "target_directory",
            "output_dir": "target_directory",
            "clone_dir": "target_directory",
            "options": "properties",
            "opts": "properties",
            "maven_options": "properties",
            "build_options": "properties",
            "context_type": "action",
            "name": "task_id",
            "parameters": "summary",
            "task_name": "task_id",
            "id": "task_id",
            "data": "content",
            "text": "content",
            "body": "content",
            "file_content": "content",
        }
        tool_specific: Dict[str, Dict[str, str]] = {
            "bash": {
                "cmd": "command",
                "script": "command",
                "exec": "command",
                "shell": "command",
                "run": "command",
                "execute": "command",
                "bash_command": "command",
                "shell_command": "command",
                "path": "working_directory",
            },
            "file_io": {
                "file": "path",
                "filename": "path",
                "filepath": "path",
                "file_path": "path",
            },
            "project_setup": {
                "output": "target_directory",
                "tag": "ref",
                "release": "ref",
                "commit": "ref",
                "commit_hash": "ref",
                "version_ref": "ref",
            },
            "report": {
                "content": "summary",
                "outcome": "status",
                "evidence": "evidence_refs",
                "key_results": "details",
            },
            "maven": {
                "project_dir": "working_directory",
                "cmd": "command",
                "maven_command": "command",
            },
            "manage_context": {
                "target": "action",
                "switch": "action",
                "description": "entry",
                "content": "entry",
                "data": "entry",
                "info": "entry",
                "details": "entry",
                "context": "entry",
                "observation": "entry",
                "result": "entry",
                "completion_summary": "summary",
                "task_summary": "summary",
                "results": "summary",
            },
        }

        normalized = dict(params)
        if tool_name in {"build", "python", "maven", "gradle", "bash"}:
            normalized = self._rename_aliases(
                normalized,
                properties,
                {
                    "cwd": "working_directory",
                    "workdir": "working_directory",
                    "working_dir": "working_directory",
                    "work_dir": "working_directory",
                    "dir": "working_directory",
                    "directory": "working_directory",
                },
                fixes,
            )

        if tool_name == "project" and normalized.get("action") == "analyze":
            normalized = self._rename_aliases(
                normalized,
                properties,
                {"path": "project_path"},
                fixes,
            )
        normalized = self._rename_aliases(
            normalized,
            properties,
            tool_specific.get(tool_name, {}),
            fixes,
        )
        return self._rename_aliases(normalized, properties, general, fixes)
