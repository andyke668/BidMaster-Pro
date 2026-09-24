"""认证路由。

登录态已从进程内字典迁到 user_sessions 表（见 middleware/session_store.py）：
API 重启不再全员掉线，多 worker / 多副本一致，且「最后登录时间、登录 IP、
当前在线」都有了数据源。每次登录成功/失败都会落一条行为流水。
"""
from __future__ import annotations

import hashlib
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.timeutil import to_local_iso, utcnow_naive
from services.database import get_db
from services.middleware import activity_logger, session_store
from services.models import (
    User,
    UserSession,
    RBACUserRole,
    RBACRole,
    RBACRolePermission,
    RBACPermission,
)

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenInfo(BaseModel):
    token: str
    user_id: str
    email: str
    name: str
    role: str
    avatar: str | None = None


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Fallback for legacy SHA256 hashes
        return hashed == hashlib.sha256(password.encode("utf-8")).hexdigest()


async def _user_payload(user: User, db: AsyncSession) -> dict:
    """登录响应与 /me 共用的用户信息（含 RBAC 角色与权限码）。"""
    ur_result = await db.execute(
        select(RBACUserRole.role_id).where(RBACUserRole.user_id == user.id)
    )
    role_ids = [row[0] for row in ur_result.all()]

    roles: list[dict] = []
    permissions: list[str] = []
    if role_ids:
        role_result = await db.execute(select(RBACRole).where(RBACRole.id.in_(role_ids)))
        roles = [
            {"id": str(r.id), "name": r.name, "display_name": r.display_name}
            for r in role_result.scalars().all()
        ]
        rp_result = await db.execute(
            select(RBACPermission.code)
            .join(RBACRolePermission, RBACRolePermission.permission_id == RBACPermission.id)
            .where(RBACRolePermission.role_id.in_(role_ids))
        )
        permissions = list({row[0] for row in rp_result.all()})

    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "avatar": user.avatar,
        "is_active": user.is_active is not False,
        "last_login_at": to_local_iso(user.last_login_at),
        "roles": roles,
        "permissions": permissions,
    }


@router.post("/login")
async def login(
    data: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    client_ip, user_agent = session_store.client_meta(request)
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user:
        activity_logger.record_activity(
            "auth.login_failed",
            user_email=data.email,
            status="failed",
            error="账号不存在",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    if not user.password_hash:
        activity_logger.record_activity(
            "auth.login_failed",
            user=user,
            status="rejected",
            error="账号未设置密码",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        raise HTTPException(status_code=401, detail="该账户未设置密码，请联系管理员")

    # 判活用 `is False` 而不是 `not`：迁移前遗留的 NULL 行应继续按启用处理
    if user.is_active is False:
        activity_logger.record_activity(
            "auth.login_failed",
            user=user,
            status="rejected",
            error="账号已被禁用",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    if not _verify_password(data.password, user.password_hash):
        activity_logger.record_activity(
            "auth.login_failed",
            user=user,
            status="failed",
            error="密码错误",
            client_ip=client_ip,
            user_agent=user_agent,
        )
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = secrets.token_hex(32)
    await session_store.create_session(
        db, token=token, user_id=str(user.id), request=request
    )
    user.last_login_at = utcnow_naive()
    await db.flush()

    activity_logger.record_activity(
        "auth.login", user=user, client_ip=client_ip, user_agent=user_agent
    )

    return {"token": token, "user": await _user_payload(user, db)}


@router.get("/me")
async def get_current_user_info(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from services.middleware.rbac_middleware import get_current_user

    user = await get_current_user(request, db)
    return await _user_payload(user, db)


@router.get("/sessions")
async def list_my_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """我自己的活跃登录会话（换设备 / 换浏览器登录留下的痕迹）。"""
    from services.middleware.rbac_middleware import get_current_user

    user = await get_current_user(request, db)
    now = utcnow_naive()
    result = await db.execute(
        select(UserSession)
        .where(
            UserSession.user_id == str(user.id),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc())
        .limit(50)
    )
    current_token = session_store.bearer_token(request)
    current_hash = session_store.hash_token(current_token) if current_token else ""
    return {
        "sessions": [
            {
                "id": str(s.id),
                "client_ip": s.client_ip,
                "user_agent": s.user_agent,
                "created_at": to_local_iso(s.created_at),
                "last_seen_at": to_local_iso(s.last_seen_at),
                "expires_at": to_local_iso(s.expires_at),
                "is_current": s.token_hash == current_hash,
            }
            for s in result.scalars().all()
        ]
    }


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    from services.middleware.rbac_middleware import get_current_user

    user = await get_current_user(request, db)
    await session_store.revoke_token(db, session_store.bearer_token(request))
    activity_logger.record_activity("auth.logout", user=user)
    return {"success": True}


@router.put("/change-password")
async def change_password(
    request: Request,
    new_password: str,
    db: AsyncSession = Depends(get_db),
):
    from services.middleware.rbac_middleware import get_current_user

    user = await get_current_user(request, db)
    token = session_store.bearer_token(request)

    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于6位")

    user.password_hash = _hash_password(new_password)
    await db.flush()

    # 改密后吊销当前会话，强制重新登录
    await session_store.revoke_token(db, token)
    activity_logger.record_activity("auth.change_password", user=user)
    return {"success": True, "message": "密码已修改，请重新登录"}
