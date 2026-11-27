"""LoopForge 统一规划执行器：把圆桌评审、蓝图拆单与异常仲裁全部路由到 LoopForge SOP / Runner。

使 Vernal 本体不再依赖宿主机本地 CLI，放进 Docker 容器后同样可以直接驱动同事或麒麟宿主机上的
Codex / CodeBuddy / OpenCode / DeepSeek Harness / Claude Code 完成全流程。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from urllib.parse import quote, urlencode

from autodev.artifacts import FileExchange
from autodev.config import Settings, Workspace
from autodev.db import Database
from autodev.harnesses.base import ActivityCallback, AgentResult, Harness, Usage, extract_json
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets, plan_workflow_id
from autodev.loopforge.bridge import (
    build_restore_command,
    extract_run_text,
    extract_run_usage,
    poll_loopforge_run,
    sign_context_sig,
)
from autodev.loopforge.client import LoopForgeClient, LoopForgeError

_STAGE_META: dict[tuple[str, str], tuple[str, str]] = {
    ("council_opinion", "pm"): ("council_pm", "[Vernal·圆桌] 产品经理评审意见"),
    ("council_opinion", "architect"): ("council_architect", "[Vernal·圆桌] 契约架构师评审意见"),
    ("council_opinion", "qa_lead"): ("council_qa_lead", "[Vernal·圆桌] QA 总监评审意见"),
    ("council_consensus", "director"): ("council_director", "[Vernal·圆桌] 研发总监共识与 ADR"),
    ("detailed_design", "architect"): ("detailed_design", "[Vernal·规划] 详细设计"),
    ("prd_spec", "pm"): ("pm_spec", "[Vernal·规划] 产品规格说明书"),
    ("project_blueprint", "director"): ("blueprint", "[Vernal·规划] 研发组与里程碑蓝图"),
    ("milestone_wave", "director"): ("wave_plan", "[Vernal·规划] 里程碑滚动拆单"),
    ("project_plan", "planner"): ("classic_plan", "[Vernal·规划] 全局任务拆解"),
    ("director_decision", "director"): ("arbitrate", "[Vernal·仲裁] 研发总监异常仲裁"),
}

_INLINE_DESC_LIMIT = 18_000
_CONTEXT_FILE_BUDGET = 60_000


def _deadlocked_run(error: str) -> str:
    """Only reconcile the specific upstream run transaction that MySQL rolled back."""
    if not re.search(r"\b1213\s*\(40001\)", error):
        return ""
    match = re.search(r"\bruns upsert (run-[A-Za-z0-9_-]+):", error)
    return match.group(1) if match else ""


async def _reconcile_run(client: LoopForgeClient, issue_id: str, run_id: str) -> bool:
    """A failed lookup is ambiguous. Retry dispatch only after confirming absence."""
    info = await client.issue_detail(issue_id)
    issue = info.get("issue", info)
    if issue.get("id") != issue_id:
        raise LoopForgeError(f"无法核实工单 {issue_id} 的运行记录")
    if client.issue_user_id is not None and str(issue.get("assigneeUserId", issue.get("responsibleUserId"))) != str(client.issue_user_id):
        raise LoopForgeError("无法核实公用工单责任人")
    try:
        run = await client.get_run(run_id)
    except LoopForgeError as exc:
        if exc.status_code != 404:
            raise
        if issue.get("latestRunId") == run_id or any(r.get("id") == run_id for r in info.get("runs") or []):
            raise LoopForgeError(f"工单已记录 {run_id}，等待运行详情可读取后再恢复") from exc
        return False
    linked_issue = run.get("issueId") or (run.get("executionContext") or {}).get("issueId")
    if linked_issue != issue_id or run.get("id") != run_id:
        raise LoopForgeError(f"无法确认 {run_id} 属于工单 {issue_id}")
    return True


def _stage_key_and_title(schema_path: Path, role: str) -> tuple[str, str]:
    stem = schema_path.name
    if stem.endswith(".schema.json"):
        stem = stem[: -len(".schema.json")]
    elif stem.endswith(".json"):
        stem = stem[: -len(".json")]
    if stem.startswith("speckit_"):
        from autodev.speckit.adapter import STEP_NAMES
        step = stem.split("_", 2)[1]
        return stem, STEP_NAMES.get(step, "整理功能交付资料")
    if (stem, role) in _STAGE_META:
        return _STAGE_META[(stem, role)]
    for (s_key, _r), pair in _STAGE_META.items():
        if s_key == stem:
            return pair
    return f"{stem}_{role}", f"[Vernal·规划] {stem} ({role})"


def _collect_context_files(ws: Workspace, db: Database, settings: Settings) -> dict[str, str]:
    """收集规划阶段可能需要查阅的只读上下文文件（inputs / PRD / AGENTS / contracts / 已交付文件）。"""
    files: dict[str, str] = {}
    used = 0

    def _add(rel: str, text: str) -> None:
        nonlocal used
        if not text or used >= _CONTEXT_FILE_BUDGET:
            return
        b = len(text.encode("utf-8"))
        if used + b > _CONTEXT_FILE_BUDGET:
            remain = max(0, _CONTEXT_FILE_BUDGET - used)
            if remain < 256:
                return
            text = text.encode("utf-8")[:remain].decode("utf-8", errors="ignore") + "\n…（已截断）"
            b = len(text.encode("utf-8"))
        files[rel] = text
        used += b

    spec_managed = db.meta_get("speckit.enabled") == "1"
    source_docs = [ws.root / "AGENTS.md"]
    if not spec_managed:
        source_docs.append(ws.prd_md_path)
    for single in source_docs:
        if single.is_file():
            try:
                _add(single.relative_to(ws.root).as_posix(), single.read_text("utf-8", errors="replace"))
            except OSError:
                pass

    for folder in (ws.inputs_dir, ws.root / "contracts"):
        if not folder.is_dir():
            continue
        for fp in sorted(folder.rglob("*")):
            if not fp.is_file() or fp.name.startswith("."):
                continue
            try:
                _add(fp.relative_to(ws.root).as_posix(), fp.read_text("utf-8"))
            except (OSError, UnicodeDecodeError):
                continue

    if settings.collaboration_mode == "files":
        try:
            delivered = FileExchange(ws, db).baseline([])
            for rel, content in sorted(delivered.items()):
                if rel not in files:
                    _add(rel, content)
        except Exception:
            pass
    return files


class LoopForgeHarness(Harness):
    """通过 LoopForge 规划 SOP（vernal-plan-sop）执行只读结构化规划与圆桌调用。"""

    name = "loopforge"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        ws: Workspace,
        activity_cb: ActivityCallback | None = None,
        *,
        poll_interval: float = 2.0,
        stop_event: asyncio.Event | None = None,
        client: LoopForgeClient | None = None,
    ):
        super().__init__(activity_cb)
        self.settings = settings
        self.db = db
        self.ws = ws
        self.stop_event = stop_event or asyncio.Event()
        self.poll_interval = poll_interval
        self._custom_client = client
        self._assets_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._assets_ready = False
        self._asset_config = ""
        self._last_rule_digest: str = ""

    def _client(self) -> LoopForgeClient:
        if self._custom_client is not None:
            return self._custom_client
        return LoopForgeClient(self.settings.loopforge_url, self.settings.effective_loopforge_token())

    async def _ensure_ready(self, client: LoopForgeClient, role: str = "director") -> tuple[str, str]:
        async with self._assets_lock:
            pid = self.settings.loopforge_project_id.strip()
            if not pid:
                projects = await client.list_projects()
                active = [p for p in projects if not p.get("archivedAt")]
                if active and active[0].get("id"):
                    pid = str(active[0]["id"])
                else:
                    created = await client.create_project(
                        project_id="vernal-default",
                        name="Vernal Workspace",
                        description="由 Vernal 多智能体架构研发平台自动初始化的默认项目",
                    )
                    pid = str(created.get("id") or "vernal-default")
                self.settings.loopforge_project_id = pid
            def config_key():
                return hashlib.sha256(json.dumps([
                    pid, self.settings.selected_pool,
                    [(r, self.settings.harness_for(r), self.settings.model_for(r), self.settings.effort_for(r))
                     for r in ("pm", "director", "qa_lead", "coder", "reviewer")],
                ], sort_keys=True).encode()).hexdigest()
            asset_config = config_key()
            if not self._assets_ready or self._asset_config != asset_config or self.settings.selected_pool == "public_docker":
                await ensure_vernal_loopforge_assets(client, pid, self.settings)
                self._assets_ready = True
                self._asset_config = config_key()
            from autodev.speckit.service import constitution_rule, enabled, sync_rule
            if enabled(self):
                rule_sig = hashlib.sha256(
                    json.dumps(
                        [
                            pid,
                            int(self.db.meta_get("decisions_rule_version") or 1),
                            self.db.list_decisions(active_only=True),
                            constitution_rule(self),
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                if rule_sig != self._last_rule_digest:
                    await sync_rule(self, client, pid)
                    self._last_rule_digest = rule_sig
            return pid, plan_workflow_id(pid, role)

    async def _effort_workflow(self, client, pid: str, wf_id: str, role: str, effort: str) -> str:
        """Snapshot a distinct effort without editing a shared planner or its SOP.

        Read the source afresh so later human gate edits remain effective.
        Reuse content-addressed native assets without repeated registration.
        """
        if not effort or effort == self.settings.effort_for(role):
            return wf_id
        import copy
        from autodev.loopforge.bootstrap import _NATIVE_METADATA, managed_asset, _managed_digest
        async with self._assets_lock:
            workflows, agents, rules = await asyncio.gather(
                client.list_workflows(pid), client.list_agents(pid),
                client._req("GET", "/api/rules", params={"projectId": pid}),
            )
            source = next((w for w in workflows if w.get("id") == wf_id), None)
            if not source:
                raise LoopForgeError("无法读取规划 SOP，不能确认步骤推理配置")
            if source.get("status") != "published":
                raise LoopForgeError("规划 SOP 尚未发布，不能自动发布其推理配置副本")
            workflow = copy.deepcopy({k: v for k, v in source.items() if k not in _NATIVE_METADATA})
            draft = next((n for n in workflow.get("nodes", []) if n.get("id") in ("draft", "draft_lite_bundle", "draft_spec", "draft_plan_and_packages") and n.get("type") == "agent"), None)
            original = next((a for a in agents if draft and a.get("id") == draft.get("agentId")), None)
            if not original or original.get("executorId") != "codex":
                raise LoopForgeError("规划角色与 Codex 推理配置不匹配，请核对原生角色")
            agent = copy.deepcopy({k: v for k, v in original.items() if k not in _NATIVE_METADATA})
            agent["effort"] = effort
            # Explicitly carry workflow-scoped rules onto the cloned planner.
            agent["rules"] = list(dict.fromkeys([*agent.get("rules", []), *[
                r["id"] for r in rules if wf_id in (r.get("workflowIds") or [])
            ]]))
            for asset in (agent, workflow):
                asset["description"] = (asset.get("description") or "").split("\n[vernal-managed-v", 1)[0]
            digest = hashlib.sha256(json.dumps([agent, workflow], sort_keys=True).encode()).hexdigest()[:20]
            aid, wid = f"{pid}-vernal-effort-agent-{digest}", f"{pid}-vernal-effort-sop-{digest}"
            draft["agentId"] = aid
            agent = managed_asset({**agent, "id": aid, "projectId": pid, "version": 1})
            workflow = managed_asset({**workflow, "id": wid, "projectId": pid, "version": 1, "status": "published"})
            for desired, existing in ((agent, agents), (workflow, workflows)):
                previous = next((x for x in existing if x.get("id") == desired["id"]), None)
                if previous and _managed_digest(previous, set(desired) - _NATIVE_METADATA) != _managed_digest(desired, set(desired) - _NATIVE_METADATA):
                    raise LoopForgeError("步骤推理配置快照已被修改，请核对后重试")
            if not any(a.get("id") == aid for a in agents):
                await client.put_agent(agent)
            existing = next((w for w in workflows if w.get("id") == wid), None)
            if not existing:
                existing = await client.put_workflow(workflow)
            if existing.get("status") != "published":
                await client.publish_workflow(wid)
            return wid

    async def _prepare_workflow(self, client, pid: str, wf_id: str, role: str,
                                stage_key: str, effort: str, restore_cmd: str) -> str:
        """Caller holds _assets_lock through dispatch so updates cannot race the run snapshot."""
        if not restore_cmd:
            return wf_id
        import copy
        from autodev.loopforge.bootstrap import (_NATIVE_METADATA, _managed_digest, managed_asset,
                                                 managed_clean, build_plan_workflow, build_plan_prepare_script)
        workflows = await client.list_workflows(pid)
        source = next((w for w in workflows if w.get("id") == wf_id), None)
        if not source or source.get("status") != "published":
            raise LoopForgeError("无法读取已发布规划 SOP，不能准备只读上下文")
        # Spec Kit schema filenames include a content signature, not a new logical planning stage.
        stable_stage = re.sub(r"_[a-f0-9]{20}$", "", stage_key) if stage_key.startswith("speckit_") else stage_key
        key = hashlib.sha256(json.dumps([pid, role, stable_stage, effort]).encode()).hexdigest()[:20]
        wid = (wf_id if wf_id in {f"{pid}-vernal-{lane}-sop" for lane in ("lite-plan", "std-spec", "std-plan")} else f"{pid}-vernal-prepare-sop-{key}")
        workflow = copy.deepcopy({k: v for k, v in source.items() if k not in _NATIVE_METADATA})
        prepare = next((n for n in workflow.get("nodes", []) if n.get("id") == "prepare"), None)
        if prepare is None:
            if {n.get("id") for n in workflow.get("nodes", [])} != {"draft", "validate"}:
                raise LoopForgeError("规划 SOP 缺少 prepare 节点，请核对自定义拓扑")
            prepare = build_plan_workflow(pid)["nodes"][0]
            workflow["nodes"].insert(0, prepare)
            workflow["edges"].insert(0, {"id": "e0", "source": "prepare", "target": "draft", "when": "success"})
        if prepare.get("type") != "script":
            raise LoopForgeError("规划 prepare 节点必须为脚本")
        prepare.update(scriptLanguage="shell", script=build_plan_prepare_script(restore_cmd), maxAttempts=1)
        workflow["description"] = (workflow.get("description") or "").split("\n[vernal-managed-v", 1)[0]
        previous = next((w for w in workflows if w.get("id") == wid), None)
        workflow = managed_asset({**workflow, "id": wid, "projectId": pid, "status": "published",
                                  "version": (previous or {}).get("version", 1)})
        if previous and not managed_clean(previous, workflow):
            raise LoopForgeError("阶段规划 SOP 已被手动修改，请核对后重试")
        if not previous or _managed_digest(previous, set(workflow) - _NATIVE_METADATA) != _managed_digest(workflow, set(workflow) - _NATIVE_METADATA):
            if previous and "revision" in previous:
                workflow["revision"] = previous["revision"]
            await client.put_workflow(workflow)
            await client.publish_workflow(wid)
        elif previous.get("status") != "published":
            await client.publish_workflow(wid)
        return wid

    def _build_issue_description(
        self,
        stage_key: str,
        prompt: str,
        schema_path: Path,
        role: str,
    ) -> tuple[str, str]:
        schema_text = schema_path.read_text("utf-8")
        try:
            schema_obj = json.loads(schema_text)
            top_required = list(schema_obj.get("required") or [])
        except ValueError:
            top_required = []
        inline_full_prompt = (
            f"{prompt}\n\n"
            f"## 严格输出格式（JSON Schema）\n\n"
            f"请将满足以下 JSON Schema 的唯一合法 JSON 对象（UTF-8 纯 JSON，不要包含 Markdown 围栏）"
            f"写入当前工作目录的 `vernal-result.json`：\n"
            f"```json\n{schema_text}\n```\n"
        )
        # Externalized bundle already ships `.vernal-schema.json` alongside `PLAN_PROMPT.md`;
        # avoid duplicating the entire multi-KB JSON Schema inside PLAN_PROMPT.md.
        req_hint = f"（顶层必填字段：{', '.join(top_required)}）" if top_required else ""
        external_prompt = (
            f"{prompt}\n\n"
            f"## 严格输出格式（JSON Schema）\n\n"
            f"完整 JSON Schema 已同步到当前工作目录的 `.vernal-schema.json`{req_hint}。\n"
            "请严格按照 `.vernal-schema.json` 生成唯一的合法 JSON 对象（UTF-8 纯 JSON，不要包含 Markdown 围栏），写入当前工作目录的 `vernal-result.json`。\n"
            "下游确定性脚本节点会自动执行完整 Schema 与文档格式校验；不要在系统中搜索或安装 `ajv`/`jsonschema` 等外部校验包。\n"
        )

        ctx_files = _collect_context_files(self.ws, self.db, self.settings)
        bundle_files = dict(ctx_files)
        bundle_files[".vernal-schema.json"] = schema_text
        bundle_files["PLAN_PROMPT.md"] = external_prompt

        bdir = self.ws.state_dir / "baselines"
        bdir.mkdir(parents=True, exist_ok=True)
        bundle_path = (bdir / f"plan-{stage_key}.json").resolve()
        bundle_path.write_text(json.dumps({"files": bundle_files}, ensure_ascii=False), "utf-8")

        need_extra_files = stage_key.startswith("speckit_") or bool(ctx_files) and (
            "inputs/" in prompt or "docs/PRD_SPEC.md" in prompt or "已真实存在的代码" in prompt or "contracts/" in prompt
        )
        inline_candidate = (
            f"## Vernal 架构规划任务（角色：`{role}`，阶段：`{stage_key}`）\n\n"
            f"{inline_full_prompt}\n"
            "完成后确保当前目录存在合法的 `vernal-result.json`，勿修改业务源码。\n"
        )
        if not need_extra_files and len(inline_candidate.encode("utf-8")) <= _INLINE_DESC_LIMIT:
            return inline_candidate, ""

        from autodev.public_pool import context_url
        public_url = (context_url(self.settings) or "http://127.0.0.1:8787").rstrip("/")
        q_params: dict[str, str] = {}
        if self.settings.loopforge_project_id:
            q_params["project"] = self.settings.loopforge_project_id
        sig = sign_context_sig(
            self.settings.project_signing_key(),
            self.settings.loopforge_project_id,
            f"plan:{stage_key}",
        )
        if sig:
            q_params["sig"] = sig
        qs = f"?{urlencode(q_params)}" if q_params else ""
        api_url = f"{public_url}/vernal-api/plan-context/{quote(stage_key, safe='')}.json{qs}"
        restore_cmd = build_restore_command(
            str(bundle_path),
            api_url,
            nested_key="files",
            save_baseline=False,
        )
        file_list = ", ".join(f"`{k}`" for k in sorted(ctx_files)[:20]) or "（完整提示词与 Schema）"
        return (
            f"## Vernal 架构规划任务（角色：`{role}`，阶段：`{stage_key}`）\n\n"
            f"本阶段上下文较多（含 {len(ctx_files)} 份工作区资料：{file_list}）。\n"
            "prepare 节点已准备好当前目录的 `PLAN_PROMPT.md`、`.vernal-schema.json` 与只读上下文文件。\n"
            "直接完整阅读这两个文件，按 JSON Schema 将唯一合法 JSON 对象写入当前目录的 `vernal-result.json`。\n"
            "产物写完后正常退出，由下一节点校验。审查节点按自身任务说明提交独立报告。\n"
        ), restore_cmd

    async def _dispatch_plan(
        self, client: LoopForgeClient, pid: str, stage_key: str, title: str, description: str,
        wf_id: str, prior_candidate: str, request_signature: str, *, snapshot=None,
    ) -> tuple[str, str]:
        # Only serialize issue writes and dispatch. Runner execution and polling remain parallel.
        from contextlib import AsyncExitStack
        async with AsyncExitStack() as locks:
            await locks.enter_async_context(self._dispatch_lock)
            if snapshot is not None:
                locks.push_async_callback(snapshot.aclose)
            meta_key = f"plan_issue:{pid}:{stage_key}"
            issue_id = self.db.meta_get(meta_key)
            pending_key = f"{meta_key}:pending_run"
            pending = json.loads(self.db.meta_get(pending_key) or "{}")
            if pending.get("issue") == issue_id and pending.get("request") == request_signature:
                prior_candidate = pending["run"]
            if issue_id and prior_candidate:
                self.db.meta_set(pending_key, json.dumps({"issue": issue_id, "request": request_signature, "run": prior_candidate}))
                if await _reconcile_run(client, issue_id, prior_candidate):
                    self.db.meta_set(pending_key, None)
                    return issue_id, prior_candidate
                self.db.meta_set(pending_key, None)
            from autodev.loopforge.dispatch import reserve, record, accept_task, observe_task
            command = f"plan:{pid}:{stage_key}:{request_signature}"
            receipt, fresh = reserve(self.db, command, 1, 1, self.settings.selected_pool,
                                     "deployment-service", issue_id)
            from autodev.speckit.adapter import PLANNING_BASELINE
            from autodev.loopforge.dispatch import bind_baseline
            if baseline := PLANNING_BASELINE.get():
                bind_baseline(self.db, receipt, baseline)
            if not fresh:
                await locks.aclose()
                run = await observe_task(self.db, receipt, client, timeout=self.settings.agent_timeout_sec)
                return receipt["issue_id"], run
            if issue_id:
                try:
                    await client.reopen_issue(issue_id)
                except LoopForgeError as exc:
                    if exc.status_code in (401, 403):
                        raise
                try:
                    await client.update_issue(issue_id=issue_id, project_id=pid, title=title,
                                              description=description, workflow_id=wf_id)
                except LoopForgeError as exc:
                    if exc.status_code != 404:
                        raise
                    issue_id = None
            rerun = bool(issue_id)
            if not issue_id:
                issue = await client.create_issue(project_id=pid, title=title, description=description, workflow_id=wf_id)
                issue_id = str(issue.get("id") or "")
                if not issue_id:
                    raise LoopForgeError("创建规划工单未返回 ID")
                self.db.meta_set(meta_key, issue_id)
                record(self.db, receipt, "DISPATCHING", issue_id=issue_id)
            for attempt in range(3):
                try:
                    dispatched = await client.dispatch_issue(issue_id, rerun=rerun)
                except LoopForgeError as exc:
                    candidate = _deadlocked_run(str(exc))
                    if not candidate:
                        raise
                    self.db.meta_set(pending_key, json.dumps({"issue": issue_id, "request": request_signature, "run": candidate}))
                    if await _reconcile_run(client, issue_id, candidate):
                        self.db.meta_set(pending_key, None)
                        record(self.db, receipt, "RUNNING", lf_run_id=candidate)
                        return issue_id, candidate
                    self.db.meta_set(pending_key, None)
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                await locks.aclose()
                run_id = await accept_task(self.db, receipt, client, dispatched, timeout=self.settings.agent_timeout_sec)
                return issue_id, run_id

    async def dispatch_bundle(self, receipt, prompt, schema, role):
        """Bind a mission receipt before the native POST; return as soon as it has a Run."""
        from autodev import public_pool
        from autodev.loopforge import dispatch

        stage, feature = receipt["stage"], receipt["feature_id"]
        # Reconciliation after a restart uses the deployment identity for this project.
        asset_client = self._custom_client
        if asset_client is None:
            token = self.settings.service_token()
            if not token:
                raise LoopForgeError("项目缺少可用于后台对账的部署凭据", 403)
            asset_client = LoopForgeClient(self.settings.loopforge_url, token)
        pool, client = await public_pool.resolve(self.settings, asset_client)
        self.settings.selected_pool = pool
        pid, _ = await self._ensure_ready(asset_client, role)
        lane = {"std_plan": "std-plan", "std_spec": "std-spec", "lite_bundle": "lite-plan"}[stage]
        wid = f"{pid}-vernal-{lane}-sop"
        effort = self.settings.effort_for(role, step=stage)
        wid = await self._effort_workflow(asset_client, pid, wid, role, effort)
        key = f"speckit_{stage}_{feature}_{receipt['expected_baseline_sha'][:20]}"
        path = self.ws.state_dir / "schemas" / (key + ".schema.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, ensure_ascii=False), "utf-8")
        async with self._assets_lock:
            description, restore = self._build_issue_description(key, prompt, path, role)
            wid = await self._prepare_workflow(asset_client, pid, wid, role, key, effort, restore)
            await public_pool.check_workflow_sandbox(self.settings, asset_client, wid, pool)
            self.db._exec("UPDATE dispatch_records SET pool=? WHERE dispatch_id=?", (pool, receipt["dispatch_id"]))
            receipt["pool"] = pool
            issue = await client.create_issue(project_id=pid, title=f"{feature} · {lane}", workflow_id=wid, description=description)
            dispatch.record(self.db, receipt, "DISPATCHING", issue_id=issue["id"])
            accepted = await client.dispatch_issue(issue["id"])
        return dispatch.bind_accepted_task(self.db, receipt, accepted)

    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult:
        started = time.monotonic()
        if self.stop_event.is_set():
            raise asyncio.CancelledError
        log_dir.mkdir(parents=True, exist_ok=True)
        stage_key, title = _stage_key_and_title(schema_path, role)
        prompt_file = log_dir / "prompt.md"
        prior_candidate = ""
        if prompt_file.is_file() and prompt_file.read_text("utf-8") == prompt:
            previous = next((r for r in reversed(self.db.list_runs("_" + stage_key)) if r.get("finished_at")), {})
            prior_candidate = _deadlocked_run(previous.get("summary_log") or "")
        prompt_file.write_text(prompt, "utf-8")

        from autodev import public_pool
        if self._custom_client is None and (not self.settings.loopforge_url.strip() or not self.settings.effective_loopforge_token()):
            return AgentResult(
                False,
                -1,
                None,
                "",
                error="LoopForge 尚未完成登录态绑定，请刷新页面或点击「自动绑定当前登录态」后再发起圆桌/规划",
                log_dir=log_dir,
                duration_sec=time.monotonic() - started,
            )

        t_resolve_start = time.monotonic()
        client = self._client()
        try:
            selected, dispatch_client = await public_pool.resolve(self.settings, client)
            if selected != self.settings.selected_pool:
                self._assets_ready = False
            self.settings.selected_pool = selected
        except LoopForgeError as exc:
            offline_err = str(exc)
            return AgentResult(
                False,
                -1,
                None,
                "",
                error=offline_err,
                log_dir=log_dir,
                duration_sec=time.monotonic() - started,
            )
        t_resolve_sec = round(time.monotonic() - t_resolve_start, 4)

        from autodev.speckit.adapter import PLANNING_BASELINE
        planning_context = PLANNING_BASELINE.get() or {}
        step, risk = planning_context.get("step", ""), planning_context.get("risk", "")
        actual_effort = self.settings.effort_for(role, step=step, risk=risk)
        t_ready_start = time.monotonic()
        try:
            pid, wf_id = await self._ensure_ready(client, role)
            if step == "lite_bundle":
                wf_id = f"{pid}-vernal-lite-plan-sop"
            actual_effort = self.settings.effort_for(role, step=step, risk=risk)
            wf_id = await self._effort_workflow(client, pid, wf_id, role, actual_effort)
            asset_client = client
            client = dispatch_client
        except LoopForgeError as exc:
            return AgentResult(
                False,
                -1,
                None,
                "",
                error=f"初始化 LoopForge 规划 SOP 失败：{exc}",
                log_dir=log_dir,
                duration_sec=time.monotonic() - started,
            )
        t_ready_sec = round(time.monotonic() - t_ready_start, 4)

        t_dispatch_start = time.monotonic()
        schema_text = schema_path.read_text("utf-8")
        try:
            from contextlib import AsyncExitStack
            async with AsyncExitStack() as snapshot:
                await snapshot.enter_async_context(self._assets_lock)
                description, restore_cmd = self._build_issue_description(stage_key, prompt, schema_path, role)
                wf_id = await self._prepare_workflow(asset_client, pid, wf_id, role, stage_key, actual_effort, restore_cmd)
                await public_pool.check_workflow_sandbox(self.settings, asset_client, wf_id, selected)
                signature = hashlib.sha256(json.dumps([prompt, schema_text, selected, wf_id, actual_effort, self.settings.model_for(role), self.settings.harness_for(role)]).encode()).hexdigest()
                issue_id, run_id = await self._dispatch_plan(client, pid, stage_key, title, description, wf_id,
                                                           prior_candidate, signature, snapshot=snapshot)
        except LoopForgeError as exc:
            return AgentResult(
                False,
                -1,
                None,
                "",
                error=f"派发 LoopForge 规划任务失败：{exc}",
                log_dir=log_dir,
                duration_sec=time.monotonic() - started,
            )
        t_dispatch_sec = round(time.monotonic() - t_dispatch_start, 4)

        actual_executor = self.settings.harness_for(role)
        actual_model = self.settings.model_for(role) or "default"
        feature = planning_context.get("feature_id")
        if feature:
            from autodev.speckit.execution import record
            from autodev.db import now_iso
            record(self.db, feature, {"run_id": run_id, "project_id": pid, "role": role, "pool": selected,
                                     "executor": actual_executor, "model": actual_model, "effort": actual_effort,
                                     "step": step, "round": 0, "started_at": now_iso()})
        (log_dir / "loopforge_run.json").write_text(
            json.dumps(
                {
                    "project_id": pid,
                    "role": role,
                    "executor": actual_executor,
                    "model": actual_model,
                    "effort": actual_effort,
                    "step": step,
                    "risk": risk,
                    "workflow_id": wf_id,
                    "issue_id": issue_id,
                    "run_id": run_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        self._activity(
            "command",
            f"已派发到 LoopForge 规划 SOP（{role} · {actual_executor}/{actual_model} · effort={actual_effort or 'default'} · Issue {issue_id} / Run {run_id[:12]}）",
            None,
        )

        def _on_progress(_st: str, summary: str, _run: dict[str, Any]) -> None:
            self._activity("message", f"[{stage_key}] {summary}", None)

        def _mark_plan_dispatch(final_state: str) -> None:
            from autodev.db import now_iso
            self.db._exec(
                "UPDATE dispatch_records SET state=?, updated_at=? "
                "WHERE lf_run_id=? AND state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')",
                (final_state, now_iso(), run_id),
            )

        poll_timing: dict[str, Any] = {}
        t_poll_start = time.monotonic()
        try:
            ok, run, msg = await poll_loopforge_run(
                client,
                run_id,
                poll_interval=self.poll_interval,
                stop_event=self.stop_event,
                timeout_sec=float(self.settings.agent_timeout_sec),
                on_progress=_on_progress,
                db=self.db,
                timing_out=poll_timing,
            )
        except (asyncio.CancelledError, Exception):
            _mark_plan_dispatch("NEEDS_CONFIRMATION")
            raise
        t_poll_sec = round(time.monotonic() - t_poll_start, 4)
        usage = extract_run_usage(run) if run else Usage(telemetry_known=False, source="unknown")

        def _write_timing(final_ok: bool, t_artifact_sec: float) -> dict[str, Any]:
            bundle_path = self.ws.state_dir / "baselines" / f"plan-{stage_key}.json"
            bundle_bytes = bundle_path.stat().st_size if bundle_path.is_file() else 0
            rec = {
                "stage_key": stage_key,
                "project_id": pid,
                "issue_id": issue_id,
                "run_id": run_id,
                "role": role,
                "executor": actual_executor,
                "model": actual_model,
                "effort": actual_effort,
                "ok": final_ok,
                "prompt_bytes": len(prompt.encode("utf-8")),
                "schema_bytes": len(schema_text.encode("utf-8")),
                "bundle_bytes": bundle_bytes,
                "phases_sec": {
                    "resolve_pool": t_resolve_sec,
                    "ensure_ready": t_ready_sec,
                    "dispatch": t_dispatch_sec,
                    "poll_run": t_poll_sec,
                    "artifact_fetch": t_artifact_sec,
                    "total": round(time.monotonic() - started, 4),
                },
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "telemetry_known": usage.telemetry_known,
                    "source": usage.source,
                },
                "poll_timing": poll_timing,
            }
            (log_dir / "timing.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), "utf-8")
            return rec

        if not ok:
            timing = _write_timing(False, 0.0)
            duration = time.monotonic() - started
            terminal = run and run.get("status") in ("failed", "cancelled", "succeeded")
            _mark_plan_dispatch("FAILED" if terminal else "NEEDS_CONFIRMATION")
            return AgentResult(
                False,
                1,
                run_id,
                extract_run_text(run) if run else "",
                usage=usage,
                error=msg,
                log_dir=log_dir,
                duration_sec=duration,
                timed_out="超时" in msg,
                timing=timing,
            )

        t_art_start = time.monotonic()
        raw_text = ""
        try:
            raw_text = await client.named_artifact(run, "vernal-result.json")
        except LoopForgeError:
            raw_text = extract_run_text(run)
        t_art_sec = round(time.monotonic() - t_art_start, 4)

        (log_dir / "final_message.txt").write_text(raw_text, "utf-8")
        structured = extract_json(raw_text)
        if structured is None:
            timing = _write_timing(False, t_art_sec)
            duration = time.monotonic() - started
            _mark_plan_dispatch("FAILED")
            return AgentResult(
                False,
                1,
                run_id,
                raw_text,
                usage=usage,
                error=f"LoopForge Run {run_id} 未产出合法的 vernal-result.json",
                log_dir=log_dir,
                duration_sec=duration,
                timing=timing,
            )

        timing = _write_timing(True, t_art_sec)
        duration = time.monotonic() - started
        _mark_plan_dispatch("FINISHED")
        return AgentResult(
            ok=True,
            exit_code=0,
            session_id=run_id,
            final_message=raw_text,
            structured=structured,
            usage=usage,
            log_dir=log_dir,
            duration_sec=duration,
            timing=timing,
        )

    async def run_workspace_task(
        self,
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        schema_path: Path | None,
        role: str,
        allow_network: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        raise RuntimeError("LoopForge 模式下研发任务由 LoopForgeBridge 统一派发，不应直接调用 run_workspace_task")

    async def resume_workspace_task(
        self,
        session_id: str,
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        schema_path: Path | None,
        role: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        raise RuntimeError("LoopForge 模式下研发任务由 LoopForgeBridge 统一派发，不应直接调用 resume_workspace_task")

    async def health_check(self) -> tuple[bool, str]:
        ok, info = await self._client().health()
        return ok, json.dumps(info, ensure_ascii=False)
