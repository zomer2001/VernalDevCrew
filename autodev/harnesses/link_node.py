"""Delegate a native Script node to a paired device and attest its exact result."""
import asyncio
import hashlib
import json
import time
from urllib.parse import urlencode

from fastapi import HTTPException

from autodev.loopforge.bridge import sign_context_sig, verify_context_sig
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.loopforge.dispatch import reserve, accept_task, record


def receipt_for(job_id, raw):
    return {"job":job_id,"result_sha256":hashlib.sha256(raw.encode()).hexdigest()}


def node_tick(engine, job_id, signature):
    settings = engine.settings
    if not verify_context_sig(settings.project_signing_key(), settings.loopforge_project_id, "link-node:" + job_id, signature):
        raise HTTPException(403, "无效的节点回执签名")
    with engine.db.transaction():
        rows = engine.db._query("SELECT j.*,h.revoked_at,h.expires_at FROM link_jobs j JOIN host_links h ON h.id=j.link_id WHERE j.id=?", (job_id,))
        if not rows or rows[0]["expires"] <= time.time() or rows[0]["revoked_at"] or rows[0]["expires_at"] <= time.time():
            raise HTTPException(410, "设备任务已取消、撤销或过期")
        job = rows[0]
        if job["state"] == "cancelled":
            raise HTTPException(410, "设备委托已取消")
        if job["state"] in ("done","accepted"):
            return receipt_for(job_id, job["result"])
        engine.db._exec("UPDATE link_jobs SET state='pending' WHERE id=? AND state='awaiting_node'",(job_id,))
        return {"waiting":True}


async def launch(engine, link, job_id, task, role):
    settings = engine.settings
    pid = settings.loopforge_project_id
    client = LoopForgeClient(settings.loopforge_url, settings.effective_loopforge_token())
    # 0.4.0 Script has no input substitution. Its immutable snapshot carries a narrow per-job capability.
    workflow = f"{pid}-link-{job_id}"
    query = urlencode({"project":pid,"sig":sign_context_sig(settings.project_signing_key(), pid, "link-node:"+job_id)})
    import os
    base = os.environ.get("VERNAL_SANDBOX_URL") or settings.vernal_public_url
    url = f"{base.rstrip('/')}/vernal-api/link-node/{job_id}?{query}"
    script = f"""import json,time,urllib.request,urllib.error
from pathlib import Path
deadline=time.monotonic()+{int(settings.agent_timeout_sec)}
while time.monotonic()<deadline:
    try:
        with urllib.request.urlopen({url!r},timeout=8) as response:
            receipt=json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code<500: raise
        time.sleep(1)
        continue
    except OSError:
        time.sleep(1)
        continue
    if not receipt.get('waiting'):
        Path('link-receipt.json').write_text(json.dumps(receipt))
        break
    time.sleep(1)
else:
    raise SystemExit('Windows Worker lease expired')
"""
    await client.put_workflow({"id":workflow,"projectId":pid,"name":"本机 CLI 委托","version":1,
        "description":"配对设备执行，原生节点归档回执摘要。","nodes":[{"id":"delegate","name":"等待设备回执",
        "type":"script","scriptLanguage":"python","script":script,"artifactOutputs":[{"name":"link-receipt.json",
        "path":"link-receipt.json","mediaType":"application/json","required":True}]}],"edges":[]})
    await client.publish_workflow(workflow)
    issue = task.loopforge_issue_id if task else ""
    if not issue:
        created = await client.create_issue(project_id=pid, title=f"本机 CLI · {role}", description="Windows 个人执行器委托",
                                            workflow_id=workflow)
        issue = created["id"]
    row, fresh = reserve(engine.db,"_link:"+job_id,0,1,"local_runner",str(link.get("owner_id") or ""),issue,command="link:"+job_id)
    if not fresh:
        raise LoopForgeError("已有原生节点委托记录",409)
    task_response = await client.dispatch_issue(issue, workflow_id=workflow)
    run_id = await accept_task(engine.db,row,client,task_response,timeout=settings.agent_timeout_sec)
    return client, run_id, row


async def observe_node(client, run_id, project):
    try:
        return await client.project_run(run_id, project)
    except LoopForgeError as exc:
        if exc.status_code in (0, 408, 429) or exc.status_code >= 500:
            return {}
        raise


async def confirm(engine, job_id, raw, handle, expires):
    client, run_id, row = handle
    while time.time() < expires:
        if engine.planning_stop.is_set():
            raise ValueError("Windows 委托已取消")
        run = await observe_node(client, run_id, engine.settings.loopforge_project_id)
        if run.get("status") == "succeeded":
            try:
                raw_receipt = await client.named_artifact(run, "link-receipt.json", node_id="delegate")
            except LoopForgeError as exc:
                if exc.status_code not in (0, 404, 408, 429) and exc.status_code < 500:
                    raise
                await asyncio.sleep(0.2)
                continue
            receipt = json.loads(raw_receipt)
            if receipt != receipt_for(job_id, raw):
                raise ValueError("原生节点与设备回执不一致")
            record(engine.db,row,"FINISHED")
            return
        if run.get("status") in ("failed","cancelled","paused"):
            record(engine.db,row,"FAILED")
            raise ValueError("原生 Windows 委托已终止")
        await asyncio.sleep(0.2)
    raise ValueError("原生 Windows 回执超时")
