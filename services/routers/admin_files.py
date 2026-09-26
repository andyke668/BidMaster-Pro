"""管理后台 · 文件与报告。

让管理员查看 / 下载**每一个用户**提交的招标文件、投标书与生成的审查报告，
覆盖两条链路：

- **上传模式**（标书检查员走的 CheckPage）：记录在 review_records / review_files，
  文件在 uploads/reviews/{review_id}/。这条链路在 v0.5.0 之前**什么都不留**——
  原件解析完即删、报告写在 /tmp（非持久卷，重启即丢），所以升级前的老记录
  只能在「行为流水」里看到文件名，没有可下载的实体，这是数据本身不存在，
  不是权限问题。
- **项目模式**：文件在 documents 表 + ./projects/{project_id}/（projects 卷），
  报告在 check_reports 表。这两张表本来就有数据，只是此前没有管理端读接口。

权限：router 级同时挂 `settings.monitor` 与显式 admin 角色门禁。后者是刻意的
双保险——settings.monitor 目前确实只有 admin 角色持有（project_manager 在
routers/rbac.py 的 excluded 名单里显式排除），但「看用量统计」与「看别人的标书
原件」不是一个敏感度；万一以后有人把 monitor 授给了别的角色，标书文件也不能
跟着泄露。**不新增权限码**，避免多一套需要维护授予关系的配置。

审计：所有下载与清理都写 admin.* 行为流水，可追溯谁在什么时候看过谁的标书。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.http_headers import content_disposition
from core.skill_engine.base import SkillContext
from core.timeutil import normalize_range, range_start_utc, to_local_iso
from services import artifact_store
from services.database import get_db
from services.middleware import activity_logger
from services.middleware.rbac_middleware import get_current_user, require_permission
from services.models import (
    CheckReport,
    Document,
    Project,
    RBACRole,
    RBACUserRole,
    ReviewFile,
    ReviewRecord,
    User,
)

logger = logging.getLogger(__name__)


async def require_admin_user(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """显式 admin 角色门禁。

    认 RBAC 角色名（与 require_permission 同一套数据源），并兼容内置管理员
    的 users.role 遗留字段——routers/rbac.py 判断「系统内置管理员不可删除」
    用的就是这个字段，两边口径要一致，否则可能出现能删不能看的怪状态。
    """
    if (user.role or "") == "admin":
        return user
    rows = (
        await db.execute(
            select(RBACRole.name)
            .join(RBACUserRole, RBACUserRole.role_id == RBACRole.id)
            .where(RBACUserRole.user_id == user.id)
        )
    ).scalars().all()
    if "admin" in rows:
        return user
    raise HTTPException(status_code=403, detail="仅管理员可查看标书文件与审查报告")


router = APIRouter(
    dependencies=[
        Depends(get_current_user),
        Depends(require_permission("settings.monitor")),
        Depends(require_admin_user),
    ]
)

_PAGE_SIZE_MAX = 200
_RANGE_DESC = "统计区间：today / 24h / 7d / 30d / 90d / all"

_SOURCE_LABELS = {
    "upload_review": "招投标文件审查",
    "upload_check": "上传模式检查",
}
_KIND_LABELS = {"tender": "招标文件", "bid": "投标书", "report": "审查报告"}
_DOC_TYPE_LABELS = {
    "tender": "招标文件",
    "bid": "投标书",
    "template": "模板",
    "reference": "参考资料",
}
# 与 services/check/skills/tender_bid_review_skill.py 的 _SHEET_CONFIGS 对齐。
# 放在服务端下发而不是前端硬编码，避免以后加维度时两边漂移。
DIMENSION_LABELS = {
    "project_info": "项目信息",
    "disqualification": "废标项核对",
    "scoring": "评分项响应",
    "star_params": "▲参数核对",
    "materials": "证明材料清单",
    "timeline": "时间节点核对",
    "contract_terms": "合同条款要点",
    "pricing": "分项报价",
    "delivery": "交付时间对比",
}

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ─────────────────────────── 通用工具 ───────────────────────────


def _duration_ms(start, end) -> int | None:
    if not start or not end:
        return None
    delta = (end - start).total_seconds() * 1000
    return int(delta) if delta >= 0 else None


def _human_size(num: int | None) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _file_dict(row: ReviewFile) -> dict[str, Any]:
    return {
        "file_id": row.id,
        "review_id": row.review_id,
        "kind": row.kind,
        "kind_label": _KIND_LABELS.get(row.kind, row.kind),
        "original_name": row.original_name,
        "file_size": row.file_size,
        "size_label": _human_size(row.file_size),
        "sha256": row.sha256,
        "created_at": to_local_iso(row.created_at),
    }


def _review_dict(record: ReviewRecord, files: list[ReviewFile]) -> dict[str, Any]:
    return {
        "review_id": record.id,
        "user_id": record.user_id,
        "user_name": record.user_name,
        "user_email": record.user_email,
        "source": record.source,
        "source_label": _SOURCE_LABELS.get(record.source, record.source),
        "check_type": record.check_type,
        "company_name": record.company_name,
        "school_name": record.school_name,
        "status": record.status,
        "error_message": record.error_message,
        "task_id": record.task_id,
        "activity_id": record.activity_id,
        "summary": record.report_summary or {},
        "duration_ms": _duration_ms(record.created_at, record.finished_at),
        "created_at": to_local_iso(record.created_at),
        "finished_at": to_local_iso(record.finished_at),
        "has_report_data": bool(record.report_data),
        "files": [_file_dict(f) for f in files],
    }


async def _files_by_review(db: AsyncSession, review_ids: list[str]) -> dict[str, list[ReviewFile]]:
    """一次取回本页所有记录的文件，避免 N+1 查询。"""
    if not review_ids:
        return {}
    rows = (
        await db.execute(
            select(ReviewFile)
            .where(ReviewFile.review_id.in_(review_ids))
            .order_by(ReviewFile.created_at)
        )
    ).scalars().all()
    grouped: dict[str, list[ReviewFile]] = {rid: [] for rid in review_ids}
    for row in rows:
        grouped.setdefault(row.review_id, []).append(row)
    return grouped


def _resolve_download(stored_path: str) -> Path:
    """把库里的相对路径解析成真实文件，并强制校验没有逃出允许的根目录。

    依次尝试 uploads / projects 两个根：上传模式档案在 uploads/reviews 下，
    项目模式文件在 projects 下。解析失败或越界一律 404，不把真实路径回显给
    前端（避免泄露目录结构，也避免把越界尝试变成探测手段）。
    """
    for root in (artifact_store.UPLOADS_ROOT, artifact_store.PROJECTS_ROOT):
        try:
            path = artifact_store.resolve_within(stored_path, root)
        except ValueError:
            continue
        if path.exists() and path.is_file():
            return path
    raise HTTPException(status_code=404, detail="文件不存在或已被清理")


def _guess_media(name: str | None) -> str:
    suffix = Path(name or "").suffix.lower()
    table = {
        ".xlsx": _XLSX_MEDIA,
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }
    return table.get(suffix, "application/octet-stream")


def _audit(
    admin: User, action: str, *, resource_type: str = "admin", **kwargs: Any
) -> None:
    """管理员查看/下载/清理别人标书是敏感操作，一律留痕（fire-and-forget）。

    resource_type 提成显式关键字参数：record_activity 已有同名形参，若让调用方
    从 **kwargs 里再传一次就会「同一关键字给了两个值」直接 TypeError，而 _audit
    又把异常吞成 warning —— 结果是审计静默丢失，出事时查不到谁看过。
    """
    try:
        activity_logger.record_activity(
            action, user=admin, resource_type=resource_type, **kwargs
        )
    except Exception as exc:  # 审计写不进也不能挡住操作，但必须留日志
        logger.warning("管理端审计流水写入失败 action=%s: %s", action, exc)


# ─────────────────── 上传模式：审查档案 ───────────────────


@router.get("/reviews")
async def list_reviews(
    range: str = Query("30d", description=_RANGE_DESC),
    user_id: str = Query("", description="按用户过滤"),
    source: str = Query("", description="upload_review / upload_check"),
    status: str = Query("", description="running / success / failed"),
    q: str = Query("", description="按文件名 / 公司 / 招标单位 / 用户模糊匹配"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """分页列出所有用户的上传模式审查记录（含其招标件 / 投标件 / 报告）。"""
    range_key = normalize_range(range)
    conditions = []
    start = range_start_utc(range_key)
    if start:
        conditions.append(ReviewRecord.created_at >= start)
    if user_id:
        conditions.append(ReviewRecord.user_id == user_id)
    if source:
        conditions.append(ReviewRecord.source == source)
    if status:
        conditions.append(ReviewRecord.status == status)
    keyword = q.strip()
    if keyword:
        like = f"%{keyword}%"
        matched_ids = select(ReviewFile.review_id).where(ReviewFile.original_name.ilike(like))
        conditions.append(
            or_(
                ReviewRecord.company_name.ilike(like),
                ReviewRecord.school_name.ilike(like),
                ReviewRecord.user_name.ilike(like),
                ReviewRecord.user_email.ilike(like),
                ReviewRecord.id.in_(matched_ids),
            )
        )

    where = tuple(conditions)
    total = (
        await db.execute(select(func.count(ReviewRecord.id)).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            select(ReviewRecord)
            .where(*where)
            .order_by(ReviewRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    grouped = await _files_by_review(db, [r.id for r in rows])
    return {
        "range": range_key,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_review_dict(r, grouped.get(r.id, [])) for r in rows],
    }


@router.get("/reviews/{review_id}")
async def get_review(
    review_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """单条审查详情，含报告结构化明细（供后台原生渲染，不必下载 Excel）。"""
    record = (
        await db.execute(select(ReviewRecord).where(ReviewRecord.id == review_id))
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    files = (
        await db.execute(
            select(ReviewFile)
            .where(ReviewFile.review_id == review_id)
            .order_by(ReviewFile.created_at)
        )
    ).scalars().all()

    _audit(
        admin,
        "admin.view_files",
        resource_id=review_id,
        resource_name=record.company_name or record.school_name,
        detail={"source": record.source, "target_user": record.user_email},
    )

    payload = _review_dict(record, list(files))
    payload["report_data"] = record.report_data or {}
    payload["dimension_labels"] = DIMENSION_LABELS
    return payload


@router.get("/reviews/{review_id}/files/{file_id}/download")
async def download_review_file(
    review_id: str,
    file_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """下载某次审查的招标件 / 投标件 / 报告原件。"""
    row = (
        await db.execute(
            select(ReviewFile).where(
                ReviewFile.id == file_id, ReviewFile.review_id == review_id
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="文件记录不存在")
    record = (
        await db.execute(select(ReviewRecord).where(ReviewRecord.id == review_id))
    ).scalar_one_or_none()

    path = _resolve_download(row.stored_path)
    name = row.original_name or path.name
    _audit(
        admin,
        "admin.download_file",
        resource_id=row.id,
        resource_name=name,
        detail={
            "kind": row.kind,
            "review_id": review_id,
            "target_user": (record.user_email if record else None),
        },
    )
    return FileResponse(
        path=str(path), filename=name, media_type=_guess_media(name),
        headers=content_disposition(name),
    )


@router.delete("/reviews/{review_id}")
async def delete_review(
    review_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """手动清理一次审查的全部档案（数据库行 + 磁盘文件）。

    文件行显式 select 出来再删，不走 record.files：异步会话里访问未加载的关系
    属性会触发懒加载并抛 MissingGreenlet。也不单靠库级 ON DELETE CASCADE——
    SQLite 默认不开 foreign_keys pragma，只依赖级联会在本地开发时留下孤儿行
    （生产是 MySQL/InnoDB，级联本身生效，这里只是不把正确性押在方言差异上）。
    """
    record = (
        await db.execute(select(ReviewRecord).where(ReviewRecord.id == review_id))
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    files = (
        await db.execute(select(ReviewFile).where(ReviewFile.review_id == review_id))
    ).scalars().all()

    snapshot = {
        "target_user": record.user_email,
        "source": record.source,
        "files": [row.original_name for row in files],
    }
    freed = artifact_store.delete_review_dir(review_id)
    for row in files:
        await db.delete(row)
    await db.delete(record)
    await db.flush()

    _audit(
        admin, "admin.delete_review", resource_id=review_id,
        resource_name=record.company_name or record.school_name,
        detail={**snapshot, "freed_bytes": freed},
    )
    return {"deleted": 1, "freed_bytes": freed, "freed_label": _human_size(freed)}


class CleanupRequest(BaseModel):
    before_days: int | None = None
    review_ids: list[str] | None = None


@router.post("/reviews/cleanup")
async def cleanup_reviews(
    payload: CleanupRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """批量清理：按「N 天前」或按指定 id 列表。

    只给手动触发，不做自动过期——投标文件是敏感商业资料，什么时候该删应由
    管理员判断，而不是被一个写死的天数悄悄抹掉。
    """
    from datetime import timedelta

    from core.timeutil import utcnow_naive

    conditions = []
    if payload.review_ids:
        conditions.append(ReviewRecord.id.in_(payload.review_ids))
    elif payload.before_days and payload.before_days > 0:
        cutoff = utcnow_naive() - timedelta(days=payload.before_days)
        conditions.append(ReviewRecord.created_at < cutoff)
    else:
        raise HTTPException(
            status_code=400, detail="请提供 before_days（大于 0）或 review_ids"
        )

    rows = (
        await db.execute(select(ReviewRecord).where(*conditions))
    ).scalars().all()

    review_ids = [record.id for record in rows]
    if review_ids:
        await db.execute(sa_delete(ReviewFile).where(ReviewFile.review_id.in_(review_ids)))

    freed = 0
    for record in rows:
        freed += artifact_store.delete_review_dir(record.id)
        await db.delete(record)
    await db.flush()

    _audit(
        admin, "admin.delete_review", resource_id="bulk",
        resource_name=f"批量清理 {len(rows)} 条",
        detail={
            "before_days": payload.before_days,
            "count": len(rows),
            "freed_bytes": freed,
        },
    )
    return {
        "deleted": len(rows),
        "freed_bytes": freed,
        "freed_label": _human_size(freed),
    }


# ─────────────────── 项目模式：文件与报告 ───────────────────


@router.get("/projects")
async def list_all_projects(
    range: str = Query("all", description=_RANGE_DESC),
    user_id: str = Query(""),
    q: str = Query("", description="按项目名 / 用户模糊匹配"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """跨用户列出全部项目，附文件数与报告数。"""
    range_key = normalize_range(range)
    conditions = []
    start = range_start_utc(range_key)
    if start:
        conditions.append(Project.created_at >= start)
    if user_id:
        conditions.append(Project.user_id == user_id)
    keyword = q.strip()
    if keyword:
        like = f"%{keyword}%"
        conditions.append(
            or_(Project.name.ilike(like), User.name.ilike(like), User.email.ilike(like))
        )

    # selectinload 预取归属用户：下面循环里要读 project.user.name/email，
    # 异步会话中访问未加载的关系属性会触发隐式 IO 并抛 MissingGreenlet。
    base = (
        select(Project)
        .options(selectinload(Project.user))
        .join(User, User.id == Project.user_id, isouter=True)
    )
    where = tuple(conditions)
    total = (
        await db.execute(
            select(func.count(Project.id))
            .join(User, User.id == Project.user_id, isouter=True)
            .where(*where)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            base.where(*where)
            .order_by(Project.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    project_ids = [p.id for p in rows]
    doc_counts: dict[str, int] = {}
    report_counts: dict[str, int] = {}
    if project_ids:
        for rid, cnt in await db.execute(
            select(Document.project_id, func.count(Document.id))
            .where(Document.project_id.in_(project_ids))
            .group_by(Document.project_id)
        ):
            doc_counts[rid] = int(cnt)
        for rid, cnt in await db.execute(
            select(CheckReport.project_id, func.count(CheckReport.id))
            .where(CheckReport.project_id.in_(project_ids))
            .group_by(CheckReport.project_id)
        ):
            report_counts[rid] = int(cnt)

    items = []
    for project in rows:
        owner = project.user
        items.append({
            "project_id": project.id,
            "name": project.name,
            "status": project.status,
            "user_id": project.user_id,
            "user_name": owner.name if owner else None,
            "user_email": owner.email if owner else None,
            "document_count": doc_counts.get(project.id, 0),
            "report_count": report_counts.get(project.id, 0),
            "created_at": to_local_iso(project.created_at),
        })
    return {
        "range": range_key, "total": total, "page": page,
        "page_size": page_size, "items": items,
    }


@router.get("/projects/{project_id}/files")
async def get_project_files(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """某个项目的全部文件与审查报告（跨用户，管理员视角）。"""
    project = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.user))
            .join(User, User.id == Project.user_id, isouter=True)
            .where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    docs = (
        await db.execute(
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.created_at)
        )
    ).scalars().all()
    reports = (
        await db.execute(
            select(CheckReport)
            .where(CheckReport.project_id == project_id)
            .order_by(CheckReport.created_at.desc())
        )
    ).scalars().all()

    _audit(
        admin, "admin.view_files", resource_type="project", resource_id=project_id,
        resource_name=project.name,
        project_id=project_id, project_name=project.name,
        detail={"target_user": project.user.email if project.user else None},
    )

    documents = []
    for doc in docs:
        exists = False
        try:
            exists = _resolve_download(doc.file_path) is not None
        except HTTPException:
            exists = False
        documents.append({
            "document_id": doc.id,
            "type": doc.type,
            "type_label": _DOC_TYPE_LABELS.get(doc.type, doc.type),
            "original_name": doc.original_name or Path(doc.file_path or "").name,
            "file_size": doc.file_size,
            "size_label": _human_size(doc.file_size),
            "available": exists,
            "created_at": to_local_iso(doc.created_at),
        })

    return {
        "project": {
            "project_id": project.id,
            "name": project.name,
            "status": project.status,
            "user_name": project.user.name if project.user else None,
            "user_email": project.user.email if project.user else None,
            "created_at": to_local_iso(project.created_at),
        },
        "documents": documents,
        "reports": [
            {
                "report_id": r.id,
                "type": r.type,
                "risk_level": r.risk_level,
                "has_results": bool(r.results),
                "created_at": to_local_iso(r.created_at),
            }
            for r in reports
        ],
    }


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """下载项目模式下的招标 / 投标文件原件。"""
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文件记录不存在")

    path = _resolve_download(doc.file_path)
    name = doc.original_name or path.name
    owner = None
    if doc.project_id:
        owner = (
            await db.execute(
                select(User).join(Project, Project.user_id == User.id)
                .where(Project.id == doc.project_id)
            )
        ).scalar_one_or_none()
    _audit(
        admin, "admin.download_file", resource_id=doc.id, resource_name=name,
        detail={
            "kind": doc.type, "project_id": doc.project_id,
            "target_user": owner.email if owner else None,
        },
    )
    return FileResponse(
        path=str(path), filename=name, media_type=_guess_media(name),
        headers=content_disposition(name),
    )


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    format: str = Query("json", description="json / markdown / html"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """查看项目模式的审查报告。

    format=json 时直接回原始 results，前端自行渲染，零 token 成本；
    markdown / html 走 CheckReportExportSkill —— 该 skill 是纯格式化、不调 LLM，
    所以同样不烧 token（这点很重要：管理员翻看报告不该产生模型开销）。
    """
    report = (
        await db.execute(select(CheckReport).where(CheckReport.id == report_id))
    ).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    project = None
    if report.project_id:
        project = (
            await db.execute(select(Project).where(Project.id == report.project_id))
        ).scalar_one_or_none()
    project_name = project.name if project else "未命名项目"

    _audit(
        admin, "admin.view_files", resource_type="report", resource_id=report_id,
        resource_name=f"{project_name} · {report.type}",
        project_id=report.project_id, project_name=project_name,
        detail={"project_id": report.project_id, "format": format},
    )

    if format == "json":
        return {
            "report_id": report.id,
            "project_id": report.project_id,
            "project_name": project_name,
            "type": report.type,
            "risk_level": report.risk_level,
            "summary": report.summary or {},
            "results": report.results or {},
            "created_at": to_local_iso(report.created_at),
        }

    from services.check.skills.check_report_export_skill import CheckReportExportSkill
    from services.llm_factory import get_agent_gateway

    gateway = await get_agent_gateway(db, "check")
    skill = CheckReportExportSkill()
    ctx = SkillContext(
        project_id=report.project_id or "",
        db=db,
        llm=gateway,
        parameters={
            "report_data": report.results or {},
            "format": format,
            "project_name": project_name,
        },
    )
    result = await skill.safe_execute(ctx)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "报告渲染失败")

    content = result.data.get("content", "")
    if format == "html":
        return PlainTextResponse(content=content, media_type="text/html; charset=utf-8")
    return {
        "report_id": report.id,
        "project_name": project_name,
        "type": report.type,
        "format": format,
        "content": content,
        "size": result.data.get("size", 0),
    }


@router.get("/storage")
async def storage_stats(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin_user),
):
    """档案占用概览，给「手动清理」按钮提供决策依据。

    数据库侧用 sum(file_size) 汇总（快）；磁盘侧只统计 uploads/reviews 的实际
    占用，因为项目模式的文件散落在 projects/{pid}/ 下、逐目录 walk 在大库上
    会明显拖慢这个接口，而它的量本来就能从 documents.file_size 推出来。
    """
    review_files = (
        await db.execute(
            select(
                func.count(ReviewFile.id),
                func.coalesce(func.sum(ReviewFile.file_size), 0),
            )
        )
    ).one()
    review_count = (await db.execute(select(func.count(ReviewRecord.id)))).scalar_one()
    doc_row = (
        await db.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(Document.file_size), 0),
            )
        )
    ).one()

    disk_bytes = 0
    reviews_root = Path.cwd() / artifact_store.REVIEWS_ROOT
    if reviews_root.exists():
        disk_bytes = artifact_store.dir_bytes(reviews_root)

    return {
        "reviews": {
            "records": int(review_count),
            "files": int(review_files[0] or 0),
            "db_bytes": int(review_files[1] or 0),
            "db_label": _human_size(int(review_files[1] or 0)),
            "disk_bytes": disk_bytes,
            "disk_label": _human_size(disk_bytes),
        },
        "project_documents": {
            "files": int(doc_row[0] or 0),
            "db_bytes": int(doc_row[1] or 0),
            "db_label": _human_size(int(doc_row[1] or 0)),
        },
        "dimension_labels": DIMENSION_LABELS,
    }