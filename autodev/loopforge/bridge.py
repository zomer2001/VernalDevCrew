"""LoopForge 桥接器：把 Goal 大脑的单任务派发为 LoopForge Issue/SOP Run，并以 LoopForge 为单任务执行真相源对账。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from autodev.config import Settings
from autodev.db import Database, Task
from autodev.loopforge.bootstrap import classify_lane, ensure_vernal_loopforge_assets, lane_workflow_id
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.models import TaskStatus


def format_issue_description(task: Task, squad_name: str, contract_hint: str = "") -> str:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
    paths = "\n".join(f"- `{p}`" for p in task.allowed_paths)
    cmds = "\n".join(f"- `{c}`" for c in task.verify_cmds)
    return (
        f"## Vernal 架构编排派发任务 `{task.task_id}`\n\n"
        f"- **里程碑**：{task.milestone_id}\n"
        f"- **研发组**：{squad_name}（`{task.squad}`）\n"
        f"- **角色**：{task.role}\n\n"
        f"### 任务目标\n{task.description}\n\n"
        f"### 验收标准\n{criteria}\n\n"
        f"### 允许修改路径白名单（Script 门禁会校验 `autodev guard --task {task.task_id}`）\n{paths}\n\n"
        f"### 必跑验证命令\n{cmds}\n"
        + (f"\n### 契约提示\n{contract_hint}\n" if contract_hint else "")
    )


class LoopForgeBridge:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.client = LoopForgeClient(settings.loopforge_url, settings.effective_loopforge_token())

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
            return await ensure_vernal_loopforge_assets(self.client, pid, self.settings)
        except LoopForgeError as exc:
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
        vernal_base_url: str = "http://127.0.0.1:8787",
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

        # 派单前预检：若已绑定个人执行机但全部处于 offline，直接中止派单避免连刷失败记录
        if not task.loopforge_run_id:
            try:
                runners = await self.client.list_runners()
                if runners and not any(isinstance(r, dict) and r.get("status") == "online" for r in runners):
                    names = ", ".join(str(r.get("name") or r.get("id")) for r in runners if isinstance(r, dict))
                    return False, f"执行器当前离线（{names}），已暂停派单避免产生无效失败记录"
            except LoopForgeError:
                pass

        trimmed_ctx = context
        if len(trimmed_ctx.encode("utf-8")) > 16_000:
            trimmed_ctx = trimmed_ctx.encode("utf-8")[:16_000].decode("utf-8", errors="ignore") + "\n…（上下文已按单任务上限截断）"
        description = format_issue_description(task, squad_name) + "\n" + trimmed_ctx
        if inputs is not None:
            inline_json = json.dumps(inputs, ensure_ascii=False)
            if len(inline_json.encode("utf-8")) <= 20_000:
                description += (
                    "\n本次采用文件交接。在空工作区按下面 JSON 还原上游文件（同时存一份 `.vernal-baseline.json`），然后实施任务并运行验证。"
                    "将新增或修改的 UTF-8 文件完整保存为 vernal-files.json，作为 SOP 声明的同名产物。"
                    '格式 {"files":{"相对路径":"完整文件内容"}}，删除使用 null。只提交本任务的改动。\n'
                    + inline_json
                )
            else:
                api_url = f"{vernal_base_url.rstrip('/')}/vernal-api/tasks/{task.task_id}/baseline.json"
                file_list = ", ".join(f"`{k}`" for k in sorted(inputs))
                restore_cmd = (
                    "python3 -c 'import json,pathlib,urllib.request; "
                    f"p=pathlib.Path({baseline_file!r}); "
                    f"d=json.loads(p.read_text(\"utf-8\")) if p.is_file() else json.loads(urllib.request.urlopen({api_url!r}).read().decode(\"utf-8\")); "
                    "pathlib.Path(\".vernal-baseline.json\").write_text(json.dumps(d,ensure_ascii=False),\"utf-8\"); "
                    "[((f:=pathlib.Path(k)).parent.mkdir(parents=True,exist_ok=True), f.write_text(v,\"utf-8\")) for k,v in d.items()]'"
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

        repo_id = self.settings.loopforge_repository_id if inputs is None else ""
        base_br = (task.branch_name or self.settings.base_branch) if inputs is None and self.settings.loopforge_repository_id else ""

        issue_id = task.loopforge_issue_id
        is_rerun = False
        workflow_id = await self.pick_workflow_id(task)
        if not issue_id:
            issue = await self.client.create_issue(
                project_id=self.settings.loopforge_project_id,
                title=f"[{task.task_id}] {task.title}",
                description=description,
                workflow_id=workflow_id,
                repository_id=repo_id,
                base_branch=base_br,
            )
            issue_id = str(issue.get("id") or "")
            if not issue_id:
                return False, f"LoopForge 创建 Issue 未返回 id: {issue}"
            self.db.update_task(task.task_id, loopforge_issue_id=issue_id)
            self.db.add_event(
                "loopforge.issue_created",
                f"已在 LoopForge 创建需求 {issue_id}",
                task_id=task.task_id,
                actor="VernalBridge",
            )
        elif not task.loopforge_run_id:
            is_rerun = True
            try:
                await self.client.update_issue(
                    issue_id=issue_id,
                    project_id=self.settings.loopforge_project_id,
                    title=f"[{task.task_id}] {task.title}",
                    description=description,
                    workflow_id=workflow_id,
                    repository_id=repo_id,
                    base_branch=base_br,
                )
            except LoopForgeError:
                pass

        run_id = task.loopforge_run_id
        if not run_id:
            dispatched = await self.client.dispatch_issue(issue_id, rerun=is_rerun) if is_rerun else await self.client.dispatch_issue(issue_id)
            run_id = str(dispatched.get("latestRunId") or dispatched.get("runId") or "")
            if not run_id:
                return False, f"派发需求 {issue_id} 未返回 Run ID"
            self.db.update_task(task.task_id, loopforge_run_id=run_id)
        self.db.update_slot(slot_id, current_action=f"LoopForge SOP 运行中（Run {run_id[:12]}）…")
        self.db.add_event(
            "loopforge.dispatched",
            f"已派发到 LoopForge 执行（Issue {issue_id} / Run {run_id}）",
            task_id=task.task_id,
            actor="VernalBridge",
        )

        # 以 LoopForge 为单任务执行真相源，每 2.5 秒幂等对账
        last_summary = ""
        auto_passed_keys: set[tuple[str, Any, Any]] = set()
        while not stop_event.is_set():
            try:
                run = await self.client.get_run(run_id)
            except LoopForgeError as exc:
                return False, f"查询 LoopForge Run {run_id} 失败：{exc}"
            st = str(run.get("status") or "")
            nodes = run.get("nodes") or {}
            wf_nodes = {
                str(n.get("id")): n
                for n in ((run.get("workflowSnapshot") or {}).get("nodes") or [])
                if isinstance(n, dict) and n.get("id")
            }
            active_nodes = [
                f"{nid}:{nr.get('status')}"
                for nid, nr in nodes.items()
                if isinstance(nr, dict) and nr.get("status") in ("running", "waiting", "waiting_approval")
            ]

            # 若非人工审批节点仅因 CLI 未回调 submit_gate_result 而挂起，自动放行交由确定性门禁与审查把关
            if st == "waiting":
                did_auto_pass = False
                for nid, nr in nodes.items():
                    if not isinstance(nr, dict) or nr.get("status") not in ("waiting", "waiting_approval"):
                        continue
                    ntype = str((wf_nodes.get(nid) or {}).get("type") or "")
                    nerr = str(nr.get("error") or "")
                    key = (nid, nr.get("attempt"), nr.get("reworkCount"))
                    if ntype != "approval" and "submit_gate_result" in nerr and key not in auto_passed_keys:
                        auto_passed_keys.add(key)
                        try:
                            await self.client.approve_run(
                                run_id,
                                comment="Vernal 自动放行已完成 CLI 回合（submit_gate_result 兜底）",
                                node_id=nid,
                            )
                            self.db.add_event(
                                "loopforge.auto_passed",
                                f"节点 {nid} 执行完毕，已自动放行 submit_gate_result 卡点（Run {run_id[:12]}）",
                                task_id=task.task_id,
                                actor="VernalBridge",
                            )
                            did_auto_pass = True
                        except LoopForgeError as exc:
                            self.db.add_event(
                                "loopforge.auto_pass_warning",
                                f"自动放行节点 {nid} 失败：{exc}",
                                task_id=task.task_id,
                                actor="VernalBridge",
                            )
                if did_auto_pass:
                    await asyncio.sleep(1.0)
                    continue

            summary = f"LoopForge [{st}] " + (", ".join(active_nodes) or "处理中")
            if summary != last_summary:
                last_summary = summary
                self.db.update_slot(slot_id, current_action=summary[:200])
                self.db.add_event(
                    "loopforge.progress",
                    summary,
                    task_id=task.task_id,
                    actor="VernalBridge",
                )
                if st == "waiting":
                    self.db.update_task(task.task_id, status=TaskStatus.REVIEWING)
                    self.db.add_event(
                        "loopforge.waiting_gate",
                        f"LoopForge Run {run_id} 正在等待人工审批门禁（可在左侧「待我确认」直接审批）",
                        task_id=task.task_id,
                        actor="VernalBridge",
                    )
            if st == "succeeded":
                return True, f"LoopForge Run {run_id} 已成功交付"
            if st in ("failed", "cancelled"):
                node_errs = [
                    f"{nid}: {nr.get('error')}"
                    for nid, nr in nodes.items()
                    if isinstance(nr, dict) and nr.get("error")
                ]
                err = str(run.get("error") or ("; ".join(node_errs)) or f"LoopForge Run {run_id} 结束于 {st}")
                return False, err
            await asyncio.sleep(2.5)

        return False, "调度器收到停止信号，已暂停对账（LoopForge 远端任务仍保留现场）"
