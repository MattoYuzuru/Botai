"""Owner: W1. Invite-only registration, cookie sessions."""

from __future__ import annotations

from fastapi import APIRouter, Response

from botai.auth.schemas import InviteCreated, LoginRequest, MeOut, RegisterRequest
from botai.core.errors import not_implemented

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post("/auth/register", response_model=MeOut, status_code=201)
async def register(body: RegisterRequest, response: Response) -> MeOut:
    raise not_implemented("register")


@router.post("/auth/login", response_model=MeOut)
async def login(body: LoginRequest, response: Response) -> MeOut:
    raise not_implemented("login")


@router.post("/auth/logout", status_code=204)
async def logout(response: Response) -> None:
    raise not_implemented("logout")


@router.post("/auth/invites", response_model=InviteCreated, status_code=201)
async def create_invite() -> InviteCreated:
    raise not_implemented("create_invite")


@router.get("/me", response_model=MeOut)
async def me() -> MeOut:
    raise not_implemented("me")
