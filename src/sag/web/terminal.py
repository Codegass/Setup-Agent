"""Docker exec terminal bridge helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from sag.runtime.exec_env import default_utf8_environment

TERMINAL_SUBPROTOCOL = "sag-terminal-v1"
TERMINAL_TOKEN_PREFIX = "sag-session."
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def terminal_request_allowed(connection: Any, hosts: set[str], *, require_origin: bool) -> bool:
    """Verify an actual host allowlist before comparing browser origins."""
    host_headers = connection.headers.getlist("host")
    origins = connection.headers.getlist("origin")
    if len(host_headers) != 1 or len(origins) > 1:
        return False
    scheme = "https" if connection.url.scheme in {"https", "wss"} else "http"
    try:
        target = urlsplit(f"{scheme}://{host_headers[0]}")
        if (
            target.hostname not in hosts
            or target.username is not None
            or target.password is not None
            or target.path
            or target.query
            or target.fragment
        ):
            return False
        target_port = target.port or (443 if scheme == "https" else 80)
        if not origins:
            return not require_origin
        origin = urlsplit(origins[0])
        return (
            origin.scheme == scheme
            and origin.hostname == target.hostname
            and (origin.port or (443 if scheme == "https" else 80)) == target_port
            and origin.username is None
            and origin.password is None
            and not (origin.path or origin.query or origin.fragment)
        )
    except ValueError:
        return False


def build_exec_options(shell: str = "/bin/bash") -> dict[str, object]:
    return {
        "cmd": shell,
        "stdin": True,
        "tty": True,
        "environment": default_utf8_environment(),
    }


class TerminalAdapter:
    """Lazy Docker SDK adapter for opening interactive exec sockets."""

    def __init__(self, docker_client: Any | None = None) -> None:
        self._docker_client = docker_client

    @property
    def docker_client(self) -> Any:
        if self._docker_client is None:
            import docker

            self._docker_client = docker.from_env()
        return self._docker_client

    def open_socket(self, container: str, shell: str = "/bin/bash") -> Any:
        exec_ref = self.docker_client.api.exec_create(
            container,
            **build_exec_options(shell),
        )
        exec_id = exec_ref["Id"] if isinstance(exec_ref, dict) else exec_ref
        return self.docker_client.api.exec_start(exec_id, tty=True, socket=True)

    def close(self) -> None:
        client = self._docker_client
        self._docker_client = None
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _socket_target(socket: Any) -> Any:
    return getattr(socket, "_sock", socket)


async def recv_socket(socket: Any, size: int = 4096) -> bytes:
    target = _socket_target(socket)
    data = await asyncio.to_thread(target.recv, size)
    if isinstance(data, str):
        return data.encode()
    return data


async def send_socket(socket: Any, data: bytes) -> None:
    target = _socket_target(socket)
    if hasattr(target, "sendall"):
        await asyncio.to_thread(target.sendall, data)
    else:
        await asyncio.to_thread(target.send, data)


async def close_socket(socket: Any) -> None:
    target = _socket_target(socket)
    close = getattr(target, "close", None)
    if close is not None:
        await asyncio.to_thread(close)


__all__ = [
    "TerminalAdapter",
    "build_exec_options",
    "close_socket",
    "recv_socket",
    "send_socket",
]
