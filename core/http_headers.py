from __future__ import annotations

from urllib.parse import quote


def content_disposition(filename: str) -> dict[str, str]:
    """生成附件下载的 Content-Disposition 响应头（RFC 5987/6266）。

    HTTP 头在 Starlette/uvicorn 侧按 latin-1 编码，直接放原始中文会抛
    UnicodeEncodeError 并把整个请求变成 500。filename* 的 UTF-8'' 只是声明
    字符集，值本身必须百分号编码，框架不会代劳。
    """
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
