from __future__ import annotations

import argparse
import os
from pathlib import Path

from fastapi import FastAPI, Query, Response

from guizhou_stay.errors import DomainError
from guizhou_stay.portal import FolderReader


def create_app(directory: Path) -> FastAPI:
    fixture = FolderReader(directory)
    app = FastAPI(title="旅居合作方回放门户")

    @app.get("/offers")
    def offers(cursor: str = Query(default="first", max_length=48)):
        try:
            html, _ = fixture.read(None if cursor == "first" else cursor)
        except DomainError as exc:
            return Response(exc.message, status_code=503 if exc.status == 503 else 400,
                            media_type="text/plain")
        return Response(html, media_type="text/html")

    return app


def from_environment() -> FastAPI:
    selected = os.environ.get("STAY_PORTAL_FIXTURES")
    directory = Path(selected) if selected else Path(__file__).resolve().parents[1] / "fixtures" / "partner"
    return create_app(directory)
