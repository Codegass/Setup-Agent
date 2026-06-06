from sag.web.workspace_registry import WorkspaceRegistry


class FakeImage:
    tags = ["sag/base:24.04"]


class FakeContainer:
    def __init__(
        self,
        name: str = "sag-commons-cli",
        status: str = "running",
        labels: dict[str, str] | None = None,
    ):
        self.name = name
        self.status = status
        self.image = FakeImage()
        labels = {"setup-agent.project": "commons-cli"} if labels is None else labels
        self.attrs = {
            "Created": "2026-06-06T02:00:00Z",
            "Config": {"Labels": labels},
        }


class FakeContainers:
    def __init__(self, containers: list[FakeContainer] | None = None):
        self._containers = containers or [FakeContainer()]

    def list(self, all=True):
        return self._containers


class FakeClient:
    containers = FakeContainers()


class FakeContainersWithIgnoreRemoved:
    def __init__(self):
        self.ignore_removed = None

    def list(self, all=True, ignore_removed=False):
        self.ignore_removed = ignore_removed
        return [FakeContainer()]


class RaisingContainers:
    def list(self, all=True, ignore_removed=False):
        raise RuntimeError("docker daemon unavailable")


class ExplodingContainer:
    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")

    @property
    def name(self):
        raise RuntimeError("name unavailable")

    @property
    def status(self):
        raise RuntimeError("status unavailable")

    @property
    def image(self):
        raise RuntimeError("image lookup should not happen")


class AttrImageContainer:
    status = "running"
    attrs = {
        "Name": "/sag-good",
        "Created": "2026-06-06T02:00:00Z",
        "Config": {
            "Image": "sag/base:from-attrs",
            "Labels": {"setup-agent.project": "good-project"},
        },
    }

    @property
    def image(self):
        raise RuntimeError("image lookup should not happen")


def test_workspace_registry_lists_sag_containers_only():
    registry = WorkspaceRegistry(client=FakeClient())
    workspaces = registry.list_workspaces()

    assert len(workspaces) == 1
    assert workspaces[0].id == "sag-commons-cli"
    assert workspaces[0].container == "sag-commons-cli"
    assert workspaces[0].docker.status == "running"


def test_workspace_registry_ignores_non_sag_containers_and_sorts_by_container():
    client = type(
        "FakeClient",
        (),
        {
            "containers": FakeContainers(
                [
                    FakeContainer(name="sag-zeta", labels={}),
                    FakeContainer(name="redis"),
                    FakeContainer(name="sag-alpha", labels={}),
                ]
            )
        },
    )()

    workspaces = WorkspaceRegistry(client=client).list_workspaces()

    assert [workspace.container for workspace in workspaces] == ["sag-alpha", "sag-zeta"]
    assert [workspace.project for workspace in workspaces] == ["alpha", "zeta"]


def test_workspace_registry_passes_ignore_removed_when_supported():
    containers = FakeContainersWithIgnoreRemoved()
    client = type("FakeClient", (), {"containers": containers})()

    WorkspaceRegistry(client=client).list_workspaces()

    assert containers.ignore_removed is True


def test_workspace_registry_returns_empty_when_container_list_raises():
    client = type("FakeClient", (), {"containers": RaisingContainers()})()

    assert WorkspaceRegistry(client=client).list_workspaces() == []


def test_workspace_registry_bad_container_does_not_hide_good_container():
    client = type(
        "FakeClient",
        (),
        {"containers": FakeContainers([ExplodingContainer(), AttrImageContainer()])},
    )()

    workspaces = WorkspaceRegistry(client=client).list_workspaces()

    assert [workspace.container for workspace in workspaces] == ["sag-good"]
    assert workspaces[0].project == "good-project"
    assert workspaces[0].docker.image == "sag/base:from-attrs"
