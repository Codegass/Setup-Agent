"""FastAPI application factory for the SAG Workbench API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from loguru import logger

from sag import __version__
from sag.trajectory.builder import build_trajectory
from sag.trajectory.schema import DETAIL_TIERS
from sag.web.launch_queue import WorkspaceBusyError
from sag.web.launch_service import LaunchBatchRequest, LaunchService, LaunchValidationError
from sag.web.read_model import ReadModelBuilder
from sag.web.session_registry import UnattributableSessionError
from sag.web.task_runner import TaskRequest, TaskRunner
from sag.web.terminal import TerminalAdapter, close_socket, recv_socket, send_socket
from sag.web.workspace_service import WorkspaceDeletionError, WorkspaceService


def _single_snapshot(payload: dict) -> Iterator[str]:
    yield "event: snapshot\n"
    yield f"data: {json.dumps(payload)}\n\n"


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

    @app.get("/api/sessions/{session_id}/trajectory")
    def get_session_trajectory(
        session_id: str,
        detail: str = "summary",
        since: int | None = Query(default=None, ge=0),
        since_seq: int | None = Query(default=None, ge=0),
    ) -> dict:
        """One session's trajectory-v1 document — whole, or since a watermark.

        The body is exactly what `sag trajectory` prints and what
        `build_trajectory` derives: `{schema_version, session, phases, turns,
        annotations, warnings, outputs}`, no aliases, one vocabulary for the CLI
        and the timeline. There is no second derivation in the web layer
        (spec §4), and nothing here writes, copies, or `docker exec`s: a live
        session is read through the host mirror that `session_mirror` maintains.

        `detail=summary` (the default) carries names, codes, timing and tokens
        and leaves `outputs` null; `detail=full` additionally resolves every ref
        the turns name to its verbatim bytes. Summary is the polling tier.

        **What a cut may remove.** Whichever cut is asked for, it removes TURNS
        and nothing else:

        - `annotations` and `warnings` are the CURRENT WHOLE state on every
          response, not an increment, so a consumer REPLACES them rather than
          appending. This is `DeltaAccumulator`'s rule read through a stateless
          GET: a warning is withdrawn when its hole fills and an annotation can
          land on a turn far below the cut (a recurrence names the turn it
          repeats), and a cut has no channel for a retraction. Whole state is
          the only rule under which a poller converges;
        - `session` and `phases` are restated whole for the same reason; they
          are small and are rewritten, not appended to;
        - `turns` are upserted by `turn_id`;
        - `outputs` (full tier only) is whole as well: refs are content-addressed
          and SHARED — every window names the same system prompt — so it is a
          map to merge, not a list to append. It is also why `full` is not the
          tier to poll with.

        **`since_seq=<control sequence>` is the cut a LIVE consumer polls**, and
        the only one that converges while a run is moving. It keeps every turn
        the ledger has touched at a sequence above the watermark — the turn still
        in flight, whose envelope arrived at one sequence and whose result and
        decision arrive at later ones; the turn a `gate_outcome_revised` regrades
        long after it closed; the dispatch a `job_live_at_close` marks at the end
        of the run. A turn naming no sequence at all cannot be shown unchanged,
        so it is always restated. Sequences are the ledger's own append order, so
        a turn's highest one is the last line folded into it, and the consumer's
        watermark is the highest sequence it holds.

        **`since=<turn_id>` cuts by turn id**, dropping `turn_id <= since`. It is
        a pager over a run that is no longer moving, NOT a live channel: a turn
        it has already sent can never be restated, so a poll that catches a turn
        in flight holds that turn's half-stated row forever while the whole-state
        rule withdraws the warnings that said so. Live consumers use `since_seq`.

        The two cuts are different watermarks over the same document, so asking
        for both at once is refused rather than silently resolved one way.

        One join still lands outside the ledger: the engine exports
        `token_usage.csv` when the loop EXITS, so the last turns' bills arrive
        with no control event to carry them. A consumer re-reads whole when the
        run stops — at the summary tier that is a cheap replay of a file it is
        already tailing.
        """
        if detail not in DETAIL_TIERS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown detail tier: {detail}. Use one of {', '.join(DETAIL_TIERS)}.",
            )

        if since is not None and since_seq is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "since and since_seq are two different cuts over one document: "
                    "since drops turn ids at or below it, since_seq keeps every turn "
                    "the ledger touched above it. Ask for one."
                ),
            )

        try:
            session_dir = builder.session_dir(session_id)
        except UnattributableSessionError as exc:
            # The session exists and so does more than one run of its project,
            # and nothing names which run this id is. A guess would attribute one
            # run's turns, warnings and bytes to another run's id, so the reader
            # is told what could not be decided instead.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Session not found: {session_id}",
            ) from exc

        try:
            trajectory = build_trajectory(session_dir, detail=detail)
        except FileNotFoundError as exc:
            # The directory was resolved and then went away — a deleted run, a
            # mirror pruned mid-poll. That is a missing session, not a 500.
            raise HTTPException(
                status_code=404,
                detail=f"Session directory is gone: {session_dir}",
            ) from exc

        document = trajectory.model_dump(mode="json")
        if since_seq is not None:
            document["turns"] = [
                turn
                for turn in document["turns"]
                # A turn that names no sequence cannot be shown unchanged, so it
                # is restated rather than assumed to be the row already held.
                if not turn["control_seq"] or max(turn["control_seq"]) > since_seq
            ]
        elif since is not None:
            document["turns"] = [turn for turn in document["turns"] if turn["turn_id"] > since]
        return document

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
