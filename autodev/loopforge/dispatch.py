"""Durable command receipts. A lost reply is never permission to repeat a POST."""
from __future__ import annotations

import asyncio

from autodev.db import now_iso
from autodev.loopforge.client import LoopForgeError


def reserve(db, task_id, seq, attempt, pool, actor, issue_id="", run_id="", *, command=""):
    with db.transaction():
        if command:
            rows = db._query("SELECT * FROM dispatch_records WHERE command_key=?", (command,))
        elif run_id and (existing := db._query(
                "SELECT * FROM dispatch_records WHERE task_id=? AND lf_run_id=? ORDER BY dispatch_id DESC LIMIT 1",
                (task_id, run_id))):
            return dict(existing[0]), False
        else:
            rows = db._query(
                "SELECT * FROM dispatch_records WHERE task_id=? "
                "AND ((attempt_no=? AND exec_seq=?) OR state NOT IN ('FINISHED','FAILED')) "
                "ORDER BY dispatch_id DESC LIMIT 1", (task_id, attempt, seq))
        if rows:
            return dict(rows[0]), False
        if command and not seq:
            seq = db._query("SELECT COALESCE(MAX(exec_seq),0)+1 n FROM dispatch_records WHERE task_id=?",
                            (task_id,))[0]["n"]
        now = now_iso()
        cursor = db._exec(
            "INSERT INTO dispatch_records(task_id,exec_seq,attempt_no,pool,actor,issue_id,lf_run_id,state,"
            "command_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, seq, attempt, pool, actor, issue_id or "", run_id or "",
             "RUNNING" if run_id else "DISPATCHING", command or None, now, now))
        return dict(db._query("SELECT * FROM dispatch_records WHERE dispatch_id=?", (cursor.lastrowid,))[0]), True


def record(db, receipt, state, **ids):
    if set(ids) - {"issue_id", "lf_task_id", "lf_run_id"}:
        raise ValueError("Invalid dispatch identity")
    db._exec("UPDATE dispatch_records SET state=?,updated_at=?" + "".join(f",{k}=?" for k in ids)
             + " WHERE dispatch_id=?", (state, now_iso(), *ids.values(), receipt["dispatch_id"]))
    receipt.update(state=state, **ids)


async def observe_task(db, receipt, client, *, stop=None, interval=2.5, timeout=600):
    """Follow only the accepted native Task. Issue history cannot identify an ambiguous rerun."""
    if receipt["lf_run_id"]:
        return receipt["lf_run_id"]
    if not receipt["lf_task_id"]:
        if receipt["issue_id"]:
            await client.list_issue_runs(receipt["issue_id"])
        record(db, receipt, "NEEDS_CONFIRMATION")
        raise LoopForgeError("派单结果待人工确认，请核对原生工单历史", 409)
    deadline = asyncio.get_running_loop().time() + timeout
    while stop is None or not stop.is_set():
        task = await client.get_task(receipt["lf_task_id"])
        if task.get("id") != receipt["lf_task_id"] or task.get("issueId") != receipt["issue_id"]:
            raise LoopForgeError("原生 Task 与持久派单记录不一致", 409)
        if task.get("runId"):
            record(db, receipt, "RUNNING", lf_run_id=task["runId"])
            return task["runId"]
        if task.get("status") in ("failed", "cancelled"):
            record(db, receipt, "FAILED")
            raise LoopForgeError("原生排队任务已终止", 409)
        if asyncio.get_running_loop().time() >= deadline:
            raise LoopForgeError("原生 Task 仍在排队，已保留派单凭据", 503)
        await asyncio.sleep(interval)
    raise LoopForgeError("本地观察已暂停，派单记录已保留", 409)


async def accept_task(db, receipt, client, task, **options):
    native_id = str(task.get("id") or "")
    run = str(task.get("runId") or task.get("latestRunId") or (task.get("run") or {}).get("id") or "")
    if task.get("issueId", receipt["issue_id"]) != receipt["issue_id"] or not (native_id or run):
        record(db, receipt, "NEEDS_CONFIRMATION")
        raise LoopForgeError("未取得对应工单的原生派单凭据", 409)
    record(db, receipt, "RUNNING" if run else "QUEUED", lf_task_id=native_id, lf_run_id=run)
    return await observe_task(db, receipt, client, **options)
