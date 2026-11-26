"""LoopForge 桥接器：把 Goal 大脑的单任务派发为 LoopForge Issue/SOP Run，并以 LoopForge 为单任务执行真相源对账。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shlex
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

from autodev.config import Settings
from autodev.db import Database, Task
from autodev.harnesses.base import Usage
from autodev.loopforge.bootstrap import classify_lane, ensure_local_git_workflow, ensure_vernal_loopforge_assets, lane_workflow_id
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.models import TaskStatus
from autodev.loopforge.protocol import FILE_DELIVERY_STAGES


_RESTORE_PY_SCRIPT = (
    "import json,pathlib,sys,urllib.request; "
    "p=pathlib.Path(sys.argv[1]) if sys.argv[1] else None; "
    'raw=json.loads(p.read_text("utf-8")) if (p and p.is_file()) else json.loads(urllib.request.urlopen(sys.argv[2]).read().decode("utf-8")); '
    'assert isinstance(raw,dict), "invalid bundle json"; '
    "nk=sys.argv[3]; "
    "d=raw.get(nk) if nk else raw; "
    'assert isinstance(d,dict), "missing bundle map"; '
    'assert (not nk) or len(d)>0, f"empty bundle: {nk}"; '
    'assert all(isinstance(v,str) for v in d.values()), "non-string file content in bundle"; '
    'pathlib.Path(".vernal-baseline.json").write_text(json.dumps(d,ensure_ascii=False),"utf-8") if sys.argv[4]=="1" else None; '
    '[((f:=pathlib.Path(k)).parent.mkdir(parents=True,exist_ok=True), f.write_text(v,"utf-8")) for k,v in d.items()]'
)


def sign_context_sig(secret: str, project_id: str, resource: str) -> str:
    """为 Runner 拉取基线/规划上下文生成绑定到项目与资源的短签名，避免局域网越权读取。"""
    if not secret:
        return ""
    msg = f"{project_id}:{resource}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:24]


def verify_context_sig(secret: str, project_id: str, resource: str, sig: str) -> bool:
    if not secret or not sig:
        return False
    expected = sign_context_sig(secret, project_id, resource)
    return hmac.compare_digest(expected, sig)


_RESTORE_NODE_SCRIPT = (
    'const fs=require("fs"),path=require("path"); '
    "(async()=>{"
    "const [p,url,nk,sb]=process.argv.slice(1);"
    "let raw;"
    'if(p&&fs.existsSync(p)){raw=JSON.parse(fs.readFileSync(p,"utf8"));}'
    'else{const r=await fetch(url);if(!r.ok)throw new Error("HTTP "+r.status+": "+(await r.text()).slice(0,200));raw=await r.json();}'
    'if(!raw||typeof raw!=="object"||Array.isArray(raw))throw new Error("invalid bundle json");'
    "const d=nk?raw[nk]:raw;"
    'if(!d||typeof d!=="object"||Array.isArray(d))throw new Error("missing bundle map: "+(nk||"root"));'
    'if(nk&&!Object.keys(d).length)throw new Error("empty bundle: "+nk);'
    'for(const [k,v] of Object.entries(d)){if(typeof v!=="string")throw new Error("non-string file content: "+k);}'
    'if(sb==="1")fs.writeFileSync(".vernal-baseline.json",JSON.stringify(d),"utf8");'
    'for(const [k,v] of Object.entries(d)){fs.mkdirSync(path.dirname(k),{recursive:true});fs.writeFileSync(k,v,"utf8");}'
    "})().catch(e=>{console.error(e&&e.message?e.message:e);process.exit(1);});"
)


def build_restore_command(
    local_path: str,
    api_url: str,
    *,
    nested_key: str = "",
    save_baseline: bool = False,
) -> str:
    """构造跨 Shell 且兼容零 Python 宿主机（优先 python3，缺失时自动降级 node）的文件还原命令。"""
    args = " ".join(
        [
            shlex.quote(local_path),
            shlex.quote(api_url),
            shlex.quote(nested_key),
            shlex.quote("1" if save_baseline else "0"),
        ]
    )
    py_cmd = f"python3 -c {shlex.quote(_RESTORE_PY_SCRIPT)} {args}"
    node_cmd = f"node -e {shlex.quote(_RESTORE_NODE_SCRIPT)} {args}"
    return f"if command -v python3 >/dev/null 2>&1; then {py_cmd}; else {node_cmd}; fi"


async def check_runners_online(client: LoopForgeClient, settings: Settings | None = None, *, require_online: bool = False) -> str | None:
    """派单前预检：若已注册个人执行机但全部离线，返回明确提示；正常或未绑定个人机返回 None。"""
    if settings is not None:
        from autodev.public_pool import resolve
        try:
            await resolve(settings, client)
        except LoopForgeError as exc:
            return str(exc)
        return None
    try:
        runners = await client.list_runners()
    except LoopForgeError as exc:
        return f"查询 LoopForge 个人执行机失败：{exc}" if require_online else None
    if require_online and not runners:
        return "个人执行器当前离线，等待本机 Runner 登录绑定"
    if runners and not any(isinstance(r, dict) and r.get("status") == "online" for r in runners):
        names = ", ".join(str(r.get("name") or r.get("id")) for r in runners if isinstance(r, dict))
        return f"执行器当前离线（{names}），已暂停派单避免产生无效失败记录"
    return None


def extract_run_usage(run: dict[str, Any]) -> Usage:
    """从 LoopForge Run 及各节点遥测字段中聚合 Token 用量。"""
    usage = Usage()
    sources: list[dict[str, Any]] = []
    if isinstance(run.get("usage"), dict):
        sources.append(run["usage"])
    elif isinstance(run.get("tokenUsage"), dict):
        sources.append(run["tokenUsage"])
    for node in (run.get("nodes") or {}).values():
        if isinstance(node, dict) and isinstance(node.get("usage"), dict):
            sources.append(node["usage"])
    for u in sources:
        usage.input_tokens += int(u.get("inputTokens") or u.get("input_tokens") or 0)
        usage.cached_tokens += int(u.get("cacheReadTokens") or u.get("cachedTokens") or u.get("cached_tokens") or 0)
        usage.output_tokens += int(u.get("outputTokens") or u.get("output_tokens") or 0)
        usage.reasoning_tokens += int(u.get("reasoningTokens") or u.get("reasoning_tokens") or 0)
    return usage


def extract_run_text(run: dict[str, Any]) -> str:
    """提取 Run 节点输出文本，作为产物缺失时的 JSON 解析兜底。"""
    parts: list[str] = []
    for node in (run.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        for key in ("output", "summary", "text", "result"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
    if isinstance(run.get("output"), str) and run["output"].strip():
        parts.append(run["output"].strip())
    return "\n\n".join(parts)


async def _review_verdict(client: LoopForgeClient, run: dict, node_id: str) -> tuple[str, str]:
    try:
        report = await client.named_artifact(run, "review-report.md", node_id=node_id)
    except LoopForgeError as exc:
        return "", str(exc)
    heading, _, details = report.strip().partition("\n")
    verdict = heading.strip()
    if verdict not in {"# PASS", "# REJECT"}:
        return "", "审查报告首行缺少 # PASS 或 # REJECT"
    return verdict, details.strip()[:1200] or verdict


async def poll_loopforge_run(
    client: LoopForgeClient,
    run_id: str,
    *,
    stop_event: asyncio.Event | None = None,
    poll_interval: float = 2.5,
    timeout_sec: float | None = None,
    on_progress: Callable[[str, str, dict[str, Any]], None] | None = None,
    on_auto_passed: Callable[[str, str], None] | None = None,
) -> tuple[bool, dict[str, Any], str]:
    """统一轮询 LoopForge Run 直到终态，自动放行已完成 CLI 节点的 submit_gate_result 卡点。"""
    loop = asyncio.get_running_loop()
    deadline = (loop.time() + timeout_sec) if timeout_sec else None
    last_summary = ""
    decided_gates: set[tuple[str, Any, Any]] = set()
    latest_run: dict[str, Any] = {}

    while stop_event is None or not stop_event.is_set():
        if deadline is not None and loop.time() >= deadline:
            return False, latest_run, f"LoopForge Run {run_id} 执行超时 ({int(timeout_sec or 0)}s)"
        try:
            run = await client.get_run(run_id)
        except LoopForgeError as exc:
            return False, latest_run, f"查询 LoopForge Run {run_id} 失败：{exc}"
        latest_run = run
        st = str(run.get("status") or "")
        nodes = run.get("nodes") or {}
        wf_nodes = {
            str(n.get("id")): n
            for n in ((run.get("workflowSnapshot") or {}).get("nodes") or [])
            if isinstance(n, dict) and n.get("id")
        }
        reviews = {nid for nid, spec in wf_nodes.items() if any(
            out.get("name") == "review-report.md" and out.get("required", True)
            for out in spec.get("artifactOutputs") or [] if isinstance(out, dict)
        )}
        gate_note = ""
        active_nodes = [
            f"{nid}:{nr.get('status')}"
            for nid, nr in nodes.items()
            if isinstance(nr, dict) and nr.get("status") in ("running", "waiting", "waiting_approval")
        ]

        # 非人工审批节点仅因 CLI 未回调 submit_gate_result 挂起时自动放行，交由后续确定性脚本/审查把关
        if st == "waiting":
            did_auto_pass = False
            for nid, nr in nodes.items():
                if not isinstance(nr, dict) or nr.get("status") not in ("waiting", "waiting_approval"):
                    continue
                ntype = str((wf_nodes.get(nid) or {}).get("type") or "")
                nerr = str(nr.get("error") or "")
                key = (nid, nr.get("attempt"), nr.get("reworkCount"))
                if ntype == "agent" and "submit_gate_result" in nerr and key not in decided_gates:
                    if nid in reviews:
                        verdict, detail = await _review_verdict(client, run, nid)
                        if not verdict:
                            gate_note = f"。等待本轮独立审查报告。{detail}"
                            continue
                        if verdict == "# REJECT":
                            gate_note = f"。独立审查要求返工。{detail[:220]}"
                            try:
                                await client.reject_run(run_id, reason=detail, node_id=nid,
                                                        target_node=wf_nodes[nid].get("rejectTo", ""))
                                decided_gates.add(key)
                            except LoopForgeError as exc:
                                gate_note += f"。提交返工失败。{exc}"
                            continue
                    try:
                        await client.approve_run(
                            run_id,
                            comment="Vernal 自动放行已完成 CLI 回合（submit_gate_result 兜底）",
                            node_id=nid,
                        )
                        decided_gates.add(key)
                        if on_auto_passed:
                            on_auto_passed(nid, "")
                        did_auto_pass = True
                    except LoopForgeError as exc:
                        if on_auto_passed:
                            on_auto_passed(nid, str(exc))
            if did_auto_pass:
                await asyncio.sleep(min(1.0, poll_interval))
                continue

        summary = f"LoopForge [{st}] " + (", ".join(active_nodes) or "处理中") + gate_note
        if st == "running":
            for nid, node in nodes.items():
                if not isinstance(node, dict) or node.get("status") == "succeeded":
                    continue
                error = str(node.get("error") or "").lower()
                if "docker workspace not ready" in error or ("container " in error and "is not running" in error):
                    summary += f"。{nid} 最近一次沙盒启动失败，等待 LoopForge 更新状态"
        if summary != last_summary:
            last_summary = summary
            if on_progress:
                on_progress(st, summary, run)

        if st == "succeeded":
            for nid in reviews:
                verdict, detail = await _review_verdict(client, run, nid)
                if verdict != "# PASS":
                    return False, run, f"独立审查未通过：{detail}"
            return True, run, f"LoopForge Run {run_id} 已成功交付"
        if st in ("failed", "cancelled"):
            node_errs = [
                f"{nid}: {nr.get('error')}"
                for nid, nr in nodes.items()
                if isinstance(nr, dict) and nr.get("error")
            ]
            err = str(run.get("error") or ("; ".join(node_errs)) or f"LoopForge Run {run_id} 结束于 {st}")
            return False, run, err
        await asyncio.sleep(poll_interval)

    return False, latest_run, "调度器收到停止信号，已暂停对账（LoopForge 远端任务仍保留现场）"


def format_issue_description(
    task: Task,
    squad_name: str,
    contract_hint: str = "",
    decisions: list[dict[str, Any]] | None = None,
) -> str:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
    paths = "\n".join(f"- `{p}`" for p in task.allowed_paths)
    cmds = "\n".join(f"- `{c}`" for c in task.verify_cmds)
    dec_block = ""
    if decisions:
        dec_lines = [
            f"- `[{d.get('decision_id')}]` **{d.get('title')}**：{d.get('rule_text')}"
            for d in decisions
            if d.get("status", "ACTIVE") == "ACTIVE"
        ]
        if dec_lines:
            dec_block = "\n### 当前项目生效规矩（全局红线，编码与审查必须遵守）\n" + "\n".join(dec_lines) + "\n"
    return (
        f"## Vernal 架构编排派发任务 `{task.task_id}`\n\n"
        f"- **里程碑**：{task.milestone_id}\n"
        f"- **研发组**：{squad_name}（`{task.squad}`）\n"
        f"- **角色**：{task.role}\n\n"
        f"### 任务目标\n{task.description}\n\n"
        f"### 验收标准\n{criteria}\n\n"
        f"### 允许修改路径白名单（Script 门禁会校验 `autodev guard --task {task.task_id}`）\n{paths}\n\n"
        f"### 必跑验证命令\n{cmds}\n"
        + dec_block
        + (f"\n### 契约提示\n{contract_hint}\n" if contract_hint else "")
    )


class LoopForgeBridge:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.client = LoopForgeClient(settings.loopforge_url, settings.effective_loopforge_token())

    def client_for_run(self, run_id):
        from autodev import public_pool
        rows = self.db._query("SELECT pool FROM dispatch_records WHERE lf_run_id=? ORDER BY dispatch_id DESC LIMIT 1", (run_id,))
        pool = rows[0]["pool"] if rows else self.db.meta_get(f"pool.run:{run_id}")
        if pool == "public_docker":
            if self.settings.loopforge_url.rstrip("/") != public_pool.server_url() or not public_pool.service_token():
                raise LoopForgeError("缺少派单时使用的公用身份", 409)
            return LoopForgeClient(public_pool.server_url(), public_pool.service_token(), transport=self.client.transport)
        if rows:
            token = self.settings.service_token()
            if not token:
                raise LoopForgeError("缺少原生派单使用的部署服务身份", 409)
            if self.client.token != token:
                return LoopForgeClient(self.settings.loopforge_url, token, transport=self.client.transport)
        return self.client

    async def await_candidate_signoff(self, task, candidate, stop):
        from autodev.loopforge.dispatch import reserve, accept_task, observe_task, record
        dispatch = self.db._query("SELECT dispatch_id,pool FROM dispatch_records WHERE lf_run_id=? "
                                  "AND task_id=? ORDER BY dispatch_id DESC LIMIT 1", (task.loopforge_run_id, task.task_id))
        saved = self.db.meta_get(f"signoff-policy:{dispatch[0]['dispatch_id']}") if dispatch else ""
        executor, approver = json.loads(saved) if saved else (
            self.settings.native_executor_user_id, self.settings.native_approver_user_id)
        if not approver:
            return
        client = self.client_for_run(task.loopforge_run_id)
        client.issue_user_id = executor
        client.approval_user_id = approver
        workflow = self.settings.loopforge_project_id + "-vernal-signoff"
        client.approval_workflow = workflow
        receipt, fresh = reserve(self.db, "_signoff:" + task.task_id, 0, task.retry_count+1,
            dispatch[0]["pool"] if dispatch else self.settings.selected_pool, str(client.approval_user_id), task.loopforge_issue_id,
            command=f"signoff:{task.loopforge_run_id}:{candidate}")
        options = dict(timeout=self.settings.agent_timeout_sec, stop=stop)
        if fresh:
            # The same native Issue provides the reviewer with the exact evidence being signed.
            await client._req("POST", f"/api/issues/{task.loopforge_issue_id}/comments", json_body={
                "body": f"待签字候选 {candidate}。开发 Run {task.loopforge_run_id}，服务端独立验证和任务质量门已通过。"})
            prior = await client.list_issue_runs(task.loopforge_issue_id)
            native = await client.dispatch_issue(task.loopforge_issue_id, rerun=any(r.get("workflowId") == workflow for r in prior),
                                                 workflow_id=workflow)
            run_id = await accept_task(self.db, receipt, client, native, **options)
        else:
            run_id = await observe_task(self.db, receipt, client, **options)
        stamp = await client.project_run(run_id, self.settings.loopforge_project_id)
        if stamp.get("workflowId") != workflow or str((stamp.get("executionContext") or {}).get("responsibleUserId")) != str(receipt["actor"]):
            raise LoopForgeError("原生审批责任人与派单不一致", 409)
        ok, last_run, message = await poll_loopforge_run(client, run_id, stop_event=stop, timeout_sec=self.settings.agent_timeout_sec)
        if ok:
            approved = (last_run.get("nodes") or {}).get("signoff") or {}
            nodes = (last_run.get("workflowSnapshot") or {}).get("nodes") or []
            if approved.get("status") != "succeeded" or not any(
                    n.get("id") == "signoff" and n.get("type") == "approval" for n in nodes):
                raise LoopForgeError("原生 Run 缺少指定人工节点的通过证据", 409)
        if not ok:
            if last_run.get("status") in ("failed", "cancelled"):
                record(self.db, receipt, "FAILED")
            raise LoopForgeError("原生候选审批未通过：" + message, 409)
        record(self.db, receipt, "FINISHED")

    def is_configured(self) -> bool:
        return bool(
            self.settings.loopforge_url.strip()
            and self.settings.effective_loopforge_token()
            and self.settings.loopforge_project_id.strip()
        )

    async def ensure_project(self) -> str:
        """若尚未绑定项目 ID，自动选择第一个活跃项目或创建 Vernal 默认项目。"""
        if self.settings.loopforge_project_id.strip():
            return self.settings.loopforge_project_id.strip()
        projects = await self.client.list_projects()
        active = [p for p in projects if not p.get("archivedAt")]
        if active:
            pid = str(active[0].get("id") or "")
            if pid:
                self.settings.loopforge_project_id = pid
                return pid
        created = await self.client.create_project(
            project_id="vernal-default",
            name="Vernal Workspace",
            description="由 Vernal 多智能体架构研发平台自动初始化的默认项目",
        )
        pid = str(created.get("id") or "vernal-default")
        self.settings.loopforge_project_id = pid
        return pid

    async def ensure_project_and_assets(self) -> dict[str, Any]:
        """幂等确保 LoopForge 项目、Vernal 标准角色、规则与交付 SOP 已全部就绪。"""
        pid = await self.ensure_project()
        try:
            result = await ensure_vernal_loopforge_assets(self.client, pid, self.settings)
            from autodev.speckit.service import sync_rule
            await sync_rule(self,self.client,pid)
            return result
        except LoopForgeError as exc:
            if self.db.meta_get("speckit.enabled") == "1":
                raise
            self.db.add_event(
                "loopforge.assets_warning",
                f"装配标准角色/SOP 跳过或降级（将沿用项目默认 SOP）：{exc}",
                actor="VernalBridge",
            )
            return {
                "project_id": pid,
                "selected_workflow_id": self.settings.loopforge_workflow_id,
            }

    async def pick_workflow_id(self, task: Task) -> str:
        """按任务类型把卡片路由到专项泳道 SOP；泳道不存在时回落到当前默认 SOP。"""
        default = self.settings.loopforge_workflow_id
        pid = self.settings.loopforge_project_id
        mode_key = "files" if self.settings.collaboration_mode == "files" else "git"
        if not default or default != lane_workflow_id(pid, mode_key, "backend"):
            return default  # 用户自定义了 SOP，尊重其选择
        lane = classify_lane(task.role, task.allowed_paths, task.title)
        if lane == "backend":
            return default
        target = lane_workflow_id(pid, mode_key, lane)
        try:
            if not hasattr(self, "_known_wfs"):
                self._known_wfs = {str(w.get("id")) for w in await self.client.list_workflows(pid)}
            if target in self._known_wfs:
                return target
        except LoopForgeError:
            pass
        return default

    async def execute_task(
        self,
        task: Task,
        squad_name: str,
        slot_id: str,
        stop_event: asyncio.Event,
        *,
        inputs: dict[str, str] | None = None,
        context: str = "",
        baseline_file: str = "",
        git_restore: str = "",
        vernal_base_url: str = "http://127.0.0.1:8787",
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
    ) -> tuple[bool, str]:
        """在 LoopForge 中建 Issue + 派发 SOP，轮询对账直到结束。返回 (是否成功, 摘要/失败原因)。"""
        if not self.settings.loopforge_url.strip() or not self.settings.effective_loopforge_token():
            return False, "LoopForge 尚未完成登录态绑定（请刷新页面或点击「自动绑定当前登录态」）"
        if not self.settings.loopforge_project_id.strip():
            try:
                await self.ensure_project_and_assets()
            except LoopForgeError as exc:
                return False, f"自动初始化 LoopForge 项目失败：{exc}"
        elif not self.settings.loopforge_workflow_id.strip():
            await self.ensure_project_and_assets()

        from autodev import public_pool
        dispatch_client = self.client
        previous = self.db._query("SELECT pool FROM dispatch_records WHERE task_id=? "
                                  "AND state NOT IN ('FAILED','FINISHED') ORDER BY dispatch_id DESC LIMIT 1",
                                  (task.task_id,))
        if previous:
            self.settings.selected_pool = previous[0]["pool"]
        restoring_shared = (previous and previous[0]["pool"] == "public_docker") or (
            task.loopforge_run_id and self.db.meta_get(f"pool.run:{task.loopforge_run_id}") == "public_docker")
        if restoring_shared:
            if self.settings.loopforge_url.rstrip("/") != public_pool.server_url() or not public_pool.service_token():
                return False, "恢复公用 Run 需要原部署的公用账号"
            dispatch_client = LoopForgeClient(public_pool.server_url(), public_pool.service_token())
        # Register project assets with the owner before using the shared execution identity.
        if not task.loopforge_run_id and not previous:
            try:
                selected, dispatch_client = getattr(self, "selected_execution", None) or await public_pool.resolve(self.settings, self.client)
                changed = bool(self.settings.selected_pool and self.settings.selected_pool != selected)
                self.settings.selected_pool = selected
                if selected == "public_docker" or changed:
                    await ensure_vernal_loopforge_assets(self.client, self.settings.loopforge_project_id, self.settings)
                if selected == "public_docker":
                    internal = public_pool.context_url(self.settings)
                    if git_restore:
                        git_restore = git_restore.replace(self.settings.vernal_public_url.rstrip("/") + "/vernal-api/",
                                                          internal.rstrip("/") + "/vernal-api/", 1)
                    vernal_base_url = internal
            except LoopForgeError as exc:
                return False, str(exc)

        from autodev.context_bundle import build_task_input_bundle
        from autodev.speckit.service import sync_rule
        await sync_rule(self,self.client,self.settings.loopforge_project_id)
        from pathlib import Path
        source_root = Path(task.worktree_path) if task.worktree_path else None
        try:
            bundle = build_task_input_bundle(self.db.path.parent.parent, self.db, task, source_root=source_root, files=inputs)
        except ValueError as exc:
            return False, str(exc)
        trimmed_ctx = context
        if len(trimmed_ctx.encode("utf-8")) > 16_000:
            trimmed_ctx = trimmed_ctx.encode("utf-8")[:16_000].decode("utf-8", errors="ignore") + "\n…（上下文已按单任务上限截断）"
        description = (
            format_issue_description(task, squad_name) + bundle
            + "\n"
            + trimmed_ctx
        )
        if inputs is not None:
            description += f"\n本次执行池：{self.settings.selected_pool}。{FILE_DELIVERY_STAGES}\n"
            inline_json = json.dumps(inputs, ensure_ascii=False)
            if len(inline_json.encode("utf-8")) <= 20_000:
                description += (
                    "\n本次采用文件交接。在空工作区按下面 JSON 还原上游文件（同时存一份 `.vernal-baseline.json`），然后实施任务并运行验证。"
                    "将新增或修改的 UTF-8 文件完整保存为 vernal-files.json，作为 SOP 声明的同名产物。"
                    '格式 {"files":{"相对路径":"完整文件内容"}}，删除使用 null。只提交本任务的改动。\n'
                    + inline_json
                )
            else:
                q_params: dict[str, str] = {}
                if self.settings.loopforge_project_id:
                    q_params["project"] = self.settings.loopforge_project_id
                sig = sign_context_sig(
                    self.settings.project_signing_key(),
                    self.settings.loopforge_project_id,
                    f"baseline:{task.task_id}",
                )
                if sig:
                    q_params["sig"] = sig
                qs = f"?{urlencode(q_params)}" if q_params else ""
                api_url = f"{vernal_base_url.rstrip('/')}/vernal-api/tasks/{quote(task.task_id, safe='')}/baseline.json{qs}"
                file_list = ", ".join(f"`{k}`" for k in sorted(inputs))
                restore_cmd = build_restore_command(
                    baseline_file,
                    api_url,
                    nested_key="",
                    save_baseline=True,
                )
                description += (
                    f"\n本次采用文件交接（共 {len(inputs)} 个上游基线文件：{file_list}）。\n"
                    "因上游基线体积较大，请先在当前空工作区执行下面这行命令一键还原上游文件并生成 `.vernal-baseline.json`：\n"
                    f"```bash\n{restore_cmd}\n```\n"
                    "还原后实施本任务并逐条运行必跑验证命令，最后将本任务新增或修改的文件保存为 `vernal-files.json`"
                    '（格式 `{"files":{"相对路径":"完整文件内容"}}`，删除用 `null`）。\n'
                )
        if task.steer_note:
            description += "\n负责人指导意见（优先遵循）：" + task.steer_note[-2000:]
        if task.last_feedback:
            description += "\n上轮反馈：" + task.last_feedback[-4000:]

        if git_restore:
            description += "\n本机 Git 项目。先运行以下命令还原本次任务基线，在 vernal-source 中编码及测试，由流程生成补丁。\n```sh\n" + git_restore + "\n```"
        repo_id = self.settings.loopforge_repository_id if inputs is None and not git_restore else ""
        base_br = (task.branch_name or self.settings.base_branch) if inputs is None and self.settings.loopforge_repository_id else ""

        from autodev.loopforge.dispatch import accept_task, observe_task, record, reserve
        receipt, fresh = reserve(self.db, task.task_id, expected_exec_seq or task.exec_seq,
                                 expected_attempt or task.retry_count + 1, self.settings.selected_pool,
                                 str(self.settings.native_executor_user_id or "deployment-service"), task.loopforge_issue_id, task.loopforge_run_id)
        if task.feature_id:
            from autodev.speckit.compiler import assert_package_current
            from autodev.loopforge.dispatch import bind_baseline
            package = assert_package_current(self.db, task)
            bind_baseline(self.db,receipt,{"spec_sha":package.spec_sha,"contract_sha":package.contract_sha,
                                         "compiled_task_hash":package.compiled_task_hash,"asset_versions":package.asset_versions})
        if fresh:
            self.db.meta_set(f"signoff-policy:{receipt['dispatch_id']}", json.dumps(
                [self.settings.native_executor_user_id, self.settings.native_approver_user_id]))
        issue_id = receipt["issue_id"]
        is_rerun = False
        if git_restore:
            base_br = ""
            workflow_id = await ensure_local_git_workflow(
                self.client,
                self.settings.loopforge_project_id,
                classify_lane(task.role, task.allowed_paths, task.title),
                settings=self.settings,
            )
        else:
            workflow_id = await self.pick_workflow_id(task)
        if self.settings.native_approver_user_id:
            dispatch_client.issue_user_id = self.settings.native_executor_user_id
            dispatch_client.approval_user_id = self.settings.native_approver_user_id
            dispatch_client.approval_workflow = self.settings.loopforge_project_id + "-vernal-signoff"
            from autodev.loopforge.bootstrap import ensure_signoff_workflow
            await ensure_signoff_workflow(self.client, self.settings.loopforge_project_id)
        if fresh and not task.loopforge_run_id:
            try:
                await public_pool.check_workflow_sandbox(self.settings, self.client, workflow_id, self.settings.selected_pool)
            except LoopForgeError as exc:
                record(self.db, receipt, "FAILED")
                return False, str(exc)
        if fresh and not issue_id:
            issue = await dispatch_client.create_issue(
                project_id=self.settings.loopforge_project_id,
                title=f"[{task.task_id}] {task.title}",
                description=description,
                workflow_id=workflow_id,
                repository_id=repo_id,
                base_branch=base_br,
            )
            issue_id = str(issue.get("id") or "")
            if not issue_id:
                record(self.db, receipt, "NEEDS_CONFIRMATION")
                return False, "派单结果待人工确认"
            record(self.db, receipt, "DISPATCHING", issue_id=issue_id)
            if expected_exec_seq is not None:
                if not self.db.update_task_if_owned(
                    task.task_id,
                    expected_exec_seq=expected_exec_seq,
                    expected_attempt=expected_attempt,
                    loopforge_issue_id=issue_id,
                ):
                    return False, "任务已被取消、重置或由替代执行接管"
            else:
                self.db.update_task(task.task_id, loopforge_issue_id=issue_id)
            self.db.add_event(
                "loopforge.issue_created",
                f"已在 LoopForge 创建需求 {issue_id}",
                task_id=task.task_id,
                actor="VernalBridge",
            )
        elif fresh and not task.loopforge_run_id:
            is_rerun = True
            try:
                await dispatch_client.reopen_issue(issue_id)
            except LoopForgeError:
                pass
            try:
                await dispatch_client.update_issue(
                    issue_id=issue_id,
                    project_id=self.settings.loopforge_project_id,
                    title=f"[{task.task_id}] {task.title}",
                    description=description,
                    workflow_id=workflow_id,
                    repository_id=repo_id,
                    base_branch=base_br,
                )
            except LoopForgeError as exc:
                record(self.db, receipt, "FAILED")
                return False, f"更新原生工单失败，已停止派发：{exc}"

        run_id = task.loopforge_run_id
        if not run_id:
            try:
                opts = {"stop": stop_event, "timeout": self.settings.agent_timeout_sec}
                if fresh:
                    if expected_exec_seq is not None and not self.db.is_task_owned(task.task_id, expected_exec_seq=expected_exec_seq):
                        return False, "派单已取消"
                    native = await dispatch_client.dispatch_issue(issue_id, rerun=True) if is_rerun else await dispatch_client.dispatch_issue(issue_id)
                    run_id = await accept_task(self.db, receipt, dispatch_client, native, **opts)
                else:
                    run_id = await observe_task(self.db, receipt, dispatch_client, **opts)
            except LoopForgeError as exc:
                if receipt["state"] == "DISPATCHING":
                    record(self.db, receipt, "NEEDS_CONFIRMATION")
                return False, f"查询 LoopForge 派单：{exc}"
            if expected_exec_seq is not None:
                if not self.db.update_task_if_owned(
                    task.task_id,
                    expected_exec_seq=expected_exec_seq,
                    expected_attempt=expected_attempt,
                    expected_run_id="",
                    loopforge_run_id=run_id,
                ):
                    return False, "任务已被取消、重置或由替代执行接管"
            else:
                self.db.update_task(task.task_id, loopforge_run_id=run_id)
            self.db.meta_set(f"pool.run:{run_id}", self.settings.selected_pool)
        self.db.update_slot(slot_id, current_action=f"LoopForge SOP 运行中（Run {run_id[:12]}）…")
        self.db.add_event(
            "loopforge.dispatched",
            f"已派发到 LoopForge 执行（Issue {issue_id} / Run {run_id}）",
            task_id=task.task_id,
            actor="VernalBridge",
        )

        # 以 LoopForge 为单任务执行真相源，每 2.5 秒幂等对账
        def _on_progress(st: str, summary: str, _run: dict[str, Any]) -> None:
            if expected_exec_seq is not None and not self.db.is_task_owned(
                task.task_id,
                expected_exec_seq=expected_exec_seq,
                expected_attempt=expected_attempt,
                expected_run_id=run_id,
            ):
                return
            self.db.update_slot(slot_id, current_action=summary[:200])
            self.db.add_event(
                "loopforge.progress",
                summary,
                task_id=task.task_id,
                actor="VernalBridge",
            )
            if st == "waiting":
                if expected_exec_seq is not None:
                    if not self.db.update_task_if_owned(
                        task.task_id,
                        expected_exec_seq=expected_exec_seq,
                        expected_attempt=expected_attempt,
                        expected_run_id=run_id,
                        status=TaskStatus.REVIEWING,
                    ):
                        return
                else:
                    self.db.update_task(task.task_id, status=TaskStatus.REVIEWING)
                self.db.add_event(
                    "loopforge.waiting_gate",
                    f"LoopForge Run {run_id} 正在等待人工审批门禁（可在左侧「待我确认」直接审批）",
                    task_id=task.task_id,
                    actor="VernalBridge",
                )

        def _on_auto_passed(nid: str, err: str) -> None:
            if err:
                self.db.add_event(
                    "loopforge.auto_pass_warning",
                    f"自动放行节点 {nid} 失败：{err}",
                    task_id=task.task_id,
                    actor="VernalBridge",
                )
            else:
                self.db.add_event(
                    "loopforge.auto_passed",
                    f"节点 {nid} 执行完毕，已自动放行 submit_gate_result 卡点（Run {run_id[:12]}）",
                    task_id=task.task_id,
                    actor="VernalBridge",
                )

        ok, _run, msg = await poll_loopforge_run(
            dispatch_client,
            run_id,
            stop_event=stop_event,
            poll_interval=2.5,
            timeout_sec=float(self.settings.agent_timeout_sec),
            on_progress=_on_progress,
            on_auto_passed=_on_auto_passed,
        )
        if ok and task.feature_id:
            from autodev.speckit.compiler import assert_package_current
            assert_package_current(self.db,self.db.get_task(task.task_id))
            from autodev.models import HandoffBrief
            from autodev.prompts_style import validate_public_text
            briefs = {}
            try:
                for node in ("implement","review"):
                    raw = await dispatch_client.named_artifact(_run,"handoff-brief.json",node_id=node)
                    if not raw:
                        raise ValueError("缺少开发或审查节点的交接便签")
                    brief = HandoffBrief.model_validate_json(raw)
                    for value in brief.model_dump().values():
                        validate_public_text(value)
                    briefs[node]=brief.model_dump()
            except (ValueError,LoopForgeError) as exc:
                record(self.db,receipt,"FAILED")
                self.db.add_event("speckit.handoff_invalid",str(exc),task_id=task.task_id,actor="VernalBridge")
                return False,"开发或审查交接便签缺失或格式无效，请检查本次运行产物"
            self.db.update_task(task.task_id,handoff_brief_json=json.dumps(briefs,ensure_ascii=False))
        if _run.get("status") in ("succeeded", "failed", "cancelled", "completed"):
            record(self.db, receipt, "FINISHED" if ok else "FAILED")
        return ok, msg
