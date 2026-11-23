"""Read-only access to event history in the local workspaces collection."""

from contextlib import closing
from pathlib import Path
import sqlite3

from autodev.config import Workspace


def history_sources(current: Workspace, include_neighbors: bool) -> dict[str, Path]:
    root = current.root.resolve()
    sources = {root.name: current.db_path}
    if include_neighbors and root.parent.name == "workspaces":
        for entry in sorted(root.parent.iterdir()):
            if entry == root or entry.is_symlink() or not entry.is_dir():
                continue
            ws = Workspace(entry)
            if ws.db_path.is_file() and ws.db_path.resolve() == ws.db_path and ws.config_path.is_file():
                sources[entry.name] = ws.db_path
    return sources


def event_page(path: Path, before: int | None, limit: int) -> dict:
    url = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(url, uri=True)) as db:
        db.row_factory = sqlite3.Row
        sql = "SELECT event_id, actor, task_id, event_type, message, created_at FROM events"
        params = []
        if before is not None:
            sql += " WHERE event_id < ?"
            params.append(before)
        records = db.execute(sql + " ORDER BY event_id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    page = records[:limit]
    return {
        "events": [dict(row) for row in page],
        "next_before": page[-1]["event_id"] if len(records) > limit else None,
    }
