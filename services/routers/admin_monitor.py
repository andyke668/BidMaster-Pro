"""管理后台 · 使用监控。

给管理员看「谁在用、用了多少、此刻在干什么」。全部端点由 settings.monitor
权限保护：admin 角色拿全量权限；project_manager 用的是「排除法」，已在
routers/rbac.py 的 DEFAULT_ROLES 里显式排除 settings.monitor，否则它会在
下次重启时被自动授予。

三个数据源，性质不同，不要混用：
- user_activity_log / llm_usage_log：历史用量，持久，是唯一的统计口径；
- user_sessions：在线与最后登录，持久，心跳按 presence_touch_seconds 节流刷新；
- TaskManager：此刻在跑的任务，**进程内**，重启即空。所以它只用于实时视图
  （presence / tasks）和配额限流的在途计数，绝不参与历史统计。

时间口径：库里全是 naive UTC，对外一律按 settings.report_timezone 换算后输出
带偏移的 ISO 串；「今日」「按天趋势」也按本地自然日切，否则会整体差 8 小时。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.http_headers import content_disposition
from core.settings import get_settings
from core.task_manager import TaskManager, task_bucket, task_label
from core.timeutil import (
    get_tz,
    local_day_series,
    local_day_start_utc,
    normalize_range,
    range_start_utc,
    to_local_iso,
    utcnow_naive,
)
from services.database import get_db
from services.middleware import activity_logger, session_store
from services.middleware import quota as quota_service
from services.middleware.rbac_middleware import get_current_user, require_permission
from services.models import (
    LLMUsageLog,
    Project,
    RBACRole,
    RBACUserRole,
    User,
    UserActivityLog,
    UserQuota,
    UserSession,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[
        Depends(get_current_user),
        Depends(require_permission("settings.monitor")),
    ]
)

_RANGE_DESC = "统计区间：today / 24h / 7d / 30d / 90d / all"
_PAGE_SIZE_MAX = 200
_CSV_BOM = "\ufeff"  # Excel 打开 UTF-8 CSV 不加 BOM 会把中文显示成乱码


# ─────────────────────────── 通用工具 ───────────────────────────


def _local_day_expr(column):
    """naive UTC 列 -> 本地自然日，用于按天分组。

    偏移取整小时（Asia/Shanghai 恒为 +8 且无夏令时），所以加一个 timedelta
    再取 date 就是正确的本地日期。调用方需自行 try/except：万一驱动不接受
    DateTime + timedelta，趋势图退化成按 UTC 日分组，而不是整个端点 500。
    """
    offset = datetime.now(get_tz()).utcoffset() or timedelta(0)
    hours = int(offset.total_seconds() // 3600)
    if hours:
        return func.date(column + timedelta(hours=hours))
    return func.date(column)


def _day_key(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


async def _load_user_index(db: AsyncSession) -> dict[str, dict]:
    """{user_id: 基础资料}，含 RBAC 角色显示名。一次查完，避免 N+1。"""
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    ur_rows = (await db.execute(select(RBACUserRole))).scalars().all()
    role_rows = (await db.execute(select(RBACRole))).scalars().all()
    role_by_id = {str(r.id): r for r in role_rows}

    user_roles: dict[str, list[str]] = {}
    user_role_names: dict[str, list[str]] = {}
    for row in ur_rows:
        role = role_by_id.get(str(row.role_id))
        if role:
            user_roles.setdefault(str(row.user_id), []).append(
                role.display_name or role.name
            )
            user_role_names.setdefault(str(row.user_id), []).append(role.name)

    return {
        str(u.id): {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "roles": user_roles.get(str(u.id), []),
            "role_names": user_role_names.get(str(u.id), []),
            "is_active": u.is_active is not False,
            "created_at": to_local_iso(u.created_at),
            "last_login_at": to_local_iso(u.last_login_at),
        }
        for u in users
    }


async def _presence_map(db: AsyncSession) -> dict[str, Any]:
    """{user_id: 最近心跳}，只含在线窗口内的会话。"""
    return await session_store.online_map(db)


def _active_tasks_by_owner() -> dict[str, list[dict]]:
    """{owner_id: [在途任务]}。owner 为空的任务归到 "" 键下，仍会出现在 /tasks 里。"""
    tz = get_tz()
    grouped: dict[str, list[dict]] = {}
    for task in TaskManager.instance().list_active_tasks():
        payload = task.to_admin_dict()
        payload["label"] = task_label(task.task_type)
        payload["bucket"] = task_bucket(task.task_type)
        payload["submitted_at"] = (
            datetime.fromtimestamp(task.created_ts, tz).isoformat(timespec="seconds")
        )
        grouped.setdefault(task.owner_id or "", []).append(payload)
    return grouped


async def _activity_totals(
    db: AsyncSession,
    start: datetime | None,
    *,
    user_id: str | None = None,
    action_prefix: str | None = None,
    extra_conditions: list | None = None,
) -> dict[str, int]:
    conditions: list = []
    if start is not None:
        conditions.append(UserActivityLog.created_at >= start)
    if user_id:
        conditions.append(UserActivityLog.user_id == str(user_id))
    if action_prefix:
        conditions.append(UserActivityLog.action.like(f"{action_prefix}.%"))
    if extra_conditions:
        conditions.extend(extra_conditions)

    def _count_status(status: str):
        return func.coalesce(
            func.sum(case((UserActivityLog.status == status, 1), else_=0)), 0
        )

    row = (
        await db.execute(
            select(
                func.count().label("total"),
                _count_status("success").label("ok"),
                _count_status("failed").label("failed"),
                _count_status("rejected").label("rejected"),
                _count_status("running").label("running"),
                func.count(distinct(UserActivityLog.user_id)).label("users"),
            )
            .select_from(UserActivityLog)
            .where(*conditions)
        )
    ).one()
    total = int(row.total or 0)
    failed = int(row.failed or 0)
    # 失败率只拿「系统故障」除以「真正跑过的动作」：rejected 是用户填错参数，
    # running 是还没跑完，混进去都会让这个数字失去意义。
    denominator = total - int(row.rejected or 0) - int(row.running or 0)
    return {
        "total": total,
        "success": int(row.ok or 0),
        "failed": failed,
        "rejected": int(row.rejected or 0),
        "running": int(row.running or 0),
        "active_users": int(row.users or 0),
        "failure_rate": round(failed / denominator, 4) if denominator > 0 else 0.0,
    }


async def _token_totals(
    db: AsyncSession,
    start: datetime | None,
    *,
    user_id: str | None = None,
    action_prefix: str | None = None,
) -> dict[str, int]:
    conditions: list = []
    if start is not None:
        conditions.append(LLMUsageLog.created_at >= start)
    if user_id:
        conditions.append(LLMUsageLog.user_id == str(user_id))
    if action_prefix:
        conditions.append(LLMUsageLog.action.like(f"{action_prefix}.%"))
    row = (
        await db.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(LLMUsageLog.prompt_tokens), 0).label("prompt"),
                func.coalesce(func.sum(LLMUsageLog.completion_tokens), 0).label("completion"),
                func.coalesce(func.sum(LLMUsageLog.total_tokens), 0).label("total"),
            ).where(*conditions)
        )
    ).one()
    return {
        "calls": int(row.calls or 0),
        "prompt_tokens": int(row.prompt or 0),
        "completion_tokens": int(row.completion or 0),
        "total_tokens": int(row.total or 0),
    }


async def _trend(db: AsyncSession, range_key: str) -> list[dict]:
    tz = get_tz()
    days = local_day_series(range_key, tz)
    actions = dict.fromkeys(days, 0)
    active_users = dict.fromkeys(days, 0)
    tokens = dict.fromkeys(days, 0)
    start = range_start_utc(range_key, tz)
    try:
        expr = _local_day_expr(UserActivityLog.created_at)
        conditions = [] if start is None else [UserActivityLog.created_at >= start]
        rows = (
            await db.execute(
                select(
                    expr.label("day"),
                    func.count().label("actions"),
                    func.count(distinct(UserActivityLog.user_id)).label("users"),
                )
                .where(*conditions)
                .group_by(expr)
            )
        ).all()
        for day, count, users in rows:
            key = _day_key(day)
            if key in actions:
                actions[key] = int(count or 0)
                active_users[key] = int(users or 0)

        token_expr = _local_day_expr(LLMUsageLog.created_at)
        token_conditions = [] if start is None else [LLMUsageLog.created_at >= start]
        token_rows = (
            await db.execute(
                select(
                    token_expr.label("day"),
                    func.coalesce(func.sum(LLMUsageLog.total_tokens), 0).label("tokens"),
                )
                .where(*token_conditions)
                .group_by(token_expr)
            )
        ).all()
        for day, total in token_rows:
            key = _day_key(day)
            if key in tokens:
                tokens[key] = int(total or 0)
    except Exception as exc:  # 驱动不支持日期偏移时退化为全 0，不拖垮整个看板
        logger.warning(f"趋势聚合失败，返回空趋势: {exc}")
    return [
        {
            "date": day,
            "actions": actions[day],
            "active_users": active_users[day],
            "tokens": tokens[day],
        }
        for day in days
    ]


def _serialize_activity(row: UserActivityLog) -> dict:
    detail = row.detail if isinstance(row.detail, dict) else {}
    return {
        "id": str(row.id),
        "user_id": row.user_id,
        "user_name": row.user_name,
        "user_email": row.user_email,
        "action": row.action,
        "action_label": activity_logger.action_label(row.action),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_name": row.resource_name,
        "project_id": row.project_id,
        "project_name": row.project_name,
        "detail": detail,
        "status": row.status,
        "status_label": activity_logger.STATUS_LABELS.get(row.status or "", row.status),
        "error_message": row.error_message,
        "duration_ms": row.duration_ms,
        "client_ip": row.client_ip,
        "user_agent": row.user_agent,
        "created_at": to_local_iso(row.created_at),
        "finished_at": to_local_iso(row.finished_at),
    }


async def _get_user_or_404(db: AsyncSession, user_id: str) -> User:
    user = (
        await db.execute(select(User).where(User.id == str(user_id)))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


# ─────────────────────────── 总览 ───────────────────────────


@router.get("/overview")
async def overview(
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
):
    """看板首屏：KPI 卡 + 按天趋势 + 动作分布 + 用量 TOP。"""
    range_key = normalize_range(range_key)
    tz = get_tz()
    range_start = range_start_utc(range_key, tz)
    day_start = local_day_start_utc(tz)

    user_index = await _load_user_index(db)
    presence = await _presence_map(db)
    tasks_by_owner = _active_tasks_by_owner()
    running_tasks = sum(len(v) for v in tasks_by_owner.values())

    users_total = len(user_index)
    users_disabled = sum(1 for u in user_index.values() if not u["is_active"])

    today_activity = await _activity_totals(db, day_start)
    range_activity = await _activity_totals(db, range_start)
    today_tokens = await _token_totals(db, day_start)
    range_tokens = await _token_totals(db, range_start)

    today_logins = (
        await db.execute(
            select(func.count())
            .select_from(UserActivityLog)
            .where(
                UserActivityLog.created_at >= day_start,
                UserActivityLog.action == "auth.login",
            )
        )
    ).scalar_one()
    today_login_failures = (
        await db.execute(
            select(func.count())
            .select_from(UserActivityLog)
            .where(
                UserActivityLog.created_at >= day_start,
                UserActivityLog.action == "auth.login_failed",
            )
        )
    ).scalar_one()

    # 动作分布（区间内）
    breakdown_conditions = (
        [] if range_start is None else [UserActivityLog.created_at >= range_start]
    )
    breakdown_rows = (
        await db.execute(
            select(
                UserActivityLog.action,
                func.count().label("count"),
                func.coalesce(
                    func.sum(case((UserActivityLog.status == "failed", 1), else_=0)), 0
                ).label("failed"),
                func.coalesce(func.avg(UserActivityLog.duration_ms), 0).label("avg_ms"),
            )
            .where(
                *breakdown_conditions,
                UserActivityLog.action.not_in(["auth.login", "auth.login_failed", "auth.logout"]),
            )
            .group_by(UserActivityLog.action)
            .order_by(func.count().desc())
            .limit(30)
        )
    ).all()
    action_breakdown = [
        {
            "action": row.action,
            "label": activity_logger.action_label(row.action),
            "count": int(row.count or 0),
            "failed": int(row.failed or 0),
            "avg_duration_ms": int(row.avg_ms or 0),
        }
        for row in breakdown_rows
    ]

    # 用量 TOP 10
    top_rows = (
        await db.execute(
            select(
                UserActivityLog.user_id,
                func.count().label("count"),
            )
            .where(
                *(breakdown_conditions or []),
                UserActivityLog.user_id.is_not(None),
                UserActivityLog.action.not_like("auth.%"),
                UserActivityLog.action.not_like("admin.%"),
            )
            .group_by(UserActivityLog.user_id)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    top_users = []
    for row in top_rows:
        info = user_index.get(str(row.user_id), {})
        top_users.append(
            {
                "user_id": str(row.user_id),
                "name": info.get("name") or "（已删除用户）",
                "email": info.get("email"),
                "roles": info.get("roles", []),
                "count": int(row.count or 0),
            }
        )

    return {
        "range": range_key,
        "timezone": str(tz),
        "generated_at": to_local_iso(utcnow_naive(), tz),
        "users": {
            "total": users_total,
            "active": users_total - users_disabled,
            "disabled": users_disabled,
        },
        "presence": {
            "online": len(presence),
            "busy": sum(1 for uid in presence if tasks_by_owner.get(uid)),
            "running_tasks": running_tasks,
            "online_window_seconds": int(get_settings().presence_online_seconds),
            "heartbeat_interval_seconds": int(get_settings().presence_touch_seconds),
        },
        "today": {
            "active_users": today_activity["active_users"],
            "actions": today_activity["total"],
            "failed_actions": today_activity["failed"],
            "failure_rate": today_activity["failure_rate"],
            "logins": int(today_logins or 0),
            "login_failures": int(today_login_failures or 0),
            "tokens": today_tokens,
        },
        "range_stats": {
            "active_users": range_activity["active_users"],
            "actions": range_activity["total"],
            "success_actions": range_activity["success"],
            "failed_actions": range_activity["failed"],
            "rejected_actions": range_activity["rejected"],
            "failure_rate": range_activity["failure_rate"],
            "tokens": range_tokens,
        },
        "trend": await _trend(db, range_key),
        "action_breakdown": action_breakdown,
        "top_users": top_users,
    }


# ─────────────────────────── 实时在线 ───────────────────────────


@router.get("/presence")
async def presence(
    db: AsyncSession = Depends(get_db),
    include_offline: bool = Query(False, description="true 时把离线用户也带上"),
):
    """当前在线 + 每人此刻在跑什么。前端 5 秒轮询这个端点。

    「在线」= 会话心跳落在 presence_online_seconds 窗口内；
    「忙碌」= 在线且 TaskManager 里有该用户的在途任务。
    """
    tz = get_tz()
    user_index = await _load_user_index(db)
    presence_map = await _presence_map(db)
    tasks_by_owner = _active_tasks_by_owner()

    user_ids = set(presence_map) | set(tasks_by_owner)
    if include_offline:
        user_ids |= set(user_index)

    items: list[dict] = []
    for uid in user_ids:
        info = user_index.get(str(uid))
        if info is None and not include_offline:
            continue  # 用户已被删除，且他也没有活跃会话/任务
        tasks = tasks_by_owner.get(str(uid), [])
        is_online = str(uid) in presence_map
        status = "offline"
        if is_online and tasks:
            status = "busy"
        elif is_online:
            status = "online"
        elif tasks:
            status = "busy"  # 会话刚过期但任务还在跑，仍算在用
        items.append(
            {
                "user_id": str(uid),
                "name": (info or {}).get("name") or "（已删除用户）",
                "email": (info or {}).get("email"),
                "roles": (info or {}).get("roles", []),
                "is_active": (info or {}).get("is_active", True),
                "status": status,
                "last_seen_at": to_local_iso(presence_map.get(str(uid)), tz),
                "last_login_at": (info or {}).get("last_login_at"),
                "tasks": tasks,
            }
        )

    order = {"busy": 0, "online": 1, "offline": 2}
    items.sort(key=lambda item: (order.get(item["status"], 3), item["name"]))
    online_count = sum(1 for i in items if i["status"] in ("online", "busy"))
    return {
        "generated_at": to_local_iso(utcnow_naive(), tz),
        "online_window_seconds": int(get_settings().presence_online_seconds),
        "online": online_count,
        "busy": sum(1 for i in items if i["status"] == "busy"),
        "running_tasks": sum(len(i["tasks"]) for i in items),
        "users": items,
    }


@router.get("/tasks")
async def running_tasks(db: AsyncSession = Depends(get_db)):
    """全部在途任务（跨用户）。TaskManager 是进程内的，重启后这里必然为空。"""
    tz = get_tz()
    user_index = await _load_user_index(db)
    tasks_by_owner = _active_tasks_by_owner()
    items: list[dict] = []
    for owner_id, tasks in tasks_by_owner.items():
        info = user_index.get(str(owner_id), {})
        for task in tasks:
            items.append(
                {
                    **task,
                    "owner_id": owner_id or None,
                    "owner_name": info.get("name") or ("（未归属）" if not owner_id else "（已删除用户）"),
                    "owner_email": info.get("email"),
                }
            )
    items.sort(key=lambda t: -(t.get("elapsed_seconds") or 0))
    return {
        "generated_at": to_local_iso(utcnow_naive(), tz),
        "count": len(items),
        "tasks": items,
    }


# ─────────────────────────── 用户用量 ───────────────────────────


_SORTABLE = {
    "name",
    "last_seen_at",
    "last_login_at",
    "last_action_at",
    "actions",
    "today_actions",
    "active_days",
    "tokens",
    "failed",
    "projects",
}


async def _build_user_usage_rows(
    db: AsyncSession, range_key: str
) -> list[dict]:
    """把「人 + 用量 + 在线态 + 配额」拼成一张宽表。/users 与 CSV 导出共用。"""
    tz = get_tz()
    start = range_start_utc(range_key, tz)
    day_start = local_day_start_utc(tz)

    user_index = await _load_user_index(db)
    presence_map = await _presence_map(db)
    tasks_by_owner = _active_tasks_by_owner()

    range_conditions = [] if start is None else [UserActivityLog.created_at >= start]
    business_filters = [
        UserActivityLog.user_id.is_not(None),
        UserActivityLog.action.not_like("auth.%"),
        UserActivityLog.action.not_like("admin.%"),
    ]

    per_user: dict[str, dict] = {
        uid: {
            "actions": 0,
            "failed": 0,
            "checks": 0,
            "active_days": 0,
            "last_action_at": None,
            "today_actions": 0,
            "logins": 0,
            "tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "llm_calls": 0,
            "projects": 0,
        }
        for uid in user_index
    }

    rows = (
        await db.execute(
            select(
                UserActivityLog.user_id,
                func.count().label("actions"),
                func.coalesce(
                    func.sum(case((UserActivityLog.status == "failed", 1), else_=0)), 0
                ).label("failed"),
                func.coalesce(
                    func.sum(case((UserActivityLog.action.like("check.%"), 1), else_=0)), 0
                ).label("checks"),
                func.max(UserActivityLog.created_at).label("last_action_at"),
            )
            .where(*range_conditions, *business_filters)
            .group_by(UserActivityLog.user_id)
        )
    ).all()
    for row in rows:
        bucket = per_user.get(str(row.user_id))
        if bucket is None:
            continue
        bucket["actions"] = int(row.actions or 0)
        bucket["failed"] = int(row.failed or 0)
        bucket["checks"] = int(row.checks or 0)
        bucket["last_action_at"] = to_local_iso(row.last_action_at, tz)

    # 活跃天数 = 区间内有动作的本地自然日数。日期偏移表达式万一是驱动不支持，
    # 就退化成 0，不让整个列表 500。
    try:
        day_expr = _local_day_expr(UserActivityLog.created_at)
        day_rows = (
            await db.execute(
                select(
                    UserActivityLog.user_id,
                    func.count(distinct(day_expr)).label("days"),
                )
                .where(*range_conditions, *business_filters)
                .group_by(UserActivityLog.user_id)
            )
        ).all()
        for row in day_rows:
            bucket = per_user.get(str(row.user_id))
            if bucket is not None:
                bucket["active_days"] = int(row.days or 0)
    except Exception as exc:
        logger.warning(f"活跃天数聚合失败，退化为 0: {exc}")

    today_rows = (
        await db.execute(
            select(UserActivityLog.user_id, func.count().label("actions"))
            .where(
                UserActivityLog.created_at >= day_start,
                *business_filters,
            )
            .group_by(UserActivityLog.user_id)
        )
    ).all()
    for row in today_rows:
        bucket = per_user.get(str(row.user_id))
        if bucket is not None:
            bucket["today_actions"] = int(row.actions or 0)

    login_conditions = [] if start is None else [UserActivityLog.created_at >= start]
    login_rows = (
        await db.execute(
            select(UserActivityLog.user_id, func.count().label("logins"))
            .where(
                *login_conditions,
                UserActivityLog.action == "auth.login",
                UserActivityLog.user_id.is_not(None),
            )
            .group_by(UserActivityLog.user_id)
        )
    ).all()
    for row in login_rows:
        bucket = per_user.get(str(row.user_id))
        if bucket is not None:
            bucket["logins"] = int(row.logins or 0)

    token_conditions = [] if start is None else [LLMUsageLog.created_at >= start]
    token_rows = (
        await db.execute(
            select(
                LLMUsageLog.user_id,
                func.count().label("calls"),
                func.coalesce(func.sum(LLMUsageLog.prompt_tokens), 0).label("prompt"),
                func.coalesce(func.sum(LLMUsageLog.completion_tokens), 0).label("completion"),
                func.coalesce(func.sum(LLMUsageLog.total_tokens), 0).label("total"),
            )
            .where(*token_conditions, LLMUsageLog.user_id.is_not(None))
            .group_by(LLMUsageLog.user_id)
        )
    ).all()
    for row in token_rows:
        bucket = per_user.get(str(row.user_id))
        if bucket is not None:
            bucket["llm_calls"] = int(row.calls or 0)
            bucket["prompt_tokens"] = int(row.prompt or 0)
            bucket["completion_tokens"] = int(row.completion or 0)
            bucket["tokens"] = int(row.total or 0)

    project_rows = (
        await db.execute(
            select(Project.user_id, func.count().label("projects")).group_by(Project.user_id)
        )
    ).all()
    for row in project_rows:
        bucket = per_user.get(str(row.user_id))
        if bucket is not None:
            bucket["projects"] = int(row.projects or 0)

    quota_rows = (await db.execute(select(UserQuota))).scalars().all()
    quota_by_user = {str(q.user_id): q for q in quota_rows}
    settings = get_settings()

    result: list[dict] = []
    for uid, info in user_index.items():
        stats = per_user.get(uid, {})
        quota = quota_by_user.get(uid)
        action_limit = (
            int(quota.daily_action_limit or 0)
            if quota
            else int(settings.default_daily_action_quota or 0)
        )
        token_limit = (
            int(quota.daily_token_limit or 0)
            if quota
            else int(settings.default_daily_token_quota or 0)
        )
        tasks = tasks_by_owner.get(uid, [])
        is_online = uid in presence_map
        if is_online and tasks:
            status = "busy"
        elif is_online or tasks:
            status = "busy" if tasks else "online"
        else:
            status = "offline"
        result.append(
            {
                **info,
                "status": status,
                "last_seen_at": to_local_iso(presence_map.get(uid), tz),
                "current_tasks": tasks,
                "actions": stats.get("actions", 0),
                "failed": stats.get("failed", 0),
                "checks": stats.get("checks", 0),
                "active_days": stats.get("active_days", 0),
                "last_action_at": stats.get("last_action_at"),
                "today_actions": stats.get("today_actions", 0),
                "logins": stats.get("logins", 0),
                "projects": stats.get("projects", 0),
                "llm_calls": stats.get("llm_calls", 0),
                "prompt_tokens": stats.get("prompt_tokens", 0),
                "completion_tokens": stats.get("completion_tokens", 0),
                "tokens": stats.get("tokens", 0),
                "quota": {
                    "daily_action_limit": action_limit,
                    "daily_token_limit": token_limit,
                    "today_actions": stats.get("today_actions", 0),
                    "today_tokens": 0,  # 由 _fill_today_tokens 补
                    "note": (quota.note if quota else None),
                    "customized": quota is not None,
                },
            }
        )
    return result


async def _fill_today_tokens(db: AsyncSession, rows: list[dict]) -> None:
    """配额条里的「今日 token」。单独查，避免 /users 主查询再多一次 join。"""
    day_start = local_day_start_utc(get_tz())
    token_rows = (
        await db.execute(
            select(
                LLMUsageLog.user_id,
                func.coalesce(func.sum(LLMUsageLog.total_tokens), 0).label("total"),
            )
            .where(
                LLMUsageLog.created_at >= day_start,
                LLMUsageLog.user_id.is_not(None),
            )
            .group_by(LLMUsageLog.user_id)
        )
    ).all()
    by_user = {str(r.user_id): int(r.total or 0) for r in token_rows}
    for row in rows:
        row["quota"]["today_tokens"] = by_user.get(row["id"], 0)


@router.get("/users")
async def list_user_usage(
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
    q: str = Query("", description="按姓名 / 邮箱模糊搜索"),
    role: str = Query("", description="按 RBAC 角色名过滤，如 bid_checker"),
    presence_filter: str = Query("", alias="presence", description="online/offline/busy/disabled"),
    sort: str = Query("last_seen_at"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
):
    range_key = normalize_range(range_key)
    rows = await _build_user_usage_rows(db, range_key)
    await _fill_today_tokens(db, rows)

    keyword = q.strip().lower()
    if keyword:
        rows = [
            r
            for r in rows
            if keyword in (r.get("name") or "").lower()
            or keyword in (r.get("email") or "").lower()
        ]
    if role.strip():
        # 兼容三种写法：旧的系统角色列、RBAC 角色名、RBAC 角色显示名
        role_name = role.strip()
        rows = [
            r
            for r in rows
            if role_name == r.get("role")
            or role_name in (r.get("role_names") or [])
            or role_name in (r.get("roles") or [])
        ]
    if presence_filter.strip():
        wanted = presence_filter.strip().lower()
        if wanted == "disabled":
            rows = [r for r in rows if not r["is_active"]]
        else:
            rows = [r for r in rows if r["status"] == wanted]

    sort_key = sort if sort in _SORTABLE else "last_seen_at"
    reverse = order.lower() != "asc"

    # 排序列混了 None / str / int / 浮点，直接排会抛 TypeError；
    # 统一成 (是否为空, 值) 二元组，空值恒定沉底。
    def _safe_key(row: dict):
        value = row.get(sort_key)
        if value is None:
            return (1, "")
        if isinstance(value, (int, float)):
            return (0, value)
        return (0, str(value))

    rows.sort(key=_safe_key, reverse=reverse)

    total = len(rows)
    start_index = (page - 1) * page_size
    page_rows = rows[start_index : start_index + page_size]
    return {
        "range": range_key,
        "timezone": str(get_tz()),
        "total": total,
        "page": page,
        "page_size": page_size,
        "users": page_rows,
    }


@router.get("/users/{user_id}/summary")
async def user_summary(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
):
    """单人详情：资料 + 用量 + 活跃会话 + 最近动作。"""
    range_key = normalize_range(range_key)
    tz = get_tz()
    start = range_start_utc(range_key, tz)
    day_start = local_day_start_utc(tz)
    user = await _get_user_or_404(db, user_id)

    presence_map = await _presence_map(db)
    tasks = _active_tasks_by_owner().get(str(user.id), [])

    range_stats = await _activity_totals(db, start, user_id=str(user.id))
    today_stats = await _activity_totals(db, day_start, user_id=str(user.id))
    range_tokens = await _token_totals(db, start, user_id=str(user.id))
    today_tokens = await _token_totals(db, day_start, user_id=str(user.id))

    breakdown_conditions = [
        UserActivityLog.user_id == str(user.id),
        UserActivityLog.action.not_like("auth.%"),
    ]
    if start is not None:
        breakdown_conditions.append(UserActivityLog.created_at >= start)
    breakdown_rows = (
        await db.execute(
            select(
                UserActivityLog.action,
                func.count().label("count"),
                func.coalesce(
                    func.sum(case((UserActivityLog.status == "failed", 1), else_=0)), 0
                ).label("failed"),
                func.coalesce(func.avg(UserActivityLog.duration_ms), 0).label("avg_ms"),
            )
            .where(*breakdown_conditions)
            .group_by(UserActivityLog.action)
            .order_by(func.count().desc())
        )
    ).all()

    now = utcnow_naive()
    session_rows = (
        await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == str(user.id),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .order_by(UserSession.last_seen_at.desc())
            .limit(20)
        )
    ).scalars().all()

    recent_rows = (
        await db.execute(
            select(UserActivityLog)
            .where(UserActivityLog.user_id == str(user.id))
            .order_by(UserActivityLog.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    quota = await quota_service.get_quota(db, str(user.id))
    settings = get_settings()
    is_online = str(user.id) in presence_map

    return {
        "range": range_key,
        "timezone": str(tz),
        "user": {
            "id": str(user.id),
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active is not False,
            "created_at": to_local_iso(user.created_at, tz),
            "last_login_at": to_local_iso(user.last_login_at, tz),
            "status": "busy" if (is_online and tasks) else ("online" if is_online else "offline"),
            "last_seen_at": to_local_iso(presence_map.get(str(user.id)), tz),
        },
        "range_stats": {**range_stats, "tokens": range_tokens},
        "today": {**today_stats, "tokens": today_tokens},
        "action_breakdown": [
            {
                "action": row.action,
                "label": activity_logger.action_label(row.action),
                "count": int(row.count or 0),
                "failed": int(row.failed or 0),
                "avg_duration_ms": int(row.avg_ms or 0),
            }
            for row in breakdown_rows
        ],
        "current_tasks": tasks,
        "sessions": [
            {
                "id": str(s.id),
                "client_ip": s.client_ip,
                "user_agent": s.user_agent,
                "created_at": to_local_iso(s.created_at, tz),
                "last_seen_at": to_local_iso(s.last_seen_at, tz),
                "expires_at": to_local_iso(s.expires_at, tz),
                "is_online": s.last_seen_at is not None
                and s.last_seen_at >= now - timedelta(seconds=settings.presence_online_seconds),
            }
            for s in session_rows
        ],
        "quota": {
            "daily_action_limit": int(
                quota.daily_action_limit if quota else settings.default_daily_action_quota or 0
            ),
            "daily_token_limit": int(
                quota.daily_token_limit if quota else settings.default_daily_token_quota or 0
            ),
            "note": quota.note if quota else None,
            "customized": quota is not None,
            "today_actions": today_stats["total"],
            "today_tokens": today_tokens["total_tokens"],
        },
        "recent_activities": [_serialize_activity(r) for r in recent_rows],
    }


# ─────────────────────────── 行为流水 ───────────────────────────


async def _query_activities(
    db: AsyncSession,
    *,
    range_key: str,
    user_id: str | None = None,
    action: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order_desc: bool = True,
) -> tuple[list[UserActivityLog], int]:
    conditions: list = []
    start = range_start_utc(range_key)
    if start is not None:
        conditions.append(UserActivityLog.created_at >= start)
    if user_id:
        conditions.append(UserActivityLog.user_id == str(user_id))
    if action:
        conditions.append(
            UserActivityLog.action == action
            if "." in action
            else UserActivityLog.action.like(f"{action}.%")
        )
    if status:
        conditions.append(UserActivityLog.status == status)
    if project_id:
        conditions.append(UserActivityLog.project_id == project_id)

    total = (
        await db.execute(
            select(func.count()).select_from(UserActivityLog).where(*conditions)
        )
    ).scalar_one()
    order_by = (
        UserActivityLog.created_at.desc() if order_desc else UserActivityLog.created_at
    )
    rows = (
        await db.execute(
            select(UserActivityLog)
            .where(*conditions)
            .order_by(order_by, UserActivityLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), int(total or 0)


@router.get("/activities")
async def list_activities(
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("24h", alias="range", description=_RANGE_DESC),
    user_id: str = Query(""),
    action: str = Query("", description="完整 action（check.full）或模块前缀（check）"),
    status: str = Query("", description="running/success/failed/rejected"),
    project_id: str = Query(""),
    limit: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
    offset: int = Query(0, ge=0),
):
    range_key = normalize_range(range_key)
    rows, total = await _query_activities(
        db,
        range_key=range_key,
        user_id=user_id or None,
        action=action or None,
        status=status or None,
        project_id=project_id or None,
        limit=limit,
        offset=offset,
    )
    return {
        "range": range_key,
        "timezone": str(get_tz()),
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": sorted(activity_logger.ACTION_LABELS.items()),
        "activities": [_serialize_activity(r) for r in rows],
    }


@router.get("/users/{user_id}/activities")
async def list_user_activities(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
    action: str = Query(""),
    status: str = Query(""),
    limit: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
    offset: int = Query(0, ge=0),
):
    range_key = normalize_range(range_key)
    rows, total = await _query_activities(
        db,
        range_key=range_key,
        user_id=user_id,
        action=action or None,
        status=status or None,
        limit=limit,
        offset=offset,
    )
    return {
        "range": range_key,
        "total": total,
        "limit": limit,
        "offset": offset,
        "activities": [_serialize_activity(r) for r in rows],
    }


@router.get("/tokens")
async def token_usage(
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
    group_by: str = Query("user", description="user / model / action"),
    limit: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
):
    """Token 消耗聚合。按需求只展示消耗量，不折算金额。"""
    range_key = normalize_range(range_key)
    tz = get_tz()
    start = range_start_utc(range_key, tz)
    conditions = [] if start is None else [LLMUsageLog.created_at >= start]

    key = group_by if group_by in ("user", "model", "action") else "user"
    column = {"user": LLMUsageLog.user_id, "model": LLMUsageLog.model, "action": LLMUsageLog.action}[key]
    rows = (
        await db.execute(
            select(
                column.label("key"),
                func.count().label("calls"),
                func.coalesce(func.sum(LLMUsageLog.prompt_tokens), 0).label("prompt"),
                func.coalesce(func.sum(LLMUsageLog.completion_tokens), 0).label("completion"),
                func.coalesce(func.sum(LLMUsageLog.total_tokens), 0).label("total"),
            )
            .where(*conditions)
            .group_by(column)
            .order_by(func.sum(LLMUsageLog.total_tokens).desc())
            .limit(limit)
        )
    ).all()

    user_index = await _load_user_index(db) if key == "user" else {}
    items = []
    for row in rows:
        raw_key = str(row.key) if row.key is not None else "（未归属）"
        label = raw_key
        if key == "user":
            info = user_index.get(raw_key)
            label = info["name"] if info else "（已删除用户）"
        elif key == "action":
            label = activity_logger.action_label(raw_key) or raw_key
        items.append(
            {
                "key": raw_key,
                "label": label,
                "calls": int(row.calls or 0),
                "prompt_tokens": int(row.prompt or 0),
                "completion_tokens": int(row.completion or 0),
                "total_tokens": int(row.total or 0),
            }
        )
    totals = await _token_totals(db, start)
    return {
        "range": range_key,
        "group_by": key,
        "totals": totals,
        "items": items,
    }


# ─────────────────────────── 告警 ───────────────────────────


_ALERT_FAILURE_RATE = 0.3
_ALERT_MIN_ACTIONS = 5
_ALERT_LOGIN_FAIL_PER_IP = 10
_ALERT_SESSIONS_PER_USER = 5
_ALERT_RUNNING_TASKS = 10
_ALERT_STUCK_TASK_SECONDS = 30 * 60


@router.get("/alerts")
async def alerts(
    db: AsyncSession = Depends(get_db),
    range_key: str = Query("24h", alias="range", description=_RANGE_DESC),
):
    """只读的风险提示，全部现算，不落库也不推送。

    六条规则：高失败率 / 配额将满 / 单 IP 登录失败集中（疑似撞库）/
    单账号会话过多（疑似共享账号）/ 在途任务堆积（网关压力大）/ 任务疑似卡死。
    """
    range_key = normalize_range(range_key)
    tz = get_tz()
    start = range_start_utc(range_key, tz) or (utcnow_naive() - timedelta(hours=24))
    user_index = await _load_user_index(db)
    settings = get_settings()
    items: list[dict] = []

    def _add(level: str, code: str, message: str, **extra):
        items.append(
            {"level": level, "code": code, "message": message, "detected_at": to_local_iso(utcnow_naive(), tz), **extra}
        )

    def _who(uid: str | None) -> str:
        info = user_index.get(str(uid), {}) if uid else {}
        return info.get("name") or "（已删除用户）"

    # 1. 高失败率
    failure_rows = (
        await db.execute(
            select(
                UserActivityLog.user_id,
                func.count().label("total"),
                func.coalesce(
                    func.sum(case((UserActivityLog.status == "failed", 1), else_=0)), 0
                ).label("failed"),
            )
            .where(
                UserActivityLog.created_at >= start,
                UserActivityLog.user_id.is_not(None),
                UserActivityLog.action.not_like("auth.%"),
            )
            .group_by(UserActivityLog.user_id)
            .having(func.count() >= _ALERT_MIN_ACTIONS)
        )
    ).all()
    for row in failure_rows:
        total = int(row.total or 0)
        failed = int(row.failed or 0)
        if total and failed / total >= _ALERT_FAILURE_RATE:
            _add(
                "danger",
                "high_failure_rate",
                f"{_who(row.user_id)} 在区间内 {total} 次操作失败 {failed} 次（{failed / total:.0%}），建议查看其失败明细",
                user_id=str(row.user_id),
                user_name=_who(row.user_id),
                value={"total": total, "failed": failed},
            )

    # 2. 配额将满（>=80%）
    day_start = local_day_start_utc(tz)
    today_rows = (
        await db.execute(
            select(UserActivityLog.user_id, func.count().label("actions"))
            .where(
                UserActivityLog.created_at >= day_start,
                UserActivityLog.user_id.is_not(None),
                UserActivityLog.status != "rejected",
                UserActivityLog.action.not_like("auth.%"),
                UserActivityLog.action.not_like("admin.%"),
            )
            .group_by(UserActivityLog.user_id)
        )
    ).all()
    quota_rows = (await db.execute(select(UserQuota))).scalars().all()
    quota_by_user = {str(q.user_id): q for q in quota_rows}
    for row in today_rows:
        uid = str(row.user_id)
        quota = quota_by_user.get(uid)
        limit = int(
            quota.daily_action_limit
            if quota
            else settings.default_daily_action_quota or 0
        )
        used = int(row.actions or 0)
        if limit > 0 and used / limit >= 0.8:
            _add(
                "warning",
                "quota_near_limit",
                f"{_who(uid)} 今日已用 {used}/{limit} 次（{used / limit:.0%}），即将触发限流",
                user_id=uid,
                user_name=_who(uid),
                value={"used": used, "limit": limit},
            )

    # 3. 单 IP 登录失败集中
    ip_rows = (
        await db.execute(
            select(
                UserActivityLog.client_ip,
                func.count().label("fails"),
                func.count(distinct(UserActivityLog.user_email)).label("accounts"),
            )
            .where(
                UserActivityLog.created_at >= start,
                UserActivityLog.action == "auth.login_failed",
                UserActivityLog.client_ip.is_not(None),
            )
            .group_by(UserActivityLog.client_ip)
            .having(func.count() >= _ALERT_LOGIN_FAIL_PER_IP)
        )
    ).all()
    for row in ip_rows:
        _add(
            "danger",
            "brute_force_suspected",
            f"IP {row.client_ip} 在区间内登录失败 {int(row.fails)} 次，涉及 {int(row.accounts)} 个账号，疑似撞库",
            client_ip=row.client_ip,
            value={"fails": int(row.fails or 0), "accounts": int(row.accounts or 0)},
        )

    # 4. 单账号会话过多
    now = utcnow_naive()
    session_rows = (
        await db.execute(
            select(UserSession.user_id, func.count().label("sessions"))
            .where(
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .group_by(UserSession.user_id)
            .having(func.count() >= _ALERT_SESSIONS_PER_USER)
        )
    ).all()
    for row in session_rows:
        _add(
            "warning",
            "too_many_sessions",
            f"{_who(row.user_id)} 当前有 {int(row.sessions)} 个活跃会话，疑似账号共享",
            user_id=str(row.user_id),
            user_name=_who(row.user_id),
            value={"sessions": int(row.sessions or 0)},
        )

    # 5. / 6. 在途任务
    tasks_by_owner = _active_tasks_by_owner()
    all_tasks = [task for tasks in tasks_by_owner.values() for task in tasks]
    if len(all_tasks) >= _ALERT_RUNNING_TASKS:
        _add(
            "warning",
            "task_backlog",
            f"当前有 {len(all_tasks)} 个在途任务，LLM 网关压力较大，可能整体变慢",
            value={"running_tasks": len(all_tasks)},
        )
    for task in all_tasks:
        elapsed = float(task.get("elapsed_seconds") or 0)
        if elapsed >= _ALERT_STUCK_TASK_SECONDS:
            _add(
                "danger",
                "stuck_task",
                f"{_who(task.get('owner_id'))} 的「{task.get('label')}」已运行 {elapsed / 60:.0f} 分钟，疑似卡死",
                user_id=task.get("owner_id"),
                task_id=task.get("task_id"),
                value={"elapsed_seconds": elapsed},
            )

    level_order = {"danger": 0, "warning": 1, "info": 2}
    items.sort(key=lambda item: level_order.get(item["level"], 3))
    return {
        "range": range_key,
        "timezone": str(tz),
        "count": len(items),
        "alerts": items,
    }


# ─────────────────────────── 配额 ───────────────────────────


class QuotaUpdate(BaseModel):
    daily_action_limit: int = 0
    daily_token_limit: int = 0
    note: str | None = None


class StatusUpdate(BaseModel):
    is_active: bool


@router.get("/quotas")
async def list_quotas(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    quota_rows = (await db.execute(select(UserQuota))).scalars().all()
    quota_by_user = {str(q.user_id): q for q in quota_rows}
    rows = await _build_user_usage_rows(db, "today")
    await _fill_today_tokens(db, rows)
    return {
        "defaults": {
            "daily_action_limit": int(settings.default_daily_action_quota or 0),
            "daily_token_limit": int(settings.default_daily_token_quota or 0),
        },
        "timezone": str(get_tz()),
        "quotas": [
            {
                "user_id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "roles": row["roles"],
                "daily_action_limit": row["quota"]["daily_action_limit"],
                "daily_token_limit": row["quota"]["daily_token_limit"],
                "today_actions": row["quota"]["today_actions"],
                "today_tokens": row["quota"]["today_tokens"],
                "note": row["quota"]["note"],
                "customized": row["quota"]["customized"],
                "is_active": row["is_active"],
            }
            for row in rows
        ],
    }


@router.put("/users/{user_id}/quota")
async def update_quota(
    user_id: str,
    data: QuotaUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
):
    user = await _get_user_or_404(db, user_id)
    if data.daily_action_limit < 0 or data.daily_token_limit < 0:
        raise HTTPException(status_code=400, detail="配额不能为负数，0 表示不限")

    quota = await quota_service.get_quota(db, str(user.id))
    if quota is None:
        quota = UserQuota(user_id=str(user.id))
        db.add(quota)
    quota.daily_action_limit = int(data.daily_action_limit)
    quota.daily_token_limit = int(data.daily_token_limit)
    quota.note = (data.note or "")[:256] or None
    await db.flush()

    activity_logger.record_activity(
        "admin.set_quota",
        user=admin,
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.name,
        detail={
            "daily_action_limit": quota.daily_action_limit,
            "daily_token_limit": quota.daily_token_limit,
        },
        client_ip=session_store.client_meta(request)[0],
    )
    return {
        "success": True,
        "user_id": str(user.id),
        "daily_action_limit": quota.daily_action_limit,
        "daily_token_limit": quota.daily_token_limit,
        "note": quota.note,
    }


# ─────────────────────────── 管理动作 ───────────────────────────


@router.post("/users/{user_id}/force-logout")
async def force_logout(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
):
    """强制下线：吊销该用户全部会话，其下一次请求即 401。"""
    user = await _get_user_or_404(db, user_id)
    revoked = await session_store.revoke_all_for_user(db, str(user.id))
    await db.flush()
    activity_logger.record_activity(
        "admin.force_logout",
        user=admin,
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.name,
        detail={"revoked_sessions": revoked},
        client_ip=session_store.client_meta(request)[0],
    )
    return {"success": True, "revoked_sessions": revoked}


@router.put("/users/{user_id}/status")
async def set_user_status(
    user_id: str,
    data: StatusUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
):
    """启用 / 禁用账号。禁用会同时吊销其全部会话。"""
    user = await _get_user_or_404(db, user_id)

    if not data.is_active:
        if str(user.id) == str(admin.id):
            raise HTTPException(status_code=400, detail="不能禁用当前登录的自己")
        # 保住最后一个可用管理员，避免把自己锁在门外（与 delete_user 的保护同源）
        if (user.role or "") == "admin":
            active_admins = (
                await db.execute(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.role == "admin",
                        User.is_active.is_not(False),
                        User.id != user.id,
                    )
                )
            ).scalar_one()
            if int(active_admins or 0) == 0:
                raise HTTPException(
                    status_code=400, detail="不能禁用最后一个启用状态的管理员"
                )

    user.is_active = bool(data.is_active)
    if not data.is_active:
        await session_store.revoke_all_for_user(db, str(user.id))
    await db.flush()

    activity_logger.record_activity(
        "admin.set_active",
        user=admin,
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.name,
        status="success" if data.is_active else "rejected",
        detail={"is_active": bool(data.is_active)},
        client_ip=session_store.client_meta(request)[0],
    )
    return {"success": True, "user_id": str(user.id), "is_active": bool(data.is_active)}


# ─────────────────────────── CSV 导出 ───────────────────────────


def _csv_response(rows: list[list[Any]], header: list[str], filename: str) -> Response:
    buffer = io.StringIO()
    buffer.write(_CSV_BOM)  # Excel 认 BOM，不加中文全乱码
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers=content_disposition(filename),
    )


@router.get("/export")
async def export_csv(
    db: AsyncSession = Depends(get_db),
    what: str = Query("users", description="users / activities"),
    range_key: str = Query("7d", alias="range", description=_RANGE_DESC),
    user_id: str = Query(""),
    action: str = Query(""),
    status: str = Query(""),
):
    range_key = normalize_range(range_key)
    today = datetime.now(get_tz()).strftime("%Y%m%d")

    if what == "users":
        rows = await _build_user_usage_rows(db, range_key)
        await _fill_today_tokens(db, rows)
        header = [
            "姓名", "邮箱", "系统角色", "RBAC角色", "启用", "当前状态",
            "最后登录", "最后心跳", "最后动作", f"区间动作数({range_key})",
            "区间失败数", "活跃天数", "检查次数", "登录次数", "项目数",
            "今日动作数", "LLM调用次数", "输入Token", "输出Token", "总Token",
            "每日动作配额", "每日Token配额", "今日已用Token", "配额备注",
        ]
        body = [
            [
                r["name"], r["email"], r["role"], "/".join(r["roles"]),
                "是" if r["is_active"] else "否", r["status"],
                r["last_login_at"] or "", r["last_seen_at"] or "",
                r["last_action_at"] or "", r["actions"], r["failed"],
                r["active_days"], r["checks"], r["logins"], r["projects"],
                r["today_actions"], r["llm_calls"], r["prompt_tokens"],
                r["completion_tokens"], r["tokens"],
                r["quota"]["daily_action_limit"], r["quota"]["daily_token_limit"],
                r["quota"]["today_tokens"], r["quota"]["note"] or "",
            ]
            for r in rows
        ]
        return _csv_response(body, header, f"使用监控_用户用量_{range_key}_{today}.csv")

    if what == "activities":
        activities, total = await _query_activities(
            db,
            range_key=range_key,
            user_id=user_id or None,
            action=action or None,
            status=status or None,
            limit=50000,
            offset=0,
        )
        header = [
            "时间", "用户", "邮箱", "动作", "动作说明", "状态", "耗时(ms)",
            "项目名", "项目ID", "资源类型", "资源名", "错误信息", "IP", "UA", "详情",
        ]
        body = []
        for row in activities:
            item = _serialize_activity(row)
            body.append(
                [
                    item["created_at"] or "", item["user_name"] or "",
                    item["user_email"] or "", item["action"],
                    item["action_label"], item["status_label"],
                    item["duration_ms"] if item["duration_ms"] is not None else "",
                    item["project_name"] or "", item["project_id"] or "",
                    item["resource_type"] or "", item["resource_name"] or "",
                    item["error_message"] or "", item["client_ip"] or "",
                    item["user_agent"] or "",
                    str(item["detail"]) if item["detail"] else "",
                ]
            )
        return _csv_response(
            body, header, f"使用监控_行为流水_{range_key}_{today}.csv"
        )

    raise HTTPException(status_code=400, detail="what 只支持 users / activities")
