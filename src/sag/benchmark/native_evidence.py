"""Small, conservative native evidence readers. Unsupported semantics stay unknown."""

from __future__ import annotations

import re
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from .requirements import bound_file, repository_artifact_path

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HEADER = re.compile(
    r"^\[INFO\]\s+---\s+(\S+?):([^:\s]+):([^\s:]+)\s+\(([^)]*)\)\s+@\s+(\S+)\s+---", re.M
)
FAILURE = re.compile(
    r"Failed to execute goal\s+(\S+):([^:\s]+):([^:\s]+)\s+\(([^)]*)\)\s+on project\s+([^:\s]+)"
)
FORK_LINE = re.compile(r"^\[INFO\][^\n]*(?:>>>|<<<)[^\n]*$", re.M)
FORK = re.compile(
    r"^\[INFO\]\s+(>>>|<<<)\s+(\S+?):([^:\s]+):([^\s:]+)\s+\(([^)]*)\)\s+([><])\s+(\S+)\s+@\s+(\S+)\s+\1\s*$"
)


def parse_local_repository(text):
    """Metadata probes may also print Maven/JDK banners because CI passes -V."""
    paths = [
        line.strip() for line in ANSI.sub("", text).splitlines() if line.strip().startswith("/")
    ]
    if len(paths) != 1 or "\x00" in paths[0] or posixpath.normpath(paths[0]) != paths[0]:
        return None
    return paths[0]


def goal_name(plugin, goal):
    qualified = ":" in plugin
    plugin = plugin.rsplit(":", 1)[-1]
    if plugin.startswith("maven-") and plugin.endswith("-plugin"):
        plugin = plugin[6:-7]
    elif qualified and plugin.endswith("-maven-plugin"):
        plugin = plugin[:-13]
    # Modern banners already contain the descriptor's literal prefix. It may
    # intentionally end in '-plugin'; stripping it invents another plugin.
    return f"{plugin}:{goal}"


def explicit_goal_skip(goal, segment):
    plugin = goal.split(":", 1)[0]
    patterns = {
        "javadoc": r"(?:Skipping (?:javadoc|Javadoc) generation|Javadoc generation is skipped)",
        "surefire": r"(?:Tests are skipped|No tests to run)\.",
        "failsafe": r"(?:Tests are skipped|No tests to run)\.",
        "enforcer": r"Skipping [Rr]ule [Ee]nforcement\.?",
        "apache-rat": r"Skipping (?:RAT|rat|Apache Rat)(?: check| execution)?\.?",
        "checkstyle": r"Skipping (?:execution of )?[Cc]heckstyle.*",
        "pmd": r"Skipping (?:execution of )?(?:PMD|pmd|CPD|cpd).*",
        "spotbugs": r"Skipping (?:execution of )?(?:SpotBugs|spotbugs).*",
        "jar": r"Skipping (?:packaging of the jar|jar packaging).*",
        "jacoco": r"Skipping JaCoCo execution(?: due to missing execution data file(?::.*)?| because property jacoco\.skip is set)?\.?",
    }
    pattern = patterns.get(plugin)
    return bool(pattern and re.search(r"^\[INFO\]\s+(?:" + pattern + r")\s*$", segment, re.M))


def bound_maven_goal(plugin, version, goal, execution, module, bindings):
    candidates = [b for b in bindings if (b['module'], b['execution'], b['goal'])
                  == (module, execution, goal)]
    matched = [b for b in candidates if b['version'] == version
               and plugin in (b['prefix'], b['artifact'], b['coordinates'])]
    if len(matched) == 1:
        binding = matched[0]
        return binding['prefix'] + ':' + goal, binding, False
    # A declared execution with the wrong coordinates must not pass merely
    # because goal_name() discards its group id. Nor can an ambiguous alias pass.
    conflict = bool(candidates) or len(matched) > 1
    return goal_name(plugin, goal), None, conflict


