"""A provision switches the domain its verification and its dispatches resolve.

Live d2r4 lucene (`logs/d2r4-20260815/slices/lucene.md`, control_events seq
93/94): `project(action='provision', java_version='21')` came back
`JAVA_VERSION_VERIFICATION_MISMATCH` over its own block —

    Java 21 was requested

    JAVA_HOME: /usr/lib/jvm/java-21-openjdk-arm64
    Verification:
    openjdk version "17.0.19" 2026-04-21
    ---
    javac 17.0.19

The JDK 21 install was honest and the JAVA_HOME string was updated; the
RESOLUTION DOMAIN was not. Every project command runs under the environment
`DockerOrchestrator._default_exec_environment` derives from the persisted
overlay, so the java-17 bin directory the first provision made active still
fronted PATH, and `export JAVA_HOME=... && java -version` verified the JDK it
was replacing. The build stayed on 17 (`ERROR: java version must be >= 21 and
<= 24, your version: 17`) and the phase closed blocked.

Live d2r3 camel-quarkus (`logs/d2r3-serial-20260814/slices/camel-quarkus.md`
§2.10, seq 125/127) is the same defect one release earlier, before the
contradiction refusal existed: `provision java_version=17` returned SUCCESS
over a block reading `openjdk version "11.0.31"` / `javac 11.0.31`.

What both need: the switch lands where the verification shell and every later
dispatch resolve `java` — update-alternatives for java AND javac, and the
persisted overlay whose PATH the runner environment is built from — and the
verification runs in THAT domain. The contradiction refusal is unchanged; it
simply stops firing on an honest switch, and still fires when the domain does
not answer for the claim.
"""

import json
import posixpath
import re

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON, EnvOverlayStore
from sag.tools.internal.system_tool import PROVISION_ACTIVATION_STATE_MARKER, SystemTool

ARCH = "arm64"
JAVA_HOMES = {major: f"/usr/lib/jvm/java-{major}-openjdk-{ARCH}" for major in ("11", "17", "21")}
FULL_VERSION = {"11": "11.0.31", "17": "17.0.19", "21": "21.0.9"}

_EXACT_JAVA_RE = re.compile(r"(/\S+)/bin/java(?:c)? -version")


def _java_banner(major: str) -> str:
    version = FULL_VERSION[major]
    return (
        f'openjdk version "{version}" 2026-04-21\n'
        f"OpenJDK Runtime Environment (build {version}+10-1-24.04.2-Ubuntu)\n"
        f"OpenJDK 64-Bit Server VM (build {version}+10-1-24.04.2-Ubuntu, mixed mode, sharing)"
    )


def _major_of(path: str) -> str | None:
    match = re.search(r"java-(\d+)-openjdk", path or "")
    return match.group(1) if match else None


