"""Executed inside the existing BotAI container; input and credentials travel on stdin."""

import json
from datetime import UTC, datetime, timedelta

from botai.auth import security, service
from botai.auth.schemas import RegisterRequest
from botai.db.models import AuthSession, Invite, User
from botai.db.session import get_engine, get_sessionmaker
from sqlalchemy import func, select


class OperationError(Exception):
    """Only fixed, non-secret error codes may cross the broker boundary."""


async def operate(request):
    if request["operation"] not in {"create", "invite", "list"}:
        raise OperationError("invalid_operation")
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        if request["operation"] == "list":
            rows = await db.execute(
                select(User, func.count(AuthSession.id))
                .outerjoin(
                    AuthSession,
                    (AuthSession.user_id == User.id) & (AuthSession.expires_at > now),
                )
                .group_by(User.id)
                .order_by(User.created_at)
            )
            return {
                "users": [
                    {
                        "id": user.id,
                        "name": user.display_name,
                        "email": user.email,
                        "is_owner": user.is_owner,
                        "disabled": user.disabled,
                        "active_sessions": sessions,
                    }
                    for user, sessions in rows
                ]
            }

        owner = await db.scalar(
            select(User)
            .where(User.is_owner.is_(True), User.disabled.is_(False))
            .order_by(User.created_at)
            .limit(1)
        )
        if owner is None:
            raise OperationError("no_active_owner")

        if request["operation"] == "create":
            body = RegisterRequest(
                invite_code=request["invite_code"],
                email=request["email"],
                display_name=request["name"],
                password=request["password"],
            )
            email = service.normalize_email(str(body.email))
            user = await db.scalar(select(User).where(User.email == email))
            if user is not None:
                if (
                    user.disabled
                    or user.is_owner
                    or user.display_name != request["name"]
                    or not await security.verify_password(user.password_hash, request["password"])
                ):
                    raise OperationError("account_conflict")
                return {
                    "status": "existing",
                    "id": user.id,
                    "email": user.email,
                    "name": user.display_name,
                    "is_owner": user.is_owner,
                }

        code_hash = security.token_hash(request["invite_code"])
        invite = await db.scalar(select(Invite).where(Invite.code_hash == code_hash))
        if invite is None:
            invite = Invite(
                code_hash=code_hash, created_by=owner.id, expires_at=now + timedelta(days=7)
            )
            db.add(invite)
            await db.commit()
        if invite.used_by is not None or invite.expires_at <= now:
            raise OperationError("invite_used_or_expired")
        if request["operation"] == "invite":
            return {"status": "ready", "expires_at": invite.expires_at.isoformat()}

        user = await service.register(
            db,
            invite_code=request["invite_code"],
            email=email,
            display_name=request["name"],
            password=request["password"],
            now=now,
        )
        return {
            "status": "created",
            "id": user.id,
            "email": user.email,
            "name": user.display_name,
            "is_owner": user.is_owner,
        }


async def entry(request):
    try:
        result = await operate(request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Validation and DB errors can include the password/SQL parameters. Never echo them.
        code = str(exc) if isinstance(exc, OperationError) else "operation_failed"
        print(json.dumps({"error": code}))
        raise SystemExit(1) from None
    finally:
        await get_engine().dispose()
