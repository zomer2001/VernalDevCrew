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
            if not self._assets_ready or self.settings.selected_pool == "public_docker":
                await ensure_vernal_loopforge_assets(client, pid, self.settings)
                self._assets_ready = True
            from autodev.speckit.service import sync_rule
            await sync_rule(self,client,pid)
            return pid, plan_workflow_id(pid, role)

    def _build_issue_description(
        self,
        stage_key: str,
        prompt: str,
        schema_path: Path,
        role: str,
    ) -> str:
        schema_text = schema_path.read_text("utf-8")
        full_prompt = (
            f"{prompt}\n\n"
            f"## 严格输出格式（JSON Schema）\n\n"
            f"请将满足以下 JSON Schema 的唯一合法 JSON 对象（UTF-8 纯 JSON，不要包含 Markdown 围栏）"
            f"写入当前工作目录的 `vernal-result.json`：\n"
            f"```json\n{schema_text}\n```\n"
        )

        ctx_files = _collect_context_files(self.ws, self.db, self.settings)
        bundle_files = dict(ctx_files)
        bundle_files[".vernal-schema.json"] = schema_text
        bundle_files["PLAN_PROMPT.md"] = full_prompt

        bdir = self.ws.state_dir / "baselines"
        bdir.mkdir(parents=True, exist_ok=True)
        bundle_path = (bdir / f"plan-{stage_key}.json").resolve()
        bundle_path.write_text(json.dumps({"files": bundle_files}, ensure_ascii=False), "utf-8")

        need_extra_files = stage_key.startswith("speckit_") or bool(ctx_files) and (
            "inputs/" in prompt or "docs/PRD_SPEC.md" in prompt or "已真实存在的代码" in prompt or "contracts/" in prompt
        )
        inline_candidate = (
            f"## Vernal 架构规划任务（角色：`{role}`，阶段：`{stage_key}`）\n\n"
            f"{full_prompt}\n"
            "完成后确保当前目录存在合法的 `vernal-result.json`，勿修改业务源码。\n"
        )
        if not need_extra_files and len(inline_candidate.encode("utf-8")) <= _INLINE_DESC_LIMIT:
            return inline_candidate

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
            "请先在当前工作区执行下面这行命令还原完整任务说明 `PLAN_PROMPT.md`、`.vernal-schema.json` 与只读上下文文件：\n"
            f"```bash\n{restore_cmd}\n```\n"
            "然后完整阅读 `PLAN_PROMPT.md`，按其中要求的 JSON Schema 将唯一的合法 JSON 对象写入当前目录的 `vernal-result.json`。\n"
            "切勿修改无关业务源码；完成后若存在 `submit_gate_result` MCP 工具请调用一次 `decision=\"pass\"`。\n"
        )

    async def _dispatch_plan(
        self, client: LoopForgeClient, pid: str, stage_key: str, title: str, description: str,
        wf_id: str, prior_candidate: str, request_signature: str,
    ) -> tuple[str, str]:
        # Only serialize issue writes and dispatch. Runner execution and polling remain parallel.
        async with self._dispatch_lock:
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
                run_id = await accept_task(self.db, receipt, client, dispatched, timeout=self.settings.agent_timeout_sec)
                return issue_id, run_id

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

        try:
            pid, wf_id = await self._ensure_ready(client, role)
            await public_pool.check_workflow_sandbox(self.settings, client, wf_id, selected)
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

        description = self._build_issue_description(stage_key, prompt, schema_path, role)
        try:
            signature = hashlib.sha256(json.dumps([prompt, schema_path.read_text("utf-8"), selected, wf_id]).encode()).hexdigest()
            issue_id, run_id = await self._dispatch_plan(client, pid, stage_key, title, description, wf_id,
                                                         prior_candidate, signature)
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

        actual_executor = self.settings.harness_for(role)
        actual_model = self.settings.model_for(role) or "default"
        (log_dir / "loopforge_run.json").write_text(
            json.dumps(
                {
                    "project_id": pid,
                    "role": role,
                    "executor": actual_executor,
                    "model": actual_model,
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
            f"已派发到 LoopForge 规划 SOP（{role} · {actual_executor}/{actual_model} · Issue {issue_id} / Run {run_id[:12]}）",
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

        try:
            ok, run, msg = await poll_loopforge_run(
                client,
                run_id,
                poll_interval=self.poll_interval,
                stop_event=self.stop_event,
                timeout_sec=float(self.settings.agent_timeout_sec),
                on_progress=_on_progress,
            )
        except (asyncio.CancelledError, Exception):
            _mark_plan_dispatch("NEEDS_CONFIRMATION")
            raise
        duration = time.monotonic() - started
        usage = extract_run_usage(run) if run else Usage()

        if not ok:
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
            )

        raw_text = ""
        try:
            raw_text = await client.named_artifact(run, "vernal-result.json")
        except LoopForgeError:
            raw_text = extract_run_text(run)

        (log_dir / "final_message.txt").write_text(raw_text, "utf-8")
        structured = extract_json(raw_text)
        if structured is None:
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
            )

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
