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
from autodev.loopforge.bridge import LoopForgeBridge, sign_context_sig
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
    def __init__(self, engine: Engine):
        self.engine = engine
        self.ws = engine.ws
        self.db = engine.db
        self.settings = engine.settings
        self.stop_event = asyncio.Event()
        self.running_tasks: dict[str, asyncio.Task[None]] = {}
        self.task_slots: dict[str, str] = {}
        self._arbitration_lock = asyncio.Lock()

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
            if not LoopForgeBridge(self.settings, self.db).is_configured():
                raise OpsError("请先登录并绑定 LoopForge 项目，调度尚未启动")
            if self.settings.collaboration_mode == "git" and not (local_source or self.settings.loopforge_repository_id):
                raise OpsError("Git 交付需要绑定与本地 origin 对应的 LoopForge 仓库")

    async def run(self, watch: bool = False) -> ProjectStatus:
        self.check_runtime()
        if self.engine.status == ProjectStatus.PLANNED:
            raise OpsError("规划尚未批准，请先执行 `autodev approve-plan` 或在看板批准排期")
        if self.engine.status in (ProjectStatus.CREATED, ProjectStatus.INGESTED, ProjectStatus.SPECIFIED):
            raise OpsError("尚未生成立项规划，请先执行 `autodev plan`")

        self.stop_event.clear()
        with _FileLock(self.ws.lock_path):
            self.db.ensure_slots(self.settings.max_concurrency)
            self.db.reset_all_slots()
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
                exc = task.exception()
                if exc is not None:
                    self.db.update_task(
                        tid,
                        status=TaskStatus.FUSED,
                        assigned_slot=None,
                        active_agent_label=None,
                        failure_reason=f"内部异常: {exc}",
                    )
                    self.db.add_event("task.fused", f"执行异常熔断: {exc}", task_id=tid, actor="Orchestrator")

    # ---------- 单任务 TDD 闭环 ----------
    async def _run_task_safe(self, task_id: str, slot_id: str) -> None:
        t_tok = current_task.set(task_id)
        s_tok = current_slot.set(slot_id)
        try:
            await self._execute_task(task_id, slot_id)
        except asyncio.CancelledError:
            cur = self.db.get_task(task_id)
            if cur and cur.status in ACTIVE_TASK_STATUSES:
                self.db.update_task(task_id, status=TaskStatus.READY, assigned_slot=None, active_agent_label=None)
            raise
        finally:
            cur = self.db.get_task(task_id)
            if cur and cur.status not in ACTIVE_TASK_STATUSES:
                self.db.update_task(task_id, assigned_slot=None, active_agent_label=None)
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
        task = self.db.get_task(task_id)
        if task is None:
            return
        attempt = task.retry_count + 1
        squad_cn = self._squad_name(task.squad)
        coder_ident = self.engine.agent_identity(task.role, squad_cn)
        a_tok = current_actor.set(coder_ident["label"])

        exchange = FileExchange(self.ws, self.db)
        file_mode = self.settings.collaboration_mode == "files"
        before = exchange.baseline(task.depends_on) if file_mode else {}
        protected = [] if task.role in ("contract", "bootstrap") else self.settings.protected_paths
        link = personal_worker(self.engine)
        try:
            if link:
                await track_issue(self.engine, task_id, "in_progress")
            if self.settings.runtime_backend == "loopforge" and not link:
                await self._execute_remote(task, squad_cn, slot_id, exchange, before, protected)
                return
            try:
                if file_mode:
                    wt = Path(mkdtemp(prefix="task-", dir=self.ws.worktrees_dir))
                    materialize(before, wt)
                    self.db.update_task(task_id, worktree_path=str(wt))
                    base_sha = ""
                else:
                    wt, base_sha = self._prepare_worktree(task)
            except Exception as exc:
                self._fuse(task, f"准备 worktree 基线失败：{exc}")
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
                    fix_prompt = build_fix_prompt(task, task.last_feedback or "", attempt, task.steer_note)
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
                            task, slices, contracts, self.settings, decisions=self.db.list_decisions(active_only=True)
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
                        task, slices, contracts, self.settings, decisions=self.db.list_decisions(active_only=True)
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
                self.db.update_task(task_id, **update_fields)
                task = self.db.get_task(task_id)
                assert task is not None

                if report and report.status in ("NEED_CONTRACT_UPDATE", "BLOCKED_BY_SPEC"):
                    reason = f"[{report.status}] {report.notes or report.summary}"
                    await self._escalate_or_fuse(task, reason, wt, event_type="task.escalated")
                    return

                if not res.ok:
                    await self._fail_attempt(task, f"编码会话执行失败：{res.error}", wt)
                    return

            # ---- 2. 确定性门禁（路径白名单 + 验证命令） ----
            self.db.update_task(task_id, status=TaskStatus.VERIFYING, active_agent_label="[门禁系统] PathGuard + TestRunner")
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
                    await self._fail_attempt(task, str(exc), wt)
                    return
            else:
                guard = verifier.guard_worktree(wt, task.allowed_paths, protected)
                if not guard.ok:
                    msg = guard.describe()
                    v_log.parent.mkdir(parents=True, exist_ok=True)
                    v_log.write_text(msg, "utf-8")
                    self.db.finish_run(v_run_id, exit_code=1, verdict="PATH_VIOLATION", summary_log=msg[:1000], log_path=str(v_log))
                    await self._fail_attempt(task, msg, wt)
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
                    await self._fail_attempt(task, msg, wt)
                    return

            results = await verifier.run_commands(
                task.verify_cmds, wt, self.settings.verify_timeout_sec, shared_env, log_path=v_log
            )
            failed = next((r for r in results if r.exit_code != 0), None)
            if failed is not None:
                msg = f"验证命令失败（exit {failed.exit_code}）：`{failed.cmd}`\n```\n{failed.output}\n```"
                self.db.finish_run(
                    v_run_id, exit_code=failed.exit_code, verdict="FAIL", summary_log=msg[:1500], log_path=str(v_log)
                )
                await self._fail_attempt(task, msg, wt)
                return

            self.db.finish_run(v_run_id, exit_code=0, verdict="PASS", summary_log=summarize_verify(results)[:1500], log_path=str(v_log))

            # ---- 3. 独立只读审查 ----
            rev_ident = self.engine.agent_identity("reviewer", "质量保障组")
            current_actor.set(rev_ident["label"])
            self.db.update_task(task_id, status=TaskStatus.REVIEWING, active_agent_label=rev_ident["label"])
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
                json.dumps(exchange.get(artifact_key), ensure_ascii=False) if file_mode else gitops.diff_since(wt, base_sha),
                decisions=self.db.list_decisions(active_only=True),
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

            if verdict is None:
                await self._fail_attempt(task, f"只读审查输出无效：{r_res.error or parse_err}", wt)
                return

            if verdict.verdict == "PASS":
                sha = None if file_mode else gitops.head_sha(wt)
                self.db.update_task(
                    task_id,
                    status=TaskStatus.APPROVED,
                    candidate_commit_sha=sha,
                    artifact_digest=artifact_key,
                    failure_reason=None,
                    last_feedback=None,
                )
                self.db.add_event(
                    "task.approved",
                    f"{task_id} 审查通过（提交 {(artifact_key or sha)[:10]} · 署名 {coder_ident['author_name']}）",
                    task_id=task_id,
                    actor=rev_ident["label"],
                )
                return

            detail = self._format_review_feedback(verdict)
            if verdict.verdict == "ESCALATE_CONTRACT":
                await self._escalate_or_fuse(task, f"[审查建议升级契约]\n{detail}", wt, event_type="task.escalated")
                return

            await self._fail_attempt(task, f"独立审查未通过：\n{detail}", wt)
        finally:
            current_actor.reset(a_tok)
            if link:
                status = self.db.get_task(task_id).status
                try:
                    await track_issue(self.engine, task_id, "done" if status in (TaskStatus.APPROVED, TaskStatus.MERGED)
                                      else "closed" if status == TaskStatus.CANCELLED else "todo")
                except LoopForgeError as exc:
                    self.db.add_event("link.issue_sync_failed", str(exc), task_id=task_id)

    async def _execute_remote(self, task, squad, slot, exchange, before, protected):
        bridge = LoopForgeBridge(self.settings, self.db)
        files = self.settings.collaboration_mode == "files"
        slices = self.db.get_slices([*task.doc_slice_ids, "PRD-S01"])
        context = "\n".join(f"{s.source_file}\n{s.content}" for s in slices)
        baseline_file = ""
        if files:
            bdir = self.ws.state_dir / "baselines"
            bdir.mkdir(parents=True, exist_ok=True)
            bp = bdir / f"{task.task_id}.json"
            bp.write_text(json.dumps(before, ensure_ascii=False), "utf-8")
            baseline_file = str(bp.resolve())
        managed_git = not files and bool(self.db.meta_get("git_delivery_source"))
        git_context = {}
        if not files and (managed_git or not task.loopforge_run_id):
            wt, base = self._prepare_worktree(task)
            task = self.db.get_task(task.task_id)
            if managed_git:
                use_mirror = self.db.meta_get("gitea_mirror") == "1"
                pack_dir = None if use_mirror else self.ws.state_dir / "git-snapshots"
                snapshot = await asyncio.to_thread(local_git.prepare_snapshot, wt, base, pack_dir)
                pid = self.settings.loopforge_project_id
                sig = sign_context_sig(self.settings.project_signing_key(), pid, f"git:{snapshot}")
                if use_mirror:
                    from autodev import local_git_bridge
                    remote = await local_git_bridge.task_remote(wt, self.settings, self.db, task.task_id, snapshot)
                    git_context["git_restore"] = local_git_bridge.restore_command(remote, snapshot)
                else:
                    git_context["git_restore"] = local_git.restore_command(self.settings.vernal_public_url, pid, snapshot, sig)
            else:
                gitops.git(["push", "origin", f"{base}:refs/heads/{task.branch_name}"], wt)
        ok, msg = await bridge.execute_task(
            task,
            squad,
            slot,
            self.stop_event,
            inputs=before if files else None,
            context=context,
            baseline_file=baseline_file,
            vernal_base_url=self.settings.vernal_public_url,
            **git_context,
        )
        if self.stop_event.is_set():
            self.db.update_task(task.task_id, status=TaskStatus.READY)
            return
        task = self.db.get_task(task.task_id)
        if not ok:
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
                if not msg.startswith("查询 LoopForge"):
                    self.db.update_task(task.task_id, loopforge_run_id=None)
                self._fuse(task, f"[环境未就绪，已暂停重试] {msg}")
                return
            # 保留 loopforge_issue_id，使同一子任务始终复用同一条 LoopForge 需求原单重跑
            self.db.update_task(task.task_id, loopforge_run_id=None)
            await self._fail_attempt(task, msg, None)
            return
        run = await bridge.client.get_run(task.loopforge_run_id)
        run.setdefault("id", task.loopforge_run_id)
        try:
            if files:
                manifest = await bridge.client.named_artifact(run, "vernal-files.json")
                after = apply_output(before, manifest)
                key = exchange.patch(task, before, after, protected)
                with TemporaryDirectory(dir=self.ws.worktrees_dir) as directory:
                    materialize(after, Path(directory))
                    await self._check_files(task.verify_cmds, Path(directory), task.task_id)
                self.db.update_task(task.task_id, artifact_digest=key)
            elif managed_git:
                artifact = await bridge.client.named_artifact(run, "vernal-patch.json")
                sha = local_git.apply_patch(wt, base, artifact, task.allowed_paths, protected)
                await self._check_files(task.verify_cmds, wt, task.task_id)
                self.db.update_task(task.task_id, candidate_commit_sha=sha)
            else:
                record = await bridge.client.named_artifact(run, "mr-body.md")
                hashes = set(re.findall(r"(?i)\b[0-9a-f]{40}\b", record))
                if len(hashes) != 1:
                    raise ValueError("MR 产物需要标明唯一的完整提交 SHA")
                sha = hashes.pop()
                gitops.git(["fetch", "origin", sha], self.ws.root)
                if sha == task.base_sha or gitops.git(["merge-base", "--is-ancestor", task.base_sha, sha], self.ws.root, check=False).returncode:
                    raise ValueError("远端提交与派单基线不匹配")
                self.db.update_task(task.task_id, candidate_commit_sha=sha)
        except (ValueError, LoopForgeError, gitops.GitError, OSError) as exc:
            self.db.add_event("loopforge.invalid_delivery", f"{task.loopforge_run_id}: {exc}", task_id=task.task_id)
            self.db.update_task(task.task_id, loopforge_run_id=None)
            await self._fail_attempt(task, str(exc), None)
            return
        self.db.update_task(task.task_id, status=TaskStatus.APPROVED, failure_reason=None, last_feedback=None)
        self.db.add_event("task.approved", msg, task_id=task.task_id, actor="LoopForgeBridge")

    async def _check_files(self, commands, directory, label):
        results = await verifier.run_commands(
            commands, directory, self.settings.verify_timeout_sec, self.engine.shared_env(),
            log_path=self.ws.runs_dir / label / "files-verify.log",
        )
        for result in results:
            if result.exit_code:
                raise ValueError(f"验证失败 {result.cmd}\n{result.output}")

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
            self.db.add_event("milestone.integrated", f"文件集成通过 {key[:12]}", actor="Orchestrator")
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

    async def _fail_attempt(self, task: Task, reason: str, wt: Path | None) -> None:
        retries = task.retry_count + 1
        if retries >= task.max_retries:
            self.db.update_task(task.task_id, retry_count=retries, last_feedback=reason)
            refreshed = self.db.get_task(task.task_id) or task
            await self._escalate_or_fuse(refreshed, reason, wt, event_type="task.fused")
        else:
            self.db.update_task(
                task.task_id,
                status=TaskStatus.READY,
                retry_count=retries,
                last_feedback=reason,
                failure_reason=None,
            )
            self.db.add_event(
                "task.retry",
                f"{task.task_id} 第 {retries}/{task.max_retries} 轮未通过，将原会话续跑修复：{reason[:220]}",
                task_id=task.task_id,
                actor="Orchestrator",
            )

    async def _escalate_or_fuse(self, task: Task, reason: str, wt: Path | None, event_type: str = "task.fused") -> None:
        """Goal 模式下先由研发总监自动仲裁（契约升版 / 精准点拨），仲裁无果再熔断交人。"""
        mode = self.db.meta_get("planning_mode", self.settings.planning_mode)
        if mode == "goal" and task.arbitration_count < self.settings.director_max_arbitrations:
            async with self._arbitration_lock:
                decision = await self.engine.arbitrate_task(task, reason, wt)
                if decision and decision.action in ("STEER_RETRY", "PATCH_CONTRACT"):
                    return
        self._fuse(task, reason, event_type=event_type)

    def _fuse(self, task: Task, reason: str, event_type: str = "task.fused") -> None:
        self.db.update_task(
            task.task_id,
            status=TaskStatus.FUSED,
            last_feedback=reason,
            failure_reason=reason,
        )
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
