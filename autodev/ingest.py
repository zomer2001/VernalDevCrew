"""文档摄入：解析多格式资料，按标题层级切分为带 slice_id 的知识块，并生成 doc_catalog.json。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from autodev.db import DocSlice

MARKDOWN_EXT = {".md", ".markdown", ".mdx"}
PLAIN_EXT = {".txt", ".rst", ".adoc"}
CODE_EXT = {
    ".proto", ".yaml", ".yml", ".json", ".toml", ".sql", ".graphql", ".thrift", ".avsc",
    ".py", ".go", ".rs", ".ts", ".js", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".sh",
}
DOC_EXT = {".pdf", ".docx"}
SUPPORTED_EXT = MARKDOWN_EXT | PLAIN_EXT | CODE_EXT | DOC_EXT

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class IngestError(RuntimeError):
    pass


@dataclass
class Section:
    heading_path: str
    body: str


# ---------- 读取 ----------

def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖可选
        raise IngestError(f"解析 {path.name} 需要 pypdf：uv pip install 'autodev[docs]'") from exc
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(f"# 第 {i} 页\n\n{text}" for i, text in enumerate(pages, 1) if text)


def read_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - 依赖可选
        raise IngestError(f"解析 {path.name} 需要 python-docx：uv pip install 'autodev[docs]'") from exc
    document = docx.Document(str(path))
    lines: list[str] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        m = re.match(r"heading\s*(\d)", style) or re.match(r"标题\s*(\d)", style)
        lines.append(f"{'#' * min(int(m.group(1)), 6)} {text}" if m else text)
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n\n".join(lines)


# ---------- 切分 ----------

def split_markdown(text: str, root_title: str) -> list[Section]:
    """按标题树切分 Markdown，忽略代码块中的 '#'。"""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def path() -> str:
        return " / ".join([root_title, *[t for _, t in stack]])

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(path(), body))
        buf.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return sections


def split_plain(text: str, root_title: str) -> list[Section]:
    body = text.strip()
    return [Section(root_title, body)] if body else []


def _hard_split(body: str, max_chars: int) -> list[str]:
    """超长块按段落 → 行 → 字符逐级切分。"""
    if len(body) <= max_chars:
        return [body]
    parts: list[str] = []
    cur = ""
    units = re.split(r"(\n\s*\n)", body)
    for unit in units:
        if len(unit) > max_chars:
            for line in unit.splitlines(keepends=True):
                while len(line) > max_chars:
                    if cur:
                        parts.append(cur)
                        cur = ""
                    parts.append(line[:max_chars])
                    line = line[max_chars:]
                if len(cur) + len(line) > max_chars and cur:
                    parts.append(cur)
                    cur = ""
                cur += line
            continue
        if len(cur) + len(unit) > max_chars and cur:
            parts.append(cur)
            cur = ""
        cur += unit
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def pack_sections(sections: list[Section], max_chars: int, min_chars: int) -> list[Section]:
    """合并过短的相邻小节（同一顶层章节内），拆分过长的小节。"""
    packed: list[Section] = []
    for sec in sections:
        top = sec.heading_path.split(" / ")[:2]
        if (
            packed
            and len(packed[-1].body) < min_chars
            and len(packed[-1].body) + len(sec.body) + 2 <= max_chars
            and packed[-1].heading_path.split(" / ")[:2] == top
        ):
            packed[-1] = Section(packed[-1].heading_path, packed[-1].body + "\n\n" + sec.body)
            continue
        chunks = _hard_split(sec.body, max_chars)
        if len(chunks) == 1:
            packed.append(Section(sec.heading_path, chunks[0]))
        else:
            for i, chunk in enumerate(chunks, 1):
                packed.append(Section(f"{sec.heading_path} (part {i}/{len(chunks)})", chunk))
    return packed


def summarize(body: str, limit: int = 220) -> str:
    """启发式摘要：去掉标题与代码围栏后取前若干字符。"""
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s or _HEADING.match(s) or _FENCE.match(s):
            continue
        lines.append(s)
        if sum(len(x) for x in lines) > limit:
            break
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------- 入口 ----------

def iter_input_files(inputs_dir: Path) -> list[Path]:
    if not inputs_dir.exists():
        return []
    return sorted(
        p for p in inputs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and not any(part.startswith(".") for part in p.parts)
    )


def ingest_directory(inputs_dir: Path, max_chars: int = 6000, min_chars: int = 1500) -> list[DocSlice]:
    files = iter_input_files(inputs_dir)
    slices: list[DocSlice] = []
    for doc_no, path in enumerate(files, 1):
        rel = path.relative_to(inputs_dir).as_posix()
        ext = path.suffix.lower()
        if ext == ".pdf":
            sections = split_markdown(read_pdf(path), rel)
        elif ext == ".docx":
            sections = split_markdown(read_docx(path), rel)
        else:
            text = path.read_text("utf-8", errors="replace")
            sections = split_markdown(text, rel) if ext in MARKDOWN_EXT else split_plain(text, rel)
        for sec_no, sec in enumerate(pack_sections(sections, max_chars, min_chars), 1):
            slices.append(
                DocSlice(
                    slice_id=f"DOC{doc_no:02d}-S{sec_no:02d}",
                    source_file=rel,
                    heading_path=sec.heading_path,
                    summary=summarize(sec.body),
                    content=sec.body,
                    char_count=len(sec.body),
                )
            )
    return slices


def write_catalog(slices: list[DocSlice], path: Path) -> None:
    catalog = [{k: v for k, v in asdict(s).items() if k != "content"} for s in slices]
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), "utf-8")
