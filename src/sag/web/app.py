"""FastAPI application factory for the SAG Workbench API."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from sag.web.read_model import ReadModelBuilder


def _single_snapshot(payload: dict) -> Iterator[str]:
    yield "event: snapshot\n"
    yield f"data: {json.dumps(payload)}\n\n"


def create_app(read_model: ReadModelBuilder | None = None) -> FastAPI:
    builder = read_model if read_model is not None else ReadModelBuilder()
    app = FastAPI(title="SAG Workbench", version="0.1.0")

    @app.get("/api/workspaces")
    def get_workspaces() -> dict:
        return builder.dashboard().model_dump(mode="json", by_alias=True)

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

    return app


__all__ = ["create_app"]
