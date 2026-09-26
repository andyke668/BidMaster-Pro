"""上传模式审查档案的落盘与取用。

为什么需要这个模块：标书检查员走的 CheckPage 调 POST /api/check/tender-bid-review，
它**不建项目**，改造前上传的招标/投标原件解析完就 os.unlink 删掉，生成的报告
Excel 写在 tempfile.gettempdir()/bidmaster_exports —— 那个目录不在任何持久卷里
（compose 的 x-api-volumes 只挂了 projects / uploads / chroma），容器一重启就没了，
用户过一阵再点下载只能拿到「文件不存在或已过期」。管理后台更是无东西可看。

这里统一负责三件事：
  ① 把原件与报告写进 uploads/reviews/{review_id}/（uploads 已是持久卷，不用改编排）；
  ② 生成安全文件名，避免同名覆盖与路径穿越；
  ③ 把库里存的相对路径解析回真实路径，并强制校验没有逃出允许的根目录。

stored_path 一律存**相对仓库根/工作目录**的路径（容器里即 /app 下），不存绝对路径：
同一份库在容器内是 /app/uploads/...、在本地开发是仓库根下，存相对路径两边都能解析。
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 审查档案根目录。挂在 uploads 卷下，容器重启不丢。
REVIEWS_ROOT = Path("uploads/reviews")

# 项目模式文件根：projects.py / interpret.py 都写 ./projects/{project_id}/
PROJECTS_ROOT = Path("projects")

# 上传目录根：format_doc.py 的 ./uploads/formatted、./uploads/exports 也在这下面
UPLOADS_ROOT = Path("uploads")

# 文件名里的危险字符：路径分隔符、Windows 保留字符、控制字符
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME_LEN = 180


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """把用户给的文件名洗成可安全落盘的形式，保留扩展名。

    只做净化不做拒绝：中文、空格、括号都要原样留着（管理员下载时要能认出是哪份），
    只去掉会改变路径语义或文件系统不接受的字符。
    """
    raw = (name or "").strip()
    if not raw:
        return fallback
    # 浏览器有时会带全路径（IE 老行为），只取最后一段
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_CHARS.sub("_", raw).strip().strip(".")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        return fallback
    if len(cleaned) > _MAX_NAME_LEN:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 10:
            cleaned = f"{stem[: _MAX_NAME_LEN - len(ext) - 1]}.{ext}"
        else:
            cleaned = cleaned[:_MAX_NAME_LEN]
    return cleaned


def review_dir(review_id: str) -> Path:
    """一次审查一个目录，目录名就是 review_id（uuid），天然不冲突也无法穿越。"""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", review_id or ""):
        raise ValueError(f"非法 review_id: {review_id!r}")
    return REVIEWS_ROOT / review_id


def save_bytes(review_id: str, kind: str, filename: str | None, content: bytes) -> dict:
    """把一份文件写进 uploads/reviews/{review_id}/{kind}__{安全名}。

    返回可直接塞进 ReviewFile 的字段字典。kind 作前缀是为了让同名的招标件与
    投标件（实践中经常都叫「招标文件.docx」）不会互相覆盖。
    """
    if kind not in ("tender", "bid", "report"):
        raise ValueError(f"非法 kind: {kind!r}")
    target_dir = review_dir(review_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    original = safe_filename(filename, fallback=f"{kind}.bin")
    stored_name = f"{kind}__{original}"
    # 极端情况下仍可能撞名（同 kind 同文件名传两次），加短哈希兜底而不是覆盖，
    # 覆盖会让管理员下载到的文件和记录里的 sha256 对不上。
    target = target_dir / stored_name
    if target.exists():
        digest = hashlib.sha256(content).hexdigest()[:8]
        stem, dot, ext = stored_name.rpartition(".")
        stored_name = f"{stem}.{digest}{dot}{ext}" if dot else f"{stored_name}.{digest}"
        target = target_dir / stored_name

    with open(target, "wb") as fh:
        fh.write(content)

    return {
        "kind": kind,
        "original_name": original,
        "stored_path": target.as_posix(),
        "file_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def resolve_within(stored_path: str, root: Path) -> Path:
    """把库里的相对路径解析成真实路径，并确认没有逃出 root。

    下载接口必须过这一关：stored_path 虽然由我们自己写，但一旦库里被塞进
    ../ 或绝对路径（历史数据、手工改库、将来的导入脚本），不校验就等于开了
    任意文件读取。resolve() 会展开符号链接，所以软链逃逸也拦得住。
    """
    if not stored_path or not str(stored_path).strip():
        raise ValueError("空路径")
    candidate = Path(str(stored_path).strip())
    resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    base = root.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"路径越界，拒绝访问: {stored_path}")
    return resolved


def delete_review_dir(review_id: str) -> int:
    """删掉一次审查的整个目录，返回释放的字节数。目录不存在时返回 0。"""
    try:
        target = review_dir(review_id)
    except ValueError:
        return 0
    resolved = resolve_within(target.as_posix(), UPLOADS_ROOT)
    if not resolved.exists():
        return 0
    freed = dir_bytes(resolved)
    shutil.rmtree(resolved, ignore_errors=True)
    return freed


def dir_bytes(path: Path) -> int:
    """目录占用字节数，用于清理前的容量提示。"""
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total