def _top_level_goals(text, terminal, plugin_bindings=()):
    """Recognize balanced Maven forks; never use nested goals as task evidence."""
    tokens = [(m.start(), "goal", m) for m in HEADER.finditer(text)]
    tokens += [(m.start(), "fork", m) for m in FORK_LINE.finditer(text)]
    stack, headers = [], []
    framing_known, active = True, None
    for position, kind, match in sorted(tokens, key=lambda token: token[0]):
        if kind == "goal":
            top = framing_known and not stack
            if active is not None:
                active.update(end=position, returned=top)
            header = {
                "match": match,
                "top": top,
                "end": len(text),
                "returned": False,
                "failed": False,
            }
            headers.append(header)
            active = header if top else None
            continue
        marker = FORK.fullmatch(match.group())
        entering = bool(marker and marker[1] == ">>>" and marker[6] == ">")
        if active is not None:
            # Entering the next top-level goal's fork proves the previous goal
            # returned. Cut its segment here so nested errors cannot taint it.
            active.update(end=position, returned=framing_known and not stack and entering)
            active = None
        if marker is None or (marker[1], marker[6]) not in {(">>>", ">"), ("<<<", "<")}:
            framing_known = False
            continue
        identity = (marker[2], marker[3], marker[4], marker[5], marker[7], marker[8])
        if entering:
            stack.append(identity)
        elif stack and stack[-1] == identity:
            stack.pop()
        else:
            framing_known = False
    if active is not None:
        active["returned"] = terminal and bool(
            re.search(r"^\[INFO\]\s+BUILD SUCCESS\s*$", text[active["match"].end() :], re.M)
        )
    # Attribute late failure summaries to the most recent matching occurrence,
    # including nested occurrences, so a nested failure cannot rewrite an
    # earlier successful top-level compile with the same module/execution.
    for failure in FAILURE.finditer(text):
        owner = next(
            (
                h
                for h in reversed(headers)
                if h["match"].start() < failure.start()
                and bound_maven_goal(*h["match"].groups(), plugin_bindings)[0]
                == bound_maven_goal(*failure.groups(), plugin_bindings)[0]
                and not bound_maven_goal(*failure.groups(), plugin_bindings)[2]
                and h["match"][4] == failure[4]
                and h["match"][5] == failure[5]
            ),
            None,
        )
        if owner is not None:
            owner["failed"] = True
    return [h for h in headers if h["top"]]


def launcher_java_home(text):
    """A unique absolute java.home printed by the invoked JVM, never an env guess."""
    import posixpath

    homes = set(re.findall(r"^\s*java\.home\s*=\s*(/[^\r\n]+?)\s*$", text, re.M))
    if len(homes) != 1:
        return None
    home = homes.pop()
    if home == "/" or posixpath.normpath(home) != home or "\0" in home:
        return None
    return home


def maven_events(text, *, terminal, serial, plugin_bindings=()):
    """Only independently returned top-level goals can satisfy frozen requirements.

    Goals inside forks, and after malformed boundaries, provide no positive
    completion evidence. Earlier top-level returns retain their own outcome.
    """
    text = ANSI.sub("", text)
    counts, result = {}, []
    for index, header in enumerate(_top_level_goals(text, terminal, plugin_bindings)):
        match = header["match"]
        plugin, version, goal, execution, module = match.groups()
        name, binding, identity_conflict = bound_maven_goal(plugin, version, goal, execution, module, plugin_bindings)
        key = (module, name, execution)
        occurrence = counts.get(key, 0)
        counts[key] = occurrence + 1
        segment = text[match.end() : header["end"]]
        state = (
            "failed"
            if header["failed"]
            else (
                "not_run"
                if explicit_goal_skip(name, segment)
                else (
                    "passed"
                    if serial and header["returned"] and not re.search(r"^\[ERROR\]", segment, re.M)
                    else "unavailable"
                )
            )
        )
        result.append(
            {
                "goal": name,
                "version": version,
                "module": module,
                "execution": execution,
                "occurrence": occurrence,
                "position": index,
                "status": state,
                "returned": bool(serial and header["returned"]),
                "segment": segment,
                "line": text[: match.start()].count("\n") + 1,
                **({"plugin_identity": binding['coordinates'], "plugin_identity_evidence": binding['source'],
                    "banner_plugin": plugin} if binding else {}),
                **({"plugin_identity_conflict": True} if identity_conflict else {}),
            }
        )
    return result


