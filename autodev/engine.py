"""编排中枢：工作区初始化、文档摄入、PM 规格补全、总监分组蓝图、滚动拆单、异常仲裁与里程碑合并。"""

from __future__ import annotations

import contextvars
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autodev.artifacts import FileExchange, checked_name, materialize
from autodev import gitops
from autodev.config import (
    DEFAULT_CONFIG_TOML,
    Settings,
    Workspace,
    detect_toolchains,
    probe_agent_runtimes,
)
from autodev.dag import (
    normalize_blueprint,
    normalize_plan,
    normalize_task,
    normalize_wave,
    parallel_overlap_warnings,
    validate_blueprint,
    validate_plan,
    validate_wave,
)
from autodev.db import Database, DocSlice, Task, now_iso
from autodev.harnesses import Harness, create_harness
from autodev.harnesses.opencode_cli import write_opencode_agent_profiles
from autodev.ingest import ingest_directory, summarize, write_catalog
from autodev.models import (
    ACTIVE_TASK_STATUSES,
    ROLE_LABELS,
    DirectorDecision,
    MilestoneStatus,
    MilestoneWavePlan,
    PlanTask,
    ProductSpec,
    ProjectBlueprint,
    ProjectPlan,
    ProjectStatus,
    TaskStatus,
    write_schemas,
)
from autodev.prompts import (
    blueprint_to_markdown,
    build_blueprint_prompt,
    build_director_arbitration_prompt,
    build_plan_fix_prompt,
    build_planner_prompt,
    build_pm_prompt,
    build_wave_prompt,
    plan_to_markdown,
    read_contracts,
)

# 当前协程正在处理的任务与工位（供执行器实时事件归属）
current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_task", default=None)
current_slot: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_slot", default=None)
current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("current_actor", default="system")

AGENTS_MD_HEADER = "<!-- 由 AutoDev Hub 研发总监生成，可人工修改 -->\n\n"


class OpsError(RuntimeError):
    """人工操作的前置条件不满足。"""


class PlanningError(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("规划未通过校验：\n" + "\n".join(f"- {e}" for e in errors))


def init_workspace(
    root: Path,
    name: str | None = None,
    collaboration_mode: str | None = None,
    base_branch: str | None = None,
) -> Workspace:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root)
    ws.ensure_dirs()
    if not ws.config_path.exists():
        mode = collaboration_mode or "git"
        if mode not in ("files", "git"):
            raise OpsError("协作方式应为 files 或 git")
        cfg_txt = DEFAULT_CONFIG_TOML.replace('collaboration_mode = "git"', f'collaboration_mode = "{mode}"')
        if base_branch and base_branch.strip():
            cfg_txt = cfg_txt.replace('base_branch = "main"', f"base_branch = {json.dumps(base_branch.strip())}")
        ws.config_path.write_text(cfg_txt, "utf-8")
    settings = ws.load_settings()
    if collaboration_mode and settings.collaboration_mode != collaboration_mode:
        raise OpsError("已存在的工作区请在立项前从界面切换协作方式")
    if base_branch and base_branch.strip():
        settings.base_branch = base_branch.strip()
    if settings.collaboration_mode == "git":
        gitops.ensure_repo(root, settings.base_branch)
    opencode_files = write_opencode_agent_profiles(root)
    if settings.collaboration_mode == "git":
        gitops.commit_paths(root, [".gitignore", *opencode_files], "autodev: init workspace & opencode profiles")
    keep = ws.inputs_dir / ".gitkeep"
    if not any(ws.inputs_dir.iterdir()):
        keep.write_text("", "utf-8")
    db = Database(ws.db_path)
    try:
        db.ensure_slots(settings.max_concurrency)
        if db.meta_get("status") is None:
            db.meta_set("name", name or root.name)
            db.meta_set("status", ProjectStatus.CREATED)
            db.add_event("project.created", f"工作区已初始化：{root}")
    finally:
        db.close()
    return ws


