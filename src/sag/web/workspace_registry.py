"""Discover SAG-managed Docker workspaces for the web dashboard."""

from __future__ import annotations

from typing import Any

from sag.web.models import BuildSummary, DockerSummary, TestSummary, WorkspaceSummary


class WorkspaceRegistry:
    def __init__(self, client: Any | None = None):
        if client is None:
            import docker

            client = docker.from_env()
        self.client = client

    def list_workspaces(self) -> list[WorkspaceSummary]:
        workspaces: list[WorkspaceSummary] = []

        for container in self.client.containers.list(all=True):
            name = getattr(container, "name", None)
            if not isinstance(name, str) or not name.startswith("sag-"):
                continue

            attrs = getattr(container, "attrs", {}) or {}
            if not isinstance(attrs, dict):
                attrs = {}

            labels = _container_labels(attrs)
            project = labels.get("setup-agent.project") or name.removeprefix("sag-")

            workspaces.append(
                WorkspaceSummary(
                    id=name,
                    project=str(project),
                    container=name,
                    docker=DockerSummary(
                        status=str(getattr(container, "status", None) or "unknown"),
                        image=_container_image(container),
                    ),
                    build=BuildSummary(),
                    test=TestSummary(),
                    updated=str(attrs.get("Created") or "unknown"),
                )
            )

        return sorted(workspaces, key=lambda workspace: workspace.container)


def _container_labels(attrs: dict[str, Any]) -> dict[str, Any]:
    config = attrs.get("Config") or {}
    if not isinstance(config, dict):
        return {}

    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        return {}

    return labels


def _container_image(container: Any) -> str | None:
    image = getattr(container, "image", None)
    tags = getattr(image, "tags", None)
    if not tags:
        return None

    first_tag = tags[0]
    if first_tag is None:
        return None

    return str(first_tag)
