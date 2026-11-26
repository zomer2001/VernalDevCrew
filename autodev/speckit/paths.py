"""One path contract for source documents, projections and task consumers."""
from pathlib import Path
import re

KINDS = {"constitution", "spec", "plan", "tasks", "checklist", "analyze"}


def feature_spec_dir(feature_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", feature_id):
        raise ValueError("功能标识只能包含字母、数字、下划线和连字符，长度不超过 80")
    return f"specs/{feature_id}"


def source_path(feature_id: str, kind: str) -> str:
    folder = feature_spec_dir(feature_id)
    if kind not in KINDS:
        raise ValueError("不支持的规格文档类型")
    return ".specify/memory/constitution.md" if kind == "constitution" else f"{folder}/{kind}.md"


def safe_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("..", ".git", ".autodev") for p in rel.parts):
        raise ValueError("非法的规格产物路径")
    target = root / rel
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise ValueError("规格产物路径不得包含符号链接")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("规格产物路径越出工作区")
    return target
