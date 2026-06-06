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

        for container in self._list_containers():
            summary = _workspace_summary(container)
            if summary is not None:
                workspaces.append(summary)

        return sorted(workspaces, key=lambda workspace: workspace.container)

    def _list_containers(self) -> list[Any]:
        try:
            return list(self.client.containers.list(all=True, ignore_removed=True))
        except TypeError:
            try:
                return list(self.client.containers.list(all=True))
            except Exception:
                return []
        except Exception:
            return []


def _workspace_summary(container: Any) -> WorkspaceSummary | None:
    try:
        attrs = _container_attrs(container)
        name = _container_name(container, attrs)
        if name is None or not name.startswith("sag-"):
            return None

        labels = _container_labels(attrs)
        project = _text(labels.get("setup-agent.project")) or name.removeprefix("sag-")

        return WorkspaceSummary(
            id=name,
            project=project,
            container=name,
            docker=DockerSummary(
                status=_text(_safe_getattr(container, "status")) or "unknown",
                image=_container_image(container, attrs),
            ),
            build=BuildSummary(),
            test=TestSummary(),
            updated=_text(attrs.get("Created")) or "unknown",
        )
    except Exception:
        return None


def _container_attrs(container: Any) -> dict[str, Any]:
    attrs = _safe_getattr(container, "attrs", {})
    if not isinstance(attrs, dict):
        return {}

    return attrs


def _container_name(container: Any, attrs: dict[str, Any]) -> str | None:
    attr_name = _text(attrs.get("Name"))
    if attr_name is not None:
        return attr_name.lstrip("/")

    name = _text(_safe_getattr(container, "name"))
    if name is None:
        return None

    return name.lstrip("/")


def _container_labels(attrs: dict[str, Any]) -> dict[str, Any]:
    config = attrs.get("Config") or {}
    if not isinstance(config, dict):
        return {}

    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        return {}

    return labels


def _container_image(container: Any, attrs: dict[str, Any]) -> str | None:
    config = attrs.get("Config") or {}
    if isinstance(config, dict):
        image = _text(config.get("Image"))
        if image is not None:
            return image

    image = _text(attrs.get("Image"))
    if image is not None:
        return image

    image_obj = _safe_getattr(container, "image")
    tags = _safe_getattr(image_obj, "tags")
    if not tags:
        return None

    try:
        first_tag = tags[0]
    except (IndexError, TypeError):
        return None

    return _text(first_tag)


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    return text
