"""投标文件审查技能 - 基于 tender-review-kit 逻辑

分别对照招标文件和投标书，从六个维度做交叉审查：
1. 废标项核对（否决/无效条款逐条比对）
2. 评分项响应（评分标准逐项找响应）
3. ▲参数核对（星号/三角标强制性参数）
4. 证明材料清单（资质/业绩/证书类材料）
5. 时间节点核对（截止时间/有效期/开标时间）
6. 合同条款要点（中标后约束，与废标项分开）

全量不压缩原则：AI 提取到多少条就出多少条，不裁剪。
每条结果带原文出处行号，可溯源到招标文件原文。
产出 Excel，不下"投/不投"结论。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

_PRIMARY_WORDS = (
    "否决", "被否决", "予以否决", "拒收", "无效投标", "废标", "不予受理",
    "不予接受", "不予考虑", "取消资格", "取消中标资格", "取消投标资格",
    "视为撤回", "视为放弃", "视为无效", "视为未送达", "视为未响应",
    "实质性偏离", "实质性偏差", "作为无效投标处理", "按无效投标处理",
    "按未送达处理", "不满足招标文件", "不响应招标文件",
)
_CONTRACT_WORDS = ("违约金", "逾期违约", "履约保证金", "扣款", "解除合同", "终止合同", "连带责任", "质保金", "赔偿", "取消承包资格")
_CERTIFICATION_PATTERN = re.compile(r"(营业执照|资质证书|授权书|授权函|业绩证明|检验报告|检测报告|社保|纳税|信用中国|财务报告|审计报告|投标保证金)")
_TIME_PATTERN = re.compile(r"(投标截止|递交截止|开启时间|开标时间|保证金.*(?:时间|前)|有效期|答疑|澄清|递交.*止|提交.*止)")
_MARK_PATTERN = re.compile(r"[★▲◆●※■◇☆]|(?<![A-Za-z0-9])\*")

_SHEET_CONFIGS = [
    {
        "key": "disqualification",
        "title": "废标项核对",
        "color": "C00000",
        "columns": ["序号", "条款类型", "条款内容", "原文出处", "命中关键句", "投标书是否响应", "响应内容", "风险等级", "修改建议"],
        "prompt": """你是废标条款提取与对照专家。从招标文件中提取所有可能导致废标或无效投标的条款，然后在投标书中逐条查找是否已响应。

条款类型：
- invalidBid: 投标无效条件（资格性审查不通过）
- rejectionItem: 废标项（符合性审查不通过）

全量原则：撒网命中每条都要纳入，有多少条列多少条，不得压缩或省略。

返回JSON:
{"items": [
  {
    "seq": 1,
    "clause_type": "invalidBid/rejectionItem",
    "clause_content": "废标条款内容摘要",
    "source_location": "招标文件 行X或章节名",
    "response_found": true/false,
    "response_content": "投标书中找到的响应内容或'未找到'",
    "risk_level": "high/medium/low",
    "suggestion": "修改建议"
  }
]}""",
    },
    {
        "key": "scoring",
        "title": "评分项响应",
        "color": "00B050",
        "columns": ["序号", "评分类别", "评分项", "分值", "评分标准", "投标书响应状态", "响应内容/差距分析", "补充建议"],
        "prompt": """你是投标文件交叉比对专家。从招标文件中提取所有评分项（商务分、技术分、价格分等），然后在投标书中逐项查找响应内容。

响应状态：
- answered: 投标书中有完整、明确的响应
- partial: 有部分响应但不完整
- missing: 投标书中未找到对应响应

全量原则：评分表每行都是独立条款，有多少行列多少行，不得合并或压缩。

返回JSON:
{"items": [
  {
    "seq": 1,
    "category": "商务/技术/价格/其他",
    "scoring_item": "评分项名称",
    "score": 分值,
    "scoring_criteria": "评分标准描述",
    "response_status": "answered/partial/missing",
    "response_content": "响应内容摘要或差距分析",
    "suggestion": "补充建议"
  }
]}""",
    },
    {
        "key": "star_params",
        "title": "▲参数核对",
        "color": "ED7D31",
        "columns": ["序号", "标识", "参数描述", "类别", "投标书响应状态", "响应内容", "偏离情况", "建议"],
        "prompt": """你是实质性要求对照专家。从招标文件中提取所有★▲标记的强制性技术参数和商务要求，然后在投标书中逐条查找是否已响应。

