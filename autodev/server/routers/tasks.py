"""任务监视、实时事件流、里程碑交付与历史记录路由。"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import mimetypes
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from autodev import gitops, local_git_bridge
from autodev.artifacts import FileExchange
from autodev.engine import Engine, OpsError
from autodev.harnesses.base import extract_json
from autodev.harnesses.codex_cli import parse_events_jsonl
from autodev.loopforge.bridge import verify_context_sig
from autodev.loopforge.client import LoopForgeError
from autodev.server.deps import effective_actor, get_ctx, is_loopback
from autodev.server.history import event_page, history_sources
from autodev.server.schemas import ActorRequest, SteerRequest

router = APIRouter()

SPECIAL_ROLES: dict[str, tuple[str, str, str]] = {
    "_pm": ("产品经理 · PM", "pm", "补全产品业务流程、异常边界与验收红线（docs/PRD_SPEC.md）"),
    "_planner": ("研发总监 · 立项蓝图", "director", "划分研发组领地、制定里程碑路线图与首轮任务 DAG"),
    "_director": ("研发总监 · 滚动排期与仲裁", "director", "结合主干真实代码滚动拆解下一里程碑，并在阻塞时自动仲裁"),
    "_council_pm": ("圆桌 · 产品经理 (PM)", "pm", "架构评审圆桌第 1 轮：业务边界、异常流与验收红线提案"),
    "_council_architect": ("圆桌 · 契约与异构架构师", "architect", "架构评审圆桌第 1 轮：跨语言契约、模块领地与异构沙箱提案"),
    "_council_qa_lead": ("圆桌 · QA 质量总监", "qa_lead", "架构评审圆桌第 1 轮：可测性审查、集成风险与门禁提案"),
    "_council_director": ("圆桌 · 研发总监裁决 (ADR)", "director", "架构评审圆桌第 2 轮：回应分歧并拍板 ADR 决策纪要"),
}


def _planner_runs(eng: Engine) -> list[dict[str, Any]]:
    runs = eng.db.list_runs("_planner")
    if runs:
        return runs
    pdir = eng.ws.runs_dir / "_planner"
    if not pdir.exists():
        return []
    fallback = []
    for idx, sub in enumerate(sorted(pdir.glob("attempt-*")), 1):
        fallback.append(
            {
                "run_id": -idx,
                "task_id": "_planner",
                "attempt_no": idx,
                "stage": "PLAN",
                "harness_name": eng.settings.harness,
                "session_id": None,
                "exit_code": 0 if (sub / "last_message.txt").exists() else None,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "verdict": "DONE" if (sub / "last_message.txt").exists() else "RUNNING",
                "summary_log": "",
                "log_path": str(sub),
                "started_at": "",
                "finished_at": "",
            }
        )
    return fallback


def _special_runs(eng: Engine, task_id: str) -> list[dict[str, Any]]:
    runs = eng.db.list_runs(task_id)
    if runs:
        return runs
    if task_id == "_planner":
        return _planner_runs(eng)
    return []


def _resolve_log_path(eng: Engine, task_id: str, run: dict[str, Any]) -> Path | None:
    if run.get("log_path"):
        p = Path(run["log_path"])
        if p.exists():
            return p
    stage = (run.get("stage") or "").lower()
    attempt = run.get("attempt_no", 1)
    if task_id == "_planner":
        cand = eng.ws.runs_dir / "_planner" / f"attempt-{attempt}"
        return cand if cand.exists() else None
    if stage == "verify":
        cand = eng.ws.runs_dir / task_id / f"attempt-{attempt}-verify.log"
        return cand if cand.exists() else None
    cand = eng.ws.runs_dir / task_id / f"attempt-{attempt}-{stage}"
    return cand if cand.exists() else None


def _read_review_verdict(log_path: Path | None) -> dict[str, Any] | None:
    if log_path is None or not log_path.is_dir():
        return None
    vf = log_path / "verdict.json"
    if vf.exists():
        try:
            return json.loads(vf.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    lm = log_path / "last_message.txt"
    if lm.exists():
        parsed = extract_json(lm.read_text("utf-8", errors="replace"))
        if isinstance(parsed, dict) and "verdict" in parsed:
            return parsed
    return None


def _enrich_run(eng: Engine, task_id: str, run: dict[str, Any]) -> dict[str, Any]:
    out = dict(run)
    lp = _resolve_log_path(eng, task_id, run)
    out["resolved_log_path"] = str(lp) if lp else None
    out["has_prompt"] = bool(lp and lp.is_dir() and (lp / "prompt.md").exists())
    out["has_events"] = bool(lp and lp.is_dir() and (lp / "events.jsonl").exists())
    out["has_verify_log"] = bool(lp and lp.is_file())
    if run.get("stage") == "REVIEW":
        out["review_verdict"] = _read_review_verdict(lp)
    elif run.get("stage") == "VERIFY" and lp and lp.is_file():
        out["verify_log"] = lp.read_text("utf-8", errors="replace")[:30_000]
    return out


def format_file_patch_diff(patch: dict[str, Any]) -> tuple[str, str]:
    stats: list[str] = []
    blocks: list[str] = []
    for path in sorted(patch):
        old_val, new_val = patch[path]
        old_lines = (old_val or "").splitlines(keepends=True)
        new_lines = (new_val or "").splitlines(keepends=True)
        tag = "A" if old_val is None else ("D" if new_val is None else "M")
        stats.append(f"{tag}\t{path}")
        blocks.append(f"diff --git a/{path} b/{path}")
        blocks.extend(
            ln.rstrip("\n")
            for ln in difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}" if old_val is not None else "/dev/null",
                tofile=f"b/{path}" if new_val is not None else "/dev/null",
            )
        )
    return "\n".join(blocks), "\n".join(stats)


@router.get("/vernal-api/extensions/issues/{issue_id}")
def issue_extensions(issue_id: str, request: Request):
    from autodev.docs_center.service import resolve_design_refs_snippet
    eng = get_ctx(request).active_slot(request).engine
    tasks = [t for t in eng.db.list_tasks() if t.loopforge_issue_id == issue_id]
    return [{"task_id": t.task_id, "acceptance": t.acceptance_criteria,
             "design": resolve_design_refs_snippet(eng.ws.root, t.design_refs),
             "reviews": [dict(r) for r in eng.db._query(
                 "SELECT review_id,round,status FROM code_reviews WHERE scope_type='task' AND scope_id=? ORDER BY round DESC",
                 (t.task_id,))]} for t in tasks]


@router.get("/vernal-api/extensions/runs/{run_id}")
def run_extensions(run_id: str, request: Request):
    db = get_ctx(request).active_slot(request).engine.db
    return [dict(r) for r in db._query(
        "SELECT DISTINCT q.* FROM quality_results q JOIN dispatch_records d "
        "ON d.task_id=q.task_id AND d.attempt_no=q.attempt WHERE d.lf_run_id=? ORDER BY q.id", (run_id,))]


@router.get("/vernal-api/tasks/{task_id}")
async def api_task_detail(task_id: str, request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if task_id in SPECIAL_ROLES:
        title, role_key, desc = SPECIAL_ROLES[task_id]
        runs = [_enrich_run(eng, task_id, r) for r in _special_runs(eng, task_id)]
        return {
            "task": {
                "task_id": task_id,
                "milestone_id": "-",
                "squad": "顶层指挥组",
                "module": role_key,
                "role": role_key,
                "title": title,
                "description": desc,
                "acceptance_criteria": [],
                "allowed_paths": ["（只读沙箱，不修改业务代码）"],
                "verify_cmds": [],
                "status": "RUNNING" if (ctx.is_planning(slot) and task_id in ("_pm", "_planner")) else eng.status.value,
                "branch_name": None,
                "worktree_path": None,
                "failure_reason": None,
            },
            "runs": runs,
            "events": eng.db.events_after(0, limit=400, task_id=task_id)[-150:],
            "diff_stat": "",
        }

    task = eng.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    def _compute_diffstat() -> str:
        if task.worktree_path and task.base_sha and Path(task.worktree_path).exists():
            try:
                return gitops.working_diff_stat(Path(task.worktree_path), task.base_sha)
            except Exception:
                return ""
        if task.base_sha and task.candidate_commit_sha:
            try:
                return gitops.git(["diff", "--stat", f"{task.base_sha}..{task.candidate_commit_sha}"], eng.ws.root, check=False).stdout.strip()
            except Exception:
                return ""
        return ""

    diffstat = await asyncio.to_thread(_compute_diffstat)
    d = task.to_dict()
    d["status"] = task.status.value
    runs = [_enrich_run(eng, task_id, r) for r in eng.db.list_runs(task_id)]
    return {
        "task": d,
        "runs": runs,
        "events": eng.db.events_after(0, limit=400, task_id=task_id)[-150:],
        "diff_stat": diffstat,
    }


_NODE_DEFAULT_TITLES = {
    "implement": "模块编码与自测",
    "package": "打包校验交付清单",
    "review": "独立契约与测试审查",
    "verify": "集成验证",
    "approve": "人工确认签字",
}


def _dedup_echoed_cmd(text: str) -> str:
    """LoopForge codex-jsonl-v1 sometimes duplicates short shell commands as '<cmd> <cmd>'."""
    s = (text or "").strip()
    if len(s) >= 12 and len(s) % 2 == 1:
        mid = len(s) // 2
        if s[mid] == " " and s[:mid] == s[mid + 1 :]:
            return s[:mid]
    return s


def _summarize_tool_body(body: str) -> tuple[str, str]:
    """Return (concise Chinese title, cleaned command/tool body)."""
    cleaned = _dedup_echoed_cmd(body)
    stripped = cleaned
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -lc ", "sh -lc "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
            if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
                stripped = stripped[1:-1]
            break
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    if "pytest" in first_line or "unittest" in first_line or "npm test" in first_line:
        label = "运行测试"
    elif first_line.startswith(("python", "python3", "node ")) and ("<<" in first_line or "-c" in first_line):
        label = "执行脚本"
    elif first_line.startswith(("rg ", "grep ", "find ", "ls ", "cat ", "head ", "tail ", "sed ", "pwd")):
        label = "检查代码与文件"
    elif first_line.startswith(("mkdir ", "cp ", "mv ", "rm ", "touch ", "git ")):
        label = "文件/仓库操作"
    elif "submit_gate_result" in cleaned or "mcp" in first_line.lower():
        label = "调用工具"
    else:
        label = "执行命令"
    preview = first_line[:96] + ("…" if len(first_line) > 96 else "")
    title = f"{label} · {preview}" if preview else label
    return title, stripped or cleaned


def _classify_loopforge_log(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    ctx = item.get("context") if isinstance(item.get("context"), dict) else {}
    src = item.get("source") if isinstance(item.get("source"), dict) else {}
    stream = str(item.get("stream") or "").lower()
    level = str(item.get("level") or "info").lower()
    cat = str(attrs.get("category") or "").lower()
    ev_name = str(attrs.get("eventName") or "").lower()
    phase = str(attrs.get("phase") or "").lower()
    status = str(attrs.get("status") or "").lower()
    body = str(item.get("body") or item.get("message") or item.get("line") or "").strip()
    node_id = str(ctx.get("nodeId") or item.get("nodeId") or "_run")
    ts = str(item.get("timestamp") or "")
    seq = int(item.get("sequence") or 0)
    provider = str(ctx.get("executorId") or src.get("provider") or "")

    if "reason" in cat or "think" in cat or "reason" in ev_name or "think" in ev_name:
        kind, title = "thought", "模型思考"
    elif cat == "tool" or ev_name.startswith("tool."):
        kind = "tool"
        title, body = _summarize_tool_body(body)
    elif cat == "output" or ev_name in ("agent.output", "agent.message"):
        kind, title = "message", "智能体进展与结论"
    elif stream == "stdout" or ev_name == "process.stdout":
        kind, title = "stdout", "标准输出"
    elif stream == "stderr" or level == "error":
        kind, title = "stderr", "错误 / 诊断输出"
    elif stream == "platform" or src.get("layer") in ("sandbox", "orchestrator"):
        kind, title = "system", "平台调度"
    else:
        # Heuristic fallback for worker lines without structured attributes
        if body.startswith(("/bin/zsh", "/bin/bash", "/bin/sh", "sh -lc")):
            kind = "tool"
            title, body = _summarize_tool_body(body)
        elif body.lower() in ("codex thread initialized",) or body.startswith(("执行机器已启动", "执行器退出")):
            kind, title = "system", "执行器状态"
        else:
            kind, title = "message", "执行日志"

    return {
        "id": str(item.get("id") or f"seq-{seq}"),
        "sequence": seq,
        "node_id": node_id,
        "kind": kind,
        "title": title,
        "body": body,
        "timestamp": ts,
        "phase": phase,
        "status": status,
        "level": level,
        "stream": stream,
        "provider": provider,
    }


def _parse_loopforge_node_streams(lf_run: dict[str, Any], raw_logs: Any) -> list[dict[str, Any]]:
    from autodev.server.routers.runtime import _extract_ordered_nodes

    ordered = _extract_ordered_nodes(lf_run)
    by_node: dict[str, dict[str, Any]] = {}
    for n in ordered:
        entry = dict(n)
        entry["items"] = []
        entry["system_count"] = 0
        by_node[entry["id"]] = entry

    log_items = raw_logs.get("logs") if isinstance(raw_logs, dict) and isinstance(raw_logs.get("logs"), list) else raw_logs
    if isinstance(log_items, list):
        for raw in log_items:
            if not isinstance(raw, dict):
                continue
            parsed = _classify_loopforge_log(raw)
            if not parsed["body"]:
                continue
            nid = parsed["node_id"]
            if nid not in by_node:
                if ordered and nid == "_run":
                    nid = ordered[0]["id"]
                else:
                    by_node[nid] = {
                        "id": nid,
                        "title": _NODE_DEFAULT_TITLES.get(nid, nid),
                        "type": "",
                        "status": "running",
                        "provider": parsed["provider"],
                        "model": "",
                        "tool_calls": 0,
                        "activity": "",
                        "output_preview": "",
                        "started_at": parsed["timestamp"],
                        "finished_at": "",
                        "elapsed_sec": None,
                        "error": "",
                        "items": [],
                        "system_count": 0,
                    }
                    ordered.append(by_node[nid])
            target = by_node[nid]
            # Collapse back-to-back duplicate tool invocations (e.g. start + finish with identical command)
            if (
                parsed["kind"] == "tool"
                and target["items"]
                and target["items"][-1]["kind"] == "tool"
                and target["items"][-1]["body"] == parsed["body"]
            ):
                if parsed["status"]:
                    target["items"][-1]["status"] = parsed["status"]
                if parsed["phase"]:
                    target["items"][-1]["phase"] = parsed["phase"]
                continue
            target["items"].append(parsed)
            if parsed["kind"] == "system":
                target["system_count"] += 1
    elif isinstance(raw_logs, dict) and raw_logs.get("text"):
        target_id = ordered[0]["id"] if ordered else "implement"
        if target_id not in by_node:
            by_node[target_id] = {
                "id": target_id,
                "title": _NODE_DEFAULT_TITLES.get(target_id, target_id),
                "type": "",
                "status": str(lf_run.get("status") or "running"),
                "provider": "",
                "model": "",
                "tool_calls": 0,
                "activity": "",
                "output_preview": "",
                "started_at": "",
                "finished_at": "",
                "elapsed_sec": None,
                "error": "",
                "items": [],
                "system_count": 0,
            }
            ordered.append(by_node[target_id])
        by_node[target_id]["items"].append(
            {
                "id": "text-0",
                "sequence": 1,
                "node_id": target_id,
                "kind": "stdout",
                "title": "执行日志",
                "body": str(raw_logs["text"])[-12000:],
                "timestamp": "",
                "phase": "",
                "status": "",
                "level": "info",
                "stream": "stdout",
                "provider": "",
            }
        )

    return [by_node[n["id"]] for n in ordered if n["id"] in by_node]


def _resolve_task_lf_run_id(eng: Engine, task_id: str, task: Any, runs: list[dict[str, Any]], selected: dict[str, Any] | None = None) -> str:
    lf_run_id = task.loopforge_run_id if task is not None else ""
    if not lf_run_id:
        rows = eng.db._query(
            "SELECT lf_run_id FROM dispatch_records WHERE task_id=? AND lf_run_id!='' ORDER BY dispatch_id DESC LIMIT 1",
            (task_id,),
        )
        if rows:
            lf_run_id = str(rows[0]["lf_run_id"] or "")
    cand_run = selected or (runs[-1] if runs else None)
    if not lf_run_id and cand_run is not None:
        lp = _resolve_log_path(eng, task_id, cand_run)
        if lp and lp.is_dir() and (lp / "loopforge_run.json").is_file():
            try:
                lf_run_id = str(json.loads((lp / "loopforge_run.json").read_text("utf-8")).get("run_id") or "")
            except Exception:
                lf_run_id = ""
        if not lf_run_id and cand_run.get("session_id"):
            lf_run_id = str(cand_run["session_id"])
    return lf_run_id


@router.get("/vernal-api/tasks/{task_id}/stream")
async def api_task_stream(task_id: str, request: Request) -> dict[str, Any]:
    """返回任务按 SOP 节点分组的思考链、工具调用与终端输出实时流。"""
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    runs = _special_runs(eng, task_id) if task_id in SPECIAL_ROLES else eng.db.list_runs(task_id)
    task = None if task_id in SPECIAL_ROLES else eng.db.get_task(task_id)
    if task_id not in SPECIAL_ROLES and task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    lf_run_id = _resolve_task_lf_run_id(eng, task_id, task, runs)
    if lf_run_id and eng.settings.effective_loopforge_token():
        from autodev.loopforge.bridge import LoopForgeBridge

        bridge = LoopForgeBridge(eng.settings, eng.db)
        bridge.client = ctx.lf_client(slot)
        try:
            client = bridge.client_for_run(lf_run_id)
        except LoopForgeError:
            client = bridge.client
        try:
            lf_run, raw_logs = await asyncio.gather(
                asyncio.wait_for(client.get_run(lf_run_id), timeout=4.0),
                asyncio.wait_for(client.get_run_logs(lf_run_id), timeout=4.0),
            )
            nodes = _parse_loopforge_node_streams(lf_run if isinstance(lf_run, dict) else {}, raw_logs)
            return {
                "task_id": task_id,
                "source": "loopforge",
                "run_id": lf_run_id,
                "run_status": str((lf_run or {}).get("status") or ""),
                "tool_calls_count": int((lf_run or {}).get("toolCalls") or 0),
                "nodes": nodes,
                "fallback_events": eng.db.events_after(0, limit=100, task_id=task_id)[-40:],
            }
        except Exception as exc:
            return {
                "task_id": task_id,
                "source": "loopforge_error",
                "run_id": lf_run_id,
                "run_status": "",
                "tool_calls_count": 0,
                "error": str(exc),
                "nodes": [],
                "fallback_events": eng.db.events_after(0, limit=100, task_id=task_id)[-40:],
            }

    # Local execution fallback: convert local events.jsonl runs into the same node-grouped stream shape
    local_nodes: list[dict[str, Any]] = []
    for r in runs[-4:]:
        lp = _resolve_log_path(eng, task_id, r)
        stage = str(r.get("stage") or "CODE").lower()
        items: list[dict[str, Any]] = []
        if lp and lp.is_dir() and (lp / "events.jsonl").exists():
            for idx, st in enumerate(parse_events_jsonl(lp / "events.jsonl"), 1):
                sk = st.get("kind") or "message"
                if sk == "reasoning":
                    items.append({"id": f"l-{idx}", "sequence": idx, "node_id": stage, "kind": "thought", "title": "模型思考", "body": str(st.get("text") or ""), "timestamp": "", "phase": "", "status": "", "level": "info", "stream": "worker", "provider": str(r.get("harness_name") or "")})
                elif sk == "command":
                    ctitle, cbody = _summarize_tool_body(str(st.get("command") or ""))
                    if st.get("output"):
                        cbody = f"{cbody}\n\n--- 输出 ---\n{str(st['output'])[:3000]}"
                    items.append({"id": f"l-{idx}", "sequence": idx, "node_id": stage, "kind": "tool", "title": ctitle, "body": cbody, "timestamp": "", "phase": str(st.get("phase") or ""), "status": "completed" if st.get("exit_code") == 0 else ("failed" if st.get("exit_code") not in (None, 0) else "in_progress"), "level": "info", "stream": "worker", "provider": str(r.get("harness_name") or "")})
                elif sk == "file":
                    changes = "\n".join(f"{c.get('kind', 'edit')}: {c.get('path', '')}" for c in (st.get("changes") or []))
                    items.append({"id": f"l-{idx}", "sequence": idx, "node_id": stage, "kind": "tool", "title": f"文件变更 · {len(st.get('changes') or [])} 个", "body": changes, "timestamp": "", "phase": "finish", "status": "completed", "level": "info", "stream": "worker", "provider": str(r.get("harness_name") or "")})
                elif sk == "message":
                    items.append({"id": f"l-{idx}", "sequence": idx, "node_id": stage, "kind": "message", "title": "智能体进展与结论", "body": str(st.get("text") or ""), "timestamp": "", "phase": "finish", "status": "", "level": "info", "stream": "worker", "provider": str(r.get("harness_name") or "")})
                elif sk == "warning":
                    items.append({"id": f"l-{idx}", "sequence": idx, "node_id": stage, "kind": "stderr", "title": "告警提示", "body": str(st.get("text") or ""), "timestamp": "", "phase": "", "status": "", "level": "warn", "stream": "stderr", "provider": str(r.get("harness_name") or "")})
        elif lp and lp.is_file():
            items.append({"id": "v-1", "sequence": 1, "node_id": stage, "kind": "stdout", "title": "验证输出", "body": lp.read_text("utf-8", errors="replace")[:12000], "timestamp": "", "phase": "", "status": "", "level": "info", "stream": "stdout", "provider": "terminal"})
        verdict = str(r.get("verdict") or "")
        st_status = "succeeded" if verdict in ("PASS", "DONE", "APPROVE") else ("failed" if verdict in ("FAIL", "NEEDS_FIX", "ERROR") else "running")
        local_nodes.append({
            "id": f"{stage}-{r.get('attempt_no', 1)}",
            "title": f"#{r.get('attempt_no', 1)} {r.get('stage', 'EXEC')}",
            "type": stage,
            "status": st_status,
            "provider": str(r.get("harness_name") or ""),
            "model": "",
            "tool_calls": sum(1 for it in items if it["kind"] == "tool"),
            "activity": "",
            "output_preview": str(r.get("summary_log") or "")[:240],
            "started_at": str(r.get("started_at") or ""),
            "finished_at": str(r.get("finished_at") or ""),
            "elapsed_sec": None,
            "error": "",
            "items": items,
            "system_count": 0,
        })
    return {
        "task_id": task_id,
        "source": "local",
        "run_id": "",
        "run_status": task.status.value if task else eng.status.value,
        "tool_calls_count": sum(n["tool_calls"] for n in local_nodes),
        "nodes": local_nodes,
        "fallback_events": eng.db.events_after(0, limit=100, task_id=task_id)[-40:],
    }


@router.get("/vernal-api/tasks/{task_id}/trace")
async def api_task_trace(task_id: str, request: Request, run_id: int | None = Query(default=None)) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    runs = _special_runs(eng, task_id) if task_id in SPECIAL_ROLES else eng.db.list_runs(task_id)
    task = None if task_id in SPECIAL_ROLES else eng.db.get_task(task_id)
    if task_id not in SPECIAL_ROLES and task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    selected = None
    if run_id is not None:
        selected = next((r for r in runs if r["run_id"] == run_id), None)
    else:
        for r in reversed(runs):
            lp = _resolve_log_path(eng, task_id, r)
            if lp and lp.is_dir() and (lp / "events.jsonl").exists():
                selected = r
                break
    if selected is not None:
        lp = _resolve_log_path(eng, task_id, selected)
        if lp and lp.is_dir() and (lp / "events.jsonl").exists():
            return {"run": selected, "steps": parse_events_jsonl(lp / "events.jsonl")}

    lf_run_id = _resolve_task_lf_run_id(eng, task_id, task, runs, selected)
    if lf_run_id and eng.settings.effective_loopforge_token():
        steps: list[dict[str, Any]] = []
        from autodev.loopforge.bridge import LoopForgeBridge
        bridge = LoopForgeBridge(eng.settings, eng.db)
        bridge.client = ctx.lf_client(slot)
        try:
            client = bridge.client_for_run(lf_run_id)
        except LoopForgeError:
            client = bridge.client
        try:
            from autodev.server.routers.runtime import project_run
            lf_run = await project_run(ctx, slot, lf_run_id, client=client)
            logs = await client.get_run_logs(lf_run_id)
            nodes = _parse_loopforge_node_streams(lf_run, logs)
            steps.append(
                {
                    "kind": "turn",
                    "summary": f"LoopForge SOP Run {lf_run_id} · 状态 {lf_run.get('status', '-')}",
                }
            )
            for node in nodes:
                nst = node.get("status") or "pending"
                steps.append(
                    {
                        "kind": "turn",
                        "summary": f"[节点] {node.get('title') or node.get('id')} ({nst})",
                    }
                )
                for it in node.get("items") or []:
                    if it["kind"] == "system":
                        continue
                    if it["kind"] == "thought":
                        steps.append({"kind": "reasoning", "text": it["body"]})
                    elif it["kind"] == "message":
                        steps.append({"kind": "message", "text": it["body"]})
                    elif it["kind"] == "tool":
                        steps.append({
                            "kind": "command",
                            "phase": "started" if it.get("status") == "in_progress" else "done",
                            "command": it["body"],
                            "exit_code": 0 if it.get("status") == "completed" else (1 if it.get("status") == "failed" else None),
                            "output": "",
                        })
                    elif it["kind"] in ("stdout", "stderr"):
                        steps.append({"kind": "warning" if it["kind"] == "stderr" else "message", "text": it["body"]})
        except LoopForgeError as exc:
            steps.append({"kind": "warning", "text": f"读取实时执行日志失败：{exc}"})
        return {
            "run": selected or {
                "run_id": 0,
                "attempt_no": max(1, (task.retry_count if task is not None else 0) + 1),
                "stage": "LOOPFORGE_SOP",
                "verdict": task.status.value if task is not None else "LOOPFORGE",
                "loopforge_run_id": lf_run_id,
            },
            "steps": steps,
        }
    return {"run": selected, "steps": []}


@router.get("/vernal-api/git-snapshots/{snapshot}.pack")
def api_git_snapshot(snapshot: str, project: str, sig: str, request: Request) -> FileResponse:
    eng = get_ctx(request).active_slot(request).engine
    if (
        not re.fullmatch(r"[0-9a-f]{40,64}", snapshot)
        or project != eng.settings.loopforge_project_id
        or not any(verify_context_sig(key, project, f"git:{snapshot}", sig) for key in eng.settings.verification_keys())
    ):
        raise HTTPException(403, "项目或快照签名无效")
    path = eng.ws.state_dir / "git-snapshots" / f"{snapshot}.pack"
    if not path.is_file():
        raise HTTPException(404, "任务快照不存在")
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.get("/vernal-api/tasks/{task_id}/baseline.json")
def api_task_baseline(task_id: str, request: Request) -> dict[str, str]:
    eng = get_ctx(request).active_slot(request).engine
    task = eng.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return FileExchange(eng.ws, eng.db).baseline(task.depends_on)


@router.get("/vernal-api/plan-context/{stage_key}.json")
def api_plan_context(stage_key: str, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    if not re.fullmatch(r"[0-9A-Za-z._\-]{1,64}", stage_key):
        raise HTTPException(status_code=400, detail="非法的规划上下文标识")
    p = eng.ws.state_dir / "baselines" / f"plan-{stage_key}.json"
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"规划上下文不存在: {stage_key}")
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取规划上下文失败: {exc}") from exc


@router.get("/vernal-api/tasks/{task_id}/diff")
async def api_task_diff(task_id: str, request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if task_id in SPECIAL_ROLES:
        return {"diff": "", "diff_stat": "", "source": "readonly"}
    task = eng.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.artifact_digest:
        patch = FileExchange(eng.ws, eng.db).get(task.artifact_digest)
        diff_text, stat_text = format_file_patch_diff(patch)
        return {"source": "files", "diff_stat": stat_text, "diff": diff_text}
    if task.loopforge_run_id and eng.settings.effective_loopforge_token():
        try:
            from autodev.server.routers.runtime import project_run
            from autodev.loopforge.bridge import LoopForgeBridge
            bridge = LoopForgeBridge(eng.settings, eng.db)
            bridge.client = ctx.lf_client(slot)
            client = bridge.client_for_run(task.loopforge_run_id)
            await project_run(ctx, slot, task.loopforge_run_id, client=client)
            lf_diff = await client.get_run_diff(task.loopforge_run_id)
            if isinstance(lf_diff, dict):
                raw_diff = str(lf_diff.get("diff") or lf_diff.get("patch") or lf_diff.get("text") or "")
                stat = str(lf_diff.get("stat") or lf_diff.get("summary") or "")
            else:
                raw_diff, stat = str(lf_diff or ""), ""
            if raw_diff:
                return {"source": f"loopforge:{task.loopforge_run_id[:10]}", "diff_stat": stat, "diff": raw_diff}
        except LoopForgeError:
            pass
    if task.worktree_path and task.base_sha and Path(task.worktree_path).exists():
        wt = Path(task.worktree_path)
        diff_txt, stat_txt = await asyncio.to_thread(
            lambda: (gitops.working_diff(wt, task.base_sha), gitops.working_diff_stat(wt, task.base_sha))
        )
        return {"diff": diff_txt, "diff_stat": stat_txt, "source": "worktree"}
    if task.base_sha and task.candidate_commit_sha:
        diff_txt, stat_txt = await asyncio.to_thread(
            lambda: (
                gitops.commit_diff(eng.ws.root, task.base_sha, task.candidate_commit_sha),
                gitops.git(["diff", "--stat", f"{task.base_sha}..{task.candidate_commit_sha}"], eng.ws.root, check=False).stdout.strip(),
            )
        )
        return {"diff": diff_txt, "diff_stat": stat_txt, "source": "commit"}
    return {"diff": "", "diff_stat": "", "source": "none"}


@router.get("/vernal-api/tasks/{task_id}/runs/{run_id}/artifacts")
def api_run_artifacts(task_id: str, run_id: int, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    runs = _special_runs(eng, task_id) if task_id in SPECIAL_ROLES else eng.db.list_runs(task_id)
    run = next((r for r in runs if r["run_id"] == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    lp = _resolve_log_path(eng, task_id, run)
    if lp is None:
        return {"run": run, "prompt": "", "command": "", "stderr": "", "verify_log": "", "last_message": "", "review_verdict": None}

    def _read(path: Path, limit: int = 60_000) -> str:
        if not path.exists() or not path.is_file():
            return ""
        txt = path.read_text("utf-8", errors="replace")
        return txt[:limit] + ("\n…（内容过长已截断）" if len(txt) > limit else "")

    if lp.is_file():
        return {"run": run, "prompt": "", "command": "", "stderr": "", "verify_log": _read(lp), "last_message": "", "review_verdict": None}
    return {
        "run": run,
        "prompt": _read(lp / "prompt.md"),
        "command": _read(lp / "command.json"),
        "stderr": _read(lp / "stderr.log"),
        "verify_log": "",
        "last_message": _read(lp / "last_message.txt"),
        "review_verdict": _read_review_verdict(lp),
    }


@router.post("/vernal-api/tasks/{task_id}/steer")
def api_steer(task_id: str, request: Request, req: SteerRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        eng.steer(task_id, note=req.note, actor=effective_actor(req.actor), verify_only=req.verify_only)
    except OpsError as exc:
        raise HTTPException(status_code=409 if req.verify_only else 400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/vernal-api/tasks/{task_id}/restart")
async def api_task_restart(task_id: str, request: Request, start_scheduler: bool = True) -> dict[str, Any]:
    """一键安全重置卡滞/失败/熔断的任务并触发调度器用最新工作流重新派发。"""
    from autodev.db import now_iso
    from autodev.loopforge.bridge import LoopForgeBridge
    from autodev.models import ProjectStatus
    from autodev.server.routers.planning import start_slot_scheduler

    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    task = eng.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status.value == "MERGED":
        raise HTTPException(status_code=409, detail=f"{task_id} 已合入主干，不能重启")

    old_run_id = task.loopforge_run_id or ""
    paused_remote = False
    if old_run_id and eng.settings.runtime_backend == "loopforge" and eng.settings.effective_loopforge_token():
        try:
            bridge = LoopForgeBridge(eng.settings, eng.db)
            bridge.client = ctx.lf_client(slot)
            try:
                run_client = bridge.client_for_run(old_run_id)
            except LoopForgeError:
                run_client = bridge.client
            await asyncio.wait_for(run_client.pause_run(old_run_id), timeout=4.0)
            paused_remote = True
        except Exception:
            paused_remote = False

    actor = effective_actor()
    now = now_iso()
    with eng.db.transaction():
        eng.db._exec(
            "UPDATE dispatch_records SET state='FAILED', updated_at=? "
            "WHERE task_id=? AND state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION','PAUSED')",
            (now, task_id),
        )
        eng.db._exec(
            "UPDATE worker_slots SET status='IDLE', task_id=NULL, current_action=NULL, started_at=NULL, updated_at=? WHERE task_id=?",
            (now, task_id),
        )
        eng.db._exec(
            "UPDATE tasks SET status='READY', loopforge_run_id=NULL, assigned_slot=NULL, active_agent_label=NULL, "
            "failure_reason=NULL, exec_seq=exec_seq+1, updated_at=? WHERE task_id=?",
            (now, task_id),
        )
        if eng.status == ProjectStatus.STALLED:
            eng.db.meta_set("status", ProjectStatus.RUNNING.value)
    eng.db.add_event(
        "task.restarted",
        f"已重置任务 {task_id}（原 Run：{old_run_id or '无'}），等待使用最新工作流重新派发",
        task_id=task_id,
        actor=actor,
    )

    scheduler_started = False
    if start_scheduler and not ctx.is_scheduler_running(slot):
        try:
            scheduler_started = start_slot_scheduler(ctx, slot, watch=True)
        except (OpsError, Exception):
            scheduler_started = False
    return {
        "ok": True,
        "task_id": task_id,
        "previous_run_id": old_run_id,
        "paused_remote": paused_remote,
        "scheduler_running": ctx.is_scheduler_running(slot),
        "scheduler_started": scheduler_started,
    }


@router.get("/vernal-api/milestones/{milestone_id}/report")
def api_milestone_report(milestone_id: str, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    ms = eng.db.get_milestone(milestone_id)
    if ms is None:
        raise HTTPException(status_code=404, detail=f"里程碑不存在: {milestone_id}")
    content = ""
    if ms.report_path and Path(ms.report_path).exists():
        content = Path(ms.report_path).read_text("utf-8")
    return {
        "milestone_id": ms.milestone_id,
        "status": ms.status.value,
        "commit_matrix": json.loads(ms.commit_matrix_json) if ms.commit_matrix_json else {},
        "report": content,
    }


def _verified_files(eng: Engine, milestone_id: str, version: int | None = None) -> dict[str, str]:
    if version is not None:
        for delivery in eng.db.list_milestone_deliveries(milestone_id):
            if delivery["version_no"] == version and delivery["artifact_digest"]:
                return FileExchange(eng.ws, eng.db).get(delivery["artifact_digest"])
        raise HTTPException(404, "该文件交付版本不存在")
    milestone = eng.db.get_milestone(milestone_id)
    key = eng.db.meta_get(f"delivery:{milestone_id}")
    if not milestone or milestone.status != "MERGED" or not key:
        raise HTTPException(404, "尚无已验收的文件交付")
    return FileExchange(eng.ws, eng.db).get(key)


@router.get("/vernal-api/preview/{project}/{milestone_id}/{path:path}")
def release_preview(project: str, milestone_id: str, path: str, request: Request) -> Response:
    eng = get_ctx(request).active_slot(request).engine
    files = _verified_files(eng, milestone_id)
    if path not in files:
        raise HTTPException(404, "交付包中没有这个文件")
    return Response(
        files[path],
        media_type=mimetypes.guess_type(path)[0] or "text/plain",
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/vernal-api/milestones/{milestone_id}/export-git")
async def export_git(milestone_id: str, req: dict[str, str], request: Request) -> dict:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    project = eng.settings.loopforge_project_id or slot.project_key
    files = _verified_files(eng, milestone_id)
    try:
        branch = await local_git_bridge.export_release(
            project, milestone_id, files, req.get("url", ""), req.get("username", ""), req.get("token", "")
        )
    except (ValueError, OSError, gitops.GitError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"branch": branch}


@router.get("/vernal-api/milestones/{milestone_id}/files.zip")
def download_files(milestone_id: str, request: Request, version: int | None = None) -> Response:
    eng = get_ctx(request).active_slot(request).engine
    files = _verified_files(eng, milestone_id, version)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return Response(
        stream.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vernal-delivery.zip"'},
    )


@router.post("/vernal-api/milestones/{milestone_id}/merge")
def api_merge_milestone(milestone_id: str, request: Request, req: ActorRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        eng.merge_milestone(milestone_id, actor=effective_actor(req.actor))
    except (OpsError, gitops.GitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": eng.status.value, "delivery_branch": eng.db.meta_get(f"delivery_branch:{milestone_id}")}


@router.post("/vernal-api/milestones/{milestone_id}/reintegrate")
def api_reintegrate(milestone_id: str, request: Request, req: ActorRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        eng.reintegrate(milestone_id, actor=effective_actor(req.actor))
    except OpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


def _sources(request: Request) -> dict[str, Path]:
    ctx = get_ctx(request)
    return history_sources(ctx.active_slot(request).engine.ws, include_neighbors=is_loopback(request) and not hasattr(request.state, "allowed_projects"))


@router.get("/vernal-api/history/workspaces")
def api_history_sources(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    return {"current": ctx.active_slot(request).engine.ws.root.resolve().name, "workspaces": list(_sources(request))}


@router.get("/vernal-api/history/events")
def api_history_page(
    request: Request, workspace: str, before: int | None = Query(None, ge=1), limit: int = Query(60, ge=1, le=200)
) -> dict[str, Any]:
    path = _sources(request).get(workspace)
    if path is None:
        raise HTTPException(status_code=404, detail="找不到这个历史工作区")
    return event_page(path, before, limit)


@router.get("/vernal-api/events")
async def api_events(request: Request, after: int | None = Query(None, ge=0)) -> StreamingResponse:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    cursor = eng.db.last_event_id() if after is None else after
    resumed = request.headers.get("last-event-id", "")
    if resumed.isascii() and resumed.isdigit() and len(resumed) <= 18:
        cursor = max(after or 0, int(resumed))

    async def gen():
        wake = asyncio.Event()
        pair = (asyncio.get_running_loop(), wake)
        slot.subscribers.add(pair)
        last_id = cursor
        try:
            while True:
                wake.clear()
                batch = eng.db.events_after(last_id)
                for ev in batch:
                    last_id = ev["event_id"]
                    yield f"id: {last_id}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if batch:
                    continue
                try:
                    await asyncio.wait_for(wake.wait(), timeout=3)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            slot.subscribers.discard(pair)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/vernal-api/milestones/{milestone_id}/deliveries/{version}/evidence")
def version_evidence(milestone_id: str, version: int, request: Request):
    db = get_ctx(request).active_slot(request).engine.db
    rows = db._query("SELECT * FROM milestone_deliveries WHERE milestone_id=? AND version_no=?", (milestone_id,version))
    if not rows:
        raise HTTPException(404, "交付版本不存在")
    delivery = dict(rows[0])
    delivery["evidence"] = json.loads(delivery.pop("evidence_json"))
    return delivery
