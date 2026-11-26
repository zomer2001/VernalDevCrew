"""需求解析与自动化测试 `@pytest.mark.req` / 注释追溯扫描器（WP-6）。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from autodev.artifacts import FileExchange
from autodev.models import RequirementItem, TaskStatus

if TYPE_CHECKING:
    from autodev.engine import Engine

_REQ_ID_RE = re.compile(r"(?<![\w:.-])((?:[A-Za-z0-9_][A-Za-z0-9_.-]*:)?(?:REQ|NFR)-[0-9]{3,})\b")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b", re.IGNORECASE)
_TEST_FN_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)|^\s*func\s+(Test[A-Za-z0-9_]+)|^\s*(?:it|test)\s*\(\s*['\"]([^'\"]+)")


def extract_requirements_from_sources(engine: "Engine") -> list[dict[str, Any]]:
    """Consume canonical requirement items; legacy names and standalone SRS remain readable."""
    by_id: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        rid = item["req_id"]
        if rid in by_id:
            raise ValueError(f"重复需求编号：{rid}；跨功能请使用 feature_id 隔离编号")
        by_id[rid] = item

    if engine.ws.prd_json_path.is_file():
        try:
            prd = json.loads(engine.ws.prd_json_path.read_text("utf-8"))
            if not isinstance(prd, dict):
                raise ValueError("需求快照顶层必须是对象")
            for key, legacy in (("requirements", "functional_requirements"), ("nfrs", "non_functional_requirements")):
                items = prd[key] if key in prd else prd.get(legacy, [])
                if not isinstance(items, list):
                    raise ValueError(f"{key} 必须是数组")
                for raw in items:
                    if key in prd:
                        req = RequirementItem.model_validate(raw)
                    else:
                        if not isinstance(raw, dict) or not raw.get("req_id"):
                            raise ValueError("历史需求缺少稳定 req_id，必须显式迁移后再同步")
                        req = RequirementItem(
                            req_id=raw["req_id"], feature_id=raw.get("feature_id", ""),
                            title=raw.get("title") or raw.get("module") or raw["req_id"],
                            statement=raw.get("description") or raw.get("metric") or "",
                            priority=raw.get("priority", "P1"),
                            acceptance=raw.get("acceptance_criteria") or [raw.get("description") or raw.get("metric") or ""],
                        )
                    add({"req_id": req.identity, "title": req.title, "priority": req.priority,
                         "acceptance": req.acceptance, "source_section": f"prd_spec.json#{key}"})
        except (OSError, ValueError) as exc:
            engine.db.add_event("trace.sync_failed", str(exc), actor="TraceService")
            raise ValueError(f"需求快照解析失败：{exc}") from exc
        # An explicitly empty canonical list retires old requirements; stale Markdown
        # must not bring them back. Fall back only when the legacy source has no lists.
        if any(key in prd for key in ("requirements", "nfrs", "functional_requirements", "non_functional_requirements")):
            return list(by_id.values())

    path = engine.ws.root / "docs" / "SRS.md"
    if not path.is_file():
        rows = engine.db._query("SELECT content_text FROM doc_versions WHERE path='docs/SRS.md' ORDER BY version DESC LIMIT 1")
        if not rows and engine.db._query("SELECT 1 FROM requirements WHERE status='ACTIVE' LIMIT 1"):
            raise ValueError("需求来源暂不可读，保留上次有效清单；请恢复来源后重新同步")
        text = rows[0]["content_text"] if rows else ""
    else:
        text = path.read_text("utf-8")
    current = None
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        match = _REQ_ID_RE.search(stripped)
        cells = [c.strip() for c in stripped.strip("|").split("|")] if stripped.startswith("|") else []
        definition = bool(match) and (bool(cells and cells[0] == match.group(1)) or
                     bool(re.match(r"^[#*\-\s]+(?:[A-Za-z0-9_][A-Za-z0-9_.-]*:)?(?:REQ|NFR)-[0-9]{3,}", stripped)))
        if definition:
            rid = match.group(1)
            prio = _PRIORITY_RE.search(stripped)
            if cells:
                values = [c for c in cells[1:] if c and not _PRIORITY_RE.fullmatch(c)]
                title, acceptance = (values[0], values[1:]) if values else (rid, [])
            else:
                title = stripped[match.end():].strip(" *:：[]()")
                title = re.sub(r"^P[0-3]\]?\s*", "", title).strip()
                acceptance = []
            current = {"req_id": rid, "title": title or rid, "priority": prio.group(1) if prio else "P1",
                       "acceptance": acceptance, "source_section": f"docs/SRS.md:L{line_no}"}
            add(current)
        elif current and stripped.startswith("#"):
            current = None
        elif current and stripped.startswith("- ") and not stripped.startswith(("- **规范", "- **验收")):
            current["acceptance"].append(stripped[2:])
    return list(by_id.values())


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
            except (OSError, ValueError, KeyError) as exc:
                engine.db.add_event("trace.artifact_failed", f"候选基线读取失败：{exc}", actor="TraceService")
                raise ValueError("候选基线无法读取，追溯校验已阻断") from exc
        for ms in engine.db.list_milestones():
            dkey = engine.db.meta_get(f"delivery:{ms.milestone_id}")
            if dkey:
                try:
                    ms_files = exchange.get(dkey)
                    if isinstance(ms_files, dict):
                        for k, v in ms_files.items():
                            if isinstance(v, str):
                                snap[k] = v
                except (OSError, ValueError, KeyError) as exc:
                    engine.db.add_event("trace.artifact_failed", f"交付产物读取失败：{exc}", actor="TraceService")
                    raise ValueError("交付产物无法读取，追溯校验已阻断") from exc
        for rel, txt in sorted(snap.items()):
            if rel not in seen_files and txt and _is_test_path(rel):
                _scan_single_test_content(rel, txt, out)

    return out
