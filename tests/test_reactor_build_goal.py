"""A multi-module Maven reactor builds with `install`, not bare `compile`.

Reactor modules can depend on siblings' produced artifacts (shaded jars, code-gen),
which only exist after those modules are built and installed. `compile` never runs
the plugins that make them, so a dependent module fails to resolve them
(cassandra-java-driver: core needs the shaded-guava jar). Reuses the fake from the
existing build-recommendation test rather than editing it.
"""

from tests.test_project_analyzer_build_recommendation import _rec


def test_maven_reactor_builds_with_install():
    rec = _rec(
        {"/workspace/p/pom.xml"},
        {"build_system": "maven", "maven_modules": ["core", "shaded-guava"]},
        packaging="pom",
        source_dirs=["/workspace/p/core/src/main/java"],
        path="/workspace/p",
    )
    assert rec["build_system"] == "maven"
    assert rec["build_root"] == "/workspace/p"
    assert rec["goal"] == "install"  # not "compile": siblings need building + installing