class FakeJdkContainer:
    """A container that resolves `java` the way a dispatch resolves it.

    PATH resolution is not this double's invention. `execute_command` builds
    every project command's environment from the persisted overlay
    (`_default_exec_environment` -> `EnvOverlayStore.authorized_environment`),
    so the ACTIVE overlay candidate's bin directory fronts `/usr/bin`; the
    update-alternatives link answers only when no candidate is active.
    """

    def __init__(
        self,
        *,
        installed=(),
        linked_major=None,
        frozen_major=None,
        overlay_writable=True,
        alternatives_need_install=False,
        binary_answers=None,
    ):
        self.installed = set(installed)
        # A JDK directory whose binaries answer for another major than its name
        # promises — the one thing an exact-path probe can catch and a PATH
        # lookup cannot.
        self.binary_answers = dict(binary_answers or {})
        self.alternatives: dict[str, str] = {}
        self.registered_alternatives: set[tuple[str, str]] = set()
        if linked_major:
            self.alternatives = {
                "java": f"{JAVA_HOMES[linked_major]}/bin/java",
                "javac": f"{JAVA_HOMES[linked_major]}/bin/javac",
            }
            self.registered_alternatives = {
                (name, path) for name, path in self.alternatives.items()
            }
        # A container whose resolution refuses to move, whatever is written:
        # the lucene shape, held fixed so a seal over it would be observable.
        self.frozen_major = frozen_major
        self.overlay_writable = overlay_writable
        self.alternatives_need_install = alternatives_need_install
        self.commands: list[str] = []
        self.files: dict[str, str] = {}

    # -- the container's own resolution -------------------------------------
    def resolved_java_home(self):
        if self.frozen_major:
            return JAVA_HOMES[self.frozen_major]
        raw = self.files.get(DEFAULT_OVERLAY_JSON)
        if raw:
            active = json.loads(raw).get("tools", {}).get("java", {}).get("active")
            if active:
                return posixpath.dirname(posixpath.dirname(active))
        link = self.alternatives.get("java")
        return posixpath.dirname(posixpath.dirname(link)) if link else None

    def resolved_major(self):
        home = self.resolved_java_home()
        return _major_of(home) if home else None

    # -- command surface ----------------------------------------------------
    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        self.commands.append(command)

        install = re.fullmatch(r"apt-get install -y openjdk-(\d+)-jdk", command)
        if install:
            major = install.group(1)
            if major not in JAVA_HOMES:
                return {"success": False, "output": "E: Unable to locate package", "exit_code": 100}
            self.installed.add(major)
            return {"success": True, "output": "installed", "exit_code": 0}
        if command.startswith("apt-get install -y"):
            return {"success": False, "output": "E: Unable to locate package", "exit_code": 100}
        if command == "apt-get update":
            return {"success": True, "output": "", "exit_code": 0}
        if command == "dpkg --print-architecture":
            return {"success": True, "output": f"{ARCH}\n", "exit_code": 0}

        if command.startswith("test -f "):
            paths = re.findall(r"test -f (\S+)", command)
            present = all(_major_of(path) in self.installed for path in paths)
            marker = "verified" if "verified" in command else "exists"
            return {
                "success": present,
                "output": marker if present else "",
                "exit_code": 0 if present else 1,
            }

        if command.startswith("update-alternatives --list "):
            name = command.split()[2]
            listed = [f"{JAVA_HOMES[major]}/bin/{name}" for major in sorted(self.installed)]
            return {"success": True, "output": "\n".join(listed), "exit_code": 0}

        install_alt = re.fullmatch(r"update-alternatives --install (\S+) (\S+) (\S+) \d+", command)
        if install_alt:
            _link, name, path = install_alt.groups()
            self.registered_alternatives.add((name, path))
            return {"success": True, "output": "", "exit_code": 0}

        set_alt = re.fullmatch(r"update-alternatives --set (\S+) (\S+)", command)
        if set_alt:
            name, path = set_alt.groups()
            if self.alternatives_need_install and (name, path) not in self.registered_alternatives:
                return {
                    "success": False,
                    "output": (
                        f"update-alternatives: error: alternative {path} for {name} "
                        "not registered; not setting"
                    ),
                    "exit_code": 2,
                }
            self.alternatives[name] = path
            return {"success": True, "output": "", "exit_code": 0}

        if "java -version" in command or "javac -version" in command:
            return self._version_block(command)

        return {"success": True, "output": "", "exit_code": 0}

    def _version_block(self, command):
        """Answer a verification exactly as the container would.

        An absolute path names one exact JDK; a bare `java`/`javac` is
        whatever this container's PATH resolves right now.
        """
        exact = _EXACT_JAVA_RE.search(command)
        home = f"{exact.group(1)}" if exact else self.resolved_java_home()
        installed_major = _major_of(home) if home else None
        if not installed_major or installed_major not in self.installed:
            return {"success": False, "output": "java: command not found", "exit_code": 127}
        major = self.binary_answers.get(installed_major, installed_major)
        lines = []
        if "command -v java" in command:
            lines.append(f"{home}/bin/java")
        lines.append(_java_banner(major))
        if "javac -version" in command:
            lines.append("---")
            lines.append(f"javac {FULL_VERSION[major]}")
        return {"success": True, "output": "\n".join(lines), "exit_code": 0}

    def write_file(self, path, content):
        if not self.overlay_writable:
            return {"success": False, "output": "read-only file system", "exit_code": 1}
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def _provision(container, major):
    return SystemTool(container)._install_and_configure_java(major)


