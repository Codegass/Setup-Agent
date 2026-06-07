"""Docker exec terminal bridge helpers."""

from __future__ import annotations

import asyncio
from typing import Any


def build_exec_options(shell: str = "/bin/bash") -> dict[str, object]:
    return {"cmd": shell, "stdin": True, "tty": True}


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
