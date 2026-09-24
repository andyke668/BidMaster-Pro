"""每人每日用量配额与限流。

只统计「会产生 LLM 开销的写操作」（POST/PUT/PATCH/DELETE），GET 一律放过——
前端轮询任务进度的 GET 一次全面检查能打出几百个，计入配额会秒封号。

计数 = 已落库的行为流水 + 当前仍在跑的在途任务。后者必须算进来：
异步任务的行为流水是「提交时落 running、结束时改结果」，如果只数库里的行，
用户可以在几秒内连点几十次全面检查而全部放行。

限额 0 表示不限；用户没有 user_quotas 记录时回落到 settings 里的全局默认值。
admin 角色不限流（他就是运营者本人）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import get_settings
from core.timeutil import get_tz, local_day_start_utc
from services.database import get_db
from services.middleware.rbac_middleware import get_current_user
from services.models import LLMUsageLog, User, UserActivityLog, UserQuota

logger = logging.getLogger(__name__)

# URL 前缀 -> 配额桶。新增业务路由时在这里登记即可
_PATH_BUCKETS: tuple[tuple[str, str], ...] = (
    ("/api/check", "check"),
    ("/api/generate", "generate"),
    ("/api/interpret", "interpret"),
    ("/api/format", "format"),
)

_BUCKET_LABELS = {
    "check": "投标检查",
    "generate": "投标生成",
    "interpret": "招标解读",
    "format": "文档输出",
}

_TRACKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def bucket_for_path(path: str) -> str | None:
    # 生产 nginx 把站点挂在 /zdx 下（虽然 rewrite 掉了前缀），这里再兜一层，
    # 与 route_activity.api_path 保持同一口径，避免 root_path 变化时配额静默失效。
    index = path.find("/api/")
    normalized = path[index:] if index >= 0 else path
    for prefix, bucket in _PATH_BUCKETS:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return bucket
    return None


async def get_quota(db: AsyncSession, user_id: str) -> UserQuota | None:
    result = await db.execute(select(UserQuota).where(UserQuota.user_id == str(user_id)))
    return result.scalar_one_or_none()


async def effective_limits(db: AsyncSession, user_id: str) -> tuple[int, int]:
    """返回 (每日动作上限, 每日 token 上限)，0 表示不限。"""
    settings = get_settings()
    quota = await get_quota(db, user_id)
    if quota is None:
        return (
            int(settings.default_daily_action_quota or 0),
            int(settings.default_daily_token_quota or 0),
        )
    return (
        int(quota.daily_action_limit or 0),
        int(quota.daily_token_limit or 0),
    )


def _running_task_count(user_id: str, bucket: str | None = None) -> int:
    """当前在途任务数。TaskManager 是进程内单例，多 worker 下只反映本进程，
    属于「宁可少算不可多算」的保守方向，不会误伤用户。"""
    try:
        from core.task_manager import TaskManager, task_bucket

        tasks = TaskManager.instance().list_active_tasks(owner_id=str(user_id))
    except Exception:
        return 0
    if bucket is None:
        return len(tasks)
    return sum(1 for t in tasks if task_bucket(t.task_type) == bucket)


async def daily_usage(
    db: AsyncSession, user_id: str, bucket: str | None = None
) -> dict[str, int]:
    """本地自然日内该用户的用量。rejected（参数/权限被拒）不计入，它没烧算力。"""
    day_start = local_day_start_utc(get_tz())
    action_conditions = [
        UserActivityLog.user_id == str(user_id),
        UserActivityLog.created_at >= day_start,
        UserActivityLog.status != "rejected",
    ]
    token_conditions = [
        LLMUsageLog.user_id == str(user_id),
        LLMUsageLog.created_at >= day_start,
    ]
    if bucket:
        action_conditions.append(UserActivityLog.action.like(f"{bucket}.%"))
        token_conditions.append(LLMUsageLog.action.like(f"{bucket}.%"))

    actions = (
        await db.execute(select(func.count()).select_from(UserActivityLog).where(*action_conditions))
    ).scalar_one()
    tokens = (
        await db.execute(
            select(func.coalesce(func.sum(LLMUsageLog.total_tokens), 0)).where(*token_conditions)
        )
    ).scalar_one()
    return {"actions": int(actions or 0), "tokens": int(tokens or 0)}


async def enforce_quota(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    """路由级依赖：挂在 check / generate / interpret / format 四个 router 上。"""
    if request.method.upper() not in _TRACKED_METHODS:
        return user
    if (user.role or "") == "admin":
        return user
    bucket = bucket_for_path(request.url.path)
    if bucket is None:
        return user

    action_limit, token_limit = await effective_limits(db, user.id)
    if not action_limit and not token_limit:
        return user

    used = await daily_usage(db, user.id, bucket)
    label = _BUCKET_LABELS.get(bucket, bucket)

    if action_limit:
        inflight = _running_task_count(user.id, bucket)
        if used["actions"] + inflight >= action_limit:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"今日「{label}」次数已达上限（{action_limit} 次/天），"
                    f"请明天再试或联系管理员调整配额"
                ),
            )
    if token_limit and used["tokens"] >= token_limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"今日「{label}」Token 消耗已达上限（{token_limit} tokens/天），"
                f"请明天再试或联系管理员调整配额"
            ),
        )
    return user