偏离情况：
- compliant: 明确响应且满足要求
- negative_deviation: 负偏离（不满足）
- vague: 模糊表述，需人工确认
- missing: 未响应

全量原则：▲有多少列多少，不得压缩。保留"加盖原厂公章"等限制性原话。

返回JSON:
{"items": [
  {
    "seq": 1,
    "marker": "★/▲",
    "requirement": "参数描述",
    "category": "技术/商务/资质",
    "response_status": "compliant/negative_deviation/vague/missing",
    "response_content": "投标书中的响应内容",
    "deviation": "偏离说明",
    "suggestion": "建议"
  }
]}""",
    },
    {
        "key": "materials",
        "title": "证明材料清单",
        "color": "7030A0",
        "columns": ["序号", "材料名称", "要求描述", "原文出处", "命中关键句", "投标书是否包含", "页码/章节", "备注"],
        "prompt": """你是投标材料核对专家。从招标文件中提取所有需要提供的证明材料、资质证书、业绩证明、授权函等，然后在投标书中逐项查找是否已包含。

全量原则：所有"须提供""须附""须加盖"的材料都要提取，不得遗漏。

返回JSON:
{"items": [
  {
    "seq": 1,
    "material_name": "材料名称",
    "requirement": "具体要求描述",
    "source_location": "招标文件 行X或章节名",
    "included": true/false,
    "location": "投标书中的页码/章节",
    "note": "备注"
  }
]}""",
    },
    {
        "key": "timeline",
        "title": "时间节点核对",
        "color": "4472C4",
        "columns": ["序号", "节点类型", "时间/期限", "要求描述", "投标书是否响应", "响应内容", "风险提示"],
        "prompt": """你是时间节点核对专家。从招标文件中提取所有关键时间节点（投标截止时间、保证金缴纳时间、有效期、开标时间、答疑时间等），然后在投标书中核对是否已响应或是否存在冲突。

全量原则：所有日期和时间要求都要提取。

返回JSON:
{"items": [
  {
    "seq": 1,
    "node_type": "投标截止/保证金/有效期/开标/答疑/其他",
    "time_value": "具体时间或期限",
    "requirement": "要求描述",
    "responded": true/false,
    "response_content": "投标书中的响应内容",
    "risk_note": "风险提示"
  }
]}""",
    },
    {
        "key": "contract_terms",
        "title": "合同条款要点",
        "color": "BF8F00",
        "columns": ["序号", "条款主题", "约束要点", "限制性原话", "原文出处", "投标书响应情况", "中标后风险", "应对建议"],
        "prompt": """你是合同履约约束提取专家。从招标文件中提取中标后约束（违约金、履约保证金、质保金、解除或终止合同、扣款、连带责任等），不要把合同约束误判为当前废标项。

全量原则：所有合同履行期约束都要提取，并与当前废标清单严格分开。