class Engine:
    def __init__(
        self,
        ws: Workspace,
        settings: Settings | None = None,
        db: Database | None = None,
        harness: Harness | None = None,
    ):
        self.ws = ws
        self.settings = settings or ws.load_settings()
        self.db = db or Database(ws.db_path)
        self._harness = harness
        self._runtimes_cache: dict[str, dict[str, Any]] | None = None
        ws.ensure_dirs()
        self.schemas = write_schemas(ws.schemas_dir)
        self.db.ensure_slots(self.settings.max_concurrency)

    @property
    def harness(self) -> Harness:
        if self._harness is None:
            if self.settings.runtime_backend == "loopforge" and self.settings.effective_loopforge_token():
                from autodev.harnesses.loopforge_harness import LoopForgeHarness

                self._harness = LoopForgeHarness(
                    self.settings, self.db, self.ws, activity_cb=self._on_activity
                )
            else:
                self._harness = create_harness(self.settings, activity_cb=self._on_activity)
        return self._harness

    def reset_harness(self) -> None:
        if self._harness is not None and getattr(self._harness, "name", "") != "mock":
            self._harness = None
        self._runtimes_cache = None

    def agent_identity(self, role: str, squad: str = "") -> dict[str, str]:
        if self._harness is not None and self._harness.name == "mock":
            hname, model = "mock", "mock"
        else:
            hname = self.settings.harness_for(role)
            model = self.settings.model_for(role) or "default"
        role_cn = ROLE_LABELS.get(role, role)
        prefix = f"{squad} · {role_cn}" if squad else role_cn
        label = f"[{prefix}] {hname}/{model}"
        slug = re.sub(r"[^a-z0-9.-]+", "-", f"{squad or 'hq'}.{role}".lower()).strip("-")
        return {
            "squad": squad or "顶层指挥组",
            "role": role,
            "role_label": role_cn,
            "harness": hname,
            "model": model,
            "label": label,
            "author_name": f"{prefix} ({hname}/{model})",
            "author_email": f"{slug}@autodev.local",
        }

    def shared_env(self) -> dict[str, str]:
        """若存在共享虚拟环境（M0 基线创建或主仓库 .venv），注入给所有子 worktree 与门禁命令。"""
        env = dict(self.settings.env)
        for cand in (self.ws.shared_venv_dir, self.ws.root / ".venv"):
            bin_dir = cand / "bin"
            if bin_dir.is_dir():
                import os

                env["VIRTUAL_ENV"] = str(cand)
                env["PATH"] = f"{bin_dir}:{env.get('PATH') or os.environ.get('PATH', '')}"
                break
        return env

    def _on_activity(self, kind: str, text: str, payload: dict[str, Any] | None = None) -> None:
        slot_id = current_slot.get()
        actor = current_actor.get()
        if slot_id:
            try:
                self.db.update_slot(slot_id, current_action=text[:240])
            except Exception:
                pass
        self.db.add_event(f"agent.{kind}", text, task_id=current_task.get(), actor=actor, payload=payload)

    def runtimes_status(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._runtimes_cache is None or refresh:
            self._runtimes_cache = probe_agent_runtimes(self.settings)
        return self._runtimes_cache

    # ---------- 状态与全局快照 ----------
    @property
    def status(self) -> ProjectStatus:
        return ProjectStatus(self.db.meta_get("status", ProjectStatus.CREATED))

    def set_status(self, status: ProjectStatus) -> None:
        self.db.meta_set("status", status)

    def snapshot(self) -> dict[str, Any]:
        usage = self.db.usage_by_task()
        latest_runs = self.db.latest_run_by_task()
        latest_events = self.db.latest_event_by_task()
        squads = self.db.list_squads()
        squad_names = {s["squad_id"]: s["name"] for s in squads}

        tasks = []
        for t in self.db.list_tasks():
            d = t.to_dict()
            d["status"] = t.status.value
            d["squad_name"] = squad_names.get(t.squad, t.squad or t.module)
            d["usage"] = usage.get(t.task_id, {"input": 0, "cached": 0, "output": 0, "reasoning": 0})
            lr = latest_runs.get(t.task_id)
            d["active_started_at"] = lr["started_at"] if (lr and not lr.get("finished_at")) else None
            d["active_stage"] = lr["stage"] if (lr and not lr.get("finished_at")) else None
            le = latest_events.get(t.task_id)
            d["latest_activity"] = (
                {
                    "event_type": le["event_type"],
                    "actor": le.get("actor", "system"),
                    "message": le["message"],
                    "created_at": le["created_at"],
                }
                if le
                else None
            )
            tasks.append(d)

        milestones = []
        for m in self.db.list_milestones():
            d = m.to_dict()
            d["status"] = m.status.value
            milestones.append(d)

        total = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
        for u in usage.values():
            for k in total:
                total[k] += u.get(k, 0)

        return {
            "name": self.db.meta_get("name", self.ws.root.name),
            "root": str(self.ws.root),
            "status": self.status.value,
            "planning_mode": self.db.meta_get("planning_mode", self.settings.planning_mode),
            "has_prd": self.ws.prd_json_path.exists(),
            "council_count": len(self.db.list_council_messages()),
            "prd_vision": self.db.meta_get("prd_vision", ""),
            "plan_summary": self.db.meta_get("plan_summary", ""),
            "contract_hash": self.db.meta_get("contract_hash", ""),
            "slices": len(self.db.list_slices()),
            "squads": squads,
            "worker_slots": self.db.list_slots(),
            "runtimes": self.runtimes_status(),
            "milestones": milestones,
            "tasks": tasks,
            "usage_total": total,
            "settings": {
                "planning_mode": self.settings.planning_mode,
                "collaboration_mode": self.settings.collaboration_mode,
                "enable_council": self.settings.enable_council,
                "enable_pm_spec": self.settings.enable_pm_spec,
                "runtime_backend": self.settings.runtime_backend,
                "loopforge_url": self.settings.loopforge_url,
                "loopforge_configured": bool(self.settings.effective_loopforge_token() and self.settings.loopforge_project_id),
                "gitea_url": self.settings.gitea_url,
                "max_concurrency": self.settings.max_concurrency,
                "max_retries": self.settings.max_retries,
                "director_max_arbitrations": self.settings.director_max_arbitrations,
                "require_plan_approval": self.settings.require_plan_approval,
                "require_merge_approval": self.settings.require_merge_approval,
                "harness": self.settings.harness,
                "effective_harness": self.settings.harness_for("director"),
                "pm_harness": self.settings.pm_harness,
                "director_harness": self.settings.director_harness,
                "coder_harness": self.settings.coder_harness or self.settings.harness,
                "raw_coder_harness": self.settings.coder_harness,
                "qa_harness": self.settings.qa_harness,
                "effective_coder_harness": self.settings.harness_for("coder"),
                "effective_qa_harness": self.settings.harness_for("reviewer"),
                "model": self.settings.model,
                "director_model": self.settings.director_model,
                "coder_model": self.settings.coder_model,
                "qa_model": self.settings.qa_model,
            },
        }

    def _ensure_not_running(self) -> None:
        if self.status == ProjectStatus.RUNNING and self.ws.lock_path.exists():
            from autodev.scheduler import lock_is_held

            if lock_is_held(self.ws.lock_path):
                raise OpsError("调度器正在运行，请先停止后再执行该操作")

    def _occupy_command_slot(self, role: str, task_id: str, action: str) -> dict[str, str]:
        ident = self.agent_identity(role, "顶层指挥组")
        self.db.ensure_slots(self.settings.max_concurrency)
        self.db.update_slot(
            "command",
            status="WORKING",
            squad=ident["squad"],
            role=role,
            harness=ident["harness"],
            model=ident["model"],
            task_id=task_id,
            current_action=action,
            started_at=now_iso(),
        )
        return ident

    # ---------- 阶段一：摄入 ----------
    def record_documents(self, paths: list[str], message: str, **author: str) -> None:
        if self.settings.collaboration_mode == "git":
            gitops.commit_paths(self.ws.root, paths, message, **author)

    def ingest(self) -> int:
        self._ensure_not_running()
        slices = ingest_directory(self.ws.inputs_dir, self.settings.slice_max_chars, self.settings.slice_min_chars)
        if not slices:
            raise OpsError(f"{self.ws.inputs_dir} 中没有可解析的资料（支持 md/txt/proto/yaml/json/pdf/docx 等）")
        self.db.replace_slices(slices)
        write_catalog(slices, self.ws.catalog_path)
        self.record_documents(["inputs"], "autodev: update inputs")
        if self.status in (ProjectStatus.CREATED, ProjectStatus.INGESTED):
            self.set_status(ProjectStatus.INGESTED)
        files = len({s.source_file for s in slices})
        self.db.add_event("ingest.done", f"摄入 {files} 个文件，切分为 {len(slices)} 个切片")
        return len(slices)

    # ---------- 阶段二 A：产品经理（PM）规格补全 ----------
    async def run_pm_spec(self, extra_instructions: str = "", max_attempts: int = 2) -> ProductSpec:
        slices = [s for s in self.db.list_slices() if s.slice_id != "PRD-S01"]
        if not slices:
            raise OpsError("还没有资料切片，请先运行 ingest")
        ident = self._occupy_command_slot("pm", "_pm", "正在阅读输入架构并补全产品规格与异常边界…")
        t_tok = current_task.set("_pm")
        s_tok = current_slot.set("command")
        a_tok = current_actor.set(ident["label"])
        prompt = build_pm_prompt(slices, extra_instructions)
        errors: list[str] = []
        try:
            for attempt in range(1, max_attempts + 1):
                self.db.add_event("pm.started", f"{ident['label']} 启动产品规格梳理（第 {attempt} 次）", task_id="_pm", actor=ident["label"])
                log_dir = self.ws.runs_dir / "_pm" / f"attempt-{attempt}"
                run_id = self.db.start_run("_pm", attempt, "PM_SPEC", ident["harness"], agent_label=ident["label"])
                res = await self.harness.run_structured_readonly(
                    prompt, schema_path=self.schemas["prd_spec"], cwd=self.ws.root, log_dir=log_dir, role="pm"
                )
                spec: ProductSpec | None = None
                if res.ok and res.structured:
                    try:
                        spec = ProductSpec.model_validate(res.structured)
                    except ValidationError as exc:
                        errors = [str(exc)]
                else:
                    errors = [res.error or "PM 未返回合法 JSON"]
                self.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict="PASS" if spec else "INVALID",
                    summary_log=(spec.product_vision if spec else "; ".join(errors))[:1000],
                    log_path=str(log_dir),
                )
                if spec is not None:
                    self.ws.prd_json_path.write_text(
                        json.dumps(spec.model_dump(), ensure_ascii=False, indent=2), "utf-8"
                    )
                    self.ws.prd_md_path.parent.mkdir(parents=True, exist_ok=True)
                    self.ws.prd_md_path.write_text(spec.spec_markdown.strip() + "\n", "utf-8")
                    self.record_documents(
                        ["docs/PRD_SPEC.md"],
                        "autodev(pm): generate product specification docs/PRD_SPEC.md",
                        author_name=ident["author_name"],
                        author_email=ident["author_email"],
                    )
                    self.db.upsert_slice(
                        DocSlice(
                            slice_id="PRD-S01",
                            source_file="docs/PRD_SPEC.md",
                            heading_path="PRD_SPEC / 产品经理补全规格与验收红线",
                            summary=summarize(spec.spec_markdown),
                            content=spec.spec_markdown[:12_000],
                            char_count=len(spec.spec_markdown),
                        )
                    )
                    self.db.meta_set("prd_vision", spec.product_vision)
                    self.set_status(ProjectStatus.SPECIFIED)
                    self.db.add_event(
                        "pm.done",
                        f"产品规格书已生成：docs/PRD_SPEC.md（{len(spec.core_workflows)} 条业务流，{len(spec.edge_cases)} 项边界约定）",
                        task_id="_pm",
                        actor=ident["label"],
                    )
                    return spec
                prompt = build_plan_fix_prompt(prompt, res.final_message or "", errors)
        finally:
            self.db.release_slot("command")
            current_actor.reset(a_tok)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)
        raise PlanningError(errors)

    # ---------- 阶段二 B：研发总监立项蓝图（分组 + 里程碑路线图 + 首轮任务） ----------
    async def plan_blueprint(self, extra_instructions: str = "", max_attempts: int = 3) -> ProjectBlueprint:
        slices = self.db.list_slices()
        known = {s.slice_id for s in slices}
        ident = self._occupy_command_slot("director", "_planner", "正在划分研发组领地并制定全局里程碑路线图…")
        t_tok = current_task.set("_planner")
        s_tok = current_slot.set("command")
        a_tok = current_actor.set(ident["label"])
        base_prompt = build_blueprint_prompt(slices, detect_toolchains(), self.settings, extra_instructions)
        prompt = base_prompt
        errors: list[str] = []
        try:
            for attempt in range(1, max_attempts + 1):
                self.db.add_event(
                    "plan.started",
                    f"{ident['label']} 启动立项蓝图设计（第 {attempt} 次）",
                    task_id="_planner",
                    actor=ident["label"],
                )
                log_dir = self.ws.runs_dir / "_planner" / f"attempt-{attempt}"
                run_id = self.db.start_run("_planner", attempt, "BLUEPRINT", ident["harness"], agent_label=ident["label"])
                res = await self.harness.run_structured_readonly(
                    prompt, schema_path=self.schemas["project_blueprint"], cwd=self.ws.root, log_dir=log_dir, role="director"
                )
                previous = json.dumps(res.structured, ensure_ascii=False) if res.structured else res.final_message
                bp_obj: ProjectBlueprint | None = None
                if not res.ok or res.structured is None:
                    errors = [res.error or "研发总监未返回合法 JSON"]
                else:
                    try:
                        bp_obj = normalize_blueprint(ProjectBlueprint.model_validate(res.structured), known)
                        errors = validate_blueprint(bp_obj, self.settings.protected_paths)
                    except ValidationError as exc:
                        errors = [f"蓝图 JSON 不符合 schema: {exc}"]
                self.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict="PASS" if (bp_obj and not errors) else "INVALID",
                    summary_log=(bp_obj.summary if (bp_obj and not errors) else "; ".join(errors))[:1000],
                    log_path=str(log_dir),
                )
                if bp_obj and not errors:
                    self._apply_blueprint(bp_obj, ident)
                    return bp_obj
                self.db.add_event(
                    "plan.invalid",
                    f"第 {attempt} 次蓝图未通过校验：{'; '.join(errors)[:500]}",
                    task_id="_planner",
                    actor=ident["label"],
                )
                prompt = build_plan_fix_prompt(base_prompt, previous or "", errors)
        finally:
            self.db.release_slot("command")
            current_actor.reset(a_tok)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)
        raise PlanningError(errors)

    def _apply_blueprint(self, bp: ProjectBlueprint, ident: dict[str, str]) -> None:
        self.db.save_blueprint(bp, self.settings.max_retries)
        bp_dict = bp.model_dump()
        self.ws.blueprint_path.write_text(json.dumps(bp_dict, ensure_ascii=False, indent=2), "utf-8")
        # 同步维护 plan.json / plan.md 以便统一展示
        compat = {
            "mode": "goal",
            "summary": bp.summary,
            "tech_stack": bp.tech_stack,
            "squads": bp_dict["squads"],
            "modules": [{"name": s.name, "path": ", ".join(s.owned_paths), "description": s.mission} for s in bp.squads],
            "milestones": bp_dict["milestones"],
            "tasks": bp_dict["initial_tasks"],
        }
        self.ws.plan_json_path.write_text(json.dumps(compat, ensure_ascii=False, indent=2), "utf-8")
        self.ws.plan_md_path.write_text(blueprint_to_markdown(bp_dict), "utf-8")
        (self.ws.root / "AGENTS.md").write_text(AGENTS_MD_HEADER + bp.agents_md.strip() + "\n", "utf-8")
        oc_files = write_opencode_agent_profiles(self.ws.root)
        self.record_documents(
            ["AGENTS.md", *oc_files],
            "autodev(director): establish AGENTS.md & squad territories",
            author_name=ident["author_name"],
            author_email=ident["author_email"],
        )
        self.db.meta_set("planning_mode", "goal")
        self.db.meta_set("plan_summary", bp.summary)
        for w in parallel_overlap_warnings(bp.initial_tasks):
            self.db.add_event("plan.warning", w, actor=ident["label"])
        rolling_cnt = sum(1 for m in self.db.list_milestones() if not m.planned)
        self.db.add_event(
            "plan.done",
            f"立项蓝图就绪：{len(bp.squads)} 个研发组，{len(bp.milestones)} 个里程碑（首轮展开 {len(bp.initial_tasks)} 单，后续 {rolling_cnt} 个里程碑将滚动拆解）",
            task_id="_planner",
            actor=ident["label"],
        )
        self.set_status(ProjectStatus.PLANNED if self.settings.require_plan_approval else ProjectStatus.APPROVED)

    # ---------- 统一规划入口（classic 单次模式 / goal 多角色滚动模式） ----------
    async def plan(
        self, extra_instructions: str = "", max_attempts: int = 3, mode: str | None = None
    ) -> ProjectPlan | ProjectBlueprint:
        self._ensure_not_running()
        progressed = [
            t for t in self.db.list_tasks() if t.status not in (TaskStatus.BLOCKED, TaskStatus.READY)
        ]
        if progressed:
            raise OpsError(f"已有 {len(progressed)} 个任务开始执行，不能重新规划（如需重来请新建工作区）")
        if not self.db.list_slices():
            raise OpsError("还没有资料切片，请先运行 ingest")

        effective_mode = (mode or self.settings.planning_mode).strip().lower()
        if effective_mode == "goal":
            if self.settings.enable_council:
                from autodev.council import run_council

                await run_council(self, extra_instructions=extra_instructions)
            elif self.settings.enable_pm_spec:
                await self.run_pm_spec(extra_instructions=extra_instructions)
            return await self.plan_blueprint(extra_instructions=extra_instructions, max_attempts=max_attempts)

        # Classic 单次全量规划（向后兼容）
        slices = self.db.list_slices()
        known = {s.slice_id for s in slices}
        ident = self._occupy_command_slot("planner", "_planner", "正在读取资料并拆解里程碑与任务 DAG…")
        t_tok = current_task.set("_planner")
        s_tok = current_slot.set("command")
        a_tok = current_actor.set(ident["label"])
        base_prompt = build_planner_prompt(slices, detect_toolchains(), self.settings, extra_instructions)
        prompt = base_prompt
        errors: list[str] = []
        try:
            for attempt in range(1, max_attempts + 1):
                self.db.add_event("plan.started", f"{ident['label']} 规划会话启动（第 {attempt} 次）", task_id="_planner", actor=ident["label"])
                log_dir = self.ws.runs_dir / "_planner" / f"attempt-{attempt}"
                run_id = self.db.start_run("_planner", attempt, "PLAN", ident["harness"], agent_label=ident["label"])
                res = await self.harness.run_structured_readonly(
                    prompt, schema_path=self.schemas["project_plan"], cwd=self.ws.root, log_dir=log_dir, role="planner"
                )
                previous = json.dumps(res.structured, ensure_ascii=False) if res.structured else res.final_message
                plan_obj: ProjectPlan | None = None
                if not res.ok or res.structured is None:
                    errors = [res.error or "规划会话没有返回合法 JSON"]
                else:
                    try:
                        plan_obj = normalize_plan(ProjectPlan.model_validate(res.structured), known)
                        errors = validate_plan(plan_obj, self.settings.protected_paths)
                    except ValidationError as exc:
                        errors = [f"JSON 结构不符合 schema: {exc}"]
                self.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict="PASS" if (plan_obj and not errors) else "INVALID",
                    summary_log=(plan_obj.summary if (plan_obj and not errors) else "; ".join(errors))[:1000],
                    log_path=str(log_dir),
                )
                if plan_obj and not errors:
                    self._apply_plan(plan_obj, ident)
                    return plan_obj
                self.db.add_event(
                    "plan.invalid",
                    f"第 {attempt} 次规划未通过校验：{'; '.join(errors)[:500]}",
                    task_id="_planner",
                    actor=ident["label"],
                )
                prompt = build_plan_fix_prompt(base_prompt, previous or "", errors)
        finally:
            self.db.release_slot("command")
            current_actor.reset(a_tok)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)
        raise PlanningError(errors)

    def _apply_plan(self, plan: ProjectPlan, ident: dict[str, str]) -> None:
        self.db.save_plan(plan, self.settings.max_retries)
        plan_dict = plan.model_dump()
        self.ws.plan_json_path.write_text(json.dumps(plan_dict, ensure_ascii=False, indent=2), "utf-8")
        self.ws.plan_md_path.write_text(plan_to_markdown(plan_dict), "utf-8")
        (self.ws.root / "AGENTS.md").write_text(AGENTS_MD_HEADER + plan.agents_md.strip() + "\n", "utf-8")
        self.record_documents(
            ["AGENTS.md"],
            "autodev: AGENTS.md from plan",
            author_name=ident["author_name"],
            author_email=ident["author_email"],
        )
        self.db.meta_set("planning_mode", "classic")
        self.db.meta_set("plan_summary", plan.summary)
        for w in parallel_overlap_warnings(plan):
            self.db.add_event("plan.warning", w, actor=ident["label"])
        self.db.add_event(
            "plan.done",
            f"规划完成：{len(plan.milestones)} 个里程碑，{len(plan.tasks)} 个任务",
            task_id="_planner",
            actor=ident["label"],
            payload={"milestones": [m.milestone_id for m in plan.milestones]},
        )
        self.set_status(ProjectStatus.PLANNED if self.settings.require_plan_approval else ProjectStatus.APPROVED)

    # ---------- 研发总监：滚动展开下一里程碑 ----------
    def _next_task_seq(self) -> int:
        mx = 0
        for t in self.db.list_tasks():
            m = re.search(r"(\d+)$", t.task_id)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx + 1

    async def expand_milestone_wave(self, milestone_id: str, max_attempts: int = 3) -> MilestoneWavePlan:
        ms = self.db.get_milestone(milestone_id)
        if ms is None:
            raise OpsError(f"里程碑不存在: {milestone_id}")
        slices = self.db.list_slices()
        known = {s.slice_id for s in slices}
        all_ms = self.db.list_milestones()
        ms_order = {m.milestone_id: m.seq_order for m in all_ms}
        existing_tasks = self.db.list_tasks()
        existing_ids = {t.task_id: ms_order.get(t.milestone_id, 0) for t in existing_tasks}
        completed = [t for t in existing_tasks if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)]

        ident = self._occupy_command_slot("director", "_director", f"正在结合主干代码滚动拆解里程碑 {milestone_id}…")
        t_tok = current_task.set("_director")
        s_tok = current_slot.set("command")
        a_tok = current_actor.set(ident["label"])
        base_prompt = build_wave_prompt(
            milestone_dict=ms.to_dict(),
            all_milestones=[m.to_dict() for m in all_ms],
            squads=self.db.list_squads(),
            slices=slices,
            repo_files=(sorted(FileExchange(self.ws, self.db).baseline([])) if self.settings.collaboration_mode == "files" else gitops.list_repo_files(self.ws.root)),
            contracts=read_contracts(self.ws.root, self.settings.protected_paths),
            completed_tasks=completed,
            next_task_seq=self._next_task_seq(),
            settings=self.settings,
        )
        prompt = base_prompt
        errors: list[str] = []
        try:
            for attempt in range(1, max_attempts + 1):
                self.db.add_event(
                    "director.wave_started",
                    f"{ident['label']} 开始滚动拆解里程碑 {milestone_id}（第 {attempt} 次）",
                    task_id="_director",
                    actor=ident["label"],
                )
                log_dir = self.ws.runs_dir / "_director" / f"wave-{milestone_id}-attempt-{attempt}"
                run_id = self.db.start_run("_director", attempt, f"WAVE:{milestone_id}", ident["harness"], agent_label=ident["label"])
                res = await self.harness.run_structured_readonly(
                    prompt, schema_path=self.schemas["milestone_wave"], cwd=self.ws.root, log_dir=log_dir, role="director"
                )
                previous = json.dumps(res.structured, ensure_ascii=False) if res.structured else res.final_message
                wave_obj: MilestoneWavePlan | None = None
                if not res.ok or res.structured is None:
                    errors = [res.error or "研发总监未返回合法波次 JSON"]
                else:
                    try:
                        wave_obj = normalize_wave(MilestoneWavePlan.model_validate(res.structured), milestone_id, known)
                        errors = validate_wave(wave_obj, ms_order, existing_ids, self.settings.protected_paths)
                    except ValidationError as exc:
                        errors = [f"波次 JSON 不符合 schema: {exc}"]
                self.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict="PASS" if (wave_obj and not errors) else "INVALID",
                    summary_log=(wave_obj.wave_notes if (wave_obj and not errors) else "; ".join(errors))[:1000],
                    log_path=str(log_dir),
                )
                if wave_obj and not errors:
                    self.db.append_wave_tasks(milestone_id, wave_obj.tasks, wave_obj.wave_notes, self.settings.max_retries)
                    self._refresh_plan_artifacts()
                    squad_dist = ", ".join(sorted({t.squad for t in wave_obj.tasks}))
                    self.db.add_event(
                        "director.wave_ready",
                        f"里程碑 {milestone_id} 滚动拆解完成：派发 {len(wave_obj.tasks)} 个任务至研发组 [{squad_dist}]",
                        task_id="_director",
                        actor=ident["label"],
                    )
                    return wave_obj
                prompt = build_plan_fix_prompt(base_prompt, previous or "", errors)
        finally:
            self.db.release_slot("command")
            current_actor.reset(a_tok)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)
        raise PlanningError(errors)

    def _refresh_plan_artifacts(self) -> None:
        if not self.ws.blueprint_path.exists():
            return
        try:
            bp_dict = json.loads(self.ws.blueprint_path.read_text("utf-8"))
        except json.JSONDecodeError:
            return
        all_tasks = [t.to_dict() for t in self.db.list_tasks()]
        self.ws.plan_md_path.write_text(blueprint_to_markdown(bp_dict, tasks=all_tasks), "utf-8")
        compat = {
            "mode": "goal",
            "summary": bp_dict.get("summary", ""),
            "tech_stack": bp_dict.get("tech_stack", ""),
            "squads": bp_dict.get("squads", []),
            "modules": [
                {"name": s["name"], "path": ", ".join(s.get("owned_paths", [])), "description": s.get("mission", "")}
                for s in bp_dict.get("squads", [])
            ],
            "milestones": bp_dict.get("milestones", []),
            "tasks": all_tasks,
        }
        self.ws.plan_json_path.write_text(json.dumps(compat, ensure_ascii=False, indent=2), "utf-8")

    # ---------- 研发总监：熔断 / 契约变更自动仲裁 ----------
    async def arbitrate_task(self, task: Task, reason: str, wt: Path | None) -> DirectorDecision | None:
        if task.arbitration_count >= self.settings.director_max_arbitrations:
            return None
        next_patch_id = f"{task.task_id}-P{task.arbitration_count + 1}"
        ident = self._occupy_command_slot("director", "_director", f"正在仲裁阻塞任务 {task.task_id}…")
        t_tok = current_task.set("_director")
        s_tok = current_slot.set("command")
        a_tok = current_actor.set(ident["label"])
        diffstat = gitops.working_diff_stat(wt, task.base_sha) if (wt and wt.exists() and task.base_sha) else ""
        contracts = read_contracts(wt if (wt and wt.exists()) else self.ws.root, self.settings.protected_paths)
        prompt = build_director_arbitration_prompt(task, reason, contracts, diffstat, next_patch_id, self.settings)
        attempt = task.arbitration_count + 1
        log_dir = self.ws.runs_dir / "_director" / f"arb-{task.task_id}-{attempt}"
        run_id = self.db.start_run("_director", attempt, f"ARBITRATE:{task.task_id}", ident["harness"], agent_label=ident["label"])
        try:
            self.db.add_event(
                "director.arbitrating",
                f"{ident['label']} 介入仲裁 {task.task_id}（第 {attempt}/{self.settings.director_max_arbitrations} 次）",
                task_id=task.task_id,
                actor=ident["label"],
            )
            res = await self.harness.run_structured_readonly(
                prompt, schema_path=self.schemas["director_decision"], cwd=self.ws.root, log_dir=log_dir, role="director"
            )
            decision: DirectorDecision | None = None
            if res.ok and res.structured:
                try:
                    decision = DirectorDecision.model_validate(res.structured)
                except ValidationError:
                    decision = None
            self.db.finish_run(
                run_id,
                session_id=res.session_id,
                exit_code=res.exit_code,
                input_tokens=res.usage.input_tokens,
                cached_tokens=res.usage.cached_tokens,
                output_tokens=res.usage.output_tokens,
                reasoning_tokens=res.usage.reasoning_tokens,
                verdict=decision.action if decision else "ERROR",
                summary_log=(decision.diagnosis if decision else res.error)[:1200],
                log_path=str(log_dir),
            )
            if decision is None:
                return None

            new_arb = task.arbitration_count + 1
            if decision.action == "STEER_RETRY":
                self.db.update_task(
                    task.task_id,
                    status=TaskStatus.READY,
                    retry_count=0,
                    arbitration_count=new_arb,
                    steer_note=f"[研发总监仲裁指导] {decision.steer_note or decision.diagnosis}",
                    failure_reason=None,
                )
                self.db.add_event(
                    "director.steered",
                    f"总监判定可自动修复，已下发排错方案并重置 {task.task_id}：{(decision.steer_note or decision.diagnosis)[:200]}",
                    task_id=task.task_id,
                    actor=ident["label"],
                )
                return decision

            if decision.action == "PATCH_CONTRACT" and decision.contract_patch.title.strip():
                cp = decision.contract_patch
                known = {s.slice_id for s in self.db.list_slices()}
                patch_task = normalize_task(
                    PlanTask(
                        task_id=next_patch_id,
                        milestone_id=task.milestone_id,
                        squad="squad-contract",
                        module="contracts",
                        role="contract",
                        title=cp.title,
                        description=cp.description,
                        acceptance_criteria=cp.acceptance_criteria or ["补齐上游契约字段并通过契约测试"],
                        depends_on=[],
                        doc_slice_ids=task.doc_slice_ids,
                        allowed_paths=cp.allowed_paths or list(self.settings.protected_paths),
                        verify_cmds=cp.verify_cmds or ["python3 -m unittest discover -s tests -t ."],
                        priority=1,
                    ),
                    known,
                )
                self.db.insert_single_task(patch_task, self.settings.max_retries)
                new_deps = list(dict.fromkeys([*task.depends_on, next_patch_id]))
                # 重置原任务 worktree，待契约补丁 APPROVED 后自动合成最新契约基线继续
                if task.worktree_path and self.settings.collaboration_mode == "git":
                    gitops.remove_worktree(self.ws.root, Path(task.worktree_path))
                self.db.update_task(
                    task.task_id,
                    status=TaskStatus.BLOCKED,
                    depends_on=new_deps,
                    retry_count=0,
                    arbitration_count=new_arb,
                    worktree_path=None,
                    base_sha=None,
                    last_session_id=None,
                    steer_note=f"[研发总监契约升版通知] 已自动派发 {next_patch_id} 补齐契约：{decision.steer_note}",
                    failure_reason=None,
                )
                self._refresh_plan_artifacts()
                self.db.add_event(
                    "director.contract_patched",
                    f"总监批准契约升版：已自动创建高优契约单 {next_patch_id}，完成后将自动解锁 {task.task_id}",
                    task_id=task.task_id,
                    actor=ident["label"],
                )
                return decision

            self.db.update_task(task.task_id, arbitration_count=new_arb)
            self.db.add_event(
                "director.escalated_human",
                f"总监判定需人工拍板：{decision.diagnosis[:240]}",
                task_id=task.task_id,
                actor=ident["label"],
            )
            return decision
        finally:
            self.db.release_slot("command")
            current_actor.reset(a_tok)
            current_slot.reset(s_tok)
            current_task.reset(t_tok)

    # ---------- 审批 / 接管 / 合并 ----------
    def approve_plan(self, actor: str = "owner") -> None:
        if self.status != ProjectStatus.PLANNED:
            raise OpsError(f"当前状态 {self.status} 不需要批准规划")
        self.set_status(ProjectStatus.APPROVED)
        self.db.add_event("plan.approved", "立项蓝图与规划已批准，可启动全自动滚动研发", actor=actor)

    def update_task_plan(
        self, task_id: str, actor: str, priority: int | None = None, depends_on: list[str] | None = None
    ) -> None:
        if self.status not in (ProjectStatus.PLANNED, ProjectStatus.APPROVED):
            raise OpsError("只能在开始运行前调整排期")
        task = self._get_task(task_id)
        values: dict[str, Any] = {}
        if priority is not None:
            values["priority"] = priority
        if depends_on is not None:
            from autodev.dag import CycleError, topo_sort

            graph = {t.task_id: t.depends_on for t in self.db.list_tasks()}
            graph[task_id] = depends_on
            unknown = [d for d in depends_on if d not in graph]
            if unknown:
                raise OpsError(f"未知任务: {unknown}")
            try:
                topo_sort(graph)
            except CycleError as exc:
                raise OpsError(str(exc)) from exc
            values["depends_on"] = depends_on
            values["status"] = TaskStatus.BLOCKED if depends_on else TaskStatus.READY
        if values:
            self.db.update_task(task.task_id, **values)
            self.db.add_event("plan.edited", f"调整排期：{values}", task_id=task_id, actor=actor)

    def _get_task(self, task_id: str) -> Task:
        task = self.db.get_task(task_id)
        if task is None:
            raise OpsError(f"任务不存在: {task_id}")
        return task

    def steer(self, task_id: str, note: str, actor: str, verify_only: bool = False) -> None:
        task = self._get_task(task_id)
        if task.status in ACTIVE_TASK_STATUSES:
            raise OpsError(f"{task_id} 正在执行（{task.status}），请等待本轮结束")
        if task.status == TaskStatus.MERGED:
            raise OpsError(f"{task_id} 已合入主干，不能重开")
        if task.status in (TaskStatus.BLOCKED, TaskStatus.READY) and not note.strip():
            raise OpsError(f"{task_id} 尚未执行，无需接管")
        self.db.update_task(
            task_id,
            status=TaskStatus.READY if task.status != TaskStatus.BLOCKED else TaskStatus.BLOCKED,
            retry_count=0,
            arbitration_count=0,
            steer_note=note.strip() or None,
            pending_action="verify_only" if verify_only else None,
            owner_member=actor,
            failure_reason=None,
            loopforge_run_id=None,
        )
        ms = self.db.get_milestone(task.milestone_id)
        if ms and ms.status in (MilestoneStatus.FAILED, MilestoneStatus.AWAITING_MERGE):
            self.db.update_milestone(ms.milestone_id, status=MilestoneStatus.PENDING)
        action = "重新验证" if verify_only else "点拨并继续"
        self.db.add_event("task.steered", f"{action}：{note.strip() or '（无附言）'}", task_id=task_id, actor=actor)
        if self.status in (ProjectStatus.STALLED, ProjectStatus.COMPLETED):
            self.set_status(ProjectStatus.APPROVED)

    def reintegrate(self, milestone_id: str, actor: str) -> None:
        ms = self.db.get_milestone(milestone_id)
        if ms is None:
            raise OpsError(f"里程碑不存在: {milestone_id}")
        if ms.status != MilestoneStatus.FAILED:
            raise OpsError(f"{milestone_id} 当前状态 {ms.status}，无需重新集成")
        self.db.update_milestone(milestone_id, status=MilestoneStatus.PENDING, failure_reason=None)
        self.db.add_event("milestone.reintegrate", f"{milestone_id} 将重新集成", actor=actor)
        if self.status in (ProjectStatus.STALLED, ProjectStatus.COMPLETED):
            self.set_status(ProjectStatus.APPROVED)

    def merge_milestone(self, milestone_id: str, actor: str) -> None:
        ms = self.db.get_milestone(milestone_id)
        if ms is None:
            raise OpsError(f"里程碑不存在: {milestone_id}")
        if ms.status == MilestoneStatus.AWAITING_MERGE and self.settings.collaboration_mode == "files":
            from tempfile import NamedTemporaryFile, TemporaryDirectory

            exchange = FileExchange(self.ws, self.db)
            key = self.db.meta_get(f"delivery:{milestone_id}")
            files = exchange.get(key)
            is_inplace = self.db.meta_get("source_mode") == "local_folder"
            changed_paths: list[str] = []
            if is_inplace:
                prev_files = exchange.baseline([])
                changed_paths = sorted(
                    p for p in (prev_files.keys() | files.keys()) if prev_files.get(p) != files.get(p)
                )
                conflicts: list[str] = []
                for rel in changed_paths:
                    safe_rel = checked_name(rel)
                    target = (self.ws.root / safe_rel).resolve()
                    if not target.is_relative_to(self.ws.root.resolve()):
                        raise OpsError(f"拒绝写回越界路径：{rel}")
                    disk_val: str | None
                    if target.exists():
                        if not target.is_file() or target.is_symlink():
                            conflicts.append(rel)
                            continue
                        try:
                            disk_val = target.read_text("utf-8")
                        except (OSError, UnicodeDecodeError):
                            conflicts.append(rel)
                            continue
                    else:
                        disk_val = None
                    base_val = prev_files.get(rel)
                    new_val = files.get(rel)
                    if disk_val != base_val and disk_val != new_val:
                        conflicts.append(rel)
                if conflicts:
                    shown = "、".join(conflicts[:8]) + (f" 等 {len(conflicts)} 个文件" if len(conflicts) > 8 else "")
                    raise OpsError(
                        f"原目录存在运行期间的手工修改冲突，已中止写回（里程碑保持待合并，请备份或同步以下文件后重试）：{shown}"
                    )

            root = self.ws.state_dir / "deliveries"
            root.mkdir(exist_ok=True)
            destination = root / key
            if not destination.exists():
                with TemporaryDirectory(dir=root) as directory:
                    temp = Path(directory) / "release"
                    materialize(files, temp)
                    temp.rename(destination)

            if is_inplace:
                ws_root = self.ws.root.resolve()
                for rel in changed_paths:
                    safe_rel = checked_name(rel)
                    target = (ws_root / safe_rel).resolve()
                    new_val = files.get(rel)
                    if new_val is None:
                        if target.is_file():
                            target.unlink()
                        parent_dir = target.parent
                        while parent_dir != ws_root and parent_dir.exists() and not any(parent_dir.iterdir()):
                            parent_dir.rmdir()
                            parent_dir = parent_dir.parent
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as tf:
                            tf.write(new_val.encode("utf-8"))
                        Path(tf.name).replace(target)

            for task in self.db.list_tasks(milestone_id=milestone_id):
                self.db.update_task(task.task_id, status=TaskStatus.MERGED)
            self.db.update_milestone(milestone_id, status=MilestoneStatus.MERGED)
            msg = (
                f"已写回原目录（更新 {len(changed_paths)} 个文件）并归档交付至 {destination}"
                if is_inplace
                else f"已交付文件 {destination}"
            )
            self.db.add_event("milestone.merged", msg, actor=actor)
            if all(m.status == MilestoneStatus.MERGED for m in self.db.list_milestones()):
                self.set_status(ProjectStatus.COMPLETED)
            return
        if ms.status != MilestoneStatus.AWAITING_MERGE or not ms.branch_name:
            raise OpsError(f"{milestone_id} 当前状态 {ms.status}，不能合并")
        root, base = self.ws.root, self.settings.base_branch
        if gitops.current_branch(root) != base:
            raise OpsError(f"主仓库当前不在 {base} 分支，请先切换")
        if not gitops.is_clean(root):
            raise OpsError("主仓库工作区存在未提交改动，请先提交或清理后再合并")
        prev_sha = gitops.head_sha(root)
        gitops.fast_forward(root, ms.branch_name, f"autodev: merge milestone {milestone_id}")
        merged_sha = gitops.head_sha(root)
        pushed_remote = False
        if gitops.has_remote(root, "origin"):
            ok_push, push_out = gitops.push_branch(root, base, remote="origin")
            if not ok_push:
                gitops.reset_hard(root, prev_sha)
                raise OpsError(f"推送 {base} 到远端 origin 失败，已回滚本地合入：{push_out}")
            pushed_remote = True
        for t in self.db.list_tasks(milestone_id=milestone_id):
            self.db.update_task(t.task_id, status=TaskStatus.MERGED, assigned_slot=None, active_agent_label=None)
            if t.worktree_path:
                gitops.remove_worktree(root, Path(t.worktree_path))
                self.db.update_task(t.task_id, worktree_path=None)
        gitops.remove_worktree(root, self.ws.integration_dir / milestone_id)
        self.db.update_milestone(milestone_id, status=MilestoneStatus.MERGED)
        self.db.meta_set("contract_hash", gitops.tree_fingerprint(root, self.settings.protected_paths))
        push_note = " 并推送至 origin" if pushed_remote else ""
        self.db.add_event(
            "milestone.merged", f"{milestone_id} 已合入 {base}{push_note}（{merged_sha[:10]}）", actor=actor
        )
        if all(m.status == MilestoneStatus.MERGED for m in self.db.list_milestones()):
            self.set_status(ProjectStatus.COMPLETED)
            self.db.add_event("project.completed", "全部里程碑已合入主干 🎉")
