"""Code Wiki 逆向生成与只读「问代码库」服务（WP-5）。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodev.codeintel.repomap import _iter_source_files, build_repo_map, extract_seed_identifiers

if TYPE_CHECKING:
    from autodev.engine import Engine


def generate_code_wiki(engine: "Engine", *, author: str = "CodeWikiAgent") -> dict[str, Any]:
    """扫描存量代码库生成 `docs/wiki/` 模块百科与逆向 `docs/ARCHITECTURE.md` 初稿。"""
    from autodev.docs_center.service import save_doc_version as save_document_version

    root = engine.ws.root
    rmap = build_repo_map(root, max_tokens=4000)
    by_module: dict[str, list[str]] = defaultdict(list)
    for rel in rmap.ranked_files:
        parts = Path(rel).parts
        mod = parts[0] if len(parts) > 1 else "root"
        by_module[mod].append(rel)

    created_paths: list[str] = []
    index_lines = [
        f"# 代码百科（Code Wiki）· {engine.ws.root.name}",
        "",
        f"- **扫描源码文件数**：{len(rmap.ranked_files)}",
        f"- **解析引擎**：`{rmap.backend}`",
        "",
        "## 核心模块导航",
        "",
    ]

    mermaid_edges: list[str] = ["flowchart TD"]
    for idx, (mod, files) in enumerate(sorted(by_module.items()), 1):
        mod_doc_rel = f"docs/wiki/{mod}.md"
        index_lines.append(f"- [{mod}]({mod}.md)（{len(files)} 个文件）")
        mermaid_edges.append(f'  M{idx}["{mod} ({len(files)} files)"]')

        mod_lines = [f"# 模块百科：`{mod}`", "", "## 核心文件与关键符号", ""]
        for f in files[:25]:
            defs = rmap.symbols_by_file.get(f) or []
            mod_lines.append(f"### `{f}`")
            if defs:
                for d in defs[:15]:
                    mod_lines.append(f"- `{d['signature']}` (L{d['line']})")
            else:
                mod_lines.append("- （脚本/配置模块，无导出类或函数定义）")
            mod_lines.append("")
        save_document_version(engine, mod_doc_rel, "\n".join(mod_lines), author=author, note="自动生成模块百科")
        created_paths.append(mod_doc_rel)

    index_lines += [
        "",
        "## 全局模块拓扑概览",
        "",
        "```mermaid",
        *mermaid_edges,
        "```",
        "",
        "## 全局高权重 Repo Map 摘要",
        "",
        "```",
        rmap.text or "（空仓库）",
        "```",
        "",
    ]
    save_document_version(engine, "docs/wiki/INDEX.md", "\n".join(index_lines), author=author, note="自动生成代码百科首页")
    created_paths.insert(0, "docs/wiki/INDEX.md")

    # 逆向生成 docs/ARCHITECTURE.md 初稿（若尚不存在）
    arch_rel = "docs/ARCHITECTURE.md"
    if not (root / arch_rel).is_file():
        arch_lines = [
            f"# 逆向架构设计说明书（初稿）· {engine.ws.root.name}",
            "",
            "> 本初稿由 Vernal CodeIntel 基于存量源码符号图逆向推导生成，供架构师在 `doc_reverse` 流程中审阅修订。",
            "",
            "## 1. 模块边界划分",
            "",
            "| 模块目录 | 文件数量 | 代表文件 |",
            "|---|---|---|",
        ]
        for mod, files in sorted(by_module.items()):
            sample = ", ".join(f"`{x}`" for x in files[:3])
            arch_lines.append(f"| `{mod}` | {len(files)} | {sample} |")
        arch_lines += [
            "",
            "## 2. 组件关系图",
            "",
            "```mermaid",
            *mermaid_edges,
            "```",
            "",
            "## 3. 核心符号索引（Top PageRank）",
            "",
            "```",
            rmap.text[:4000] or "（无）",
            "```",
            "",
        ]
        save_document_version(engine, arch_rel, "\n".join(arch_lines), author=author, note="逆向推导架构初稿")
        created_paths.append(arch_rel)

    engine.db.add_event("codeintel.wiki_generated", f"已生成代码百科（共 {len(created_paths)} 篇文档）", actor=author)
    return {
        "files_scanned": len(rmap.ranked_files),
        "backend": rmap.backend,
        "generated_docs": created_paths,
    }


def ask_codebase(engine: "Engine", question: str, *, max_matches: int = 12) -> dict[str, Any]:
    """基于 Personalized PageRank + 源码精确检索回答代码库问题（纯只读零副作用）。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")

    root = engine.ws.root
    seeds = extract_seed_identifiers(q)
    # 兼容中文或短词检索关键词
    raw_terms = [tok.strip() for tok in q.replace("？", " ").replace("?", " ").split() if len(tok.strip()) >= 2]
    search_terms = list(dict.fromkeys([*sorted(seeds), *raw_terms]))[:8]

    rmap = build_repo_map(root, max_tokens=1800, seed_identifiers=seeds)
    citations: list[dict[str, Any]] = []
    for fp in _iter_source_files(root):
        rel = fp.relative_to(root).as_posix()
        try:
            lines = fp.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, 1):
            if any(term.lower() in line.lower() for term in search_terms):
                citations.append(
                    {
                        "file": rel,
                        "line": idx,
                        "snippet": line.strip()[:180],
                        "score": round(rmap.file_scores.get(rel, 0.0), 5),
                    }
                )
                if len(citations) >= max_matches * 3:
                    break
        if len(citations) >= max_matches * 3:
            break

    citations.sort(key=lambda c: (-float(c["score"]), str(c["file"]), int(c["line"])))
    top_citations = citations[:max_matches]

    top_files = rmap.ranked_files[:5]
    summary_lines = [f"针对问题「{q}」，在当前代码库定位到以下高相关模块与符号："]
    if top_files:
        summary_lines.append("- **PageRank 最相关文件**：" + "、".join(f"`{f}`" for f in top_files))
    if top_citations:
        summary_lines.append("- **关键代码命中位置**：")
        for c in top_citations[:5]:
            summary_lines.append(f"  - `{c['file']}:{c['line']}` — `{c['snippet']}`")
    else:
        summary_lines.append("- 未检索到直接字面匹配行，请参考下方 Repo Map 核心符号结构。")

    return {
        "question": q,
        "answer_markdown": "\n".join(summary_lines),
        "ranked_files": top_files,
        "citations": top_citations,
        "repo_map": rmap.text,
    }
