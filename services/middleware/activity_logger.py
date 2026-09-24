"""用户行为流水 / LLM token 流水的写入器。

三条铁律：
1. 用独立 session 写，不搭请求事务的便车——端点抛异常时请求事务会回滚，
   而「失败」恰恰是最需要被记下来的那一条。
2. 任何异常都只降级成 warning。监控绝不能把业务主流程带崩
   （与 middleware/api_key.py 记录调用日志同一套写法）。
3. 白名单式埋点，只记关键业务动作。前端轮询 GET /check/task/{id} 一次全面检查
   能打出几百个请求，用全局中间件无脑记会把表瞬间灌满。

两种用法：
- 同步端点：`async with track("check.project") as ctx: ...`，退出时按真实耗时与
  结果落一条；ctx 是个可变字典，可在块内补 resource_name / project_id 等。
- 异步任务端点：提交时 `await log_activity(..., status="running")` 拿到 activity_id，
  后台协程结束时 `update_activity(activity_id, status=..., duration_ms=...)` 收尾。
  这样一条记录就能反映真实耗时与成败，也不会出现「提交成功但后台炸了」的假绿。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete as sa_delete

from core.timeutil import utcnow_naive
from services.middleware import request_context

logger = logging.getLogger(__name__)

_pending: set[asyncio.Task] = set()
# 积压保护：DB 卡住时宁可丢监控数据，也不能让后台任务无限堆积吃内存
_MAX_PENDING = 500

# action -> 中文名。管理后台直接展示，避免前端再维护一份映射
ACTION_LABELS: dict[str, str] = {
    "auth.login": "登录",
    "auth.login_failed": "登录失败",
    "auth.logout": "退出登录",
    "auth.change_password": "修改密码",
    "project.create": "创建项目",
    "project.delete": "删除项目",
    "interpret.upload": "上传招标文件",
    "interpret.parse": "解析招标文件",
    "interpret.run": "AI 智能解读",
    "interpret.scoring": "生成评分矩阵",
    "interpret.risk": "风险提示",
    "interpret.export": "导出解读结果",
    "generate.outline": "生成投标大纲",
    "generate.structure": "生成结构模板",
    "generate.score_coverage": "评分覆盖分析",
    "generate.mandatory_extract": "强制要求提取",
    "generate.content": "生成正文",
    "generate.edit_chapter": "编辑章节",
    "generate.export": "导出投标文档",
    "check.upload": "上传模式检查",
    "check.project": "项目模式单项检查",
    "check.full": "项目模式全面检查",
    "check.tender_bid_review": "招投标文件审查",
    "check.export": "导出检查报告",
    "format.run": "文档排版",
    "format.check": "排版检查",
    "format.diff": "排版差异对比",
    "format.beautify": "文档美化",
    "format.export": "导出排版文档",
    "admin.force_logout": "管理员强制下线",
    "admin.set_active": "管理员启用/禁用账号",
    "admin.set_quota": "管理员调整配额",
}

# 配额桶前缀。限流按桶计数，不按单个 action
QUOTA_BUCKETS: tuple[str, ...] = ("check", "generate", "interpret", "format")

STATUS_LABELS: dict[str, str] = {
    "running": "进行中",
    "success": "成功",
    "failed": "失败",
    "rejected": "被拒绝",
}


def action_label(action: str | None) -> str:
    if not action:
        return ""
    return ACTION_LABELS.get(action, action)


def new_activity_id() -> str:
    return str(uuid.uuid4())


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _spawn(coro) -> None:
    """fire-and-forget，但保住 task 引用，避免被 GC 提前回收。"""
    if len(_pending) >= _MAX_PENDING:
        coro.close()
        logger.warning("行为流水积压超过 %s 条，丢弃本次记录", _MAX_PENDING)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 同步上下文（脚本、测试）里直接丢弃
        coro.close()
        return
    task = loop.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _build_payload(
    action: str,
    *,
    activity_id: str | None = None,
    user: Any = None,
    user_id: str | None = None,
    user_email: str | None = None,
    user_name: str | None = None,
    status: str = "success",
    error: str | None = None,
    duration_ms: int | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_name: str | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
    detail: dict | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    uid = user_id
    email = user_email
    name = user_name
    if user is not None and getattr(user, "id", None):
        uid = str(user.id)
        email = email or getattr(user, "email", None)
        name = name or getattr(user, "name", None)
    if not uid:
        uid = request_context.get_user_id()
    email = email or request_context.get_user_email()
    name = name or request_context.get_user_name()
    ip = client_ip or request_context.get_client_ip()
    payload: dict[str, Any] = {
        "id": activity_id or new_activity_id(),
        "user_id": uid,
        "user_email": _clip(email, 255),
        "user_name": _clip(name, 100),
        "action": _clip(action, 64),
        "resource_type": _clip(resource_type, 32),
        "resource_id": _clip(resource_id, 64),
        "resource_name": _clip(resource_name, 500),
        "project_id": _clip(project_id, 36),
        "project_name": _clip(project_name, 200),
        "detail": detail if isinstance(detail, dict) else {},
        "status": status,
        "error_message": _clip(error, 2000) if error else None,
        "duration_ms": int(duration_ms) if duration_ms is not None else None,
        "client_ip": _clip(ip, 64),
        "user_agent": _clip(user_agent, 256),
        "created_at": utcnow_naive(),
    }
    if status in ("success", "failed", "rejected"):
        payload["finished_at"] = utcnow_naive()
    return payload


async def _insert_activity(payload: dict[str, Any]) -> str | None:
    from services.database import async_session, is_db_ready
    from services.models import UserActivityLog

    if not is_db_ready():
        return None
    try:
        factory = async_session()
        async with factory() as db:
            db.add(UserActivityLog(**payload))
            await db.commit()
        return str(payload["id"])
    except Exception as exc:
        logger.warning(f"写入行为流水失败: {exc}")
        return None


async def log_activity(action: str, **kwargs: Any) -> str | None:
    """await 版：写入一条行为流水并返回其 id。用于异步任务的「提交」时刻。"""
    return await _insert_activity(_build_payload(action, **kwargs))


def record_activity(action: str, **kwargs: Any) -> None:
    """fire-and-forget 版：不阻塞主流程，写失败只记 warning。"""
    _spawn(_insert_activity(_build_payload(action, **kwargs)))


async def _update_activity(activity_id: str, fields: dict[str, Any]) -> None:
    from services.database import async_session, is_db_ready
    from services.models import UserActivityLog

    if not is_db_ready() or not activity_id or not fields:
        return
    try:
        from sqlalchemy import update as sa_update

        factory = async_session()
        async with factory() as db:
            await db.execute(
                sa_update(UserActivityLog)
                .where(UserActivityLog.id == activity_id)
                .values(**fields)
            )
            await db.commit()
    except Exception as exc:
        logger.warning(f"更新行为流水失败: {exc}")


def update_activity(
    activity_id: str | None,
    *,
    status: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    detail: dict | None = None,
    resource_id: str | None = None,
    resource_name: str | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
) -> None:
    """异步任务收尾时调用，把提交时那条 running 记录改成真实结果。"""
    if not activity_id:
        return
    fields: dict[str, Any] = {"finished_at": utcnow_naive()}
    if status is not None:
        fields["status"] = status
    if error is not None:
        fields["error_message"] = _clip(error, 2000)
    if duration_ms is not None:
        fields["duration_ms"] = int(duration_ms)
    if detail is not None:
        fields["detail"] = detail
    if resource_id is not None:
        fields["resource_id"] = _clip(resource_id, 64)
    if resource_name is not None:
        fields["resource_name"] = _clip(resource_name, 500)
    if project_id is not None:
        fields["project_id"] = _clip(project_id, 36)
    if project_name is not None:
        fields["project_name"] = _clip(project_name, 200)
    _spawn(_update_activity(activity_id, fields))


@contextlib.asynccontextmanager
async def track(action: str, **base: Any):
    """把一段同步端点逻辑包成一条行为流水。

    用法::

        async with track("check.project", project_id=pid) as ctx:
            ctx["resource_name"] = project.name
            ...

    4xx（HTTPException）记为 rejected，其余异常记为 failed——把「用户填错了」
    和「系统挂了」分开，管理后台的失败率才有意义。
    """
    request_context.set_current_action(action)
    ctx: dict[str, Any] = dict(base)
    started = time.monotonic()
    status = "success"
    error: str | None = None
    try:
        yield ctx
    except HTTPException as exc:
        status = "rejected" if exc.status_code < 500 else "failed"
        error = str(exc.detail)[:500]
        ctx.setdefault("detail", {})
        if isinstance(ctx["detail"], dict):
            ctx["detail"]["http_status"] = exc.status_code
        raise
    except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
        status = "failed"
        error = str(exc)[:500]
        raise
    finally:
        request_context.set_current_action(None)
        duration_ms = int((time.monotonic() - started) * 1000)
        await log_activity(
            action, status=status, error=error, duration_ms=duration_ms, **ctx
        )


async def _insert_llm_usage(payload: dict[str, Any]) -> None:
    from services.database import async_session, is_db_ready
    from services.models import LLMUsageLog

    if not is_db_ready():
        return
    try:
        factory = async_session()
        async with factory() as db:
            db.add(LLMUsageLog(**payload))
            await db.commit()
    except Exception as exc:
        logger.warning(f"写入 token 流水失败: {exc}")


def record_llm_usage(
    *,
    model: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    user_id: str | None = None,
    action: str | None = None,
) -> None:
    """由 LLMGateway._record_usage 调用。身份与动作从 ContextVar 里取，
    所以 skill 层完全不需要感知「当前是谁在调用」。"""
    _spawn(
        _insert_llm_usage(
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id or request_context.get_user_id(),
                "action": _clip(action or request_context.get_action(), 64),
                "model": _clip(model, 100),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "created_at": utcnow_naive(),
            }
        )
    )


async def purge_old_logs(db, *, retention_days: int) -> dict[str, int]:
    """按保留期清理行为流水与 token 流水，由 main.py 的后台协程每天调一次。"""
    from services.models import LLMUsageLog, UserActivityLog

    if retention_days <= 0:
        return {"activity": 0, "llm_usage": 0}
    cutoff = utcnow_naive() - timedelta(days=retention_days)
    removed_activity = await db.execute(
        sa_delete(UserActivityLog).where(UserActivityLog.created_at < cutoff)
    )
    removed_usage = await db.execute(
        sa_delete(LLMUsageLog).where(LLMUsageLog.created_at < cutoff)
    )
    await db.commit()
    return {
        "activity": int(removed_activity.rowcount or 0),
        "llm_usage": int(removed_usage.rowcount or 0),
    }


# ─────────────────────── 异步任务的行为流水 ───────────────────────
#
# 解读 / 全面检查 / 大纲生成这些都是「POST 提交 -> 返回 task_id -> 后台跑几分钟」。
# 如果只在提交时记一条，拿到的是「提交成功 + 3 毫秒」，既看不出真实耗时，
# 也看不出后台到底成没成。所以统一由 TaskManager 的钩子处理：
# 开始时落一条 running，结束时改成真实成败与耗时。
#
# 声明式地写清楚「第几个位置参数是什么」，避免把 bid_text / tender_text 这类
# 几十万字的正文误存进 detail——所有取值都会被截断到 _DETAIL_VALUE_MAX。
_DETAIL_VALUE_MAX = 200

TASK_ACTIVITY_META: dict[str, dict] = {
    "full_check": {"action": "check.full", "project_arg": 0},
    "single_check": {
        "action": "check.project",
        "project_arg": 0,
        "detail_args": {1: "check_item"},
    },
    "upload_check": {
        "action": "check.upload",
        "name_arg": 3,
        "detail_args": {0: "check_type", 4: "tender_filename"},
    },
    "tender_bid_review": {
        "action": "check.tender_bid_review",
        "name_arg": 5,
        "detail_args": {3: "company_name", 4: "school_name", 6: "tender_filename"},
    },
    "tender_interpret": {"action": "interpret.run", "project_arg": 0},
    "outline_gen": {
        "action": "generate.outline",
        "project_arg": 0,
        "detail_args": {1: "mode"},
    },
    "structure_gen": {
        "action": "generate.structure",
        "project_arg": 0,
        "detail_args": {1: "structure_type"},
    },
    "score_coverage": {"action": "generate.score_coverage", "project_arg": 0},
    "mandatory_extract": {"action": "generate.mandatory_extract", "project_arg": 0},
    "content_gen": {"action": "generate.content", "project_arg": 0},
    "chapter_gen": {"action": "generate.content", "project_arg": 0},
}


def _arg(args: tuple, index: int | None) -> str | None:
    """按声明的下标取一个「可作为元数据保存」的参数，其余一律不碰。"""
    if index is None or not isinstance(index, int):
        return None
    try:
        value = args[index]
    except (IndexError, TypeError):
        return None
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value)
    return text[:_DETAIL_VALUE_MAX] if len(text) > _DETAIL_VALUE_MAX else text


async def _project_name(project_id: str | None) -> str | None:
    """补上项目名，管理员在流水里才看得懂这条记录属于哪个标。"""
    if not project_id or not isinstance(project_id, str):
        return None
    from sqlalchemy import select

    from services.database import async_session, is_db_ready
    from services.models import Project

    if not is_db_ready():
        return None
    try:
        factory = async_session()
        async with factory() as db:
            row = (
                await db.execute(select(Project.name).where(Project.id == project_id))
            ).first()
            return str(row[0])[:200] if row and row[0] else None
    except Exception as exc:
        logger.debug(f"查询项目名失败: {exc}")
        return None


async def task_activity_hook(event: str, task, args: tuple) -> None:
    """注册到 TaskManager.set_activity_hook 的回调（见 main.py 的 lifespan）。"""
    meta = TASK_ACTIVITY_META.get(task.task_type or "")
    if not meta:
        return
    action = meta["action"]

    if event == "start":
        detail: dict[str, Any] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
        }
        for index, key in (meta.get("detail_args") or {}).items():
            value = _arg(args, index)
            if value is not None:
                detail[key] = value
        project_id = _arg(args, meta.get("project_arg"))
        task.activity_id = await log_activity(
            action,
            user_id=task.owner_id,
            status="running",
            resource_type="task",
            resource_id=task.task_id,
            resource_name=_arg(args, meta.get("name_arg")),
            project_id=project_id,
            project_name=await _project_name(project_id),
            detail=detail,
        )
        return

    duration_ms = None
    if task.started_at and task.completed_at:
        duration_ms = int((task.completed_at - task.started_at) * 1000)
    status = "success" if str(getattr(task.status, "value", task.status)) == "completed" else "failed"
    update_activity(
        task.activity_id,
        status=status,
        error=(task.error or None) if status == "failed" else None,
        duration_ms=duration_ms,
    )


async def reap_stale_running(db, *, stale_minutes: int = 180) -> int:
    """把「running 但早已无人认领」的流水标记为失败。

    进程在任务跑到一半时被重启，那条 running 记录就永远停在「进行中」，
    管理后台看着像还在跑。这里按开始时间兜底清理。
    """
    from sqlalchemy import update as sa_update

    from services.models import UserActivityLog

    cutoff = utcnow_naive() - timedelta(minutes=stale_minutes)
    result = await db.execute(
        sa_update(UserActivityLog)
        .where(
            UserActivityLog.status == "running",
            UserActivityLog.created_at < cutoff,
        )
        .values(
            status="failed",
            error_message="服务重启或任务超时，未收到结束回执",
            finished_at=utcnow_naive(),
        )
    )
    await db.commit()
    return int(result.rowcount or 0)

