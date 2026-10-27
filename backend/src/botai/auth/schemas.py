from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, SecretStr

from botai.core.api import ApiModel


class RegisterRequest(ApiModel):
    invite_code: str = Field(min_length=6, max_length=64)
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=8, max_length=200)


class LoginRequest(ApiModel):
    email: EmailStr
    password: SecretStr


class InviteCreated(ApiModel):
    code: str  # shown once
    expires_at: datetime


class UserOut(ApiModel):
    id: str
    email: str
    display_name: str
    is_owner: bool


class QuotaOut(ApiModel):
    tokens_today: int
    tokens_limit: int
    resets_at: datetime


class MeOut(ApiModel):
    user: UserOut
    quota: QuotaOut