def test_a_provision_that_switches_versions_verifies_the_new_version():
    """lucene seq 8 then seq 93: 17 provisioned, then 21 asked for."""
    container = FakeJdkContainer()
    assert _provision(container, "17").succeeded is True

    result = _provision(container, "21")

    assert result.succeeded is True
    assert result.metadata["java_version"] == "21"
    assert result.metadata["java_home"] == JAVA_HOMES["21"]
    # The block the model reads names the JDK that will now run, and only it.
    assert FULL_VERSION["21"] in result.output
    assert FULL_VERSION["17"] not in result.output


def test_a_subsequent_dispatch_resolves_the_provisioned_version():
    container = FakeJdkContainer()
    _provision(container, "17")
    _provision(container, "21")

    # The domain a later dispatch runs in, read from the persisted overlay.
    environment = EnvOverlayStore(container).authorized_environment({})
    assert environment["JAVA_HOME"] == JAVA_HOMES["21"]
    assert environment["PATH"].split(":")[0] == f"{JAVA_HOMES['21']}/bin"
    assert f"{JAVA_HOMES['17']}/bin" not in environment["PATH"].split(":")
    # And the container itself: bare `java`, resolved the dispatch way.
    assert container.resolved_major() == "21"
    # The other half of the same domain: both links, not just java.
    assert container.alternatives["java"] == f"{JAVA_HOMES['21']}/bin/java"
    assert container.alternatives["javac"] == f"{JAVA_HOMES['21']}/bin/javac"


def test_the_lucene_shape_is_unconstructible():
    """A domain that did not take the switch cannot be sealed as one that did."""
    container = FakeJdkContainer()
    assert _provision(container, "17").succeeded is True
    container.frozen_major = "17"

    result = _provision(container, "21")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_VERIFICATION_MISMATCH"
    assert result.metadata["claimed_java_version"] == "21"
    assert result.metadata["verified_java_version"] == FULL_VERSION["17"]
    assert result.metadata.get("java_version") != "21"


def test_the_post_activation_refusal_states_the_container_it_actually_left():
    """A refusal returned AFTER the switch landed may not describe the
    container as untouched. The old text closed with "verify the exact binary
    before registering it" — advice for a container that has not been
    registered, over a container whose overlay, profile and alternatives were
    all rewritten one step earlier."""
    container = FakeJdkContainer()
    assert _provision(container, "17").succeeded is True
    container.frozen_major = "17"

    result = _provision(container, "21")

    assert result.succeeded is False
    stated = " ".join([result.error or "", *(result.suggestions or ())])
    assert "activation persisted; verification refused the seal" in stated
    assert "before registering it" not in stated


def test_the_post_activation_refusal_leaves_a_typed_activation_marker():
    """The next consult reads structure, not prose: the state the container was
    left in is a typed fact on the refusal, so nothing has to re-derive it."""
    container = FakeJdkContainer()
    assert _provision(container, "17").succeeded is True
    container.frozen_major = "17"

    result = _provision(container, "21")

    marker = result.metadata[PROVISION_ACTIVATION_STATE_MARKER]
    assert marker == {
        "tool": "java",
        "state": "activated",
        "executable": f"{JAVA_HOMES['21']}/bin/java",
        "home": JAVA_HOMES["21"],
        "requested": "21",
        "verified": FULL_VERSION["17"],
    }
    # And the overlay agrees: the switch really did land.
    assert (
        json.loads(container.files[DEFAULT_OVERLAY_JSON])["tools"]["java"]["active"]
        == f"{JAVA_HOMES['21']}/bin/java"
    )


