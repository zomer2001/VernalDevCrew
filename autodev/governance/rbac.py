"""角色权限控制（RBAC）与门禁审批角色核验（WP-7）。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from autodev.db import Database, now_iso

VALID_ROLES = ("admin", "architect", "developer", "reviewer", "viewer")


def list_members(db: Database, project: str = "") -> list[dict[str, Any]]:
    if project.strip():
        rows = db._query(
            "SELECT * FROM members WHERE project IN (?, '*') ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, user_id",
            (project.strip(),),
        )
    else:
        rows = db._query("SELECT * FROM members ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, user_id")
    return [dict(r) for r in rows]


def upsert_member(db: Database, user_id: str, role: str, *, project: str = "*") -> dict[str, Any]:
    uid = (user_id or "").strip()
    norm_role = (role or "").strip().lower()
    proj = (project or "*").strip()
    if not uid:
        raise ValueError("成员账号不能为空")
    if norm_role not in VALID_ROLES:
        raise ValueError(f"无效的角色 {role}（可选：{', '.join(VALID_ROLES)}）")
    ts = now_iso()
    db._exec(
        "INSERT INTO members(user_id, project, role, created_at) VALUES(?, ?, ?, ?)"
        " ON CONFLICT(user_id, project) DO UPDATE SET role=excluded.role",
        (uid, proj, norm_role, ts),
    )
    row = db._query("SELECT * FROM members WHERE user_id=? AND project=?", (uid, proj))[0]
    return dict(row)


def remove_member(db: Database, user_id: str, *, project: str = "*") -> bool:
    cur = db._exec("DELETE FROM members WHERE user_id=? AND project=?", (user_id.strip(), (project or "*").strip()))
    return cur.rowcount > 0


def check_role_permission(
    db: Database,
    actor: str,
    allowed_roles: Iterable[str],
    *,
    project: str = "*",
) -> None:
    """当项目启用了成员表（存在至少一条成员记录）时，强制核验操作者角色在允许集合中。"""
    all_rows = db._query("SELECT user_id, project, role FROM members WHERE project IN (?, '*')",
                         (project,))
    if not all_rows:
        return
    clean_actor = (actor or "").strip()
    allowed = {r.strip().lower() for r in allowed_roles if r.strip()}
    if not allowed:
        return
    specific = [r for r in all_rows if r["user_id"] == clean_actor and r["project"] == project]
    matched_roles = {
        str(r["role"]).lower()
        for r in specific or all_rows
        if str(r["user_id"]) == clean_actor and str(r["project"]) in {project, "*", ""}
    }
    if not matched_roles or not (matched_roles & allowed):
        raise PermissionError(
            f"权限不足：当前用户 '{clean_actor or 'anonymous'}'（角色：{', '.join(sorted(matched_roles)) or '未授权'}）"
            f"无权执行该操作（要求角色：{', '.join(sorted(allowed))}）"
        )