def _single_rerun_proof(suite, cases, declared, actual, segment):
    """Corroborate one successful Surefire retry without trusting its broken header.

    Some producers retain all case rows but replace suite statistics with the
    last retry. Admit only two matching class summaries, one retry per flaky
    case, and identical final case outcomes. More complex retries stay unknown.
    """
    if not segment or suite.get("version") != "3.0":
        return None
    schema = suite.get("{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation", "")
    if schema != "https://maven.apache.org/surefire/maven-surefire-plugin/xsd/surefire-test-report-3.0.xsd":
        return None
    name = suite.get("name")
    if not name or actual["failures"] or actual["errors"]:
        return None
    identities, flaky = set(), {"flakyFailure": 0, "flakyError": 0}
    for case in cases:
        identity = (case.get("classname"), case.get("name"))
        if identity[0] != name or not identity[1] or identity in identities:
            return None
        identities.add(identity)
        outcomes = [child.tag.rsplit("}", 1)[-1] for child in case]
        retry_tags = [tag for tag in outcomes if tag in {"flakyFailure", "flakyError", "rerunFailure", "rerunError"}]
        if not retry_tags:
            continue
        if len(retry_tags) != 1 or retry_tags[0] not in flaky or "skipped" in outcomes:
            return None
        flaky[retry_tags[0]] += 1
    retries = sum(flaky.values())
    if not retries:
        return None
    pattern = (r"^\[(?:INFO|WARNING|ERROR)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),"
               r"\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+),[^\n]*?\s+-+ in " + re.escape(name) + r"\s*$")
    summaries = [tuple(map(int, match)) for match in re.findall(pattern, segment, re.M)]
    first = (actual["tests"], flaky["flakyFailure"], flaky["flakyError"], actual["skipped"])
    last = (retries, 0, 0, 0)
    original = tuple(declared[k] for k in ("tests", "failures", "errors", "skipped"))
    resolved = tuple(actual[k] for k in ("tests", "failures", "errors", "skipped"))
    if summaries != [first, last] or original not in {last, resolved}:
        return None
    return {"rule": "surefire_single_rerun_class_and_case_agreement", "suite": name,
            "raw_suite_counts": dict(declared), "resolved_case_counts": dict(actual),
            "class_summaries": [list(first), list(last)], "flaky_cases": retries,
            "retry_executions": retries, "header_normalized": declared != actual}


def junit_counts(base, reports, *, native_segment=None, reconciliations=None):
    counts = dict(reported=0, passed=0, failed=0, errors=0, skipped=0, assessed=0)
    seen = set()
    for ref in reports:
        if ref.get("fresh") is not True:
            raise ValueError("Test report freshness is unavailable")
        path = bound_file(base, ref)
        if str(path) in seen:
            raise ValueError("Duplicate report evidence")
        seen.add(str(path))
        root = ET.parse(path).getroot()
        suites = [
            x
            for x in root.iter()
            if x.tag.rsplit("}", 1)[-1] == "testsuite"
            and not any(y.tag.rsplit("}", 1)[-1] == "testsuite" for y in x if y is not x)
        ]
        if not suites:
            raise ValueError("No native test suites")
        for suite in suites:
            cases = [x for x in suite if x.tag.rsplit("}", 1)[-1] == "testcase"]
            declared = {
                k: int(suite.get(k, "0")) for k in ("tests", "failures", "errors", "skipped")
            }
            if any(v < 0 for v in declared.values()):
                raise ValueError("Negative JUnit count")
            if cases:
                c = {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0}
                for case in cases:
                    tags = {x.tag.rsplit("}", 1)[-1] for x in case}
                    states = tags & {"failure", "error", "skipped"}
                    if len(states) > 1:
                        raise ValueError("Conflicting testcase result")
                    if states:
                        c[
                            {"failure": "failures", "error": "errors", "skipped": "skipped"}[
                                states.pop()
                            ]
                        ] += 1
                proof = _single_rerun_proof(suite, cases, declared, c, native_segment)
                if c != declared and proof is None:
                    raise ValueError("JUnit case and suite counts conflict")
                if proof is not None:
                    declared = c
                    if reconciliations is not None:
                        reconciliations.append({**proof, "report": ref})
            if sum(declared[k] for k in ("failures", "errors", "skipped")) > declared["tests"]:
                raise ValueError("JUnit counts exceed reported tests")
            counts["reported"] += declared["tests"]
            counts["failed"] += declared["failures"]
            counts["errors"] += declared["errors"]
            counts["skipped"] += declared["skipped"]
    counts["assessed"] = counts["reported"] - counts["skipped"]
    counts["passed"] = counts["assessed"] - counts["failed"] - counts["errors"]
    return counts


