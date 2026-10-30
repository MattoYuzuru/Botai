"""Owner: W0. Assembles the synthetic university. Owners extend their own router module."""

from __future__ import annotations

from fastapi import FastAPI

from tests.fakeuni import api, deep, factory, lms, login, pages, slide, spa


def create_fakeuni() -> FastAPI:
    app = FastAPI(title="fakeuni")
    for r in (
        login.router,
        api.router,
        pages.router,
        deep.router,
        factory.router,
        slide.router,
        spa.router,
        lms.router,
    ):
        app.include_router(r)
    return app
