"""请求级上下文变量。

LLM token 统计发生在 skill 深处，逐层把 user_id 传下去要改几十个函数签名；
这里用 ContextVar 在鉴权依赖里设一次，深层直接读。
asyncio.create_task 会拷贝当前上下文，所以 TaskManager 里的后台协程
（全面检查 / AI 解读 / 正文生成）同样能读到任务提交者的身份——这正是
「token 归人」不需要改动任何 skill 代码的原因。
"""
from __future__ import annotations

from contextvars import ContextVar

_current_user_id: ContextVar[str | None] = ContextVar("bmp_user_id", default=None)
_current_user_email: ContextVar[str | None] = ContextVar("bmp_user_email", default=None)
_current_user_name: ContextVar[str | None] = ContextVar("bmp_user_name", default=None)
_current_action: ContextVar[str | None] = ContextVar("bmp_action", default=None)
_current_client_ip: ContextVar[str | None] = ContextVar("bmp_client_ip", default=None)


def set_current_user(user, *, client_ip: str | None = None) -> None:
    """在 get_current_user 里调用一次。user 为 None 时清空（可选鉴权端点用）。"""
    if user is None:
        _current_user_id.set(None)
        _current_user_email.set(None)
        _current_user_name.set(None)
        return
    uid = getattr(user, "id", None)
    _current_user_id.set(str(uid) if uid else None)
    _current_user_email.set(getattr(user, "email", None))
    _current_user_name.set(getattr(user, "name", None))
    if client_ip:
        _current_client_ip.set(client_ip)


def set_current_action(action: str | None) -> None:
    _current_action.set(action)


def get_user_id() -> str | None:
    return _current_user_id.get()


def get_user_email() -> str | None:
    return _current_user_email.get()


def get_user_name() -> str | None:
    return _current_user_name.get()


def get_action() -> str | None:
    return _current_action.get()


def get_client_ip() -> str | None:
    return _current_client_ip.get()
