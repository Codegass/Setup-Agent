"""FastAPI application factory for the SAG Workbench API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from sag.web.read_model import ReadModelBuilder
from sag.web.task_runner import TaskRequest, TaskRunner
from sag.web.terminal import TerminalAdapter, close_socket, recv_socket, send_socket


def _single_snapshot(payload: dict) -> Iterator[str]:
    yield "event: snapshot\n"
    yield f"data: {json.dumps(payload)}\n\n"


def create_app(
    read_model: ReadModelBuilder | None = None,
    task_runner: TaskRunner | None = None,
    terminal_adapter: TerminalAdapter | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    builder = read_model if read_model is not None else ReadModelBuilder()
    runner = task_runner if task_runner is not None else TaskRunner()
    terminal_bridge = terminal_adapter if terminal_adapter is not None else TerminalAdapter()
    owns_terminal_bridge = terminal_adapter is None

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owns_terminal_bridge:
                close = getattr(terminal_bridge, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(close)

    app = FastAPI(title="SAG Workbench", version="0.1.0", lifespan=lifespan)

    @app.get("/api/workspaces")
    def get_workspaces() -> dict:
        return builder.dashboard().model_dump(mode="json", by_alias=True)

    @app.post("/api/workspaces/{workspace_id}/tasks", status_code=202)
    def submit_task(workspace_id: str, request: TaskRequest) -> dict:
        return runner.submit(workspace_id, request)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        try:
            detail = builder.session_detail(session_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {session_id}",
            ) from exc

        return detail.model_dump(mode="json", by_alias=True)

    @app.get("/api/stream/dashboard")
    def stream_dashboard() -> StreamingResponse:
        payload = builder.dashboard().model_dump(mode="json", by_alias=True)
        return StreamingResponse(
            _single_snapshot(payload),
            media_type="text/event-stream",
        )

    @app.websocket("/api/workspaces/{workspace_id}/terminal")
    async def workspace_terminal(websocket: WebSocket, workspace_id: str) -> None:
        await websocket.accept()
        socket: Any | None = None
        output_task: asyncio.Task[None] | None = None

        try:
            container = await asyncio.to_thread(
                _resolve_terminal_container,
                builder,
                workspace_id,
            )
            socket = await asyncio.to_thread(terminal_bridge.open_socket, container)
            output_task = asyncio.create_task(_pump_terminal_output(websocket, socket))
            input_task = asyncio.create_task(_pump_websocket_input(websocket, socket))
            done, pending = await asyncio.wait(
                {output_task, input_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            await websocket.send_text(f"Terminal unavailable: {exc}")
            await websocket.close()
        finally:
            if output_task is not None:
                output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await output_task
            if socket is not None:
                await close_socket(socket)

    if static_dir is not None:
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def _resolve_terminal_container(builder: ReadModelBuilder, workspace_id: str) -> str:
    dashboard = builder.dashboard()
    for workspace in dashboard.workspaces:
        if workspace.id != workspace_id:
            continue
        if workspace.docker.status.strip().lower() != "running":
            raise ValueError(f"Workspace is not running: {workspace_id}")
        return workspace.container

    raise ValueError(f"Unknown workspace: {workspace_id}")


async def _pump_terminal_output(websocket: WebSocket, socket: Any) -> None:
    while True:
        data = await recv_socket(socket)
        if not data:
            break
        await websocket.send_bytes(data)


async def _pump_websocket_input(websocket: WebSocket, socket: Any) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if "bytes" in message and message["bytes"] is not None:
            await send_socket(socket, message["bytes"])
        elif "text" in message and message["text"] is not None:
            await send_socket(socket, message["text"].encode())


__all__ = ["create_app"]
