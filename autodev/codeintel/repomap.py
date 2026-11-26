"""Repo Map 构建器：多语言符号抽取（Tree-sitter 可选 + Python AST / 正则离线兜底）、内容哈希增量缓存与 Personalized PageRank（WP-5）。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

SUPPORTED_EXTS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
}

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".autodev",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        "target",
    }
)

_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_STOPWORDS = frozenset(
    {
        "def",
        "class",
        "return",
        "import",
        "from",
        "self",
        "cls",
        "None",
        "True",
        "False",
        "async",
        "await",
        "function",
        "const",
        "let",
        "var",
        "export",
        "default",
        "interface",
        "type",
        "struct",
        "package",
        "func",
        "public",
        "private",
        "protected",
        "static",
        "void",
        "int",
        "string",
        "bool",
        "boolean",
        "float",
        "double",
        "char",
        "new",
        "this",
        "super",
        "if",
        "else",
        "for",
        "while",
        "switch",
        "case",
        "try",
        "catch",
        "finally",
        "throw",
        "throws",
        "raise",
    }
)


@dataclass
class SymbolDef:
    name: str
    kind: str
    line: int
    signature: str


@dataclass
class RepoMapResult:
    text: str
    ranked_files: list[str]
    file_scores: dict[str, float]
    cache_hits: int = 0
    cache_misses: int = 0
    backend: str = "fallback"
    symbols_by_file: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _try_tree_sitter_extract(lang: str, text: str) -> tuple[list[dict[str, Any]], list[str]] | None:
    """若环境已安装 tree_sitter_language_pack 则优先使用语法树抽取，缺失时返回 None 走确定性降级。"""
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        parser = get_parser(lang)  # type: ignore[arg-type]
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None

    defs: list[dict[str, Any]] = []
    refs: set[str] = set()
    lines = text.splitlines()

    def _walk(node: Any) -> None:
        ntype = getattr(node, "type", "")
        if ntype in (
            "function_definition",
            "class_definition",
            "function_declaration",
            "method_definition",
            "class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "method_declaration",
        ):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                nm = name_node.text.decode("utf-8", errors="ignore")
                ln = int(node.start_point[0]) + 1
                sig = lines[ln - 1].strip() if 1 <= ln <= len(lines) else nm
                kind = "class" if "class" in ntype or "interface" in ntype else "func"
                defs.append({"name": nm, "kind": kind, "line": ln, "signature": sig[:140]})
        elif ntype in ("identifier", "type_identifier", "property_identifier"):
            nm = node.text.decode("utf-8", errors="ignore")
            if len(nm) >= 3 and nm not in _STOPWORDS:
                refs.add(nm)
        for ch in getattr(node, "children", ()):
            _walk(ch)

    _walk(tree.root_node)
    defined_names = {d["name"] for d in defs}
    return defs, sorted(refs - defined_names)


def _extract_python_ast(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    defs: list[dict[str, Any]] = []
    refs: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_regex("python", text)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            base_str = f"({', '.join(bases)})" if bases else ""
            defs.append({"name": node.name, "kind": "class", "line": node.lineno, "signature": f"class {node.name}{base_str}"})
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    sig = f"  {prefix} {node.name}.{item.name}({', '.join(args[:6])})"
                    defs.append({"name": item.name, "kind": "method", "line": item.lineno, "signature": sig[:140]})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sig = f"{prefix} {node.name}({', '.join(args[:6])})"
            defs.append({"name": node.name, "kind": "func", "line": node.lineno, "signature": sig[:140]})

    for sub in ast.walk(tree):
        if isinstance(sub, ast.Name) and len(sub.id) >= 3 and sub.id not in _STOPWORDS:
            refs.add(sub.id)
        elif isinstance(sub, ast.Attribute) and len(sub.attr) >= 3 and sub.attr not in _STOPWORDS:
            refs.add(sub.attr)

    defined_names = {d["name"] for d in defs}
    return defs, sorted(refs - defined_names)


_LANG_DEF_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "python": [
        ("class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("func", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")),
    ],
    "javascript": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("func", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("func", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(")),
    ],
    "typescript": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface|type)\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("func", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("func", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?\(")),
    ],
    "go": [
        ("class", re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b")),
        ("func", re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ],
    "java": [
        ("class", re.compile(r"^\s*(?:public|protected|private)?\s*(?:abstract|final)?\s*(?:class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("method", re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+)?[A-Za-z0-9_<>\[\], ?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ],
    "c": [
        ("class", re.compile(r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("func", re.compile(r"^[A-Za-z_][A-Za-z0-9_\s\*]+\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{?\s*$")),
    ],
    "cpp": [
        ("class", re.compile(r"^\s*(?:template\s*<[^>]+>\s*)?(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("func", re.compile(r"^[A-Za-z_][A-Za-z0-9_:\s\*&<>]+\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?:const)?\s*\{?\s*$")),
    ],
}


def _extract_regex(lang: str, text: str) -> tuple[list[dict[str, Any]], list[str]]:
    patterns = _LANG_DEF_PATTERNS.get(lang, [])
    defs: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue
        for kind, pat in patterns:
            m = pat.search(raw_line)
            if m:
                name = m.group(1)
                if name not in _STOPWORDS:
                    sig = stripped.rstrip("{").strip()[:140]
                    defs.append({"name": name, "kind": kind, "line": idx, "signature": sig})
                break
    refs = {m.group(0) for m in _IDENT_RE.finditer(text) if m.group(0) not in _STOPWORDS}
    defined_names = {d["name"] for d in defs}
    return defs, sorted(refs - defined_names)


def extract_file_symbols(
    path: Path,
    text: str | None = None,
    *,
    allow_tree_sitter: bool = True,
) -> dict[str, Any]:
    """抽取单个源码文件的符号定义与外部标识符引用。"""
    ext = path.suffix.lower()
    lang = SUPPORTED_EXTS.get(ext, "")
    if not lang:
        return {"lang": "", "backend": "none", "defs": [], "refs": []}
    content = text if text is not None else path.read_text("utf-8", errors="replace")
    if allow_tree_sitter:
        ts_res = _try_tree_sitter_extract(lang, content)
        if ts_res is not None:
            return {"lang": lang, "backend": "tree-sitter", "defs": ts_res[0], "refs": ts_res[1]}
    if lang == "python":
        defs, refs = _extract_python_ast(content)
        return {"lang": lang, "backend": "ast+regex", "defs": defs, "refs": refs}
    defs, refs = _extract_regex(lang, content)
    return {"lang": lang, "backend": "ast+regex", "defs": defs, "refs": refs}


def _iter_source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if p.suffix.lower() in SUPPORTED_EXTS:
            out.append(p)
    return out


def _load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache_path: Path, entries: dict[str, Any]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(entries, ensure_ascii=False), "utf-8")
    except OSError:
        pass


def personalized_pagerank(
    file_entries: dict[str, dict[str, Any]],
    *,
    seed_paths: list[str] | None = None,
    seed_identifiers: set[str] | None = None,
    damping: float = 0.85,
    iterations: int = 25,
) -> dict[str, float]:
    files = sorted(file_entries.keys())
    n = len(files)
    if n == 0:
        return {}
    if n == 1:
        return {files[0]: 1.0}

    # 建立 symbol -> 定义该符号的文件列表
    definers: dict[str, list[str]] = {}
    for rel, info in file_entries.items():
        for d in info.get("defs") or []:
            definers.setdefault(str(d["name"]), []).append(rel)

    # 个性化向量 p（种子路径与种子符号所在文件权重显著抬升）
    norm_prefixes = [sp.strip().lstrip("./") for sp in (seed_paths or []) if sp.strip()]
    seed_idents = seed_identifiers or set()
    p_raw: dict[str, float] = {}
    for rel in files:
        w = 1.0
        for pref in norm_prefixes:
            clean_p = pref.rstrip("/")
            if rel == clean_p or rel.startswith(clean_p + "/"):
                w += 12.0
        defined_here = {str(d["name"]) for d in (file_entries[rel].get("defs") or [])}
        referenced_here = set(file_entries[rel].get("refs") or [])
        overlap_def = len(defined_here & seed_idents)
        overlap_ref = len(referenced_here & seed_idents)
        if overlap_def:
            w += 8.0 * overlap_def
        if overlap_ref:
            w += 2.5 * overlap_ref
        p_raw[rel] = w

    total_p = sum(p_raw.values()) or 1.0
    personal = {f: p_raw[f] / total_p for f in files}

    # 有向边 src -> dst：当 src 引用了 dst 定义的符号时连接；同时若存在种子，反向给调用种子文件的上游也传导关联分
    out_edges: dict[str, dict[str, float]] = {f: {} for f in files}
    for src, info in file_entries.items():
        for ident in info.get("refs") or []:
            targets = definers.get(ident)
            if not targets:
                continue
            spec_weight = 1.0 / math.sqrt(len(targets))
            if ident in seed_idents:
                spec_weight *= 3.0
            for dst in targets:
                if dst != src:
                    out_edges[src][dst] = out_edges[src].get(dst, 0.0) + spec_weight
                    # 双向耦合微权重，让被种子文件调用或调用种子文件的邻接模块一起浮现
                    out_edges[dst][src] = out_edges[dst].get(src, 0.0) + spec_weight * 0.35

    ranks = dict(personal)
    for _ in range(iterations):
        next_ranks = {f: (1.0 - damping) * personal[f] for f in files}
        sink_mass = 0.0
        for src in files:
            edges = out_edges[src]
            total_w = sum(edges.values())
            if total_w <= 0:
                sink_mass += ranks[src]
            else:
                for dst, w in edges.items():
                    next_ranks[dst] += damping * ranks[src] * (w / total_w)
        if sink_mass > 0:
            for f in files:
                next_ranks[f] += damping * sink_mass * personal[f]
        ranks = next_ranks

    return ranks


def extract_seed_identifiers(*texts: str) -> set[str]:
    found: set[str] = set()
    for t in texts:
        if not t:
            continue
        for m in _IDENT_RE.finditer(t):
            tok = m.group(0)
            if tok not in _STOPWORDS:
                found.add(tok)
    return found


def build_repo_map(
    root: Path,
    *,
    max_tokens: int = 2000,
    seed_paths: list[str] | None = None,
    seed_identifiers: list[str] | set[str] | None = None,
    allow_tree_sitter: bool = True,
    persist_cache: bool = True,
) -> RepoMapResult:
    """扫描工作区源码，利用增量缓存 + Personalized PageRank 生成指定 Token 预算内的精简仓库地图。"""
    cache_path = root / ".autodev" / "codeintel" / "repomap_cache.json"
    cache_key_mode = "ts" if allow_tree_sitter else "fallback"
    raw_cache = _load_cache(cache_path) if persist_cache else {}
    mode_cache: dict[str, Any] = raw_cache.get(cache_key_mode, {}) if isinstance(raw_cache.get(cache_key_mode), dict) else {}

    hits = 0
    misses = 0
    backend_used = "ast+regex"
    active_entries: dict[str, dict[str, Any]] = {}
    new_mode_cache: dict[str, Any] = {}

    for fp in _iter_source_files(root):
        rel = fp.relative_to(root).as_posix()
        try:
            raw_bytes = fp.read_bytes()
        except OSError:
            continue
        sha = hashlib.sha256(raw_bytes).hexdigest()
        cached = mode_cache.get(rel)
        if isinstance(cached, dict) and cached.get("sha") == sha:
            hits += 1
            entry = cached
        else:
            misses += 1
            text = raw_bytes.decode("utf-8", errors="replace")
            extracted = extract_file_symbols(fp, text, allow_tree_sitter=allow_tree_sitter)
            entry = {
                "sha": sha,
                "lang": extracted["lang"],
                "backend": extracted["backend"],
                "defs": extracted["defs"],
                "refs": extracted["refs"],
            }
        if entry.get("backend") == "tree-sitter":
            backend_used = "tree-sitter"
        active_entries[rel] = entry
        new_mode_cache[rel] = entry

    if persist_cache and (root / ".autodev").is_dir() and misses > 0:
        raw_cache[cache_key_mode] = new_mode_cache
        _save_cache(cache_path, raw_cache)

    if not active_entries:
        return RepoMapResult(text="", ranked_files=[], file_scores={}, cache_hits=hits, cache_misses=misses, backend=backend_used)

    seeds = set(seed_identifiers or [])
    scores = personalized_pagerank(active_entries, seed_paths=seed_paths, seed_identifiers=seeds)
    ranked = sorted(active_entries.keys(), key=lambda f: (-scores.get(f, 0.0), f))

    char_budget = max(120, int(max_tokens) * 4)
    lines: list[str] = []
    used_chars = 0
    included_files: list[str] = []

    for rel in ranked:
        defs = active_entries[rel].get("defs") or []
        block_lines = [f"{rel}:"]
        for d in defs[:12]:
            block_lines.append(f"  │ {d['signature']}")
        if len(defs) > 12:
            block_lines.append(f"  │ … (+{len(defs) - 12} symbols)")
        block = "\n".join(block_lines)
        if used_chars + len(block) + 1 > char_budget:
            if not included_files:
                trimmed = block[: max(0, char_budget - 20)] + "\n  │ …(truncated)"
                lines.append(trimmed)
                included_files.append(rel)
            break
        lines.append(block)
        used_chars += len(block) + 1
        included_files.append(rel)

    return RepoMapResult(
        text="\n".join(lines),
        ranked_files=ranked,
        file_scores=scores,
        cache_hits=hits,
        cache_misses=misses,
        backend=backend_used,
        symbols_by_file={k: v.get("defs") or [] for k, v in active_entries.items()},
    )


def find_impact_callers(root: Path, diff_text: str, *, max_items: int = 15) -> list[dict[str, Any]]:
    """分析 diff 触及的文件与符号，在全仓引用图中查找受影响的外部调用方（供代码审查提示词使用）。"""
    if not diff_text.strip() or not root.is_dir():
        return []
    touched_files: set[str] = set()
    touched_tokens: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            touched_files.add(line[6:].strip())
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            for m in _IDENT_RE.finditer(line[1:]):
                if m.group(0) not in _STOPWORDS:
                    touched_tokens.add(m.group(0))
    if not touched_files:
        return []

    file_entries: dict[str, dict[str, Any]] = {}
    for fp in _iter_source_files(root):
        rel = fp.relative_to(root).as_posix()
        try:
            txt = fp.read_text("utf-8", errors="replace")
        except OSError:
            continue
        file_entries[rel] = extract_file_symbols(fp, txt)

    impacts: list[dict[str, Any]] = []
    for tfile in sorted(touched_files):
        info = file_entries.get(tfile)
        if not info:
            continue
        for d in info.get("defs") or []:
            sym = str(d["name"])
            if touched_tokens and sym not in touched_tokens and len(info.get("defs") or []) > 5:
                continue
            callers = sorted(
                other
                for other, oinfo in file_entries.items()
                if other != tfile and sym in set(oinfo.get("refs") or [])
            )
            if callers:
                impacts.append({"symbol": sym, "defined_in": tfile, "callers": callers[:10]})
                if len(impacts) >= max_items:
                    return impacts
    return impacts
