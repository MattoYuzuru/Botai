"""Owner: W1. Request-scoped dependencies shared by every module's router.

Tests override them: `app.dependency_overrides[get_services] = lambda: services`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from botai.core.clock import Clock, SystemClock
from botai.core.services import Services
from botai.settings import Settings, get_settings


@lru_cache
def _services() -> Services:
    from botai.wiring import build_services

    return build_services()


def get_services() -> Services:
    """The process-wide service container (vault, queue, llm, ...), built on first use."""
    return _services()


def get_clock() -> Clock:
    """Wall clock for request handling. Independent of `Services` so auth stays light."""
    return SystemClock()


ServicesDep = Annotated[Services, Depends(get_services)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
