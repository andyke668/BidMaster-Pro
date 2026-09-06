from __future__ import annotations

from core.doc_engine.parsers.base import ParsedDocument

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


class DocxParser:
    def parse(self, file_path: str) -> ParsedDocument:
        import os

        if not os.path.exists(file_path):
            raise ValueError(f"文件不存在: {file_path}（请重新上传）")

        with open(file_path, "rb") as f:
            head = f.read(8)

        if head[:4] == _ZIP_MAGIC:
            return self._parse_docx(file_path)

        if head == _OLE2_MAGIC:
            # 老式 Word 97-2003 二进制 .doc（或老 WPS）：python-docx 打不开，走 antiword
            return self._parse_legacy_doc(file_path)

        if head[:4] == b"%PDF":
            # 扩展名与内容不符：实际是 PDF，交给 PdfParser
            from core.doc_engine.parsers.pdf_parser import PdfParser
            return PdfParser().parse(file_path)

        raise ValueError(
            "无法识别的文件内容（不是 docx/doc/pdf）。请在 Word/WPS 中另存为 .docx 后重新上传"
        )

    def _parse_docx(self, file_path: str) -> ParsedDocument:
        from docx import Document

        doc = Document(file_path)
        text_parts = []
        tables = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                style_name = para.style.name if para.style else ""
                text_parts.append(text)

        for i, table in enumerate(doc.tables):
            rows = []
            for row in table.rows:
                rows.append([cell.text.strip() for cell in row.cells])
            tables.append({
                "index": i,
                "headers": rows[0] if rows else [],
                "rows": rows[1:] if len(rows) > 1 else [],
            })

        return ParsedDocument(
            text="\n".join(text_parts),
            tables=tables,
            images=[],
            metadata={
                "paragraph_count": len(doc.paragraphs),
                "table_count": len(doc.tables),
                "parser": "python-docx",
            },
        )

    def _parse_legacy_doc(self, file_path: str) -> ParsedDocument:
        """老式 Word 97-2003 二进制 .doc：用 antiword 提取 UTF-8 纯文本。

        antiword 输出为纯文本（表格转为制表文本），无法还原结构化表格；
        失败时给出可操作的提示（转存 .docx/PDF）。
        """
        import shutil
        import subprocess

        antiword = shutil.which("antiword")
        if not antiword:
            raise ValueError(
                "旧版 .doc（Word 97-2003 二进制格式）解析需要 antiword（当前镜像未安装）。"
                "请在 Word/WPS 中另存为 .docx 后重新上传"
            )
        try:
            proc = subprocess.run(
                [antiword, "-m", "UTF-8.txt", file_path],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("旧版 .doc 解析超时，请在 Word/WPS 中另存为 .docx 后重新上传")

        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise ValueError(
                f"旧版 .doc 解析失败（文件可能加密、损坏或为 WPS 私有格式）：{err or 'antiword 无法读取'}。"
                "请在 Word/WPS 中另存为 .docx 后重新上传"
            )

        text = proc.stdout.decode("utf-8", errors="replace")
        lines = [ln.rstrip() for ln in text.splitlines()]
        text = "\n".join(ln for ln in lines if ln.strip())
        if not text.strip():
            raise ValueError(
                "旧版 .doc 未提取到文本（可能是纯图片/扫描文档），请转存 .docx 或 PDF 后重新上传"
            )

        return ParsedDocument(
            text=text,
            tables=[],
            images=[],
            metadata={
                "parser": "antiword",
                "legacy_doc": True,
                "char_count": len(text),
            },
        )