"""Small UTF-8 file deliveries. Immutable task patches refer to their input versions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from autodev.config import Workspace
from autodev.dag import path_allowed, topo_sort
from autodev.db import Database, Task
from autodev.models import TaskStatus

MAX_TOTAL = 4 * 1024 * 1024
SKIP = {"__pycache__", ".pytest_cache", ".DS_Store", "node_modules", ".git", ".autodev", ".venv"}


def checked_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or str(path) != name or "\\" in name
            or any(p in SKIP or p.startswith(".") or ":" in p for p in path.parts)
            or any(ord(c) < 32 for c in name)):
        raise ValueError(f"非法交付路径 {name!r}")
    return name


def snapshot(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    size = 0
    for directory, dirs, names in os.walk(root):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if d not in SKIP]
        if any((parent / d).is_symlink() for d in dirs):
            raise ValueError("不支持符号链接目录")
        for name in names:
            if name in SKIP:
                continue
            path = parent / name
            key = checked_name(path.relative_to(root).as_posix())
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"产物必须是普通文件 {key}")
            size += path.stat().st_size
            if size > MAX_TOTAL:
                raise ValueError("文本产物超过 4 MiB")
            files[key] = path.read_bytes().decode()
    return files


def materialize(files: dict[str, str], root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = root / checked_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileExchange:
    def __init__(self, ws: Workspace, db: Database):
        self.ws = ws
        self.db = db
        self.objects = ws.state_dir / "objects"

    def put(self, value: dict) -> str:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        key = digest(data)
        self.objects.mkdir(exist_ok=True)
        with NamedTemporaryFile(dir=self.objects, delete=False) as f:
            f.write(data)
        Path(f.name).replace(self.objects / key)
        return key

    def get(self, key: str | None) -> dict:
        if not key or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("缺少已验证的文件版本")
        data = (self.objects / key).read_bytes()
        if digest(data) != key:
            raise ValueError(f"文件版本校验失败 {key}")
        return json.loads(data)

    def baseline(self, dependencies: list[str]) -> dict[str, str]:
        tasks = {t.task_id: t for t in self.db.list_tasks()}
        order = topo_sort({i: t.depends_on for i, t in tasks.items()})
        needed = set(dependencies)
        needed.update(i for i, t in tasks.items() if t.status == TaskStatus.MERGED)
        for tid in reversed(order):
            if tid in needed:
                needed.update(tasks[tid].depends_on)
        files: dict[str, str] = {}
        for tid in order:
            if tid not in needed:
                continue
            for name, (before, after) in self.get(tasks[tid].artifact_digest).items():
                if files.get(name) not in (before, after):
                    raise ValueError(f"任务 {tid} 与其他任务修改了同一文件 {name}，需协调后重试")
                if after is None:
                    files.pop(name, None)
                else:
                    files[name] = after
        return files

    def patch(self, task: Task, before: dict[str, str], after: dict[str, str], protected: list[str]) -> str:
        changes = {}
        for name in before.keys() | after.keys():
            old, new = before.get(name), after.get(name)
            if old == new:
                continue
            checked_name(name)
            if not path_allowed(name, task.allowed_paths) or path_allowed(name, protected):
                raise ValueError(f"{task.task_id} 越权修改 {name}")
            changes[name] = [old, new]
        if not changes:
            raise ValueError("未提交新增或变更的文件")
        return self.put(changes)


def apply_output(before: dict[str, str], manifest: str) -> dict[str, str]:
    data = json.loads(manifest)
    changes = data.get("files") if isinstance(data, dict) else None
    if not isinstance(changes, dict):
        raise ValueError("交付清单必须包含 files 对象")
    result = before.copy()
    for name, body in changes.items():
        checked_name(name)
        if body is None:
            result.pop(name, None)
        elif isinstance(body, str):
            result[name] = body
        else:
            raise ValueError(f"{name} 不是文本")
    if sum(len(s.encode()) for s in result.values()) > MAX_TOTAL:
        raise ValueError("文本产物超过 4 MiB")
    return result
