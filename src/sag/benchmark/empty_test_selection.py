"""Source-bound empty Surefire selection review shared by importer and verifier.

This intentionally supports a bounded discovery profile, not arbitrary Java.
Unsupported discovery stays unavailable and never becomes a passed test pool.
"""

import hashlib
import re
from pathlib import PurePosixPath
from .ci_native import ci_native_text, ci_binding_inventory


def text_at(root, path):
    found = root.findtext(path)
    return found.strip() if found and found.strip() else None


def reviewed_empty_surefire_selection(binding, model, witness, source_base, task, workspace):
    """Validate an explicit, source-bound review of an empty default test scope.

    This is intentionally narrower than general Java or Maven discovery. The
    reviewed module has one ordinary test source, no Surefire configuration or
    custom discovery, and no preceding source-producing goal except javac.
    Native compilation and the complete pinned source tree corroborate it.
    """
    import fnmatch
    import zipfile
    from sag.benchmark.requirements import bound_file, load_json

    if (
        binding["goal"] != "surefire:test"
        or binding["version"] != "3.5.6"
        or witness.get("effective_pom_sha256") is None
        or witness.get("reviewed_by") is None
        or not witness.get("reason")
    ):
        raise ValueError("Empty selection requires a versioned explicit Surefire review")
    plugins = [
        p
        for p in model.findall("build/plugins/plugin")
        if text_at(p, "artifactId") == "maven-surefire-plugin"
    ]
    if (
        len(plugins) != 1
        or text_at(plugins[0], "version") != binding["version"]
        or plugins[0].find("configuration") is not None
        or plugins[0].find("dependencies") is not None
        or any(
            e.find("configuration") is not None for e in plugins[0].findall("executions/execution")
        )
    ):
        raise ValueError("Custom Surefire selection cannot use the empty default-scope review")
    tokens = task["steps"][0]["argv"]
    if any(
        t.startswith("-D") or t == "--define" or t.startswith("--define=") for t in tokens
    ) or any(
        re.match(r"(?:test|surefire\.|maven\.test)", x.tag) for x in model.findall("properties/*")
    ):
        raise ValueError("Test selectors or test properties make empty default scope uncertain")
    tree = load_json(bound_file(source_base, witness["git_tree"]))
    commit = load_json(bound_file(source_base, witness["git_commit"]))
    if (
        commit.get("sha") != task["sha"]
        or tree.get("truncated") is not False
        or tree.get("sha") not in {task["sha"], commit.get("commit", {}).get("tree", {}).get("sha")}
    ):
        raise ValueError("Empty selection needs the complete exact-commit Git tree")
    source_dir = text_at(model, "build/testSourceDirectory")
    test_root = str(PurePosixPath(source_dir).relative_to(workspace))
    if (
        not test_root.endswith("/src/test/java")
        or text_at(model, "build/testOutputDirectory")
        != source_dir.removesuffix("/src/test/java") + "/target/test-classes"
    ):
        raise ValueError(
            "Custom test source or output directories require separate discovery review"
        )
    files = [
        r for r in tree["tree"] if r.get("type") == "blob" and r["path"].startswith(test_root + "/")
    ]
    resources_root = test_root.removesuffix("/java") + "/resources/"
    resources = model.findall("build/testResources/testResource")
    if len(resources) > 1 or any(
        {x.tag for x in r} != {"directory"}
        or text_at(r, "directory") != workspace.rstrip("/") + "/" + resources_root.rstrip("/")
        for r in resources
    ):
        raise ValueError("Custom test resources require a separate selected-class inventory")
    resource_plugins = [
        p
        for p in model.findall("build/plugins/plugin")
        if text_at(p, "artifactId") == "maven-resources-plugin"
    ]
    if any(
        p.find("configuration") is not None
        or p.find("dependencies") is not None
        or any(e.find("configuration") is not None for e in p.findall("executions/execution"))
        for p in resource_plugins
    ):
        raise ValueError("Custom resource copying can introduce selected test classes")
    if any(
        r.get("type") == "blob"
        and r["path"].startswith(resources_root)
        and r["path"].endswith(".class")
        for r in tree["tree"]
    ):
        raise ValueError("Copied test class resources can change default test discovery")
    reviewed = witness.get("test_sources", [])
    if (
        len(files) != 1
        or len(reviewed) != 1
        or files[0].get("mode") not in {"100644", "100755"}
        or files[0]["path"] != reviewed[0].get("source_path")
        or not files[0]["path"].endswith(".java")
    ):
        raise ValueError(
            "Empty selection review does not cover the complete simple test-source inventory"
        )
    source = bound_file(source_base, reviewed[0]).read_bytes()
    if (
        hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
        != files[0]["sha"]
    ):
        raise ValueError("Reviewed test source differs from its pinned Git blob")
    # Remove lexical comments/literals before checking the reviewed declarations.
    lexical = re.sub(
        r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", source.decode()
    )
    type_matches = list(
        re.finditer(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)", lexical)
    )
    declarations = [m[1] for m in type_matches]
    depths = [
        lexical[: m.start()].count("{") - lexical[: m.start()].count("}") for m in type_matches
    ]
    if (
        declarations != reviewed[0].get("declared_types")
        or not declarations
        or '"""' in source.decode()
        or depths != [0] + [1] * (len(declarations) - 1)
        or not re.search(
            r"\bpublic\s+class\s+" + re.escape(PurePosixPath(files[0]["path"]).stem) + r"\b",
            lexical,
        )
    ):
        raise ValueError(
            "Review must preserve the actual Java declarations, not infer them from file names"
        )
    jar_path = bound_file(source_base, witness["surefire_sources"])
    with zipfile.ZipFile(jar_path) as jar:
        source_text = jar.read("org/apache/maven/plugin/surefire/SurefireMojo.java").decode()
        props = jar.read(
            "META-INF/maven/org.apache.maven.plugins/maven-surefire-plugin/pom.properties"
        ).decode()
    defaults = ["**/Test*.java", "**/*Test.java", "**/*Tests.java", "**/*TestCase.java"]
    match = re.search(
        r"protected String\[\] getDefaultIncludes\(\)\s*\{\s*return new String\[\]\s*\{([^}]+)\};\s*\}",
        source_text,
    )
    if (
        not match
        or re.findall(r'"([^"\n]+)"', match[1]) != defaults
        or not re.search(r"^version=3\.5\.6\s*$", props, re.M)
    ):
        raise ValueError(
            "Versioned Surefire source does not establish the reviewed default patterns"
        )
    binary_names = reviewed[0].get("reviewed_binary_names", [])
    if (
        len(binary_names) != len(declarations)
        or binary_names[0] != declarations[0]
        or [n.rsplit("$", 1)[-1] for n in binary_names] != declarations
        or any(not n.startswith(binary_names[0] + "$") for n in binary_names[1:])
    ):
        raise ValueError("Explicit nested Java type review is missing or inconsistent")
    if any(fnmatch.fnmatchcase("/" + name + ".java", p) for name in binary_names for p in defaults):
        raise ValueError("Pinned Java declarations include a default-selected unit test")
    log = ci_native_text(bound_file(source_base, witness["official_ci"]).read_text())
    events, _ = ci_binding_inventory(log)
    earlier = [
        b
        for b in events
        if b["module"] == binding["module"] and b["position"] < binding["position"]
    ]
    if any(
        b["goal"]
        not in {
            "enforcer:enforce",
            "resources:resources",
            "resources:testResources",
            "compiler:compile",
            "compiler:testCompile",
        }
        for b in earlier
    ):
        raise ValueError("Unreviewed source producers precede the empty test selection")
    compilers = [b for b in earlier if b["goal"] == "compiler:testCompile"]
    if len(compilers) != 1:
        raise ValueError("Empty selection needs one actual native test compilation")
    lines = log.splitlines()
    start = compilers[0]["line"] - 1
    end = binding["line"] - 1
    native = "\n".join(lines[start:end])
    if not re.search(
        r"^\[INFO\] Compiling 1 source file with javac \[[^\r\n]+\] to target/test-classes\s*$",
        native,
        re.M,
    ):
        raise ValueError(
            "Native test compilation does not corroborate the complete source inventory"
        )
    # Custom annotation processing or javac plug-ins are not inferred from
    # names: their input closure needs an explicit, byte-bound expert review.
    compiler = [
        p
        for p in model.findall("build/plugins/plugin")
        if text_at(p, "artifactId") == "maven-compiler-plugin"
    ]
    if len(compiler) != 1 or not witness.get("compiler_discovery_review", {}).get("reason"):
        raise ValueError("Compiler discovery requires an explicit source-generation review")
    import xml.etree.ElementTree as ET

    compiler_hash = hashlib.sha256(ET.tostring(compiler[0])).hexdigest()
    if witness["compiler_discovery_review"].get("effective_plugin_sha256") != compiler_hash:
        raise ValueError("Compiler discovery review differs from actual effective configuration")
    configs = compiler[0].findall("configuration") + compiler[0].findall(
        "executions/execution/configuration"
    )
    processors = compiler[0].findall(".//annotationProcessorPaths/path")
    processor_coordinates = {
        (text_at(p, "groupId"), text_at(p, "artifactId"), text_at(p, "version")) for p in processors
    }
    if processor_coordinates:
        processor_review = witness["compiler_discovery_review"]
        if processor_coordinates != {tuple(processor_review.get("coordinates", []))}:
            raise ValueError("Unreviewed annotation processor path can change test discovery")
        with zipfile.ZipFile(bound_file(source_base, processor_review["dependency_bundle"])) as jar:
            group, artifact, version = processor_review["coordinates"]
            props = jar.read(
                "META-INF/maven/" + group + "/" + artifact + "/pom.properties"
            ).decode()
            if any(
                not re.search(r"^" + key + "=" + re.escape(value) + r"\s*$", props, re.M)
                for key, value in (
                    ("groupId", group),
                    ("artifactId", artifact),
                    ("version", version),
                )
            ):
                raise ValueError("Compiler service bundle differs from its declared coordinates")
            if any(
                n.startswith("META-INF/services/")
                and n.endswith("javax.annotation.processing.Processor")
                for n in jar.namelist()
            ):
                raise ValueError("Annotation processors can generate additional selected tests")
            plugin = jar.read("META-INF/services/com.sun.source.util.Plugin").decode().strip()
            if plugin != processor_review.get("javac_plugin_class"):
                raise ValueError(
                    "Compiler plugin service differs from its source-generation review"
                )
    if any(
        config.find(k) is not None
        for config in configs
        for k in (
            "annotationProcessors",
            "testIncludes",
            "testExcludes",
            "testSource",
            "testOutputDirectory",
        )
    ):
        raise ValueError("Custom compiler discovery is outside the reviewed source scope")
