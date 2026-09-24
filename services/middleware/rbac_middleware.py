from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.models import (
    User,
    RBACPermission,
    RBACUserRole,
    RBACRolePermission,
)
from services.middleware import request_context, session_store


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """解析 Bearer Token -> 会话表 -> 用户，并顺带完成两件事：

    1. 刷新会话心跳（session_store.touch，带节流），这是「当前在线」的数据来源；
    2. 把身份写入 ContextVar，深层的 LLM 网关据此把 token 消耗归到具体某个人。

    登录态原先在 auth.py 的进程内字典里，现已迁到 user_sessions 表，
    因此 API 重启不再全员掉线，也不再被「UVICORN_WORKERS 必须为 1」绑死。
    """
    client_ip, _ = session_store.client_meta(request)
    token = session_store.bearer_token(request)
    if not request.headers.get("Authorization", "").startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="未提供有效的Authorization头，请使用Bearer Token认证",
        )
    if not token:
        raise HTTPException(status_code=401, detail="Token不能为空")

    session = await session_store.get_session(db, token)
    if not session:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    user_id_str = session.user_id
    if not user_id_str or len(user_id_str) < 10:
        raise HTTPException(status_code=401, detail="无效的用户标识")

    result = await db.execute(select(User).where(User.id == user_id_str))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    # 判活用 `is False` 而不是 `not`：迁移前遗留的 NULL 行应继续按启用处理
    if user.is_active is False:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    await session_store.touch(db, session.token_hash)
    request_context.set_current_user(user, client_ip=client_ip)
    # 供 ActivityMonitorMiddleware 归属身份：request.state 落在共享的
    # scope["state"] 上，中间件在响应阶段还能读到，不需要再查一次库。
    try:
        request.state.bmp_user = {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
        }
    except Exception:  # 极端情况下 scope 不可写，监控降级即可，不能挡住请求
        pass
    return user


async def get_current_user_optional(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    """可选的当前用户依赖：未登录时不抛 401,返回 None

    适用于「允许未登录使用」或「未登录时使用默认用户兜底」的端点。
    """
    try:
        return await get_current_user(request, db)
    except HTTPException:
        request_context.set_current_user(None)
        return None


async def check_user_permission(
    user_id: str,
    permission_code: str,
    db: AsyncSession,
) -> bool:
    perm_result = await db.execute(
        select(RBACPermission).where(RBACPermission.code == permission_code)
    )
    perm = perm_result.scalar_one_or_none()
    if not perm:
        return False

    ur_result = await db.execute(
        select(RBACUserRole.role_id).where(RBACUserRole.user_id == user_id)
    )
    role_ids = [row[0] for row in ur_result.all()]
    if not role_ids:
        return False

    rp_result = await db.execute(
        select(RBACRolePermission).where(
            RBACRolePermission.role_id.in_(role_ids),
            RBACRolePermission.permission_id == perm.id,
        )
    )
    return rp_result.scalar_one_or_none() is not None


def require_permission(permission_code: str):
    async def permission_dependency(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> User:
        user = await get_current_user(request, db)

        has_perm = await check_user_permission(user.id, permission_code, db)
        if not has_perm:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足: 需要 {permission_code}",
            )
        return user

    return permission_dependency
