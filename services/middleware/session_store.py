"""会话存储与在线状态（presence）。

登录态从 routers/auth.py 的进程内字典迁到 user_sessions 表。收益：
① API 重启不再全员掉线；② 多 uvicorn worker / 多副本一致，UVICORN_WORKERS
   不再被「必须为 1」绑死；③「当前在线 / 最后登录时间 / 登录 IP / 设备」有了
   可靠数据源，管理后台才做得出来。

在线判定：last_seen_at 落在 presence_online_seconds 窗口内即算在线。
心跳由 get_current_user 顺带刷新，并按 presence_touch_seconds 节流——
前端轮询很密，不节流会把 UPDATE 放大成写风暴。
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import get_settings
from core.timeutil import utcnow_naive
from services.models import UserSession

logger = logging.getLogger(__name__)

# token_hash -> 上次落库 last_seen_at 的 monotonic 时刻，仅用于心跳节流。
# 进程内缓存丢了也无所谓：最坏就是多写一次库。
_touch_marks: dict[str, float] = {}
_TOUCH_MARKS_MAX = 20000


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_meta(request: Request | None) -> tuple[str | None, str | None]:
    """取客户端 IP 与 UA。生产走 nginx 反代，优先 X-Forwarded-For 的第一跳。"""
    if request is None:
        return None, None
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not ip and request.client is not None:
        ip = (request.client.host or "").strip()
    ua = (request.headers.get("User-Agent") or "").strip()
    return (ip[:64] or None), (ua[:256] or None)


def bearer_token(request: Request | None) -> str:
    if request is None:
        return ""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


async def create_session(
    db: AsyncSession,
    *,
    token: str,
    user_id: str,
    request: Request | None = None,
) -> UserSession:
    settings = get_settings()
    ip, ua = client_meta(request)
    now = utcnow_naive()
    row = UserSession(
        token_hash=hash_token(token),
        user_id=str(user_id),
        client_ip=ip,
        user_agent=ua,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(row)
    await db.flush()
    return row


async def get_session(db: AsyncSession, token: str) -> UserSession | None:
    """按明文 token 取有效会话；过期或已吊销返回 None。"""
    if not token:
        return None
    now = utcnow_naive()
    result = await db.execute(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    )
    row = result.scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < now:
        return None
    return row


async def touch(db: AsyncSession, token_hash: str) -> None:
    """刷新 last_seen_at（带节流）。这是「在线」判定的数据来源。"""
    now_mono = time.monotonic()
    last = _touch_marks.get(token_hash)
    interval = get_settings().presence_touch_seconds
    if last is not None and now_mono - last < interval:
        return
    if len(_touch_marks) > _TOUCH_MARKS_MAX:
        _touch_marks.clear()
    _touch_marks[token_hash] = now_mono
    try:
        await db.execute(
            update(UserSession)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
            )
            .values(last_seen_at=utcnow_naive())
        )
    except Exception as exc:  # 心跳失败绝不能影响正常请求
        logger.warning(f"刷新会话心跳失败: {exc}")


def forget_touch(token_hash: str) -> None:
    _touch_marks.pop(token_hash, None)


async def revoke_token(db: AsyncSession, token: str) -> int:
    if not token:
        return 0
    token_hash = hash_token(token)
    forget_touch(token_hash)
    result = await db.execute(
        update(UserSession)
        .where(UserSession.token_hash == token_hash, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow_naive())
    )
    return int(result.rowcount or 0)


async def revoke_all_for_user(db: AsyncSession, user_id: str) -> int:
    """管理员强制下线：吊销该用户的全部会话，下一次请求即 401。"""
    result = await db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == str(user_id),
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow_naive())
    )
    _touch_marks.clear()
    return int(result.rowcount or 0)


async def online_map(db: AsyncSession) -> dict[str, object]:
    """{user_id: 最近一次心跳时刻}，只统计在线窗口内且未过期未吊销的会话。"""
    settings = get_settings()
    now = utcnow_naive()
    threshold = now - timedelta(seconds=settings.presence_online_seconds)
    result = await db.execute(
        select(UserSession.user_id, func.max(UserSession.last_seen_at))
        .where(
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            UserSession.last_seen_at >= threshold,
        )
        .group_by(UserSession.user_id)
    )
    return {str(uid): seen for uid, seen in result.all() if uid}


async def purge_expired(db: AsyncSession, *, grace_days: int = 7) -> int:
    """清理早已过期 / 早已吊销的会话行，避免表无限膨胀。"""
    cutoff = utcnow_naive() - timedelta(days=grace_days)
    result = await db.execute(
        sa_delete(UserSession).where(
            (UserSession.expires_at < cutoff)
            | (UserSession.revoked_at.is_not(None) & (UserSession.revoked_at < cutoff))
        )
    )
    _touch_marks.clear()
    return int(result.rowcount or 0)
