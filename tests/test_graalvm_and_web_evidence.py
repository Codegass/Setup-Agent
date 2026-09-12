import shlex
from unittest.mock import Mock

import pytest
import requests

from sag.agent.tool_orchestration import format_tool_result
from sag.tools.internal.system_tool import JAVA_DOMAIN_VERIFICATION, SystemTool
from sag.tools.internal.web_search import WebSearchTool
from sag.tools.project_tool import ProjectTool
from sag.tools.search_tool import SearchTool


class GraalHost:
    def __init__(self, *, bad_download=False, stale=False, arch="x86_64", native=True):
        self.commands, self.files = [], {}
        self.bad_download, self.stale, self.arch = bad_download, stale, arch
        self.native = native

    def read_file(self, path):
        return self.files.get(path)

    def write_file(self, path, content):
        self.files[path] = content
        return {"exit_code": 0, "success": True, "output": ""}

    def execute_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        code = 0
        if cmd == "uname -s; uname -m":
            output = "Linux\n" + self.arch
        elif cmd.startswith("set -eu"):
            if self.bad_download:
                code, output = 1, "sha256sum: archive.tar.gz: FAILED"
            else:
                output = (
                    'openjdk version "21.0.9"\nGraalVM Runtime\njavac 21.0.9\nnative-image 21.0.9\nSAG_GRAAL_HOME=/opt/.sag-graalvm.test123/jdk\nSAG_GRAAL_SHA256='
                    + "a" * 64
                )
        elif cmd.startswith("test -x"):
            output = "EXISTS"
        elif cmd.startswith("readlink -f"):
            output = shlex.split(cmd)[-1]
        elif "native-image" in cmd:
            output = "/opt/.sag-graalvm.test123/jdk/bin/native-image\nnative-image 21.0.9"
            if not self.native:
                code, output = 127, "native-image: not found"
        elif "javac -version" in cmd:
            output = "/opt/.sag-graalvm.test123/jdk/bin/javac\njavac 21.0.9"
        elif "java -version" in cmd:
            output = (
                '/opt/.sag-graalvm.test123/jdk/bin/java\nopenjdk version "21.0.9"\nGraalVM Runtime'
            )
            if self.stale and cmd.startswith("command -v"):
                output = output.replace("21.0.9", "17.0.1")
        else:
            output = ""
        return {"success": code == 0, "exit_code": code, "output": output}


@pytest.mark.parametrize("arch,asset", [("x86_64", "x64"), ("aarch64", "aarch64")])
def test_explicit_graalvm_route_verifies_native_capability_and_archive(arch, asset):
    host = GraalHost(arch=arch)
    result = ProjectTool(system_tool=SystemTool(host)).execute(
        action="provision",
        java_version="21",
        java_distribution="graalvm",
        java_capabilities=["native-image"],
    )
    assert result.succeeded, result.error
    assert result.facts["sha256"] == "a" * 64
    assert result.facts["source_url"].endswith(f"graalvm-jdk-21_linux-{asset}_bin.tar.gz")
    assert result.facts["measured_version"] == "21.0.9"
    assert not any("apt-get" in cmd for cmd in host.commands)
    download = next(cmd for cmd in host.commands if cmd.startswith("set -eu"))
    assert download.index("sha256sum -c") < download.index("tar -xzf")
    assert "native-image" not in download
    assert any(cmd.startswith("command -v native-image") for cmd in host.commands)
    assert result.raw_data["runtime_observations"]["dispatch:java"]["distribution"] == "graalvm"


def test_graalvm_java_only_selection_does_not_require_native_image():
    host = GraalHost(native=False)
    result = ProjectTool(system_tool=SystemTool(host)).execute(
        action="provision", java_version="21", java_distribution="graalvm"
    )
    assert result.succeeded, result.error
    assert not any("native-image" in cmd for cmd in host.commands)
    assert result.raw_data["active_candidate"]["capabilities"] == ["javac"]


def test_explicit_missing_native_capability_is_refused_before_activation():
    host = GraalHost(native=False)
    result = ProjectTool(system_tool=SystemTool(host)).execute(
        action="provision",
        java_version="21",
        java_distribution="graalvm",
        java_capabilities=["native-image"],
    )
    assert result.error_code == "ENV_CAPABILITY_UNAVAILABLE"
    assert result.facts["dispatch_probe"] is False
    assert not host.files
    assert not result.metadata["activation_confirmed"]


def test_failed_checksum_never_activates_graalvm():
    host = GraalHost(bad_download=True)
    result = SystemTool(host).execute(
        action="install_java", java_version="21", java_distribution="graalvm"
    )
    assert result.error_code == "JAVA_DISTRIBUTION_VERIFICATION_FAILED"
    assert not host.files
    assert not any(cmd.startswith(JAVA_DOMAIN_VERIFICATION) for cmd in host.commands)


def test_graalvm_install_cannot_seal_a_stale_dispatch_environment():
    result = SystemTool(GraalHost(stale=True)).execute(
        action="install_java", java_version="21", java_distribution="graalvm"
    )
    assert result.error_code == "ENV_ACTIVATION_NOT_CONFIRMED"


def test_distribution_does_not_silently_disappear_into_package_route():
    host = GraalHost()
    result = ProjectTool(system_tool=SystemTool(host)).execute(
        action="provision", packages=["zlib1g-dev"], java_distribution="graalvm"
    )
    assert result.error_code == "PROJECT_PROVISION_AMBIGUOUS"
    assert host.commands == []


def test_empty_search_is_no_evidence_not_one_successful_placeholder(monkeypatch):
    response = Mock()
    response.json.return_value = {"RelatedTopics": []}
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    result = WebSearchTool().execute("graalvm native-image")
    assert not result.succeeded
    assert result.error_code == "WEB_SEARCH_NO_EVIDENCE"
    assert result.metadata["results_count"] == 0
    assert "Web Search Limited" not in result.output
    assert "url:<https URL>" in format_tool_result("search", result)


def test_network_failure_is_distinct_from_an_empty_search(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout("provider timed out")

    monkeypatch.setattr(requests, "get", fail)
    assert WebSearchTool().execute("graalvm").error_code == "WEB_SEARCH_UNAVAILABLE"


def test_url_reader_retains_source_links_and_complete_large_text(monkeypatch):
    body = (
        "<html><script>hidden script</script><h1>Release</h1><pre>"
        + ("release detail\n" * 4000)
        + '</pre><a href="/asset.tar.gz">Download</a></html>'
    )
    response = Mock(
        url="https://www.graalvm.org/releases/21",
        status_code=200,
        encoding="utf-8",
        headers={"Content-Type": "text/html"},
    )
    response.iter_content.return_value = [body.encode()]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    result = SearchTool(None, web_search=WebSearchTool()).execute(
        target="url:https://www.graalvm.org/releases/21"
    )
    assert result.succeeded
    assert result.output.count("release detail") == 4000
    assert "https://www.graalvm.org/asset.tar.gz" in result.output
    assert "hidden script" not in result.output
    assert result.facts["complete"] is True


def test_url_fetch_limit_is_explicit_and_cannot_claim_a_complete_read(monkeypatch):
    response = Mock(url="https://example.org", headers={"Content-Type": "text/plain"})
    response.iter_content.return_value = [b"x" * (4 * 1024 * 1024 + 1)]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    result = WebSearchTool().read_url("https://example.org")
    assert result.error_code == "WEB_PAGE_TOO_LARGE"
    assert result.facts["complete"] is False
