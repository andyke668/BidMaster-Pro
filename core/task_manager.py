"""异步任务管理器。

解决 LLM 长耗时任务（大纲生成、内容生成等）前端超时问题。
模式：POST 立即返回 task_id → 前端轮询 GET /task/{task_id} 获取结果。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# 任务类型 -> 业务桶。配额限流要数「在途任务」，管理后台要显示中文名，
# 两边共用这一份映射，避免各写一套对不上。
TASK_TYPE_BUCKETS: dict[str, str] = {
    "full_check": "check",
    "single_check": "check",
    "upload_check": "check",
    "tender_bid_review": "check",
    "tender_interpret": "interpret",
    "outline_gen": "generate",
    "structure_gen": "generate",
    "score_coverage": "generate",
    "mandatory_extract": "generate",
    "content_gen": "generate",
    "chapter_gen": "generate",
}

TASK_TYPE_LABELS: dict[str, str] = {
    "full_check": "全面检查（项目模式）",
    "single_check": "单项检查（项目模式）",
    "upload_check": "上传模式检查",
    "tender_bid_review": "招投标文件审查",
    "tender_interpret": "AI 招标解读",
    "outline_gen": "生成投标大纲",
    "structure_gen": "生成结构模板",
    "score_coverage": "评分覆盖分析",
    "mandatory_extract": "强制要求提取",
    "content_gen": "生成正文",
    "chapter_gen": "生成章节",
}


def task_bucket(task_type: str | None) -> str | None:
    return TASK_TYPE_BUCKETS.get(task_type or "")


def task_label(task_type: str | None) -> str:
    return TASK_TYPE_LABELS.get(task_type or "", task_type or "未知任务")


_activity_hook = None


def set_activity_hook(hook) -> None:
    """由 services 层在启动时注入行为流水钩子（见 main.py 的 lifespan）。

    core 不反向依赖 services：钩子为 None 时（脚本、单测里直接用 TaskManager）
    整段逻辑跳过，行为与改动前完全一致。
    """
    global _activity_hook
    _activity_hook = hook


async def _notify_activity(event: str, task: "AsyncTask", args: tuple) -> None:
    if _activity_hook is None:
        return
    try:
        await _activity_hook(event, task, args)
    except Exception as exc:  # 钩子失败绝不能影响任务本身
        logger.warning(f"[TaskManager] 行为流水钩子失败 (可忽略): {exc}")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AsyncTask:
    task_id: str
    task_type: str
    owner_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    progress_message: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    # created_at 用的是 monotonic（只为算耗时），换算不出墙钟时间。
    # 管理后台要展示「几点提交的」，所以另存一份 time.time()。
    created_ts: float = field(default_factory=time.time)
    # 行为流水 ID：提交时落一条 running，任务结束时由钩子改成真实成败与耗时
    activity_id: str | None = None

    def to_dict(self) -> dict:
        elapsed = 0.0
        if self.started_at:
            end = self.completed_at or time.monotonic()
            elapsed = end - self.started_at
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "result": self.result,
            "error": self.error,
            "elapsed_seconds": round(elapsed, 1),
        }

    def to_admin_dict(self) -> dict:
        """管理后台视图：不带 result（可能是几百 KB 的检查结果或 base64 文件），
        但带上归属人和墙钟时间。"""
        elapsed = 0.0
        if self.started_at:
            end = self.completed_at or time.monotonic()
            elapsed = end - self.started_at
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "owner_id": self.owner_id,
            "status": self.status.value,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "error": self.error,
            "elapsed_seconds": round(elapsed, 1),
            "created_ts": self.created_ts,
        }


class TaskManager:
    """全局异步任务管理器（单例）"""

    _instance: TaskManager | None = None

    def __init__(self):
        self._tasks: dict[str, AsyncTask] = {}
        self._running_tasks: set[asyncio.Task] = set()
        self._cleanup_interval = 300  # 5分钟清理一次
        self._max_task_age = 3600     # 1小时后清理

    @classmethod
    def instance(cls) -> TaskManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def create_task(self, task_type: str, task_id: str | None = None) -> AsyncTask:
        task = AsyncTask(task_id=task_id or str(uuid.uuid4()), task_type=task_type)
        self._tasks[task.task_id] = task
        logger.info(f"[TaskManager] 创建任务 task_id={task.task_id}, type={task_type}")
        return task

    def get_task(self, task_id: str) -> AsyncTask | None:
        return self._tasks.get(task_id)

    def list_active_tasks(self, owner_id: str | None = None) -> list[AsyncTask]:
        """列出在途任务（pending / running），可按归属人过滤。

        管理后台的「谁此刻正在跑什么」就靠它；配额限流也要把在途任务算进当日
        用量，否则用户可以在行为流水落库之前连点几十次全面检查。
        """
        active = [
            task
            for task in self._tasks.values()
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]
        if owner_id is not None:
            active = [task for task in active if task.owner_id == owner_id]
        return sorted(active, key=lambda t: t.created_ts)

    def count_active(self, owner_id: str | None = None) -> int:
        return len(self.list_active_tasks(owner_id))

    def set_progress(self, task_id: str, progress: float, message: str = "") -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task.progress = max(0.0, min(1.0, progress))
        task.progress_message = message

    async def submit(
        self,
        task_type: str,
        coro_fn: Callable[..., Coroutine],
        *args,
        task_id: str | None = None,
        owner_id: str | None = None,
        **kwargs,
    ) -> AsyncTask:
        """提交异步任务并立即返回 task_id。

        Args:
            task_type: 任务类型（如 outline_gen, content_gen）
            coro_fn: 异步函数
            *args, **kwargs: 传给 coro_fn 的参数
        """
        task = self.create_task(task_type, task_id)
        # 兜底：没显式传 owner_id 的调用点（generate.py 的四个 submit）也能归到人。
        # 提交发生在请求上下文里，ContextVar 此时正是提交者的身份；
        # asyncio.create_task 会把上下文拷进后台协程，所以协程内也读得到。
        if not owner_id:
            try:
                from services.middleware import request_context

                owner_id = request_context.get_user_id()
            except Exception:
                owner_id = None
        task.owner_id = owner_id

        async def _run():
            task.status = TaskStatus.RUNNING
            task.started_at = time.monotonic()
            await _notify_activity("start", task, args)
            try:
                coro_kwargs = {key: value for key, value in kwargs.items() if key != "task_id"}
                result = await coro_fn(*args, **coro_kwargs)
                task.result = result
                task.status = TaskStatus.COMPLETED
                task.progress = 1.0
                task.completed_at = time.monotonic()
                elapsed = task.completed_at - task.started_at
                logger.info(f"[TaskManager] 任务完成 task_id={task.task_id}, "
                            f"耗时={elapsed:.1f}s")
            except Exception as e:
                task.error = str(e)
                task.status = TaskStatus.FAILED
                task.completed_at = time.monotonic()
                logger.error(f"[TaskManager] 任务失败 task_id={task.task_id}, "
                             f"error={e}")
            finally:
                await _notify_activity("end", task, args)

        asyncio_task = asyncio.create_task(_run())
        self._running_tasks.add(asyncio_task)
        asyncio_task.add_done_callback(self._running_tasks.discard)
        return task

    def cleanup_old_tasks(self):
        """清理过期任务"""
        now = time.monotonic()
        expired = [
            tid for tid, t in self._tasks.items()
            if now - t.created_at > self._max_task_age
        ]
        for tid in expired:
            del self._tasks[tid]
        if expired:
            logger.info(f"[TaskManager] 清理了 {len(expired)} 个过期任务")