def artifact_proof(base, expected, artifacts, *, verified_installed_copies=None):
    """Require declared role/location, producer evidence and readable bytes.

    A main artifact is never substituted by a tests/sources/classifier artifact.
    POM aggregators express no binary obligation in metadata, not an empty pass here.
    Unchanged installed destinations need a separately recomputed native copy
    proof from a fresh source; mere destination existence is insufficient.
    """
    if not expected:
        return False
    for item in expected:
        if item.get("repository_relative_path"):
            location = "repository_relative_path"
            target = repository_artifact_path(item)
        else:
            location, target = "producer_relative_path", item.get("path")
        if not isinstance(target, str) or not target:
            return False
        candidates = [
            a
            for a in artifacts
            if a.get(location) == target
            and a.get("role") == item.get("role")
            and a.get("module") == item.get("module")
        ]
        if len(candidates) != 1:
            return False
        artifact = candidates[0]
        copied = (verified_installed_copies or {}).get(target, {}) if location == "repository_relative_path" else {}
        if artifact.get("fresh") is not True and (not copied or copied.get("sha256") != artifact.get("sha256")):
            return False
        if item.get("coordinates") and artifact.get("coordinates") != item["coordinates"]:
            return False
        if artifact.get("classifier") != item.get("classifier"):
            return False
        if (
            item.get("extension")
            and artifact.get("extension", artifact.get("format")) != item["extension"]
        ):
            return False
        path = bound_file(base, artifact)
        if not path.stat().st_size:
            return False
        if item.get("format", item.get("extension")) in {
            "jar",
            "war",
            "zip",
            "bundle",
        } or target.endswith((".jar", ".war", ".zip")):
            try:
                with zipfile.ZipFile(path) as archive:
                    if archive.testzip() is not None or not archive.namelist():
                        return False
            except zipfile.BadZipFile:
                return False
    return True


def fail_fast_observable(invocation, text):
    inputs = invocation.get("effective_execution", {})
    if (
        inputs.get("inputs_complete") is not True
        or inputs.get("serial") is not True
        or inputs.get("wrapper_reviewed") is not True
    ):
        return False
    # The recorder must include all effective sources, not only requested argv.
    if not all(isinstance(inputs.get(k), list) for k in ("argv", "maven_config", "maven_args")):
        return False
    args = inputs["argv"] + inputs["maven_config"] + inputs["maven_args"]
    forbidden = {"-fae", "--fail-at-end", "-fn", "--fail-never", "--continue", "-q", "--quiet"}
    if any(
        x in forbidden or x == "-T" or x.startswith("-T") or x.startswith("--threads") for x in args
    ):
        return False
    return (
        invocation.get("status") == "completed"
        and invocation.get("log_complete") is True
        and not re.search(r"^\[INFO\].*(?:>>>|<<<)", ANSI.sub("", text), re.M)
    )
