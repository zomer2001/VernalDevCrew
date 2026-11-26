"""异步 DAG 调度器：多研发组轮转派单、工位槽位实况、滚动波次展开、总监自动仲裁与里程碑集成。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
from tempfile import TemporaryDirectory, mkdtemp
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from autodev import gitops, local_git, verifier
from autodev.harnesses.link_worker import personal_worker, track_issue
from autodev.artifacts import FileExchange, materialize, snapshot, apply_output
from autodev.loopforge.bridge import LoopForgeBridge, extract_run_usage, format_issue_description, sign_context_sig
from autodev.loopforge.client import LoopForgeError
from autodev.dag import topo_sort
from autodev.db import Milestone, Task, now_iso
from autodev.engine import Engine, OpsError, PlanningError, current_actor, current_slot, current_task
from autodev.models import (
    ACTIVE_TASK_STATUSES,
    MilestoneStatus,
    ProjectStatus,
    ReviewVerdict,
    TaskStatus,
    WorkerReport,
)
from autodev.prompts import (
    build_coder_prompt,
    build_fallback_context,
    build_fix_prompt,
    build_review_prompt,
    read_contracts,
    summarize_verify,
)


def lock_is_held(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        fp = lock_path.open("a+")
    except OSError:
        return False
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        fp.close()


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fp: TextIO | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a+")
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fp.close()
            self._fp = None
            raise OpsError("已有另一个调度器进程正在运行") from exc
        self._fp.seek(0)
        self._fp.truncate()
        self._fp.write(f"{os.getpid()}\n")
        self._fp.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self._fp is not None:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            finally:
                self._fp.close()
                self._fp = None


class Scheduler:
    def __init__(self, engine: Engine, *, ci_transport: object = None):
        self.engine = engine
        self.ws = engine.ws
        self.db = engine.db
        self.settings = engine.settings
        self.stop_event = asyncio.Event()
        self.running_tasks: dict[str, asyncio.Task[None]] = {}
        self.task_slots: dict[str, str] = {}
        self.task_exec_seqs: dict[str, int] = {}
        self._arbitration_lock = asyncio.Lock()
        self.ci_transport = ci_transport

    def request_stop(self) -> None:
        self.stop_event.set()

    def _squad_name(self, squad_id: str) -> str:
        for s in self.db.list_squads():
            if s["squad_id"] == squad_id:
                return str(s["name"])
        return squad_id

    # ---------- 主循环 ----------
    def check_runtime(self) -> None:
        local_source = self.db.meta_get("git_delivery_source")
        if self.settings.collaboration_mode == "git" and self.db.meta_get("source_mode") == "local_folder" and not local_source:
            raise OpsError("旧工作区位于原仓库内，请用新项目名称重新接入本机 Git 目录")
        if self.settings.runtime_backend == "loopforge":
            if not self.settings.service_token():
                raise OpsError("后台调度缺少部署服务身份，请配置 AGENTFLOW_TOKEN 或该项目的专用服务令牌")
            if not LoopForgeBridge(self.settings, self.db).is_configured():
                raise OpsError("请先登录并绑定 LoopForge 项目，调度尚未启动")
            if self.settings.collaboration_mode == "git" and not (local_source or self.settings.loopforge_repository_id):
                raise OpsError("Git 交付需要绑定与本地 origin 对应的 LoopForge 仓库")

    async def run(self, watch: bool = False) -> ProjectStatus:
        self.check_runtime()
        if self.engine.status == ProjectStatus.PLANNED:
            raise OpsError("立项蓝图尚未批准，请先批准蓝图后再启动研发")
        if self.engine.status in (ProjectStatus.CREATED, ProjectStatus.INGESTED, ProjectStatus.SPECIFIED):
            raise OpsError("尚未生成立项蓝图，请先发起立项评审")

        self.stop_event.clear()
        with _FileLock(self.ws.lock_path):
            self.db.ensure_slots(self.settings.max_concurrency)
            self.db.reset_all_slots()
            await self._recover_remote_dispatches()
            self._recover_stale()
            self.engine.set_status(ProjectStatus.RUNNING)
            self.db.add_event(
                "scheduler.started",
                f"编排中枢启动（并发工位 {self.settings.max_concurrency}，重试阈值 {self.settings.max_retries}）",
                actor="Orchestrator",
            )
            try:
                while not self.stop_event.is_set():
                    self._reap_finished()
                    ms = self._active_milestone()
                    if ms is None:
                        if not self.running_tasks:
                            self.engine.set_status(ProjectStatus.COMPLETED)
                            break
                    elif ms.status == MilestoneStatus.PENDING:
                        if not ms.planned:
                            if not self.running_tasks:
                                try:
                                    await self.engine.expand_milestone_wave(ms.milestone_id)
                                except (PlanningError, OpsError, Exception) as exc:
                                    self._fail_milestone(ms, f"研发总监滚动拆解失败：{exc}")
                                continue
                        else:
                            self._unlock_ready(ms.milestone_id)
                            ms_tasks = self.db.list_tasks(milestone_id=ms.milestone_id)
                            ms_tasks = [t for t in ms_tasks if t.status != TaskStatus.CANCELLED]
                            if not ms_tasks:
                                self.db.update_milestone(ms.milestone_id, status=MilestoneStatus.MERGED)
                                continue
                            if (
                                ms_tasks
                                and all(t.status in (TaskStatus.APPROVED, TaskStatus.MERGED) for t in ms_tasks)
                                and not self.running_tasks
                            ):
                                await self._integrate_milestone(ms)
                                continue
                            self._dispatch_ready(ms.milestone_id)
                    elif ms.status == MilestoneStatus.CI_RUNNING:
                        from autodev.ci.service import list_ci_runs, poll_ci_run

                        ci_list = list_ci_runs(self.db, ms.milestone_id)
                        active_ci = next((r for r in ci_list if r["status"] in ("QUEUED", "RUNNING")), None)
                        if active_ci is not None:
                            await poll_ci_run(self.engine, int(active_ci["ci_run_id"]), transport=self.ci_transport)  # type: ignore[arg-type]
                            refreshed_ms = self.db.get_milestone(ms.milestone_id)
                            if refreshed_ms and refreshed_ms.status != MilestoneStatus.CI_RUNNING:
                                continue
                            try:
                                await asyncio.wait_for(self.stop_event.wait(), timeout=0.05 if self.ci_transport else 2.0)
                            except TimeoutError:
                                continue
                            break

                    if self.running_tasks:
                        stop_waiter = asyncio.create_task(self.stop_event.wait())
                        await asyncio.wait(
                            [*self.running_tasks.values(), stop_waiter],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not stop_waiter.done():
                            stop_waiter.cancel()
                        self._reap_finished()
                    elif watch:
                        try:
                            await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                        except TimeoutError:
                            pass
                    else:
                        break
            finally:
                for t in list(self.running_tasks.values()):
                    t.cancel()
                if self.running_tasks:
                    await asyncio.gather(*self.running_tasks.values(), return_exceptions=True)
                self.running_tasks.clear()
                self.task_slots.clear()
                self.db.reset_all_slots()
                all_merged = bool(self.db.list_milestones()) and all(
                    m.status == MilestoneStatus.MERGED for m in self.db.list_milestones()
                )
                final_status = ProjectStatus.COMPLETED if all_merged else ProjectStatus.STALLED
                active = self._active_milestone()
                if active and active.status == MilestoneStatus.AWAITING_MERGE:
                    final_status = ProjectStatus.APPROVED
                self.engine.set_status(final_status)
                self.db.add_event(
                    "scheduler.stopped",
                    f"编排中枢停止，当前项目状态：{final_status.value}",
                    actor="Orchestrator",
                )
        return self.engine.status

    async def _recover_remote_dispatches(self):
        if self.settings.runtime_backend != "loopforge":
            return
        from autodev import public_pool
        from autodev.loopforge.client import LoopForgeClient, run_project_id
        from autodev.loopforge.dispatch import record, reserve
        for task in self.db.list_tasks():
            if task.status not in ACTIVE_TASK_STATUSES:
                continue
            if not self.db._query("SELECT 1 FROM dispatch_records WHERE task_id=? LIMIT 1", (task.task_id,)):
                pool = self.db.meta_get(f"pool.run:{task.loopforge_run_id}") or self.settings.execution_pool
                reserve(self.db, task.task_id, task.exec_seq, task.retry_count + 1, pool, "legacy",
                        task.loopforge_issue_id, task.loopforge_run_id)
        rows = self.db._query("SELECT d.* FROM dispatch_records d JOIN tasks t ON t.task_id=d.task_id "
                              "WHERE d.state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION','PAUSED') "
                              "AND d.dispatch_id=(SELECT MAX(x.dispatch_id) FROM dispatch_records x WHERE x.task_id=d.task_id)")
        for row in rows:
            receipt = dict(row)
            task = self.db.get_task(receipt["task_id"])
            if not task or task.status not in {*ACTIVE_TASK_STATUSES, TaskStatus.READY, TaskStatus.FUSED}:
                continue
            client = LoopForgeClient(self.settings.loopforge_url, public_pool.service_token()
                                     if receipt["pool"] == "public_docker" else self.settings.service_token())
            try:
                run_id = receipt["lf_run_id"]
                if not run_id and receipt["lf_task_id"]:
                    native = await client.get_task(receipt["lf_task_id"])
                    if native.get("issueId") != receipt["issue_id"] or native.get("id") != receipt["lf_task_id"]:
                        raise LoopForgeError("原生工单与派单记录不符")
                    run_id = native.get("runId")
                    if not run_id and native.get("status") in ("failed", "cancelled"):
                        record(self.db, receipt, "FAILED")
                        raise LoopForgeError("原生 Task 已终止")
                if run_id:
                    run = await client.get_run(run_id)
                    if run_project_id(run) != self.settings.loopforge_project_id:
                        raise LoopForgeError("原生运行归属不符")
                    if run.get("status") == "paused" or receipt["state"] in ("DISPATCHING", "NEEDS_CONFIRMATION") and receipt["command_key"]:
                        raise LoopForgeError("需确认原生运行状态")
                    record(self.db, receipt, "RUNNING", lf_run_id=run_id)
                elif not receipt["lf_task_id"]:
                    if receipt["issue_id"]:
                        await client.list_issue_runs(receipt["issue_id"])
                    record(self.db, receipt, "NEEDS_CONFIRMATION")
                    raise LoopForgeError("派单结果待人工确认")
                self.db._exec("UPDATE tasks SET loopforge_run_id=?,status='READY' WHERE task_id=? AND exec_seq=?",
                              (run_id or None, task.task_id, task.exec_seq))
            except LoopForgeError as exc:
                self.db._exec("UPDATE tasks SET status='FUSED',failure_reason=? WHERE task_id=? AND exec_seq=?",
                              (str(exc), task.task_id, task.exec_seq))

    def _recover_stale(self) -> None:
        for t in self.db.list_tasks():
            if t.status in ACTIVE_TASK_STATUSES:
                self.db.update_task(t.task_id, status=TaskStatus.READY, assigned_slot=None, active_agent_label=None)
                self.db.add_event(
                    "task.recovered",
                    f"从异常中断状态 {t.status.value} 恢复为 READY",
                    task_id=t.task_id,
                    actor="Orchestrator",
                )
        for m in self.db.list_milestones():
            if m.status == MilestoneStatus.INTEGRATING:
                self.db.update_milestone(m.milestone_id, status=MilestoneStatus.PENDING)

    def _active_milestone(self) -> Milestone | None:
        for m in self.db.list_milestones():
            if m.status != MilestoneStatus.MERGED:
                return m
        return None

    def _unlock_ready(self, milestone_id: str) -> None:
        all_tasks = {t.task_id: t for t in self.db.list_tasks()}
        for t in all_tasks.values():
            if t.milestone_id != milestone_id or t.status != TaskStatus.BLOCKED:
                continue
            if all(
                d in all_tasks and all_tasks[d].status in (TaskStatus.APPROVED, TaskStatus.MERGED)
                for d in t.depends_on
            ):
                self.db.update_task(t.task_id, status=TaskStatus.READY)
                self.db.add_event(
                    "task.ready",
                    f"前置依赖已就绪，解锁 {t.task_id}（{self._squad_name(t.squad)}）进入派单队列",
                    task_id=t.task_id,
                    actor="Orchestrator",
                )

    def _free_slots(self) -> list[str]:
        used = set(self.task_slots.values())
        return [f"slot-{i}" for i in range(1, self.settings.max_concurrency + 1) if f"slot-{i}" not in used]

    def _dispatch_ready(self, milestone_id: str) -> None:
        free = self._free_slots()
        if not free:
            return
        ready = [
            t for t in self.db.list_tasks(milestone_id=milestone_id, status=TaskStatus.READY)
            if t.task_id not in self.running_tasks
        ]
        if not ready:
            return

        # WP-7：项目级 Token 预算上限守卫（超限时暂停新任务派发并记录告警事件）
        if int(self.settings.token_budget_limit or 0) > 0:
            usage_map = self.db.usage_by_task()
            used_tokens = sum(u["input"] + u["output"] + u["reasoning"] for u in usage_map.values())
            if used_tokens >= int(self.settings.token_budget_limit):
                if self.db.meta_get("budget_paused_notified") != str(self.settings.token_budget_limit):
                    self.db.meta_set("budget_paused_notified", str(self.settings.token_budget_limit))
                    self.db.add_event(
                        "governance.budget_exceeded",
                        f"项目累计消耗 Token ({used_tokens}) 已达预算上限 ({self.settings.token_budget_limit})，已暂停自动派单",
                        actor="GovernanceGuard",
                    )
                self.request_stop()
                return

        # H3：若流程模板开启详细设计阻塞门禁，未通过前仅允许 bootstrap / contract 任务先行执行
        from autodev.process.runner import blocking_document_gates, ensure_gate_record, is_gate_passed, load_template

        tpl = load_template(self.ws, self.settings)
        if any(g != "detailed_design_approval" for g in blocking_document_gates(self.db, tpl, milestone_id)):
            return
        dd_stage = next((s for s in tpl.stages if s.id == "detailed_design" and s.enabled), None)
        if dd_stage and any(g.id == "detailed_design_approval" and g.blocking for g in dd_stage.gates):
            if not is_gate_passed(self.db, "detailed_design_approval", "milestone", milestone_id):
                ensure_gate_record(
                    self.db,
                    stage_id="detailed_design",
                    gate_id="detailed_design_approval",
                    kind="human_approval",
                    scope_type="milestone",
                    scope_id=milestone_id,
                    summary=f"里程碑 {milestone_id} 详细设计待评审，暂缓派发编码实现任务",
                )
                ready = [t for t in ready if t.role in ("bootstrap", "contract")]
                if not ready:
                    return

        # 统计各研发组当前在跑任务数，跨组 Round-Robin 公平派单
        active_by_squad: dict[str, int] = {}
        for tid in self.running_tasks:
            cur = self.db.get_task(tid)
            if cur:
                active_by_squad[cur.squad] = active_by_squad.get(cur.squad, 0) + 1

        remaining = list(ready)
        for slot_id in free:
            if not remaining:
                break
            remaining.sort(key=lambda t: (active_by_squad.get(t.squad, 0), t.priority, t.task_id))
            task = remaining.pop(0)
            active_by_squad[task.squad] = active_by_squad.get(task.squad, 0) + 1
            squad_cn = self._squad_name(task.squad)
            ident = self.engine.agent_identity(task.role, squad_cn)
            self.task_slots[task.task_id] = slot_id
            self.db.update_task(
                task.task_id,
                status=TaskStatus.IN_PROGRESS,
                assigned_slot=slot_id,
                active_agent_label=ident["label"],
            )
            self.db.update_slot(
                slot_id,
                status="WORKING",
                squad=squad_cn,
                role=task.role,
                harness=ident["harness"],
                model=ident["model"],
                task_id=task.task_id,
                current_action=f"准备隔离工作树并派单 {task.task_id}…",
                started_at=now_iso(),
            )
            self.db.add_event(
                "orchestrator.dispatched",
                f"派发 {task.task_id} 至 {slot_id.upper()} → {ident['label']}（依赖：{', '.join(task.depends_on) or '无'}）",
                task_id=task.task_id,
                actor="Orchestrator",
            )
            self.running_tasks[task.task_id] = asyncio.create_task(self._run_task_safe(task.task_id, slot_id))

    def _reap_finished(self) -> None:
        for tid in [k for k, v in self.running_tasks.items() if v.done()]:
            task = self.running_tasks.pop(tid)
            slot_id = self.task_slots.pop(tid, None)
            if slot_id:
                self.db.release_slot(slot_id)
            if not task.cancelled():
                task.result()

    # ---------- 单任务 TDD 闭环 ----------
    async def _run_task_safe(self, task_id: str, slot_id: str) -> None:
        t_tok = current_task.set(task_id)
        s_tok = current_slot.set(slot_id)
        try:
            await self._execute_task(task_id, slot_id)
        except asyncio.CancelledError:
            seq = self.task_exec_seqs.get(task_id)
            if seq is not None:
                self.db.update_task_if_owned(
                    task_id,
                    expected_exec_seq=seq,
                    status=TaskStatus.READY,
                    assigned_slot=None,
                    active_agent_label=None,
                )
            else:
                cur = self.db.get_task(task_id)
                if cur and cur.status in ACTIVE_TASK_STATUSES:
                    self.db.update_task(task_id, status=TaskStatus.READY, assigned_slot=None, active_agent_label=None)
            raise
        except Exception as exc:
            seq = self.task_exec_seqs.get(task_id)
            if seq is not None and self.db.update_task_if_owned(
                task_id, expected_exec_seq=seq, status=TaskStatus.FUSED, assigned_slot=None,
                active_agent_label=None, failure_reason=f"内部异常: {exc}",
            ):
                self.db.add_event("task.fused", f"执行异常熔断: {exc}", task_id=task_id, actor="Orchestrator")
        finally:
            seq = self.task_exec_seqs.pop(task_id, None)
            cur = self.db.get_task(task_id)
            if cur and cur.status not in ACTIVE_TASK_STATUSES and (seq is None or cur.exec_seq == seq):
                if cur.assigned_slot == slot_id or cur.active_agent_label is not None:
                    self.db._exec(
                        "UPDATE tasks SET assigned_slot=NULL, active_agent_label=NULL, updated_at=?"
                        " WHERE task_id=? AND status NOT IN ('IN_PROGRESS','VERIFYING','REVIEWING')",
                        (now_iso(), task_id),
                    )
            self.db.release_slot(slot_id)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)

    def _prepare_worktree(self, task: Task) -> tuple[Path, str]:
        root = self.ws.root
        if task.worktree_path and Path(task.worktree_path).exists() and task.base_sha:
            return Path(task.worktree_path), task.base_sha

        wt = self.ws.worktrees_dir / task.task_id
        branch = f"autodev/{task.task_id}"
        gitops.remove_worktree(root, wt)
        gitops.delete_branch(root, branch)
        gitops.create_worktree(root, wt, branch, self.settings.base_branch)

        all_tasks = {t.task_id: t for t in self.db.list_tasks()}
        ancestors: set[str] = set()
        stack = list(task.depends_on)
        while stack:
            dep_id = stack.pop()
            if dep_id in ancestors or dep_id not in all_tasks:
                continue
            ancestors.add(dep_id)
            stack.extend(all_tasks[dep_id].depends_on)

        unmerged = [
            all_tasks[d]
            for d in ancestors
            if all_tasks[d].status == TaskStatus.APPROVED and all_tasks[d].candidate_commit_sha
        ]
        if unmerged:
            sub = {t.task_id: [d for d in t.depends_on if d in {u.task_id for u in unmerged}] for t in unmerged}
            for dep_id in topo_sort(sub):
                dep = all_tasks[dep_id]
                sha = dep.candidate_commit_sha
                assert sha is not None
                if gitops.is_ancestor(wt, sha):
                    continue
                ok, msg = gitops.merge(wt, sha, f"autodev: sync upstream {dep_id} into {task.task_id}")
                if not ok:
                    raise RuntimeError(f"合并上游任务 {dep_id} 到 {task.task_id} 基线时冲突：{msg}")

        base_sha = gitops.head_sha(wt)
        contract_hash = gitops.tree_fingerprint(wt, self.settings.protected_paths)
        self.db.update_task(
            task.task_id,
            worktree_path=str(wt),
            branch_name=branch,
            base_sha=base_sha,
            bound_contract_hash=contract_hash,
        )
        return wt, base_sha

    async def _execute_task(self, task_id: str, slot_id: str) -> None:
        exec_seq = self.db.claim_task_execution(task_id, allow_from_ready=True)
        if exec_seq is None:
            return
        self.task_exec_seqs[task_id] = exec_seq
        task = self.db.get_owned_task(task_id, expected_exec_seq=exec_seq)
        if task is None:
            return
        attempt = task.retry_count + 1
        squad_cn = self._squad_name(task.squad)
        coder_ident = self.engine.agent_identity(task.role, squad_cn)
        a_tok = current_actor.set(coder_ident["label"])

        exchange = FileExchange(self.ws, self.db)
        file_mode = self.settings.collaboration_mode == "files"
        pinned_base = self.db.meta_get(f"verified-base:{task.task_id}:{task.artifact_digest}") if file_mode and task.pending_action == "verify_only" else None
        before = (exchange.get(pinned_base) if pinned_base else exchange.baseline(task.depends_on)) if file_mode else {}
        protected = [] if task.role in ("contract", "bootstrap") else self.settings.protected_paths
        link = personal_worker(self.engine)
        try:
            if link:
                await track_issue(self.engine, task_id, "in_progress")
            if self.settings.runtime_backend == "loopforge" and not link:
                await self._execute_remote(task, squad_cn, slot_id, exchange, before, protected, exec_seq=exec_seq)
                return
            try:
                if file_mode:
                    wt = Path(mkdtemp(prefix="task-", dir=self.ws.worktrees_dir))
                    materialize(before, wt)
                    self.db.update_task_if_owned(task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, worktree_path=str(wt))
                    base_sha = ""
                else:
                    wt, base_sha = self._prepare_worktree(task)
            except Exception as exc:
                self._fuse(task, f"准备 worktree 基线失败：{exc}", expected_exec_seq=exec_seq, expected_attempt=attempt)
                return

            self.db.update_slot(slot_id, worktree_path=str(wt))
            task = self.db.get_task(task_id)
            assert task is not None
            slices = self.db.get_slices(task.doc_slice_ids)
            # 若存在 PM 规格切片 PRD-S01，自动附带投喂给各工位
            if "PRD-S01" not in task.doc_slice_ids:
                prd_slices = self.db.get_slices(["PRD-S01"])
                if prd_slices:
                    slices = [*prd_slices, *slices]

            verify_only = task.pending_action == "verify_only" and not file_mode
            if verify_only:
                self.db.update_task(task_id, pending_action=None)
            shared_env = self.engine.shared_env()

            # ---- 1. 编码阶段 ----
            if not verify_only:
                self.db.update_task(task_id, status=TaskStatus.IN_PROGRESS, active_agent_label=coder_ident["label"])
                self.db.update_slot(
                    slot_id,
                    squad=squad_cn,
                    role=task.role,
                    harness=coder_ident["harness"],
                    model=coder_ident["model"],
                    current_action=f"第 {attempt}/{task.max_retries} 轮编码与单测中…",
                )
                self.db.add_event(
                    "task.coding",
                    f"{coder_ident['label']} 开始第 {attempt}/{task.max_retries} 轮开发",
                    task_id=task_id,
                    actor=coder_ident["label"],
                )
                run_id = self.db.start_run(task_id, attempt, "CODE", coder_ident["harness"], agent_label=coder_ident["label"])
                log_dir = self.ws.runs_dir / task_id / f"attempt-{attempt}-code"
                contracts = read_contracts(wt, self.settings.protected_paths)
                allow_net = task.role == "bootstrap" and self.settings.bootstrap_network_access

                use_resume = bool(not file_mode and task.last_session_id and (task.retry_count > 0 or task.steer_note or task.last_feedback))
                if use_resume and task.last_session_id:
                    from autodev.context_bundle import build_task_input_bundle
                    fix_prompt = build_task_input_bundle(self.ws, self.db, task, source_root=wt) + build_fix_prompt(task, task.last_feedback or "", attempt, task.steer_note)
                    res = await self.engine.harness.resume_workspace_task(
                        task.last_session_id,
                        fix_prompt,
                        cwd=wt,
                        log_dir=log_dir,
                        schema_path=self.engine.schemas["worker_report"],
                        role=task.role,
                        extra_env=shared_env,
                    )
                    if not res.ok and not res.timed_out:
                        self.db.add_event(
                            "task.resume_fallback",
                            f"会话恢复失败（{res.error[:160]}），降级为新会话继续",
                            task_id=task_id,
                            actor=coder_ident["label"],
                        )
                        base_prompt = build_coder_prompt(
                            task, slices, contracts, self.settings, db=self.db, ws_root=self.ws.root, source_root=wt
                        )
                        full_prompt = base_prompt + build_fallback_context(
                            ("" if file_mode else gitops.diff_stat(wt, base_sha)), task.last_feedback or "", task.steer_note
                        )
                        res = await self.engine.harness.run_workspace_task(
                            full_prompt,
                            cwd=wt,
                            log_dir=self.ws.runs_dir / task_id / f"attempt-{attempt}-code-fallback",
                            schema_path=self.engine.schemas["worker_report"],
                            role=task.role,
                            allow_network=allow_net,
                            extra_env=shared_env,
                        )
                else:
                    prompt = build_coder_prompt(
                        task, slices, contracts, self.settings, db=self.db, ws_root=self.ws.root, source_root=wt
                    )
                    if task.steer_note or task.last_feedback:
                        prompt += build_fallback_context(
                            ("" if file_mode else gitops.diff_stat(wt, base_sha)), task.last_feedback or "", task.steer_note
                        )
                    res = await self.engine.harness.run_workspace_task(
                        prompt,
                        cwd=wt,
                        log_dir=log_dir,
                        schema_path=self.engine.schemas["worker_report"],
                        role=task.role,
                        allow_network=allow_net,
                        extra_env=shared_env,
                    )

                report: WorkerReport | None = None
                if res.structured:
                    try:
                        report = WorkerReport.model_validate(res.structured)
                    except ValidationError:
                        report = None

                self.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict=report.status if report else ("OK" if res.ok else "ERROR"),
                    summary_log=(report.summary if report else res.error or res.final_message)[:1000],
                    log_path=str(res.log_dir) if res.log_dir else None,
                )
                update_fields: dict[str, object] = {"steer_note": None}
                if res.session_id:
                    update_fields["last_session_id"] = res.session_id
                    self.db.update_slot(slot_id, session_id=res.session_id)
                self.db.update_task_if_owned(task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, **update_fields)
                task = self._task_still_owned(task_id, attempt, expected_exec_seq=exec_seq)
                if task is None:
                    return

                if report and report.status in ("NEED_CONTRACT_UPDATE", "BLOCKED_BY_SPEC"):
                    reason = f"[{report.status}] {report.notes or report.summary}"
                    await self._escalate_or_fuse(task, reason, wt, event_type="task.escalated", expected_exec_seq=exec_seq, expected_attempt=attempt)
                    return

                if not res.ok:
                    await self._fail_attempt(task, f"编码会话执行失败：{res.error}", wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                    return

            # ---- 2. 确定性门禁（路径白名单 + 验证命令） ----
            if self._task_still_owned(task_id, attempt, expected_exec_seq=exec_seq) is None:
                return
            if not self.db.update_task_if_owned(
                task_id,
                expected_exec_seq=exec_seq,
                expected_attempt=attempt,
                status=TaskStatus.VERIFYING,
                active_agent_label="[门禁系统] PathGuard + TestRunner",
            ):
                return
            self.db.update_slot(slot_id, current_action=f"第 {attempt} 轮门禁校验（白名单 + 单元测试）…")
            self.db.add_event(
                "task.verifying",
                f"第 {attempt} 轮确定性门禁检查中（路径白名单 + 验证命令）",
                task_id=task_id,
                actor="GateVerifier",
            )
            v_run_id = self.db.start_run(task_id, attempt, "VERIFY", "local", agent_label="GateVerifier")
            v_log = self.ws.runs_dir / task_id / f"attempt-{attempt}-verify.log"

            protected = [] if task.role in ("contract", "bootstrap") else self.settings.protected_paths
            artifact_key = None
            if file_mode:
                try:
                    artifact_key = exchange.patch(task, before, snapshot(wt), protected)
                except (OSError, ValueError) as exc:
                    self.db.finish_run(v_run_id, exit_code=1, verdict="INVALID_OUTPUT", summary_log=str(exc))
                    await self._fail_attempt(task, str(exc), wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                    return
            else:
                guard = verifier.guard_worktree(wt, task.allowed_paths, protected)
                if not guard.ok:
                    msg = guard.describe()
                    v_log.parent.mkdir(parents=True, exist_ok=True)
                    v_log.write_text(msg, "utf-8")
                    self.db.finish_run(v_run_id, exit_code=1, verdict="PATH_VIOLATION", summary_log=msg[:1000], log_path=str(v_log))
                    await self._fail_attempt(task, msg, wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                    return

                # 以具体研发组与智能体真名署名提交！
                gitops.commit_all(
                    wt,
                    f"autodev({task.squad or task.module}): {task_id} attempt {attempt} - {task.title}",
                    author_name=coder_ident["author_name"],
                    author_email=coder_ident["author_email"],
                )
                if gitops.commits_since(wt, base_sha) == 0:
                    msg = "工作区相对基线没有任何代码或测试改动"
                    self.db.finish_run(v_run_id, exit_code=1, verdict="EMPTY_DIFF", summary_log=msg, log_path=str(v_log))
                    await self._fail_attempt(task, msg, wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                    return

            results = []
            verified = set()
            from autodev.trace.candidate import candidate_test_markers, verified_pytest_requirements
            import shlex

            markers = candidate_test_markers(wt)
            with TemporaryDirectory(dir=self.ws.runs_dir) as temp:
                report = Path(temp) / "pytest.xml"
                env = {**shared_env, "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS", "") +
                       " --junitxml=" + shlex.quote(str(report))}
                for command in task.verify_cmds:
                    report.unlink(missing_ok=True)
                    batch = await verifier.run_commands([command], wt, self.settings.verify_timeout_sec, env)
                    results.extend(batch)
                    if any(result.exit_code for result in batch):
                        break
                    verified.update(verified_pytest_requirements(markers, report))
            verifier.write_command_log(v_log, results)
            failed = next((r for r in results if r.exit_code != 0), None)
            if failed is not None:
                msg = f"验证命令失败（exit {failed.exit_code}）：`{failed.cmd}`\n```\n{failed.output}\n```"
                self.db.finish_run(
                    v_run_id, exit_code=failed.exit_code, verdict="FAIL", summary_log=msg[:1500], log_path=str(v_log)
                )
                await self._fail_attempt(task, msg, wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                return

            changed_files = (
                sorted(k for k, v in (exchange.get(artifact_key) or {}).items() if v is not None)
                if file_mode
                else [ln.strip() for ln in gitops.git(["diff", "--name-only", base_sha], wt, check=False).stdout.splitlines() if ln.strip()]
            )
            q_err = await self._evaluate_task_quality(
                task,
                attempt=attempt,
                cwd=wt,
                changed_files=changed_files,
                base_ref="" if file_mode else base_sha,
            )
            if q_err:
                self.db.finish_run(v_run_id, exit_code=1, verdict="FAIL", summary_log=q_err[:1500], log_path=str(v_log))
                await self._fail_attempt(task, q_err, wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                return

            from autodev.server.routers.tasks import format_file_patch_diff
            candidate_diff = (format_file_patch_diff(exchange.get(artifact_key))[0] if file_mode
                              else gitops.commit_diff(wt, base_sha, gitops.head_sha(wt)))
            self._record_candidate_evidence(task, artifact_key or gitops.head_sha(wt), attempt, wt, candidate_diff, verified)
            self.db.finish_run(v_run_id, exit_code=0, verdict="PASS", summary_log=summarize_verify(results)[:1500], log_path=str(v_log))

            # ---- 3. 独立只读审查 ----
            rev_ident = self.engine.agent_identity("reviewer", "质量保障组")
            current_actor.set(rev_ident["label"])
            if not self.db.update_task_if_owned(
                task_id,
                expected_exec_seq=exec_seq,
                expected_attempt=attempt,
                status=TaskStatus.REVIEWING,
                active_agent_label=rev_ident["label"],
            ):
                return
            self.db.update_slot(
                slot_id,
                squad="质量保障组",
                role="reviewer",
                harness=rev_ident["harness"],
                model=rev_ident["model"],
                current_action=f"正在独立只读审查 {task_id} 代码与验收标准…",
            )
            self.db.add_event(
                "task.reviewing",
                f"{rev_ident['label']} 接手审查 {task_id}",
                task_id=task_id,
                actor=rev_ident["label"],
            )
            r_run_id = self.db.start_run(task_id, attempt, "REVIEW", rev_ident["harness"], agent_label=rev_ident["label"])
            r_log = self.ws.runs_dir / task_id / f"attempt-{attempt}-review"
            review_prompt = build_review_prompt(
                task,
                slices,
                summarize_verify(results),
                candidate_diff,
                decisions=self.db.list_decisions(active_only=True),
                ws_root=self.ws.root, source_root=wt,
            )
            r_res = await self.engine.harness.run_structured_readonly(
                review_prompt,
                schema_path=self.engine.schemas["review_verdict"],
                cwd=wt,
                log_dir=r_log,
                role="reviewer",
            )

            verdict: ReviewVerdict | None = None
            parse_err = ""
            if r_res.ok and r_res.structured:
                try:
                    verdict = ReviewVerdict.model_validate(r_res.structured)
                    r_log.mkdir(parents=True, exist_ok=True)
                    (r_log / "verdict.json").write_text(
                        json.dumps(verdict.model_dump(), ensure_ascii=False, indent=2), "utf-8"
                    )
                except ValidationError as exc:
                    parse_err = str(exc)

            self.db.finish_run(
                r_run_id,
                session_id=r_res.session_id,
                exit_code=r_res.exit_code,
                input_tokens=r_res.usage.input_tokens,
                cached_tokens=r_res.usage.cached_tokens,
                output_tokens=r_res.usage.output_tokens,
                reasoning_tokens=r_res.usage.reasoning_tokens,
                verdict=verdict.verdict if verdict else "ERROR",
                summary_log=(verdict.summary if verdict else r_res.error or parse_err)[:1500],
                log_path=str(r_log),
            )

            if self._task_still_owned(task_id, attempt, expected_exec_seq=exec_seq) is None:
                return

            if verdict is None:
                await self._fail_attempt(task, f"只读审查输出无效：{r_res.error or parse_err}", wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
                return

            if verdict.verdict == "PASS":
                sha = None if file_mode else gitops.head_sha(wt)
                self._complete_task_after_ai_review(
                    task_id,
                    sha=sha,
                    artifact_key=artifact_key,
                    actor=rev_ident["label"],
                    message=f"{task_id} 审查通过（提交 {(artifact_key or sha)[:10]} · 署名 {coder_ident['author_name']}）",
                    expected_exec_seq=exec_seq,
                    expected_attempt=attempt,
                )
                return

            detail = self._format_review_feedback(verdict)
            if verdict.verdict == "ESCALATE_CONTRACT":
                await self._escalate_or_fuse(task, f"[审查建议升级契约]\n{detail}", wt, event_type="task.escalated", expected_exec_seq=exec_seq, expected_attempt=attempt)
                return

            await self._fail_attempt(task, f"独立审查未通过：\n{detail}", wt, expected_exec_seq=exec_seq, expected_attempt=attempt)
        finally:
            current_actor.reset(a_tok)
            if link:
                cur_task = self.db.get_task(task_id)
                status = cur_task.status if cur_task else TaskStatus.CANCELLED
                try:
                    await track_issue(self.engine, task_id, "done" if status in (TaskStatus.APPROVED, TaskStatus.MERGED)
                                      else "closed" if status == TaskStatus.CANCELLED else "todo")
                except LoopForgeError as exc:
                    self.db.add_event("link.issue_sync_failed", str(exc), task_id=task_id)

    async def _evaluate_task_quality(
        self,
        task: Task,
        *,
        attempt: int,
        cwd: Path,
        changed_files: list[str],
        base_ref: str = "",
        required: bool = False,
    ) -> str:
        from autodev.process.runner import load_template
        from autodev.quality.runner import format_quality_feedback, run_quality, save_quality_results

        tpl = load_template(self.ws, self.settings)
        testing_stage = next((s for s in tpl.stages if s.id == "testing" and s.enabled), None)
        if not required and (not testing_stage or not any(g.kind == "quality" and g.blocking for g in testing_stage.gates)):
            return ""
        results = await run_quality(
            "task",
            cwd=cwd,
            changed_files=changed_files,
            base_ref=base_ref,
            cfg=tpl.quality,
        )
        save_quality_results(self.db, results, task_id=task.task_id, milestone_id=task.milestone_id, attempt=attempt)
        return format_quality_feedback(results)

    def _task_still_owned(
        self,
        task_id: str,
        attempt: int,
        *,
        expected_exec_seq: int | None = None,
        expected_run_id: str | None = None,
    ) -> Task | None:
        """确认当前执行协程仍持有该任务（未被阶段转向取消、重置或绑定替代执行）。"""
        return self.db.get_owned_task(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=attempt,
            expected_run_id=expected_run_id,
        )

    def _complete_task_after_ai_review(
        self,
        task_id: str,
        *,
        sha: str | None = None,
        artifact_key: str | None = None,
        actor: str,
        message: str,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
    ) -> None:
        update_vals: dict[str, object] = {
            "failure_reason": None,
            "last_feedback": None,
        }
        if sha is not None:
            update_vals["candidate_commit_sha"] = sha
        if artifact_key is not None:
            update_vals["artifact_digest"] = artifact_key
        if not self.db.update_task_if_owned(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
            **update_vals,
        ):
            return

        from autodev.quality.runner import list_quality_results
        from autodev.review.diffs import task_diff
        from autodev.review.policy import effective_review_policy, evaluate_review_policy
        from autodev.review.service import open_review

        task = self.db.get_owned_task(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
        )
        if task is None:
            return
        policy = effective_review_policy(self.ws, self.settings)
        if policy in ("risk", "all"):
            dview = task_diff(self.engine, task)
            q_rows = list_quality_results(self.db, task_id=task_id, latest_only=True)
            need_hr, reasons = evaluate_review_policy(
                policy,
                task,
                dview,
                q_rows,
                protected_paths=self.settings.protected_paths,
                risk_lines=self.settings.review_risk_lines,
                required_paths=self.settings.review_required_paths,
            )
            if need_hr:
                if not self.db.update_task_if_owned(
                    task_id,
                    expected_exec_seq=expected_exec_seq,
                    expected_attempt=expected_attempt,
                    expected_run_id=expected_run_id,
                    status=TaskStatus.AWAITING_HUMAN_REVIEW,
                    assigned_slot=None,
                    active_agent_label=None,
                ):
                    return
                open_review(
                    self.engine,
                    scope_type="task",
                    scope_id=task_id,
                    milestone_id=task.milestone_id,
                    head_ref=str(artifact_key or sha or "head"),
                    base_ref=str(task.base_sha or ""),
                    risk_reasons=reasons,
                )
                return

        if self.db.update_task_if_owned(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
            status=TaskStatus.APPROVED,
        ):
            self.db.add_event("task.approved", message, task_id=task_id, actor=actor)

    async def _save_remote_review_record(
        self,
        bridge: LoopForgeBridge,
        task: Task,
        attempt: int,
        run: dict,
        *,
        passed: bool,
        fallback_summary: str,
    ) -> None:
        rev_ident = self.engine.agent_identity("reviewer", "质量保障组")
        r_log = self.ws.runs_dir / task.task_id / f"attempt-{attempt}-review"
        r_log.mkdir(parents=True, exist_ok=True)
        report_md = ""
        if run:
            try:
                report_md = await bridge.client_for_run(run["id"]).named_artifact(run, "review-report.md")
            except LoopForgeError:
                report_md = ""
        summary_txt = fallback_summary
        if report_md.strip():
            (r_log / "last_message.txt").write_text(report_md, "utf-8")
            _, _, body = report_md.strip().partition("\n")
            summary_txt = (body.strip() or report_md.strip())[:1200]
        else:
            (r_log / "last_message.txt").write_text(fallback_summary, "utf-8")
        verdict_payload = {
            "verdict": "PASS" if passed else "NEEDS_FIX",
            "summary": summary_txt,
            "criteria_check": [
                {"criterion": c, "met": passed, "evidence": "LoopForge 独立审查节点已核验" if passed else "见审查打回详情"}
                for c in (task.acceptance_criteria or [])
            ],
            "blocking_issues": [],
        }
        (r_log / "verdict.json").write_text(json.dumps(verdict_payload, ensure_ascii=False, indent=2), "utf-8")
        r_run_id = self.db.start_run(task.task_id, attempt, "REVIEW", rev_ident["harness"], agent_label=rev_ident["label"])
        self.db.finish_run(
            r_run_id,
            session_id=str(run.get("id") or task.loopforge_run_id or "") or None,
            exit_code=0 if passed else 1,
            verdict="PASS" if passed else "NEEDS_FIX",
            summary_log=summary_txt[:1500],
            log_path=str(r_log),
        )

    async def _execute_remote(
        self,
        task,
        squad,
        slot,
        exchange,
        before,
        protected,
        *,
        exec_seq: int | None = None,
    ):
        if exec_seq is None:
            exec_seq = self.db.claim_task_execution(task.task_id, allow_from_ready=True)
            if exec_seq is None:
                return
        owned_start = self.db.get_owned_task(task.task_id, expected_exec_seq=exec_seq)
        if owned_start is None:
            return
        task = owned_start
        attempt = task.retry_count + 1
        coder_ident = self.engine.agent_identity(task.role, squad)
        bridge = LoopForgeBridge(self.settings, self.db)
        files = self.settings.collaboration_mode == "files"
        if task.pending_action == "verify_only":
            frozen = self.db.meta_get(f"verified-base:{task.task_id}:{task.artifact_digest}") if files else ""
            if frozen:
                before = exchange.get(frozen)
            v_log = self.ws.runs_dir / task.task_id / f"attempt-{attempt}-verify.log"
            v_run = self.db.start_run(task.task_id, attempt, "VERIFY", "local", agent_label="GateVerifier")
            try:
                await self.verify_and_record_candidate(task, attempt, exchange, before, protected,
                                                       task.candidate_commit_sha, task.artifact_digest, v_log)
                if not self.db.is_task_owned(task.task_id, expected_exec_seq=exec_seq):
                    self.db.finish_run(v_run, exit_code=130, verdict="SUPERSEDED", summary_log="复验期间候选已替换")
                    return
                await bridge.await_candidate_signoff(task, task.artifact_digest or task.candidate_commit_sha, self.stop_event)
            except (OSError, ValueError, gitops.GitError, LoopForgeError) as exc:
                self.db.finish_run(v_run, exit_code=1, verdict="FAIL", summary_log=str(exc), log_path=str(v_log))
                self._fuse(task, f"复验失败：{exc}", expected_exec_seq=exec_seq, expected_attempt=attempt)
                return
            if self.db.update_task_if_owned(task.task_id, expected_exec_seq=exec_seq, pending_action=None):
                self.db.finish_run(v_run, exit_code=0, verdict="PASS", summary_log="已复验固定候选", log_path=str(v_log))
                self._complete_task_after_ai_review(task.task_id, sha=task.candidate_commit_sha,
                    artifact_key=task.artifact_digest, actor="GateVerifier", message="已复验原候选",
                    expected_exec_seq=exec_seq, expected_attempt=attempt, expected_run_id=task.loopforge_run_id or "")
            return
        slices = self.db.get_slices([*task.doc_slice_ids, "PRD-S01"])
        context = "\n".join(f"{s.source_file}\n{s.content}" for s in slices)
        baseline_file = ""
        if files:
            bdir = self.ws.state_dir / "baselines"
            bdir.mkdir(parents=True, exist_ok=True)
            bp = bdir / f"{task.task_id}.json"
            bp.write_text(json.dumps(before, ensure_ascii=False), "utf-8")
            baseline_file = str(bp.resolve())
        if self.settings.data_classification == "internal" and not task.loopforge_run_id:
            from autodev import public_pool
            try:
                bridge.selected_execution = await public_pool.resolve(self.settings, bridge.client)
            except LoopForgeError as exc:
                self._fuse(task, str(exc), expected_exec_seq=exec_seq, expected_attempt=attempt)
                return
        managed_git = not files and bool(self.db.meta_get("git_delivery_source"))
        git_context = {}
        wt = None
        base = ""
        if not files and (managed_git or not task.loopforge_run_id):
            wt, base = self._prepare_worktree(task)
            refreshed = self.db.get_owned_task(task.task_id, expected_exec_seq=exec_seq, expected_attempt=attempt)
            if refreshed is None:
                return
            task = refreshed
            if managed_git:
                use_mirror = self.db.meta_get("gitea_mirror") == "1"
                pack_dir = None if use_mirror else self.ws.state_dir / "git-snapshots"
                snapshot_sha = await asyncio.to_thread(local_git.prepare_snapshot, wt, base, pack_dir)
                pid = self.settings.loopforge_project_id
                sig = sign_context_sig(self.settings.project_signing_key(), pid, f"git:{snapshot_sha}")
                if use_mirror:
                    from autodev import local_git_bridge
                    remote = await local_git_bridge.task_remote(wt, self.settings, self.db, task.task_id, snapshot_sha)
                    git_context["git_restore"] = local_git_bridge.restore_command(remote, snapshot_sha)
                else:
                    git_context["git_restore"] = local_git.restore_command(self.settings.vernal_public_url, pid, snapshot_sha, sig)
            else:
                gitops.git(["push", "origin", f"{base}:refs/heads/{task.branch_name}"], wt)

        c_log = self.ws.runs_dir / task.task_id / f"attempt-{attempt}-code"
        c_log.mkdir(parents=True, exist_ok=True)
        prompt_preview = format_issue_description(task, squad, decisions=self.db.list_decisions(active_only=True)) + "\n" + context[:16_000]
        (c_log / "prompt.md").write_text(prompt_preview, "utf-8")
        c_run_id = self.db.start_run(task.task_id, attempt, "CODE", coder_ident["harness"], agent_label=coder_ident["label"])

        ok, msg = await bridge.execute_task(
            task,
            squad,
            slot,
            self.stop_event,
            inputs=before if files else None,
            context=context,
            baseline_file=baseline_file,
            vernal_base_url=self.settings.vernal_public_url,
            expected_exec_seq=exec_seq,
            expected_attempt=attempt,
            **git_context,
        )
        if self.stop_event.is_set():
            self.db.finish_run(c_run_id, exit_code=130, verdict="STOPPED", summary_log=msg[:1000], log_path=str(c_log))
            self.db.update_task_if_owned(task.task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, status=TaskStatus.READY)
            return

        owned = self._task_still_owned(task.task_id, attempt, expected_exec_seq=exec_seq)
        if owned is None:
            self.db.finish_run(c_run_id, exit_code=130, verdict="CANCELLED", summary_log="任务已被转向评审取消或重置", log_path=str(c_log))
            return
        task = owned
        bound_run_id = task.loopforge_run_id or ""

        run: dict = {}
        if bound_run_id:
            (c_log / "loopforge_run.json").write_text(json.dumps({"run_id": bound_run_id, "issue_id": task.loopforge_issue_id}, ensure_ascii=False), "utf-8")
            try:
                run = await bridge.client_for_run(bound_run_id).get_run(bound_run_id)
                run.setdefault("id", bound_run_id)
            except LoopForgeError:
                run = {"id": bound_run_id}
        usage = extract_run_usage(run) if run else extract_run_usage({})
        self.db.finish_run(
            c_run_id,
            session_id=bound_run_id or None,
            exit_code=0 if ok else 1,
            input_tokens=usage.input_tokens,
            cached_tokens=usage.cached_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            verdict="DONE" if ok else "ERROR",
            summary_log=msg[:1000],
            log_path=str(c_log),
        )

        if self._task_still_owned(task.task_id, attempt, expected_exec_seq=exec_seq, expected_run_id=bound_run_id) is None:
            return

        if not ok:
            if msg.startswith("独立审查未通过"):
                await self._save_remote_review_record(bridge, task, attempt, run, passed=False, fallback_summary=msg)
            infra_markers = (
                "查询 LoopForge",
                "无法连接 LoopForge",
                "当前离线",
                "gate MCP binary",
                "未配置",
                "尚未完成登录态绑定",
                "Data too long",
            )
            if any(m in msg for m in infra_markers):
                extra_infra = {"loopforge_run_id": None} if not msg.startswith("查询 LoopForge") else {}
                self._fuse(
                    task,
                    f"[环境未就绪，已暂停重试] {msg}",
                    expected_exec_seq=exec_seq,
                    expected_attempt=attempt,
                    expected_run_id=bound_run_id,
                    extra_updates=extra_infra,
                )
                return
            # 保留 loopforge_issue_id，并在单条原子 CAS 更新中清空旧 run ID 与累加 retry_count
            await self._fail_attempt(
                task,
                msg,
                None,
                expected_exec_seq=exec_seq,
                expected_attempt=attempt,
                expected_run_id=bound_run_id,
                extra_updates={"loopforge_run_id": None},
            )
            return

        v_log = self.ws.runs_dir / task.task_id / f"attempt-{attempt}-verify.log"
        v_run_id = self.db.start_run(task.task_id, attempt, "VERIFY", "local", agent_label="GateVerifier")
        sha: str | None = None
        key: str | None = None
        try:
            if files:
                from autodev.context_bundle import resolve_candidate_artifact
                manifest = await bridge.client_for_run(run["id"]).named_artifact(run, "vernal-files.json")
                manifest = resolve_candidate_artifact(self.ws, manifest)
                if not self.db.is_task_owned(task.task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, expected_run_id=bound_run_id):
                    self.db.finish_run(v_run_id, exit_code=130, verdict="SUPERSEDED", summary_log="任务已重置或绑定替代执行，忽略旧产物", log_path=str(v_log))
                    return
                after = apply_output(before, manifest)
                key = exchange.patch(task, before, after, protected)
            elif managed_git:
                from autodev.context_bundle import resolve_candidate_artifact
                artifact = await bridge.client_for_run(run["id"]).named_artifact(run, "vernal-patch.json")
                artifact = resolve_candidate_artifact(self.ws, artifact)
                if not self.db.is_task_owned(task.task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, expected_run_id=bound_run_id):
                    self.db.finish_run(v_run_id, exit_code=130, verdict="SUPERSEDED", summary_log="任务已重置或绑定替代执行，忽略旧补丁", log_path=str(v_log))
                    return
                sha = local_git.apply_patch(wt, base, artifact, task.allowed_paths, protected)
            else:
                record = await bridge.client_for_run(run["id"]).named_artifact(run, "mr-body.md")
                if not self.db.is_task_owned(task.task_id, expected_exec_seq=exec_seq, expected_attempt=attempt, expected_run_id=bound_run_id):
                    self.db.finish_run(v_run_id, exit_code=130, verdict="SUPERSEDED", summary_log="任务已重置或绑定替代执行，忽略旧 MR 产物", log_path=str(v_log))
                    return
                hashes = set(re.findall(r"(?i)\b[0-9a-f]{40}\b", record))
                if len(hashes) != 1:
                    raise ValueError("MR 产物需要标明唯一的完整提交 SHA")
                sha = hashes.pop()
                gitops.git(["fetch", "origin", sha], self.ws.root)
                if sha == task.base_sha or gitops.git(["merge-base", "--is-ancestor", task.base_sha, sha], self.ws.root, check=False).returncode:
                    raise ValueError("远端提交与派单基线不匹配")
            await self.verify_and_record_candidate(task, attempt, exchange, before, protected, sha, key, v_log)
        except (ValueError, LoopForgeError, gitops.GitError, OSError) as exc:
            if not v_log.exists():
                v_log.parent.mkdir(parents=True, exist_ok=True)
                v_log.write_text(str(exc), "utf-8")
            if not self.db.is_task_owned(
                task.task_id,
                expected_exec_seq=exec_seq,
                expected_attempt=attempt,
                expected_run_id=bound_run_id,
            ):
                self.db.finish_run(
                    v_run_id,
                    exit_code=130,
                    verdict="SUPERSEDED",
                    summary_log="旧远端执行产物校验失败，但任务已重置或绑定替代执行，忽略旧结果",
                    log_path=str(v_log),
                )
                return
            self.db.finish_run(v_run_id, exit_code=1, verdict="FAIL", summary_log=str(exc)[:1500], log_path=str(v_log))
            self.db.add_event("loopforge.invalid_delivery", f"{bound_run_id}: {exc}", task_id=task.task_id)
            await self._fail_attempt(
                task,
                str(exc),
                None,
                expected_exec_seq=exec_seq,
                expected_attempt=attempt,
                expected_run_id=bound_run_id,
                extra_updates={"loopforge_run_id": None},
            )
            return

        if not self.db.is_task_owned(task.task_id, expected_exec_seq=exec_seq, expected_run_id=bound_run_id):
            self.db.finish_run(v_run_id, exit_code=130, verdict="SUPERSEDED", summary_log="验证期间候选已替换")
            return
        try:
            await bridge.await_candidate_signoff(task, key or sha, self.stop_event)
        except LoopForgeError as exc:
            # The exact verified candidate and both native receipts survive a paused observation.
            self.db.finish_run(v_run_id, exit_code=0, verdict="PASS", summary_log="候选验证通过，原生审批尚未放行", log_path=str(v_log))
            self._fuse(task, str(exc), event_type="task.signoff_pending", expected_exec_seq=exec_seq,
                       expected_attempt=attempt, expected_run_id=bound_run_id, extra_updates={"pending_action": "verify_only"})
            return

        if not v_log.exists():
            v_log.parent.mkdir(parents=True, exist_ok=True)
            v_log.write_text("远端交付产物与路径门禁校验通过。\n", "utf-8")
        self.db.finish_run(v_run_id, exit_code=0, verdict="PASS", summary_log="远端交付产物与门禁验证通过", log_path=str(v_log))
        await self._save_remote_review_record(bridge, task, attempt, run, passed=True, fallback_summary=msg)
        if self._task_still_owned(task.task_id, attempt, expected_exec_seq=exec_seq, expected_run_id=bound_run_id) is None:
            return
        self._complete_task_after_ai_review(
            task.task_id,
            sha=sha,
            artifact_key=key,
            actor="LoopForgeBridge",
            message=msg,
            expected_exec_seq=exec_seq,
            expected_attempt=attempt,
            expected_run_id=bound_run_id,
        )

    def _record_candidate_evidence(self, task, candidate, attempt, source, diff, verified):
        import hashlib
        from autodev.context_bundle import build_candidate_review_bundle
        review = build_candidate_review_bundle(self.ws.root, task, diff, source_root=source)
        self.db._exec("INSERT OR REPLACE INTO candidate_evidence "
                      "(task_id,candidate,attempt,issue_id,run_id,diff_digest,review_context,verified_requirements,verified_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (task.task_id, candidate, attempt, task.loopforge_issue_id, task.loopforge_run_id,
                       hashlib.sha256(diff.encode()).hexdigest(), review, json.dumps(sorted(verified)), now_iso()))

    async def verify_and_record_candidate(self, task, attempt, exchange, before, protected, sha, key, log):
        from autodev.trace.candidate import candidate_test_markers, verified_pytest_requirements
        from autodev.server.routers.tasks import format_file_patch_diff

        root = self.ws.root
        with TemporaryDirectory(dir=self.ws.worktrees_dir) as tmp:
            staging = Path(tmp) / "candidate"
            report = Path(tmp) / "pytest.xml"
            if key:
                after = dict(before)
                patch = exchange.get(key)
                for name, (old, new) in patch.items():
                    if before.get(name) != old:
                        raise ValueError("候选补丁基线已变化")
                    if new is None:
                        after.pop(name, None)
                    else:
                        after[name] = new
                if exchange.patch(task, before, after, protected) != key:
                    raise ValueError("候选文件摘要不匹配")
                materialize(after, staging)
                changed = sorted(patch)
                diff, _ = format_file_patch_diff(patch)
            else:
                if not sha or not re.fullmatch("[a-fA-F0-9]{40}", sha) or not task.base_sha:
                    raise ValueError("缺少可还原的 Git 候选")
                if sha == task.base_sha or gitops.git(["merge-base","--is-ancestor",task.base_sha,sha], root, check=False).returncode:
                    raise ValueError("候选与派单基线不匹配")
                changed = gitops.git(["diff","--no-renames","--name-only","-z",task.base_sha,sha], root).stdout.split("\0")[:-1]
                if not changed:
                    raise ValueError("候选没有交付改动")
                diff = gitops.commit_diff(root, task.base_sha, sha)
                gitops.git(["worktree","add","--detach",str(staging),sha], root)
            try:
                guard = verifier.guard_paths(staging, changed, task.allowed_paths, protected)
                if not guard.ok:
                    raise ValueError(guard.describe())
                with self.db.transaction():
                    if key:
                        self.db.meta_set(f"verified-base:{task.task_id}:{key}", exchange.put(before))
                    self.db.update_task_if_owned(task.task_id, expected_exec_seq=task.exec_seq,
                        **({"artifact_digest":key} if key else {"candidate_commit_sha":sha}))
                verified = set()
                markers = candidate_test_markers(staging)
                for index, command in enumerate(task.verify_cmds):
                    report.unlink(missing_ok=True)
                    await self._check_files([command], staging, task.task_id, log_path=log.with_suffix(f".{index}.log"),
                                            pytest_report=report)
                    verified.update(verified_pytest_requirements(markers, report))
                err = await self._evaluate_task_quality(task, attempt=attempt, cwd=staging, changed_files=changed,
                                                        base_ref=task.base_sha if sha else "", required=True)
                if err:
                    raise ValueError(err)
                with self.db.transaction():
                    self._record_candidate_evidence(task, key or sha, attempt, staging, diff, verified)
            finally:
                if not key:
                    gitops.remove_worktree(root, staging)

    async def _check_files(self, commands, directory, label, *, log_path: Path | None = None, pytest_report=None):
        target_log = log_path or (self.ws.runs_dir / label / "files-verify.log")
        env = self.engine.shared_env()
        if pytest_report:
            import shlex
            env = {**env, "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS", "") + " --junitxml=" + shlex.quote(str(pytest_report))}
        results = await verifier.run_commands(
            commands, directory, self.settings.verify_timeout_sec, env,
            log_path=target_log,
        )
        for result in results:
            if result.exit_code:
                raise ValueError(f"验证失败 {result.cmd}\n{result.output}")
        return results

    async def _integrate_files(self, ms: Milestone) -> None:
        exchange = FileExchange(self.ws, self.db)
        tasks = [t for t in self.db.list_tasks(milestone_id=ms.milestone_id) if t.status != TaskStatus.CANCELLED]
        try:
            files = exchange.baseline([t.task_id for t in tasks])
            key = exchange.put(files)
            with TemporaryDirectory(dir=self.ws.integration_dir) as directory:
                staging = Path(directory)
                materialize(files, staging)
                await self._check_files([ms.integration_cmd] if ms.integration_cmd.strip() else [], staging, ms.milestone_id)
            matrix = {t.task_id: t.artifact_digest for t in tasks}
            report = self.ws.reports_dir / f"{ms.milestone_id}.md"
            report.write_text(f"# {ms.title}\n\n文件交付版本 `{key}`\n\n" +
                              "\n".join(f"* {name}" for name in sorted(files)), "utf-8")
            self.db.update_milestone(ms.milestone_id, status=MilestoneStatus.AWAITING_MERGE,
                                     commit_matrix_json=json.dumps(matrix), report_path=str(report), failure_reason=None)
            self.db.meta_set(f"delivery:{ms.milestone_id}", key)
            self.db.meta_set(f"candidate_ref:{ms.milestone_id}", key)
            self.db.add_event("milestone.integrated", f"文件集成通过 {key[:12]}", actor="Orchestrator")
            from autodev.ci.service import ci_gate_enabled, trigger_milestone_ci
            from autodev.review.policy import effective_review_policy
            from autodev.review.service import open_review

            if ci_gate_enabled(self.engine):
                await trigger_milestone_ci(self.engine, ms, key, transport=self.ci_transport)  # type: ignore[arg-type]
                return
            if effective_review_policy(self.ws, self.settings) == "milestone":
                open_review(
                    self.engine,
                    scope_type="milestone",
                    scope_id=ms.milestone_id,
                    milestone_id=ms.milestone_id,
                    head_ref=key,
                    risk_reasons=["当前策略要求里程碑合入前执行整体代码审查 (policy=milestone)"],
                )
                return
            if not self.settings.require_merge_approval:
                self.engine.merge_milestone(ms.milestone_id, actor="Orchestrator")
        except (ValueError, OSError) as exc:
            self._fail_milestone(ms, str(exc))

    def _format_review_feedback(self, v: ReviewVerdict) -> str:
        lines = [v.summary]
        unmet = [c for c in v.criteria_check if not c.met]
        if unmet:
            lines.append("\n未满足的验收标准：")
            for c in unmet:
                lines.append(f"- {c.criterion}（{c.evidence}）")
        if v.blocking_issues:
            lines.append("\n阻塞缺陷：")
            for issue in v.blocking_issues:
                loc = f"{issue.file}:{issue.line}" if issue.line else issue.file
                lines.append(f"- [{loc}] {issue.problem} → 建议：{issue.fix}")
        return "\n".join(lines)

    async def _fail_attempt(
        self,
        task: Task,
        reason: str,
        wt: Path | None,
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
        extra_updates: dict[str, object] | None = None,
    ) -> None:
        retries = task.retry_count + 1
        extra = dict(extra_updates or {})
        if retries >= task.max_retries:
            if not self.db.update_task_if_owned(
                task.task_id,
                expected_exec_seq=expected_exec_seq,
                expected_attempt=expected_attempt,
                expected_run_id=expected_run_id,
                retry_count=retries,
                last_feedback=reason,
                **extra,
            ):
                return
            refreshed = self.db.get_owned_task(
                task.task_id,
                expected_exec_seq=expected_exec_seq,
                expected_attempt=retries + 1,
                expected_run_id="" if "loopforge_run_id" in extra and extra["loopforge_run_id"] is None else expected_run_id,
            )
            if refreshed is None:
                return
            await self._escalate_or_fuse(
                refreshed,
                reason,
                wt,
                event_type="task.fused",
                expected_exec_seq=expected_exec_seq,
                expected_attempt=retries + 1,
                expected_run_id="" if "loopforge_run_id" in extra and extra["loopforge_run_id"] is None else expected_run_id,
            )
        else:
            if self.db.update_task_if_owned(
                task.task_id,
                expected_exec_seq=expected_exec_seq,
                expected_attempt=expected_attempt,
                expected_run_id=expected_run_id,
                status=TaskStatus.READY,
                retry_count=retries,
                last_feedback=reason,
                failure_reason=None,
                **extra,
            ):
                self.db.add_event(
                    "task.retry",
                    f"{task.task_id} 第 {retries}/{task.max_retries} 轮未通过，将原会话续跑修复：{reason[:220]}",
                    task_id=task.task_id,
                    actor="Orchestrator",
                )

    async def _escalate_or_fuse(
        self,
        task: Task,
        reason: str,
        wt: Path | None,
        event_type: str = "task.fused",
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
    ) -> None:
        """Goal 模式下先由研发总监自动仲裁（契约升版 / 精准点拨），仲裁无果再熔断交人。"""
        mode = self.db.meta_get("planning_mode", self.settings.planning_mode)
        if mode == "goal" and task.arbitration_count < self.settings.director_max_arbitrations:
            async with self._arbitration_lock:
                if not self.db.is_task_owned(
                    task.task_id,
                    expected_exec_seq=expected_exec_seq,
                    expected_attempt=expected_attempt,
                    expected_run_id=expected_run_id,
                ):
                    return
                decision = await self.engine.arbitrate_task(task, reason, wt)
                if decision and decision.action in ("STEER_RETRY", "PATCH_CONTRACT"):
                    return
        self._fuse(
            task,
            reason,
            event_type=event_type,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
        )

    def _fuse(
        self,
        task: Task,
        reason: str,
        event_type: str = "task.fused",
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
        extra_updates: dict[str, object] | None = None,
    ) -> None:
        if self.db.update_task_if_owned(
            task.task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
            status=TaskStatus.FUSED,
            last_feedback=reason,
            failure_reason=reason,
            **(extra_updates or {}),
        ):
            self.db.add_event(
                event_type,
                f"{task.task_id} 已挂起等待人工介入：{reason[:260]}",
                task_id=task.task_id,
                actor="Orchestrator",
            )

    # ---------- 阶段五：里程碑集成 ----------
    async def _integrate_milestone(self, ms: Milestone) -> None:
        if self.settings.collaboration_mode == "files":
            await self._integrate_files(ms)
            return
        root = self.ws.root
        self.db.update_milestone(ms.milestone_id, status=MilestoneStatus.INTEGRATING, failure_reason=None)
        self.db.update_slot(
            "command",
            status="WORKING",
            squad="顶层指挥组",
            role="director",
            harness="local",
            model="git-merge",
            task_id="_director",
            current_action=f"正在合拢验证里程碑 {ms.milestone_id}…",
            started_at=now_iso(),
        )
        self.db.add_event("milestone.integrating", f"开始集成里程碑 {ms.milestone_id}：{ms.title}", actor="Orchestrator")

        try:
            int_wt = self.ws.integration_dir / ms.milestone_id
            branch = f"autodev/ms-{ms.milestone_id}"
            gitops.remove_worktree(root, int_wt)
            gitops.delete_branch(root, branch)
            gitops.create_worktree(root, int_wt, branch, self.settings.base_branch)

            tasks = [t for t in self.db.list_tasks(milestone_id=ms.milestone_id) if t.status != TaskStatus.CANCELLED]
            by_id = {t.task_id: t for t in tasks}
            order = topo_sort({t.task_id: [d for d in t.depends_on if d in by_id] for t in tasks})

            commit_matrix: dict[str, str] = {}
            for tid in order:
                t = by_id[tid]
                sha = t.candidate_commit_sha
                if not sha:
                    self._fail_milestone(ms, f"任务 {tid} 缺少 candidate_commit_sha")
                    return
                if not gitops.is_ancestor(int_wt, sha):
                    ok, msg = gitops.merge(int_wt, sha, f"autodev: integrate {tid} into {ms.milestone_id}")
                    if not ok:
                        self._fail_milestone(ms, msg)
                        return
                if not gitops.is_ancestor(int_wt, sha):
                    self._fail_milestone(ms, f"提交指纹校验失败：{tid} ({sha}) 不在集成历史中")
                    return
                commit_matrix[tid] = sha

            int_output = "（未配置里程碑集成命令）"
            if ms.integration_cmd.strip():
                log_path = self.ws.runs_dir / "_integration" / f"{ms.milestone_id}.log"
                results = await verifier.run_commands(
                    [ms.integration_cmd], int_wt, self.settings.verify_timeout_sec, self.engine.shared_env(), log_path=log_path
                )
                r = results[0]
                int_output = f"$ {r.cmd}\n[exit {r.exit_code}, {r.duration_sec:.1f}s]\n{r.output}"
                if r.exit_code != 0:
                    self._fail_milestone(ms, f"集成测试失败（exit {r.exit_code}）：\n{r.output}")
                    return

            report_path = self.ws.reports_dir / f"{ms.milestone_id}.md"
            report_lines = [
                f"# 里程碑交付报告：{ms.milestone_id} — {ms.title}",
                "",
                f"**目标**：{ms.goal}",
                f"**集成分支**：`{branch}`（HEAD `{gitops.head_sha(int_wt)[:12]}`）",
                "",
                "## 提交指纹矩阵（Commit Matrix）",
                "",
                "| 任务 | 研发组 | 角色 | 标题 | Commit SHA |",
                "|---|---|---|---|---|",
            ]
            for tid in order:
                t = by_id[tid]
                report_lines.append(
                    f"| {tid} | {self._squad_name(t.squad)} | {t.role} | {t.title} | `{commit_matrix[tid][:12]}` |"
                )
            report_lines += [
                "",
                "## 改动统计",
                "```",
                gitops.diff_stat(int_wt, self.settings.base_branch) or "（无）",
                "```",
                "",
                "## 集成验证输出",
                "```",
                int_output,
                "```",
                "",
            ]
            report_path.write_text("\n".join(report_lines), "utf-8")

            self.db.update_milestone(
                ms.milestone_id,
                status=MilestoneStatus.AWAITING_MERGE,
                branch_name=branch,
                commit_matrix_json=json.dumps(commit_matrix, ensure_ascii=False),
                report_path=str(report_path),
                failure_reason=None,
            )
            self.db.add_event(
                "milestone.integrated",
                f"里程碑 {ms.milestone_id} 集成通过，报告已生成：{report_path.name}",
                actor="Orchestrator",
                payload={"commit_matrix": commit_matrix},
            )
            from autodev.ci.service import ci_gate_enabled, trigger_milestone_ci
            from autodev.review.policy import effective_review_policy
            from autodev.review.service import open_review

            int_sha = gitops.head_sha(int_wt)
            self.db.meta_set(f"candidate_ref:{ms.milestone_id}", int_sha)
            if ci_gate_enabled(self.engine):
                await trigger_milestone_ci(self.engine, ms, int_sha, transport=self.ci_transport)  # type: ignore[arg-type]
                return
            if effective_review_policy(self.ws, self.settings) == "milestone":
                open_review(
                    self.engine,
                    scope_type="milestone",
                    scope_id=ms.milestone_id,
                    milestone_id=ms.milestone_id,
                    head_ref=int_sha,
                    base_ref=self.settings.base_branch,
                    risk_reasons=["当前策略要求里程碑合入前执行整体代码审查 (policy=milestone)"],
                )
                return
            if not self.settings.require_merge_approval:
                self.engine.merge_milestone(ms.milestone_id, actor="Orchestrator")
            else:
                self.db.add_event(
                    "milestone.awaiting_merge",
                    f"里程碑 {ms.milestone_id} 等待人工签字合入主干",
                    actor="Orchestrator",
                )
        finally:
            self.db.release_slot("command")

    def _fail_milestone(self, ms: Milestone, reason: str) -> None:
        self.db.update_milestone(ms.milestone_id, status=MilestoneStatus.FAILED, failure_reason=reason)
        self.db.add_event("milestone.failed", f"里程碑 {ms.milestone_id} 集成失败：{reason[:260]}", actor="Orchestrator")
