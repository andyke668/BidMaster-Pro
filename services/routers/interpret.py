from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from services.database import get_db
from services.models import Project, Document, Analysis, ProjectStatus
from services.llm_factory import get_agent_gateway
from core.task_manager import TaskManager

router = APIRouter()

MAX_FILE_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".wps", ".md"}

# 解读链路（TenderInterpretSkill）要跑 15 个维度，每维度一次大模型调用，
# 并发度默认 3；用推理型模型（如 qwen3.8-max，单次 75-141s）时整链实测约 11 分钟。
# 前端超时/用户误以为卡死而重复点击，会让多条流水线同时压向 LLM 网关，
# 反而把网关拖慢、所有请求一起超时。这里做在途登记，拒绝并发重复解读。
# 注意：uvicorn 多 worker 时每个 worker 各持一份，属尽力而为的护栏；
# 残留条目按 _INTERPRET_TTL 秒过期，不会永久锁死某个项目。
_INTERPRET_TTL = 1800
_interpret_inflight: dict[str, float] = {}


def _interpret_inflight_started(project_id: str) -> float | None:
    """返回该项目在途解读的开始时刻；无在途或登记已过期则返回 None。"""
    started = _interpret_inflight.get(project_id)
    if started is None or time.monotonic() - started >= _INTERPRET_TTL:
        return None
    return started


def _interpret_max_concurrent() -> int:
    """解读链并发度。默认 3（与历史行为一致）；网关扛得住时调高可近似线性提速。"""
    try:
        value = int(os.getenv("BMP_INTERPRET_MAX_CONCURRENT", "3"))
    except ValueError:
        return 3
    return max(1, min(value, 15))


