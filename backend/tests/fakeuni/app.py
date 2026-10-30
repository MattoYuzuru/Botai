"""Owner: W0. Assembles the synthetic university. Owners extend their own router module."""

from __future__ import annotations

from fastapi import FastAPI

from tests.fakeuni import api, deep, factory, login, pages, slide


def create_fakeuni() -> FastAPI:
    app = FastAPI(title="fakeuni")
    for r in (login.router, api.router, pages.router, deep.router, factory.router, slide.router):
        app.include_router(r)
    return app
