"""Owner: W1. Token usage and quota of the current user."""

from __future__ import annotations

from fastapi import APIRouter

from botai.core.errors import not_implemented
from botai.usage.schemas import UsageOut

router = APIRouter(prefix="/api/v1", tags=["usage"])


@router.get("/usage", response_model=UsageOut)
async def usage() -> UsageOut:
    raise not_implemented("usage")
