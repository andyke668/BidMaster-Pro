"""按路由白名单记录行为流水（纯 ASGI 中间件）。

为什么不用 BaseHTTPMiddleware：它会包一层响应流，而 generate.py 的 SSE 流式
生成、format_doc.py 的文件下载全靠 StreamingResponse，缓冲行为一变就是线上
事故。纯 ASGI 中间件只包 send() 读一个状态码，对流式响应零干扰；命中白名单
之外的路径时直接透传，连包装都不做。

为什么不用 yield 依赖：FastAPI 在路由处理器内部就把 HTTPException 转成了响应，
异常不会抛回依赖的 yield 点，依赖里看到的永远是「成功」，失败率会恒为 0。
中间件拿到的是真实响应状态码。

白名单原则：只记「会产生业务价值或 LLM 开销」的动作。前端轮询 GET /task/{id}
一次全面检查能打出几百个请求，一个都不能记。
异步任务端点（返回 task_id 那批）在这里一律返回 None，改由 TaskManager 的
activity hook 统一记录——那样才能拿到真实耗时与最终成败，而不是「提交成功」。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from services.middleware import activity_logger

logger = logging.getLogger(__name__)

Resolver = Callable[[str, str], tuple[str, dict] | None]

# 这些端点提交异步任务后立即返回，行为流水由 TaskManager 钩子负责
_ASYNC_CHECK_SUFFIXES = (
    "/upload-check",
    "/tender-bid-review",
    "/full-check",
    "/check-async",
)


def api_path(path: str) -> str:
    """剥掉反代可能加的子路径前缀（生产 nginx 把站点挂在 /zdx 下）。"""
    index = path.find("/api/")
    return path[index:] if index >= 0 else path


def _segment(path: str, index: int) -> str | None:
    parts = [p for p in path.split("/") if p]
    try:
        return parts[index]
    except IndexError:
        return None


def _resolve_check(method: str, path: str) -> tuple[str, dict] | None:
    stripped = path.rstrip("/")
    if method == "GET":
        # 只有导出报告是值得记录的 GET，其余（任务轮询、报告详情）一律放过
        if re.search(r"/reports/[^/]+/export$", stripped):
            return "check.export", {"project_id": _segment(stripped, 2)}
        return None
    if method != "POST":
        return None
    if stripped.endswith(_ASYNC_CHECK_SUFFIXES):
        return None
    match = re.match(r"^/api/check/([^/]+)/([^/]+)$", stripped)
    if not match:
        return None
    project_id, item = match.groups()
    if item in ("reports", "task"):
        return None
    return "check.project", {"project_id": project_id, "check_item": item}


def _resolve_interpret(method: str, path: str) -> tuple[str, dict] | None:
    if method != "POST":
        return None
    stripped = path.rstrip("/")
    # /interpret/{pid} 是异步 AI 解读，交给 TaskManager 钩子
    table = {
        "upload": "interpret.upload",
        "parse": "interpret.parse",
        "scoring-matrix": "interpret.scoring",
        "risk-alert": "interpret.risk",
        "export": "interpret.export",
    }
    match = re.match(r"^/api/interpret/([^/]+)/([^/]+)$", stripped)
    if not match:
        return None
    verb, project_id = match.groups()
    action = table.get(verb)
    if action is None:
        return None
    return action, {"project_id": project_id}


def _resolve_generate(method: str, path: str) -> tuple[str, dict] | None:
    stripped = path.rstrip("/")
    if method == "GET":
        # SSE 流式端点（/content/stream、/content/stream-all）绝不记录也不包装
        if stripped.endswith("/export-docx"):
            return "generate.export", {"project_id": _segment(stripped, -2)}
        return None
    if method == "POST":
        # 大纲 / 结构模板 / 评分覆盖 / 强制要求提取都是异步任务
        if re.search(r"/(outline|score-coverage|mandatory-extract)$", stripped):
            return None
        if re.search(r"/structure/[^/]+$", stripped):
            return None
        match = re.match(r"^/api/generate/([^/]+)/content/([^/]+)$", stripped)
        if match:
            return "generate.content", {
                "project_id": match.group(1),
                "chapter_id": match.group(2),
            }
        return None
    if method == "PUT":
        match = re.match(r"^/api/generate/([^/]+)/chapters/([^/]+)$", stripped)
        if match:
            return "generate.edit_chapter", {
                "project_id": match.group(1),
                "chapter_id": match.group(2),
            }
    return None


_FORMAT_ACTIONS = {
    "/api/format/format": "format.run",
    "/api/format/check-format": "format.check",
    "/api/format/diff-format": "format.diff",
    "/api/format/beautify": "format.beautify",
    "/api/format/export-doc": "format.export",
    "/api/format/export-pdf": "format.export",
    "/api/format/export-formatted-docx": "format.export",
}


def _resolve_format(method: str, path: str) -> tuple[str, dict] | None:
    stripped = path.rstrip("/")
    if method == "GET":
        if stripped == "/api/format/download":
            return "format.export", {}
        return None
    if method != "POST":
        # 模板的增删改属于平台配置，不算业务使用量
        return None
    if stripped in _FORMAT_ACTIONS:
        return _FORMAT_ACTIONS[stripped], {}
    match = re.match(r"^/api/format/from-project/([^/]+)$", stripped)
    if match:
        return "format.run", {"project_id": match.group(1)}
    return None


def _resolve_projects(method: str, path: str) -> tuple[str, dict] | None:
    stripped = path.rstrip("/")
    if method == "POST" and stripped in ("/api/projects", "/api/projects/"):
        return "project.create", {}
    return None


_RESOLVERS: tuple[tuple[str, Resolver], ...] = (
    ("/api/check", _resolve_check),
    ("/api/interpret", _resolve_interpret),
    ("/api/generate", _resolve_generate),
    ("/api/format", _resolve_format),
    ("/api/projects", _resolve_projects),
)


def resolve_action(method: str, path: str) -> tuple[str, dict] | None:
    """返回 (action, 附加 detail)；不在白名单内返回 None。"""
    for prefix, resolver in _RESOLVERS:
        if path == prefix or path.startswith(prefix + "/"):
            try:
                return resolver(method, path)
            except Exception as exc:  # 解析失败绝不能影响请求本身
                logger.debug(f"行为动作解析失败 {method} {path}: {exc}")
                return None
    return None


def _scope_client_ip(scope: dict) -> str | None:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name == b"x-forwarded-for":
            first = raw_value.decode("latin-1").split(",")[0].strip()
            if first:
                return first[:64]
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])[:64]
    return None


def _status_bucket(code: int) -> str:
    if code >= 500:
        return "failed"
    if code >= 400:
        return "rejected"
    return "success"


def _record(scope: dict, action: str, extra: dict, holder: dict, started: float) -> None:
    state = scope.get("state") or {}
    identity = state.get("bmp_user") or {}
    user_id = identity.get("id")
    # 解析不出身份（基本都是 401 未登录）就不记：既归属不到人，
    # 又会在流水里堆出一批无法定位的噪音。
    if not user_id:
        return
    code = int(holder.get("status") or 500)
    detail: dict[str, Any] = dict(extra or {})
    project_id = detail.pop("project_id", None)
    detail["http_status"] = code
    if holder.get("error"):
        detail["server_error"] = str(holder["error"])[:300]
    try:
        activity_logger.record_activity(
            action,
            user_id=str(user_id),
            user_email=identity.get("email"),
            user_name=identity.get("name"),
            status=_status_bucket(code),
            error=holder.get("error"),
            duration_ms=int((time.monotonic() - started) * 1000),
            project_id=str(project_id) if project_id else None,
            detail=detail,
            client_ip=_scope_client_ip(scope),
        )
    except Exception as exc:
        logger.warning(f"记录行为流水失败: {exc}")


class ActivityMonitorMiddleware:
    """只包 send() 取响应状态码，不改写响应体，对流式端点零干扰。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = (scope.get("method") or "GET").upper()
        resolved = resolve_action(method, api_path(scope.get("path") or ""))
        if resolved is None:
            await self.app(scope, receive, send)
            return

        action, extra = resolved
        started = time.monotonic()
        holder: dict[str, Any] = {"status": 500, "error": None}

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:  # noqa: BLE001 - 记完再原样抛出
            holder["status"] = 500
            holder["error"] = str(exc)[:500]
            raise
        finally:
            _record(scope, action, extra, holder, started)