@router.post("/upload/{project_id}")
async def upload_tender_file(
    project_id: str,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    upload_dir = Path(f"./projects/{project_id}")
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for file in files:
        file_ext = Path(file.filename).suffix.lower() if file.filename else ""
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {file_ext}")

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制({MAX_FILE_SIZE // 1024 // 1024}MB)")

        file_path = upload_dir / file.filename
        with open(file_path, "wb") as f:
            f.write(content)

        doc = Document(
            project_id=project.id,
            type="tender",
            file_path=str(file_path),
            original_name=file.filename,
            file_size=len(content),
        )
        db.add(doc)
        await db.flush()

        if not project.tender_doc_id:
            project.tender_doc_id = doc.id
            await db.flush()

        uploaded.append({
            "document_id": str(doc.id),
            "file_name": file.filename,
            "file_size": len(content),
        })

    return {"uploaded": uploaded, "total": len(uploaded)}


@router.get("/documents/{project_id}")
async def list_documents(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Document).where(Document.project_id == project_id).order_by(Document.created_at)
    )
    docs = result.scalars().all()
    return {"documents": [
        {
            "id": str(d.id),
            "file_name": d.original_name,
            "file_size": d.file_size,
            "type": d.type,
            "parsed": d.parsed_content is not None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in docs
    ]}


@router.get("/document/{document_id}")
async def get_document_content(document_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    content_preview = None
    if doc.parsed_content:
        content_preview = doc.parsed_content[:50000]

    return {
        "id": str(doc.id),
        "file_name": doc.original_name,
        "file_size": doc.file_size,
        "type": doc.type,
        "parsed_content": content_preview,
        "doc_metadata": doc.doc_metadata,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


@router.post("/parse/{project_id}")
async def parse_tender_file(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    doc_result = await db.execute(
        select(Document).where(Document.id == project.tender_doc_id)
    )
    doc = doc_result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="招标文件未上传")

    from core.doc_engine import get_parser, SectionDetector
    from pathlib import Path
    file_ext = Path(doc.file_path).suffix
    try:
        parser = get_parser(file_ext)
        parsed = parser.parse(doc.file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件解析失败: {e}")

    doc.parsed_content = parsed.text
    doc.doc_metadata = parsed.metadata
    project.status = ProjectStatus.INTERPRETING.value
    await db.flush()

    detector = SectionDetector(llm_gateway=await get_agent_gateway(db, "interpret"))
    sections = await detector.detect_async(parsed.text)

    return {
        "project_id": project_id,
        "text_length": len(parsed.text),
        "tables_count": len(parsed.tables),
        "sections_count": len(sections),
        "sections": sections,
        "doc_metadata": parsed.metadata,
    }


@router.get("/analysis/{project_id}")
async def get_analysis(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(
        select(Analysis).where(Analysis.project_id == project.id)
    )
    analysis = analysis_result.scalar_one_or_none()

    doc_result = await db.execute(
        select(Document).where(Document.id == project.tender_doc_id)
    )
    doc = doc_result.scalar_one_or_none()

    has_documents = False
    doc_list_result = await db.execute(
        select(Document).where(Document.project_id == project.id).order_by(Document.created_at)
    )
    doc_list = doc_list_result.scalars().all()
    has_documents = len(doc_list) > 0
    has_parsed = any(d.parsed_content is not None for d in doc_list)

    return {
        "has_documents": has_documents,
        "has_parsed": has_parsed,
        "has_analysis": analysis is not None and analysis.dimensions is not None,
        # 解读改为异步后，前端靠这两个字段判断进度：
        # updated_at 来自 DB，跨 worker 可靠；interpret_running 来自本进程登记，
        # 单 worker 部署（本项目 UVICORN_WORKERS=1）下即为准确值。
        "interpret_running": _interpret_inflight_started(project_id) is not None,
        "analysis": {
            "dimensions": analysis.dimensions if analysis else None,
            "scoring_matrix": analysis.scoring_matrix if analysis else None,
            "risk_flags": analysis.risk_flags if analysis else None,
            "sections": analysis.sections if analysis else None,
            "updated_at": (
                analysis.updated_at.isoformat()
                if analysis and analysis.updated_at
                else None
            ),
        } if analysis else None,
        "parse_info": {
            "text_length": len(doc.parsed_content) if doc and doc.parsed_content else 0,
            "doc_metadata": doc.doc_metadata if doc else None,
        } if doc and doc.parsed_content else None,
    }


@router.post("/interpret/{project_id}")
async def interpret_tender(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    doc_result = await db.execute(
        select(Document).where(Document.id == project.tender_doc_id)
    )
    doc = doc_result.scalar_one_or_none()
    if not doc or not doc.parsed_content:
        raise HTTPException(status_code=400, detail="请先解析招标文件")

    if _interpret_inflight_started(project_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="该项目正在解读中（通常需 5-15 分钟），请勿重复提交，稍后刷新页面查看结果",
        )

    # 解读整链实测约 11 分钟（15 维度 ÷ 并发 3，推理型模型单次 75-141s），
    # 远超任何合理的 HTTP / 反代超时：实测 nginx 在 600s 处返回 504，
    # 而后端 664s 才把结果落库——前端只能看到「解读失败」，其实后台已成功。
    # 因此改为「提交即返回 task_id + 前端轮询」，沿用 /generate 的既有异步范式。
    # 在途登记在提交前同步完成，闭合两次快速点击之间的竞态。
    _interpret_inflight[project_id] = time.monotonic()

    tm = TaskManager.instance()
    task = await tm.submit("tender_interpret", _do_interpret_tender, project_id)

    return {
        "task_id": task.task_id,
        "status": "pending",
        "project_id": project_id,
        "message": "AI 解读任务已提交，通常需 5-15 分钟；页面会自动轮询进度，刷新也能看到已完成的结果",
    }


@router.get("/task/{task_id}")
async def get_interpret_task_status(task_id: str):
    task = TaskManager.instance().get_task(task_id)
    if not task:
        # TaskManager 是进程内的，uvicorn 多 worker 时轮询可能落到另一个 worker。
        # 这里返回 unknown 而非 404，让前端回退到 DB 支撑的 /interpret/analysis 轮询。
        return {"task_id": task_id, "status": "unknown"}
    return task.to_dict()


async def _do_interpret_tender(project_id: str):
    """后台执行解读链。必须自建 DB 会话：请求级会话在响应返回后就关闭了。"""
    from services.database import async_session
    from services.interpret.skills.tender_interpret_skill import TenderInterpretSkill
    from core.skill_engine.base import SkillContext

    session_factory = async_session()
    try:
        async with session_factory() as db:
            result = await db.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if not project:
                return {"success": False, "error": "项目不存在"}

            doc_result = await db.execute(
                select(Document).where(Document.id == project.tender_doc_id)
            )
            doc = doc_result.scalar_one_or_none()
            if not doc or not doc.parsed_content:
                return {"success": False, "error": "请先解析招标文件"}

            gateway = await get_agent_gateway(db, "interpret")
            skill = TenderInterpretSkill()
            ctx = SkillContext(
                project_id=project_id,
                db=db,
                llm=gateway,
                parameters={
                    "document_text": doc.parsed_content,
                    "max_concurrent": _interpret_max_concurrent(),
                },
            )
            skill_result = await skill.safe_execute(ctx)

            if skill_result.success:
                existing = await db.execute(
                    select(Analysis).where(Analysis.project_id == project.id)
                )
                analysis = existing.scalar_one_or_none()
                if analysis:
                    analysis.dimensions = skill_result.data.get("dimensions", {})
                    analysis.scoring_matrix = skill_result.data.get("scoring_matrix", {})
                    analysis.risk_flags = skill_result.data.get("risk_flags", {})
                    analysis.sections = skill_result.data.get("sections", [])
                else:
                    analysis = Analysis(
                        project_id=project.id,
                        dimensions=skill_result.data.get("dimensions", {}),
                        scoring_matrix=skill_result.data.get("scoring_matrix", {}),
                        risk_flags=skill_result.data.get("risk_flags", {}),
                        sections=skill_result.data.get("sections", []),
                    )
                    db.add(analysis)
                # 后台任务不再经过 get_db 的自动提交，必须显式 commit。
                await db.commit()

            return {
                "success": skill_result.success,
                "data": skill_result.data,
                "error": skill_result.error,
                "warnings": skill_result.warnings,
            }
    finally:
        # safe_execute 只吞 Exception，CancelledError 等仍会外抛，
        # 用 finally 保证在途登记一定被摘除，不会把项目锁满 TTL。
        _interpret_inflight.pop(project_id, None)


@router.post("/scoring-matrix/{project_id}")
async def build_scoring_matrix(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(
        select(Analysis).where(Analysis.project_id == project.id)
    )
    analysis = analysis_result.scalar_one_or_none()
    if not analysis or not analysis.dimensions:
        raise HTTPException(status_code=400, detail="请先完成招标解读")

    from services.interpret.skills.scoring_matrix_skill import ScoringMatrixSkill
    from core.skill_engine.base import SkillContext

    gateway = await get_agent_gateway(db, "interpret")
    skill = ScoringMatrixSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={"scoring_data": analysis.dimensions.get("scoring", {})},
    )
    skill_result = await skill.safe_execute(ctx)

    if skill_result.success and analysis:
        analysis.scoring_matrix = skill_result.data
        await db.flush()

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
    }


@router.post("/risk-alert/{project_id}")
async def risk_alert(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    doc_result = await db.execute(
        select(Document).where(Document.id == project.tender_doc_id)
    )
    doc = doc_result.scalar_one_or_none()
    if not doc or not doc.parsed_content:
        raise HTTPException(status_code=400, detail="请先上传招标文件")

    from services.interpret.skills.risk_alert_skill import RiskAlertSkill

    gateway = await get_agent_gateway(db, "interpret")
    skill = RiskAlertSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={"tender_text": doc.parsed_content[:6000]},
    )
    skill_result = await skill.safe_execute(ctx)

    return {
        "success": skill_result.success,
        "data": skill_result.data,
        "error": skill_result.error,
    }


@router.post("/export/{project_id}")
async def export_interpret(
    project_id: str,
    format: str = "markdown",
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import PlainTextResponse

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(
        select(Analysis).where(Analysis.project_id == project.id)
    )
    analysis = analysis_result.scalar_one_or_none()
    if not analysis or not analysis.dimensions:
        raise HTTPException(status_code=400, detail="请先完成招标解读")

    from services.interpret.skills.interpret_export_skill import InterpretExportSkill

    gateway = await get_agent_gateway(db, "interpret")
    skill = InterpretExportSkill()
    ctx = SkillContext(
        project_id=project_id,
        db=db,
        llm=gateway,
        parameters={
            "interpret_data": analysis.dimensions,
            "format": format,
            "project_name": project.name,
        },
    )
    skill_result = await skill.safe_execute(ctx)

    if not skill_result.success:
        raise HTTPException(status_code=500, detail=skill_result.error)

    content = skill_result.data.get("content", "")
    content_type = "text/markdown" if format == "markdown" else "text/html" if format == "html" else "application/json"
    return PlainTextResponse(content=content, media_type=content_type)
