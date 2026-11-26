"""Markdown 到规范化 Word (.docx) 与归档 ZIP 导出器（WP-1：优先使用 python-docx，无依赖环境内置标准 OOXML 生成器）。"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape as xml_escape


def _parse_md_blocks(markdown_text: str) -> list[tuple[str, object]]:
    """将 Markdown 解析为结构化块序列：heading / bullet / table / code / para。"""
    blocks: list[tuple[str, object]] = []
    lines = markdown_text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", (lang, "\n".join(code_lines))))
            continue
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            level = min(4, max(1, hashes))
            title = stripped[hashes:].strip()
            blocks.append(("heading", (level, title)))
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            tbl_rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                row_txt = lines[i].strip().strip("|")
                cells = [c.strip() for c in row_txt.split("|")]
                if not all(set(c) <= {"-", ":", " "} for c in cells):
                    tbl_rows.append(cells)
                i += 1
            if tbl_rows:
                blocks.append(("table", tbl_rows))
            continue
        if stripped.startswith(("- ", "* ")):
            blocks.append(("bullet", stripped[2:].strip()))
            i += 1
            continue
        blocks.append(("para", stripped))
        i += 1
    return blocks


def _export_with_python_docx(
    blocks: list[tuple[str, object]],
    *,
    header_text: str,
    footer_text: str,
) -> bytes:
    import docx  # type: ignore[import-not-found]

    doc = docx.Document()
    section = doc.sections[0]
    if header_text:
        section.header.paragraphs[0].text = header_text
    if footer_text:
        section.footer.paragraphs[0].text = footer_text

    for kind, payload in blocks:
        if kind == "heading":
            level, text = payload  # type: ignore[misc]
            doc.add_heading(str(text), level=int(level))
        elif kind == "bullet":
            doc.add_paragraph(str(payload), style="List Bullet")
        elif kind == "code":
            lang, code_txt = payload  # type: ignore[misc]
            note = f"[{lang} 代码块 / 图表源码]\n" if lang == "mermaid" else ""
            p = doc.add_paragraph()
            run = p.add_run(note + str(code_txt))
            run.font.name = "Courier New"
        elif kind == "table":
            rows: list[list[str]] = payload  # type: ignore[assignment]
            cols_cnt = max(len(r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=cols_cnt)
            for r_idx, r_cells in enumerate(rows):
                for c_idx, val in enumerate(r_cells):
                    tbl.cell(r_idx, c_idx).text = val
        else:
            doc.add_paragraph(str(payload))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _export_stdlib_ooxml(
    blocks: list[tuple[str, object]],
    *,
    header_text: str,
    footer_text: str,
) -> bytes:
    """零外部依赖生成符合 ECMA-376 规范的 .docx 文档（兼容 Word / LibreOffice / python-docx）。"""
    body_xml: list[str] = []
    for kind, payload in blocks:
        if kind == "heading":
            level, text = payload  # type: ignore[misc]
            body_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{int(level)}"/></w:pPr>'
                f"<w:r><w:t>{xml_escape(str(text))}</w:t></w:r></w:p>"
            )
        elif kind == "bullet":
            body_xml.append(f'<w:p><w:r><w:t>• {xml_escape(str(payload))}</w:t></w:r></w:p>')
        elif kind == "code":
            lang, code_txt = payload  # type: ignore[misc]
            prefix = "[Mermaid 图表源码] " if lang == "mermaid" else ""
            lines = (prefix + str(code_txt)).splitlines() or [""]
            runs = "<w:br/>".join(f"<w:t xml:space=\"preserve\">{xml_escape(ln)}</w:t>" for ln in lines)
            body_xml.append(f'<w:p><w:pPr><w:pStyle w:val="CodeBlock"/></w:pPr><w:r>{runs}</w:r></w:p>')
        elif kind == "table":
            rows: list[list[str]] = payload  # type: ignore[assignment]
            tr_xml: list[str] = []
            for r_cells in rows:
                tcs = "".join(
                    f"<w:tc><w:p><w:r><w:t>{xml_escape(c)}</w:t></w:r></w:p></w:tc>" for c in r_cells
                )
                tr_xml.append(f"<w:tr>{tcs}</w:tr>")
            body_xml.append(f"<w:tbl>{''.join(tr_xml)}</w:tbl>")
        else:
            body_xml.append(f"<w:p><w:r><w:t>{xml_escape(str(payload))}</w:t></w:r></w:p>")

    sect_refs = ""
    if header_text:
        sect_refs += '<w:headerReference w:type="default" r:id="rIdHeader1"/>'
    if footer_text:
        sect_refs += '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
    body_xml.append(f"<w:sectPr>{sect_refs}</w:sectPr>")

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{''.join(body_xml)}</w:body></w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/></w:style>'
        "</w:styles>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
        '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rIdHeader1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
        '<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
        "</Relationships>"
    )
    hdr_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:p><w:r><w:t>{xml_escape(header_text)}</w:t></w:r></w:p></w:hdr>"
    )
    ftr_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:p><w:r><w:t>{xml_escape(footer_text)}</w:t></w:r></w:p></w:ftr>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/header1.xml", hdr_xml)
        zf.writestr("word/footer1.xml", ftr_xml)
    return buf.getvalue()


def markdown_to_docx_bytes(
    markdown_text: str,
    *,
    lab_name: str = "Vernal 软件工程实验室",
    classification: str = "内部公开",
    version_label: str = "v1.0",
) -> bytes:
    blocks = _parse_md_blocks(markdown_text)
    header_txt = f"{lab_name} · 密级：{classification}"
    footer_txt = f"版本：{version_label}"
    try:
        return _export_with_python_docx(blocks, header_text=header_txt, footer_text=footer_txt)
    except ImportError:
        return _export_stdlib_ooxml(blocks, header_text=header_txt, footer_text=footer_txt)


def inspect_docx_structure(docx_bytes: bytes) -> dict[str, object]:
    """解析验证 .docx 字节流中的标题层级、段落文本与表格数（供自检与单测使用）。"""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
        xml_data = zf.read("word/document.xml")
    root = ET.fromstring(xml_data)
    headings: list[tuple[str, str]] = []
    paragraphs: list[str] = []
    for p in root.findall(".//w:body/w:p", ns):
        style_el = p.find("./w:pPr/w:pStyle", ns)
        style_val = style_el.attrib.get(f"{{{ns['w']}}}val", "") if style_el is not None else ""
        text = "".join(t.text or "" for t in p.findall(".//w:t", ns))
        paragraphs.append(text)
        if style_val.lower().startswith("heading"):
            headings.append((style_val, text))
    tables = root.findall(".//w:body/w:tbl", ns)
    return {"headings": headings, "paragraphs": paragraphs, "table_count": len(tables)}
