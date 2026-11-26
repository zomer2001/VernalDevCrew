"""Shared paths for versioned, milestone-scoped detailed designs."""

from pathlib import Path
import re


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) or value in {".", ".."}:
        raise ValueError(f"非法的设计范围标识：{value}")
    return value


def detailed_design_json_path(ws, milestone_id: str, squad_id: str) -> Path:
    return ws.state_dir / "design" / _identifier(milestone_id) / f"{_identifier(squad_id)}.json"


def detailed_design_doc_path(milestone_id: str, squad_id: str) -> str:
    return f"docs/design/{_identifier(milestone_id)}/{_identifier(squad_id)}.md"


def detailed_design_approval_paths(workspace_root: Path, milestone_id: str) -> list[Path]:
    """List the complete on-disk approval group, including unarchived new members."""
    scope = _identifier(milestone_id)
    roots = [(workspace_root / "docs/design", "*.md"), (workspace_root / ".autodev/design", "*.json")]
    scoped = [p for root, pattern in roots for p in (root / scope).rglob(pattern) if p.is_file()]
    paths = scoped or [p for root, pattern in roots for p in root.glob(pattern) if p.is_file()]
    for path in paths:
        if not path.resolve().is_relative_to(workspace_root.resolve()):
            raise ValueError(f"详设文件越出工作区：{path.name}")
    return sorted(paths)


def iter_detailed_design_json_paths(ws, milestone_id: str | None = None):
    root = ws.state_dir / "design"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"详设文件越出工作区：{path.name}")
        rel = path.relative_to(root)
        if milestone_id is None or len(rel.parts) == 1 or rel.parts[0] == _identifier(milestone_id):
            yield path
