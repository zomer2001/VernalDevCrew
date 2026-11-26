"""需求解析与自动化测试 `@pytest.mark.req` / 注释追溯扫描器（WP-6）。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from autodev.artifacts import FileExchange
from autodev.models import TaskStatus

if TYPE_CHECKING:
    from autodev.engine import Engine

_REQ_ID_RE = re.compile(r"\b((?:REQ|NFR)-[0-9]{3,})\b")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b", re.IGNORECASE)
_TEST_FN_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)|^\s*func\s+(Test[A-Za-z0-9_]+)|^\s*(?:it|test)\s*\(\s*['\"]([^'\"]+)")


def extract_requirements_from_sources(engine: "Engine") -> list[dict[str, Any]]:
    """从 `.autodev/prd_spec.json` 与 `docs/SRS.md` 提取并合并标准化需求条目（手工编辑 SRS 可覆盖更新）。"""
    by_id: dict[str, dict[str, Any]] = {}

    if engine.ws.prd_json_path.is_file():
        try:
            prd = json.loads(engine.ws.prd_json_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            prd = {}
        for idx, item in enumerate(prd.get("functional_requirements") or [], 1):
            if not isinstance(item, dict):
                continue
            rid = str(item.get("req_id") or f"REQ-{idx:03d}").strip()
            by_id[rid] = {
                "req_id": rid,
                "title": str(item.get("title") or item.get("module") or rid).strip(),
                "priority": str(item.get("priority") or "P1").upper(),
                "acceptance": list(item.get("acceptance_criteria") or ([item["description"]] if item.get("description") else [])),
                "source_section": "prd_spec.json#functional",
            }
        for idx, item in enumerate(prd.get("non_functional_requirements") or [], 1):
            if isinstance(item, dict):
                rid = str(item.get("req_id") or f"NFR-{idx:03d}").strip()
                by_id[rid] = {
                    "req_id": rid,
                    "title": str(item.get("title") or item.get("category") or rid).strip(),
                    "priority": str(item.get("priority") or "P1").upper(),
                    "acceptance": [str(item.get("metric") or item.get("description") or "")],
                    "source_section": "prd_spec.json#nfr",
                }

    # 解析 docs/SRS.md 中的 Markdown 表格行或标题（如 `| REQ-001 | P0 | 订单创建 | ... |` 或 `### REQ-001 (P0) 订单创建`）
    srs_path = engine.ws.root / "docs" / "SRS.md"
    srs_text = ""
    if srs_path.is_file():
        srs_text = srs_path.read_text("utf-8", errors="replace")
    else:
        rows = engine.db._query("SELECT content_text FROM doc_versions WHERE path='docs/SRS.md' ORDER BY version DESC LIMIT 1")
        if rows:
            srs_text = str(rows[0]["content_text"] or "")

    if srs_text:
        for line_no, raw_line in enumerate(srs_text.splitlines(), 1):
            m = _REQ_ID_RE.search(raw_line)
            if not m:
                continue
            rid = m.group(1)
            stripped = raw_line.strip()
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                prio_match = _PRIORITY_RE.search(raw_line)
                prio = prio_match.group(1).upper() if prio_match else (by_id.get(rid, {}).get("priority") or "P1")
                non_meta = [c for c in cells if c and rid not in c and not _PRIORITY_RE.fullmatch(c)]
                title = non_meta[0] if non_meta else (by_id.get(rid, {}).get("title") or rid)
                acc = non_meta[1:] if len(non_meta) > 1 else (by_id.get(rid, {}).get("acceptance") or [])
                by_id[rid] = {
                    "req_id": rid,
                    "title": title[:200],
                    "priority": prio,
                    "acceptance": [a for a in acc if a],
                    "source_section": f"docs/SRS.md:L{line_no}",
                }
            elif stripped.startswith(("#", "-", "*")):
                prio_match = _PRIORITY_RE.search(raw_line)
                prio = prio_match.group(1).upper() if prio_match else (by_id.get(rid, {}).get("priority") or "P1")
                cleaned_title = re.sub(r"^[#*\-\s]+", "", stripped)
                cleaned_title = cleaned_title.replace(rid, "").strip(" ：:—-()[]")
                if rid not in by_id or cleaned_title:
                    by_id[rid] = {
                        "req_id": rid,
                        "title": (cleaned_title or by_id.get(rid, {}).get("title") or rid)[:200],
                        "priority": prio,
                        "acceptance": by_id.get(rid, {}).get("acceptance") or [],
                        "source_section": f"docs/SRS.md:L{line_no}",
                    }

    return [by_id[k] for k in sorted(by_id.keys())]


def _is_test_path(rel_path: str) -> bool:
    p = Path(rel_path)
    name = p.name.lower()
    return (
        "tests" in {pt.lower() for pt in p.parts}
        or "test" in {pt.lower() for pt in p.parts}
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "_test.go", "test.java"))
    )


def _scan_single_test_content(rel_path: str, text: str, out: dict[str, list[dict[str, Any]]]) -> None:
    lines = text.splitlines()
    pending_reqs: set[str] = set()
    for idx, line in enumerate(lines, 1):
        found = set(_REQ_ID_RE.findall(line))
        fn_match = _TEST_FN_RE.search(line)
        if fn_match:
            fn_name = next((g for g in fn_match.groups() if g), f"line_{idx}")
            all_linked = pending_reqs | found
            # 同时向后探查函数体内前 6 行注释或 docstring 中的 REQ-xxx
            for look_ahead in lines[idx : idx + 6]:
                if _TEST_FN_RE.search(look_ahead):
                    break
                all_linked |= set(_REQ_ID_RE.findall(look_ahead))
            for rid in sorted(all_linked):
                out.setdefault(rid, []).append({"file": rel_path, "test": fn_name, "line": idx})
            pending_reqs.clear()
        elif found:
            pending_reqs |= found
    # 若文件级注释含 REQ 但未紧邻单个函数，兜底记在文件级
    for rid in sorted(pending_reqs):
        out.setdefault(rid, []).append({"file": rel_path, "test": Path(rel_path).stem, "line": 1})


def scan_workspace_tests(engine: "Engine") -> dict[str, list[dict[str, Any]]]:
    """扫描工作区磁盘测试文件及 files 模式最新快照中的需求标记。"""
    out: dict[str, list[dict[str, Any]]] = {}
    seen_files: set[str] = set()

    root = engine.ws.root
    if root.is_dir():
        for fp in sorted(root.rglob("*")):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel_parts = fp.relative_to(root).parts
            if any(part.startswith(".") or part in ("node_modules", "__pycache__", ".venv", "venv") for part in rel_parts[:-1]):
                continue
            rel = fp.relative_to(root).as_posix()
            if not _is_test_path(rel):
                continue
            try:
                txt = fp.read_text("utf-8", errors="replace")
            except OSError:
                continue
            seen_files.add(rel)
            _scan_single_test_content(rel, txt, out)

    if engine.settings.collaboration_mode == "files":
        exchange = FileExchange(engine.ws, engine.db)
        snap: dict[str, str] = {}
        tasks = [t for t in engine.db.list_tasks() if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)]
        if tasks:
            try:
                snap.update(exchange.baseline([t.task_id for t in tasks]))
            except Exception:
                pass
        for ms in engine.db.list_milestones():
            dkey = engine.db.meta_get(f"delivery:{ms.milestone_id}")
            if dkey:
                try:
                    ms_files = exchange.get(dkey)
                    if isinstance(ms_files, dict):
                        for k, v in ms_files.items():
                            if isinstance(v, str):
                                snap[k] = v
                except Exception:
                    pass
        for rel, txt in sorted(snap.items()):
            if rel not in seen_files and txt and _is_test_path(rel):
                _scan_single_test_content(rel, txt, out)

    return out
