"""Frozen reviews of literal Maven JVM configuration, shared by both recorders.

Only exact, explicitly reviewed task inputs can be accepted. An unrelated
environment override or another argument file never inherits that approval.
The review describes the CI's original input; it does not remove its effects.
"""

from __future__ import annotations

import hashlib
import re
import shlex


def observer_rc_known(spec, observed):
    """Only the prospectively frozen passive observer may supply this startup rc."""
    policy = spec.get('maven_observer', {})
    expected = {path: policy.get(key) for path, key in (
        ('/etc/mavenrc', 'activation_sha256'),
        ('/opt/sag-maven-witness/launch.py', 'launcher_sha256'),
        ('/opt/sag-maven-witness/witness.jar', 'observer_sha256'))}
    return (policy.get('policy') == 'passive-maven-events-v1' and isinstance(observed, dict)
            and all(isinstance(v, str) and len(v) == 64 for v in expected.values())
            and observed == expected)


JVM_ENVIRONMENT_NAMES = ("MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")
MEMORY_OPTION = r"-(?:Xm[sx]\d+[kKmMgG]?|XX:(?:MaxMetaspaceSize|ReservedCodeCacheSize)=\d+[kKmMgG]?)"


def literal_tokens(raw):
    tokens = raw.decode("utf-8").split()
    if not tokens or any(
        any(c in arg for c in "'\"\\$`#") or arg.startswith("@") for arg in tokens
    ):
        raise ValueError("JVM input review requires literal tokens without expansion or argfiles")
    return tokens


def validate_review(review, commit):
    if (
        not isinstance(review, dict)
        or review.get("policy") != "pinned-maven-jvm-config-v1"
        or review.get("source_commit") != commit
        or review.get("source_path") != ".mvn/jvm.config"
        or not re.fullmatch(r"[a-f0-9]{64}", str(review.get("sha256", "")))
        or type(review.get("bytes")) is not int
        or review["bytes"] <= 0
        or not review.get("reviewed_by")
        or not review.get("review_basis")
    ):
        raise ValueError("JVM configuration review is incomplete or bound to another revision")
    tokens = review.get("tokens")
    if (
        not isinstance(tokens, list)
        or any(not isinstance(arg, str) for arg in tokens)
        or literal_tokens("\n".join(tokens).encode()) != tokens
    ):
        raise ValueError("JVM configuration tokens are not literal")
    properties = [arg for arg in tokens if arg.startswith("-D")]
    decisions = review.get("properties", [])
    if (
        not isinstance(decisions, list)
        or [row.get("argument") for row in decisions] != properties
        or any(
            row.get("effect") not in {"runtime_property", "task_property"} or not row.get("basis")
            for row in decisions
        )
    ):
        raise ValueError("Every pinned JVM property needs a source review of its effect")
    return review


def review_for(spec, step):
    return next(
        (
            s.get("jvm_config_review")
            for s in spec.get("steps", [])
            if s.get("step_id") == step["id"]
        ),
        None,
    )


def matches(spec, task, step, raw):
    review = review_for(spec, step)
    if review is None:
        return False
    validate_review(review, task["sha"])
    return (
        raw is not None
        and hashlib.sha256(raw).hexdigest() == review["sha256"]
        and len(raw) == review["bytes"]
        and literal_tokens(raw) == review["tokens"]
    )


def environment_review_for(spec, step):
    return next((s.get("jvm_environment_review") for s in spec.get("steps", [])
                 if s.get("step_id") == step["id"]), None)


def validate_environment_review(review, task, step, *, base=None):
    """Review one frozen step's public JVM options, never arbitrary variables."""
    if (not isinstance(review, dict) or review.get("policy") != "pinned-jvm-environment-v1"
            or review.get("source_commit") != task["sha"] or review.get("repository") != task["repo"]
            or review.get("step_id") != step["id"] or review.get("argv") != step["argv"]
            or step.get("runner") != "maven" or not review.get("reviewed_by")
            or not review.get("review_basis")):
        raise ValueError("JVM environment review differs from the frozen task")
    values = review.get("values")
    if not isinstance(values, dict) or not values or set(values) - set(JVM_ENVIRONMENT_NAMES):
        raise ValueError("JVM environment review requires explicit JVM option variables")
    for name, item in values.items():
        value = item.get("value")
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise ValueError("Reviewed JVM environment value must be one literal line")
        tokens = literal_tokens(value.encode())
        if any(not (re.fullmatch(MEMORY_OPTION, token) or re.fullmatch(r"-D[\w.-]+=[^\s]+", token)) for token in tokens):
            raise ValueError("Only reviewed properties and memory settings are supported")
        properties = [token for token in tokens if token.startswith("-D")]
        decisions = item.get("properties", [])
        if (not isinstance(decisions, list) or [r.get("argument") for r in decisions] != properties
                or any(r.get("effect") not in {"runtime_property", "task_property"} or not r.get("basis") for r in decisions)):
            raise ValueError("Every JVM environment property needs an explicit effect review")
        ref, line = item.get("source", {}), item.get("source_line")
        if (not ref.get("path") or not re.fullmatch(r"[a-f0-9]{64}", str(ref.get("sha256", "")))
                or type(ref.get("bytes")) is not int or ref["bytes"] <= 0 or type(line) is not int or line < 1):
            raise ValueError("JVM environment review needs a byte-bound source line")
        if base is not None:
            from .ci_sources import read_source
            from .ci_native import ci_native_text
            lines = read_source(base, ref).decode("utf-8-sig").splitlines()
            if line > len(lines) or ci_native_text(lines[line - 1]).strip() != name + ": " + value:
                raise ValueError("Reviewed JVM environment is not present in its archived source")
    return review


def environment_errors(spec, task, step, actual, *, base=None):
    """A declared value must be present exactly; extra properties remain unknown."""
    review = environment_review_for(spec, step)
    approved = {}
    if review is not None:
        validate_environment_review(review, task, step, base=base)
        approved = {name: item["value"] for name, item in review["values"].items()}
    errors = []
    for name in JVM_ENVIRONMENT_NAMES:
        value = actual.get(name, "")
        if name in approved:
            if value != approved[name]:
                errors.append(name + " differs from the frozen task environment")
        elif any(arg.startswith(("-D", "@")) for arg in shlex.split(value)):
            errors.append(name + " contains unreviewed execution properties")
    return errors