def test_a_pre_activation_refusal_still_says_the_container_did_not_move():
    """The other side of the same law: the exact-path probe refuses BEFORE
    anything is switched, and that refusal keeps saying so."""
    container = FakeJdkContainer(
        installed=("11",),
        linked_major="11",
        binary_answers={"17": "11"},
    )

    result = _provision(container, "17")

    assert result.succeeded is False
    stated = " ".join([result.error or "", *(result.suggestions or ())])
    assert "activation persisted" not in stated
    assert PROVISION_ACTIVATION_STATE_MARKER not in (result.metadata or {})


def test_a_jdk_whose_own_binary_answers_another_major_moves_nothing():
    """The refusal that precedes the switch leaves the container as it was."""
    container = FakeJdkContainer(
        installed=("11",),
        linked_major="11",
        binary_answers={"17": "11"},
    )

    result = _provision(container, "17")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_VERIFICATION_MISMATCH"
    assert result.metadata["verified_java_version"] == FULL_VERSION["11"]
    # Nothing switched: no overlay, and the links still name the old runtime.
    assert DEFAULT_OVERLAY_JSON not in container.files
    assert container.alternatives["java"] == f"{JAVA_HOMES['11']}/bin/java"


def test_an_activation_that_did_not_persist_is_not_sealed_as_success():
    """An overlay write that failed leaves later dispatches on the old runtime."""
    container = FakeJdkContainer(overlay_writable=False)

    result = _provision(container, "17")

    assert result.succeeded is False
    assert result.error_code == "JAVA_RUNTIME_ACTIVATION_FAILED"
    assert "17" in result.error


def test_an_activation_that_did_not_persist_still_states_the_links_that_moved():
    """The phrase "not activated" is true of the overlay and false of the container: the
    /usr/bin links were repointed one step earlier, so anything resolving
    through the system PATH directories already runs the new JDK. The same law
    as the post-activation refusal — no refusal describes a switched container
    as untouched."""
    container = FakeJdkContainer(installed=("11",), linked_major="11", overlay_writable=False)

    result = _provision(container, "17")

    assert result.error_code == "JAVA_RUNTIME_ACTIVATION_FAILED"
    assert container.alternatives["java"] == f"{JAVA_HOMES['17']}/bin/java"
    stated = " ".join(result.suggestions or ())
    assert "/usr/bin/java" in stated and "already repointed" in stated
    assert result.metadata[PROVISION_ACTIVATION_STATE_MARKER] == {
        "tool": "java",
        "state": "partially_activated",
        "executable": f"{JAVA_HOMES['17']}/bin/java",
        "home": JAVA_HOMES["17"],
        "requested": "17",
        "landed": ["java", "javac"],
    }


def test_an_activation_failure_that_moved_no_link_claims_none():
    """The marker states what landed, never what might have."""
    container = FakeJdkContainer(overlay_writable=False)
    system = SystemTool(container)
    system._set_java_alternative = lambda name, binary: False

    result = system._install_and_configure_java("17")

    assert result.error_code == "JAVA_RUNTIME_ACTIVATION_FAILED"
    assert PROVISION_ACTIVATION_STATE_MARKER not in (result.metadata or {})
    assert not any("already repointed" in s for s in (result.suggestions or ()))


def test_a_javac_alternative_repair_does_not_repoint_the_java_link():
    """The repair for one link may never register another link's binary."""
    container = FakeJdkContainer(alternatives_need_install=True)

    result = _provision(container, "17")

    assert result.succeeded is True
    assert container.alternatives["javac"] == f"{JAVA_HOMES['17']}/bin/javac"
    assert not any("--install /usr/bin/java javac" in command for command in container.commands)
