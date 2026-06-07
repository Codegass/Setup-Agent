"""Local web server entry points for SAG Workbench."""

from pathlib import Path

import uvicorn

from sag.web.app import create_app
from sag.web.read_model import ReadModelBuilder


STATIC_DIR = Path(__file__).with_name("static")


def run_web_server(host: str = "127.0.0.1", port: int = 0, demo: bool = False) -> None:
    app = create_app(ReadModelBuilder(demo_mode=demo))
    uvicorn.run(app, host=host, port=port, log_level="info")