返回JSON:
{"items": [
  {
    "seq": 1,
    "topic": "违约责任",
    "requirement": "约束要点",
    "original_quote": "限制性原话",
    "source_location": "招标文件 行12",
    "response_status": "无投标书响应要求",
    "contract_risk": "中标后风险",
    "suggestion": "应对建议"
  }
]}""",
    },
]

_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_PRIMARY_WORDS = (
    "否决", "被否决", "予以否决", "拒收", "无效投标", "废标", "不予受理",
    "不予接受", "不予考虑", "取消资格", "取消中标资格", "取消投标资格",
    "视为撤回", "视为放弃", "视为无效", "视为未送达", "视为未响应",
    "实质性偏离", "实质性偏差", "作为无效投标处理", "按无效投标处理",
    "按未送达处理", "不满足招标文件", "不响应招标文件",
)
_CONTRACT_WORDS = ("违约金", "逾期违约", "履约保证金", "扣款", "解除合同", "终止合同", "连带责任", "质保金", "赔偿", "取消承包资格")
_CERTIFICATION_PATTERN = re.compile(r"(营业执照|资质证书|授权书|授权函|业绩证明|检验报告|检测报告|社保|纳税|信用中国|财务报告|审计报告|投标保证金)")
_TIME_PATTERN = re.compile(r"(投标截止|递交截止|开启时间|开标时间|保证金.*(?:时间|前)|有效期|答疑|澄清|递交.*止|提交.*止)")
_MARK_PATTERN = re.compile(r"[★▲◆●※■◇☆]|(?<![A-Za-z0-9])\*")


class TenderBidReviewSkill(Skill):
    name = "tender_bid_review"
    description = "投标文件审查：招标文件+投标书六维度交叉审查并导出Excel"
    category = "check"
    version = "1.1.0"
    triggers = ["投标文件审查", "审查报告", "投标审查"]

    _MAX_CONCURRENT_REQUESTS = 4
    _MAX_RELATED_BID_CHUNKS = 3
    _MAX_BID_FALLBACK_CHUNKS = 2

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_lines", ctx.parameters.get("tender_text", ""))
        bid_text = ctx.parameters.get("bid_lines", ctx.parameters.get("bid_text", ""))
        company_name = ctx.parameters.get("company_name", "")
        school_name = ctx.parameters.get("school_name", "")

        if not tender_text:
            return SkillResult(success=False, error="招标文件内容为空")
        if not bid_text:
            return SkillResult(success=False, error="投标文件内容为空")

        tender_chunks = self._make_chunks(self._to_lines(tender_text))
        bid_chunks = self._make_chunks(self._to_lines(bid_text))
        total_chunks = max(1, len(tender_chunks) * len(_SHEET_CONFIGS))
        completed_chunks = 0
        completed_dimensions = 0
        progress_lock = asyncio.Lock()
        llm_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_REQUESTS)
        if ctx.progress_callback:
            await ctx.progress_callback(self.name, "dimension_started", {"progress": 0.02, "completed": 0, "total": total_chunks})

        async def complete_chunk() -> None:
            nonlocal completed_chunks
            async with progress_lock:
                completed_chunks += 1
                if ctx.progress_callback:
                    progress = 0.02 + 0.92 * completed_chunks / total_chunks
                    await ctx.progress_callback(
                        self.name,
                        "chunk_completed",
                        {"progress": progress, "completed": completed_chunks, "total": total_chunks},
                    )

        async def run_one_dimension(cfg: dict) -> tuple[dict, list[dict] | Exception]:
            nonlocal completed_dimensions
            try:
                result = await self._run_dimension(
                    ctx,
                    cfg,
                    tender_text,
                    bid_text,
                    llm_semaphore=llm_semaphore,
                    on_chunk_done=complete_chunk,
                )
            except Exception as exc:
                result = exc
            async with progress_lock:
                completed_dimensions += 1
            if ctx.progress_callback:
                await ctx.progress_callback(
                    self.name,
                    "dimension_completed",
                    {"progress": 0.94 + 0.01 * completed_dimensions, "completed": completed_chunks, "total": total_chunks},
                )
            return cfg, result

        dimension_results = await asyncio.gather(
            *[run_one_dimension(cfg) for cfg in _SHEET_CONFIGS],
            return_exceptions=True,
        )

        dimension_data: dict[str, list[dict]] = {}
        errors: list[str] = []
        for cfg in _SHEET_CONFIGS:
            dimension_data[cfg["key"]] = []
        for dimension_result in dimension_results:
            if isinstance(dimension_result, BaseException):
                errors.append(str(dimension_result))
                continue
            cfg, result = dimension_result
            if isinstance(result, Exception):
                logger.warning("[tender_bid_review] %s error: %s", cfg["key"], result)
                errors.append(f"{cfg['title']}: {result}")
            else:
                dimension_data[cfg["key"]] = result

        guard_rows = self._scan_guardrails(self._to_lines(tender_text))
        covered = {
            cfg["key"]: {
                line_no
                for item in dimension_data.get(cfg["key"], [])
                if isinstance(item, dict)
                for line_no in re.findall(r"\d+", str(item.get("source_location", "")))
            }
            for cfg in _SHEET_CONFIGS
        }
        kind_to_key = {"primary": "disqualification", "contract": "contract_terms", "materials": "materials", "timeline": "timeline", "star_params": "star_params"}
        guardrail_missing = sum(
            1
            for hit in guard_rows
            if hit["line"] not in covered[kind_to_key[hit["kind"]]]
        )

        excel_bytes = self._build_excel(dimension_data, company_name, school_name, guard_rows, covered, kind_to_key)
        total_items = sum(len(v) for v in dimension_data.values())
        high_count = sum(
            1 for items in dimension_data.values()
            for item in items if isinstance(item, dict) and item.get("risk_level") == "high"
        )

        warnings = [f"发现 {high_count} 项高风险"] if high_count else []
        if guardrail_missing:
            warnings.append(f"{guardrail_missing} 条护栏命中未覆盖，请复核")
        if errors:
            warnings.append(f"{len(errors)} 个维度执行异常，结果可能不完整")

        return SkillResult(
            success=True,
            data={
                "excel_base64": excel_bytes,
                "file_name": f"投标文件审查_{company_name}_{school_name}.xlsx",
                "total_items": total_items,
                "high_count": high_count,
                "guardrail_missing": guardrail_missing,
                "guardrail_total": len(guard_rows),
                "dimension_counts": {k: len(v) for k, v in dimension_data.items()},
                "errors": errors,
                "generated_at": datetime.now().isoformat(),
            },
            warnings=warnings,
        )

    async def _run_dimension(
        self,
        ctx: SkillContext,
        cfg: dict,
        tender_text: str,
        bid_text: str,
        *,
        llm_semaphore: asyncio.Semaphore,
        on_chunk_done,
    ) -> list[dict]:
        tender_chunks = self._make_chunks(self._to_lines(tender_text))
        bid_chunks = self._make_chunks(self._to_lines(bid_text))
        results: list[dict] = []

        for tender_chunk in tender_chunks:
            related_bid = self._related_bid_chunks(tender_chunk, bid_chunks)
            selected_bid = related_bid[:self._MAX_RELATED_BID_CHUNKS]
            if not selected_bid:
                selected_bid = bid_chunks[:self._MAX_BID_FALLBACK_CHUNKS]
            bid_context = "\n\n".join(chunk["text"] for chunk in selected_bid)
            messages = [
                {
                    "role": "system",
                    "content": cfg["prompt"] + "\n\n必须遵守：只输出本片段真实存在的条款；source_location 必须原样使用提供的“行X”或“行X–行Y”；不确定时保留候选并写“需人工复核”。",
                },
                {
                    "role": "user",
                    "content": f"招标文件片段（{tender_chunk['range']}）：\n{tender_chunk['text']}\n\n投标书相关片段：\n{bid_context}",
                },
            ]
            async with llm_semaphore:
                result = await ctx.llm.collect_json(messages=messages, temperature=0.1, max_tokens=16384)
            if not isinstance(result, dict):
                raise TypeError("模型返回的不是 JSON 对象")
            chunk_items = result.get("items", [])
            if isinstance(chunk_items, list):
                results.extend(item for item in chunk_items if isinstance(item, dict))
            await on_chunk_done()
        return results

    def _to_lines(self, text: str) -> list[tuple[int, str]]:
        lines: list[tuple[int, str]] = []
        fallback_no = 1
        for raw_line in text.splitlines():
            value = raw_line.strip()
            if value and "\t" in value:
                prefix, content = value.split("\t", 1)
                if prefix.isdigit():
                    lines.append((int(prefix), content.strip()))
                    continue
            if value:
                lines.append((fallback_no, value))
                fallback_no += 1
        return lines

    def _make_chunks(self, lines: list[tuple[int, str]]) -> list[dict]:
        chunks: list[dict] = []
        start = 0
        while start < len(lines):
            end = min(start + 90, len(lines))
            chunks.append({
                "range": f"行{lines[start][0]}–行{lines[end - 1][0]}",
                "text": "\n".join(f"行{line_no}: {content}" for line_no, content in lines[start:end]),
                "plain": "\n".join(content for _, content in lines[start:end]),
            })
            start += 60
        return chunks

    def _related_bid_chunks(self, tender_chunk: dict, bid_chunks: list[dict]) -> list[dict]:
        tender_terms = set(re.findall(r"[\u4e00-\u9fff]{2,}", tender_chunk["plain"]))
        scored: list[tuple[float, dict]] = []
        for chunk in bid_chunks:
            bid_terms = set(re.findall(r"[\u4e00-\u9fff]{2,}", chunk["plain"]))
            if not bid_terms:
                continue
            overlap = sum(1 for term in bid_terms if term in tender_terms)
            scored.append((overlap + len(chunk["plain"]) / 10000, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _, chunk in scored]

    def _build_excel(
        self,
        data: dict[str, list[dict]],
        company: str,
        school: str,
        guard_rows: list[dict[str, str]],
        covered: dict[str, set[int]],
        kind_to_key: dict[str, str],
    ) -> str:
        wb = Workbook()
        wb.remove(wb.active)

        # 概要 sheet
        ws = wb.create_sheet("审查概要")
        ws.cell(1, 1, "投标文件审查报告").font = Font(bold=True, size=16, color="1F4E79")
        ws.cell(2, 1, f"投标方：{company}").font = Font(size=12)
        ws.cell(3, 1, f"招标方/业主：{school}").font = Font(size=12)
        ws.cell(4, 1, f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}").font = Font(size=10, color="808080")
        ws.cell(5, 1, "基于 tender-review-kit 全量不压缩原则 · 每条带原文出处 · 产清单不下结论").font = Font(size=9, color="808080", italic=True)

        row = 7
        ws.cell(row, 1, "审查维度").font = Font(bold=True)
        ws.cell(row, 2, "条目数").font = Font(bold=True)
        ws.cell(row, 3, "高风险数").font = Font(bold=True)
        row += 1
        for cfg in _SHEET_CONFIGS:
            items = data.get(cfg["key"], [])
            high = sum(1 for i in items if isinstance(i, dict) and i.get("risk_level") == "high")
            ws.cell(row, 1, cfg["title"])
            ws.cell(row, 2, len(items))
            ws.cell(row, 3, high)
            ws.cell(row, 3).font = Font(color="DC2626" if high > 0 else "059669")
            row += 1

        ws.column_dimensions["A"].width = 20
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 10

        # 各维度 sheet
        for cfg in _SHEET_CONFIGS:
            items = data.get(cfg["key"], [])
            ws = wb.create_sheet(cfg["title"])
            fill = PatternFill("solid", fgColor=cfg["color"])
            for c, col_name in enumerate(cfg["columns"], 1):
                cell = ws.cell(1, c, col_name)
                cell.fill = fill
                cell.font = Font(bold=True, color="FFFFFF")
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                cell.border = _BORDER
            ws.row_dimensions[1].height = 24
            ws.freeze_panes = "A2"

            for r, item in enumerate(items, 2):
                if not isinstance(item, dict):
                    continue
                for c in range(1, len(cfg["columns"]) + 1):
                    key = self._item_key(cfg["key"], c)
                    val = item.get(key, "") if key else ""
                    if isinstance(val, bool):
                        val = "是" if val else "否"
                    elif val is None:
                        val = ""
                    elif not isinstance(val, str):
                        val = str(val)
                    cell = ws.cell(r, c, val)
                    cell.alignment = Alignment(vertical="center", wrap_text=True)
                    cell.border = _BORDER

            for c in range(1, len(cfg["columns"]) + 1):
                max_len = max(
                    (len(str(ws.cell(r, c).value or "")) for r in range(1, len(items) + 2)),
                    default=8,
                )
                ws.column_dimensions[get_column_letter(c)].width = min(max(max_len * 1.8, 10), 50)

            ws.auto_filter.ref = f"A1:{get_column_letter(len(cfg['columns']))}{len(items) + 1}"

        guard_sheet = wb.create_sheet("覆盖护栏")
        guard_headers = ["护栏类别", "命中词", "原文行号", "原文摘要", "AI清单覆盖"]
        for c, header in enumerate(guard_headers, 1):
            cell = guard_sheet.cell(1, c, header)
            cell.fill = PatternFill("solid", fgColor="64748B")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.border = _BORDER
        for r, hit in enumerate(guard_rows, 2):
            target_key = kind_to_key[hit["kind"]]
            is_covered = hit["line"] in covered[target_key]
            values = [self._dimension_title(target_key), hit["word"], f"行{hit['line']}", hit["text"], "已覆盖" if is_covered else "未覆盖"]
            for c, value in enumerate(values, 1):
                cell = guard_sheet.cell(r, c, value)
                cell.border = _BORDER
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if c == 5 and not is_covered:
                    cell.font = Font(color="DC2626", bold=True)
        for c, width in enumerate([18, 16, 12, 60, 14], 1):
            guard_sheet.column_dimensions[get_column_letter(c)].width = width
        guard_sheet.freeze_panes = "A2"
        guard_sheet.auto_filter.ref = f"A1:E{len(guard_rows) + 1}"

        buf = io.BytesIO()
        wb.save(buf)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _item_key(self, dim_key: str, col_index: int) -> str | None:
        key_maps = {
            "disqualification": {1: "seq", 2: "clause_type", 3: "clause_content", 4: "source_location", 5: "matched_quote", 6: "response_found", 7: "response_content", 8: "risk_level", 9: "suggestion"},
            "scoring": {1: "seq", 2: "category", 3: "scoring_item", 4: "score", 5: "scoring_criteria", 6: "response_status", 7: "response_content", 8: "suggestion"},
            "star_params": {1: "seq", 2: "marker", 3: "requirement", 4: "category", 5: "response_status", 6: "response_content", 7: "deviation", 8: "suggestion"},
            "materials": {1: "seq", 2: "material_name", 3: "requirement", 4: "source_location", 5: "matched_quote", 6: "included", 7: "location", 8: "note"},
            "timeline": {1: "seq", 2: "node_type", 3: "time_value", 4: "requirement", 5: "responded", 6: "response_content", 7: "risk_note"},
            "contract_terms": {1: "seq", 2: "topic", 3: "requirement", 4: "original_quote", 5: "source_location", 6: "response_status", 7: "contract_risk", 8: "suggestion"},
        }
        return key_maps.get(dim_key, {}).get(col_index)

    def _scan_guardrails(self, lines: list[tuple[int, str]]) -> list[dict[str, str]]:
        hits: list[dict[str, str]] = []
        for line_no, content in lines:
            if word := next((word for word in _PRIMARY_WORDS if word in content), None):
                hits.append({"line": str(line_no), "word": word, "text": content[:220], "kind": "primary"})
            if word := next((word for word in _CONTRACT_WORDS if word in content), None):
                hits.append({"line": str(line_no), "word": word, "text": content[:220], "kind": "contract"})
            if match := _CERTIFICATION_PATTERN.search(content):
                hits.append({"line": str(line_no), "word": match.group(1), "text": content[:220], "kind": "materials"})
            if match := _TIME_PATTERN.search(content):
                hits.append({"line": str(line_no), "word": match.group(1), "text": content[:220], "kind": "timeline"})
        for line_no, content in lines:
            if _MARK_PATTERN.search(content):
                hits.append({"line": str(line_no), "word": "★▲标识", "text": "标识参数候选行", "kind": "star_params"})
        return hits

    def _dimension_title(self, dimension_key: str) -> str:
        return next((cfg["title"] for cfg in _SHEET_CONFIGS if cfg["key"] == dimension_key), dimension_key)
