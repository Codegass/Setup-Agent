"""FastAPI application factory for the SAG Workbench API."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from loguru import logger

from sag import __version__
from sag.web.launch_queue import WorkspaceBusyError
from sag.web.launch_service import LaunchBatchRequest, LaunchService, LaunchValidationError
from sag.web.read_model import ReadModelBuilder
from sag.web.task_runner import TaskRequest, TaskRunner
from sag.web.terminal import TerminalAdapter, close_socket, recv_socket, send_socket
from sag.web.workspace_service import WorkspaceDeletionError, WorkspaceService


def create_app(
    read_model: ReadModelBuilder | None = None,
    task_runner: TaskRunner | None = None,
    terminal_adapter: TerminalAdapter | None = None,
    static_dir: Path | None = None,
    launch_service: LaunchService | None = None,
    workspace_service: WorkspaceService | None = None,
) -> FastAPI:
    builder = read_model if read_model is not None else ReadModelBuilder()
    runner = task_runner if task_runner is not None else TaskRunner()
    terminal_bridge = terminal_adapter if terminal_adapter is not None else TerminalAdapter()
    owns_terminal_bridge = terminal_adapter is None
    launches = launch_service if launch_service is not None else LaunchService()
    # Share the launch service's store/DB so queue cleanup and launch state stay
    # consistent; a fake launch service without a store falls back to the default.
    workspaces = (
        workspace_service
        if workspace_service is not None
        else WorkspaceService(store=getattr(launches, "_store", None))
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await asyncio.to_thread(launches.start)
        except Exception:
            logger.exception("Failed to start launch scheduler")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(launches.stop)
            if owns_terminal_bridge:
                close = getattr(terminal_bridge, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(close)

    app = FastAPI(title="SAG Workbench", version=__version__, lifespan=lifespan)

    @app.get("/api/workspaces")
    def get_workspaces() -> dict:
        return builder.dashboard().model_dump(mode="json", by_alias=True)

    @app.get("/api/system")
    def get_system() -> dict:
        return builder.system().model_dump(mode="json", by_alias=True)

    @app.post("/api/workspaces/{workspace_id}/tasks", status_code=202)
    def submit_task(workspace_id: str, request: TaskRequest) -> dict:
        return runner.submit(workspace_id, request)

    @app.delete("/api/workspaces/{workspace_id}")
    def delete_workspace(workspace_id: str) -> dict:
        try:
            return workspaces.delete_workspace(workspace_id)
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceDeletionError as exc:
            # Docker daemon unreachable or the container could not be removed.
            # Surface a meaningful detail (not an opaque 500) so the client can
            # keep the dialog open and tell the user what to retry.
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/project-launches/batch")
    def submit_project_batch(request: LaunchBatchRequest) -> JSONResponse:
        try:
            outcome = launches.submit_batch(request)
        except LaunchValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        status_code = 202 if outcome["accepted"] else 409
        return JSONResponse(status_code=status_code, content=outcome)

    @app.get("/api/project-launches")
    def get_project_launches() -> dict:
        return launches.queue_state()

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
