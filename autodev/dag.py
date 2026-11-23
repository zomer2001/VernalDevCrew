"""DAG 工具：基于 Python 标准库 graphlib 的拓扑排序、环路检测，以及蓝图/滚动波次静态校验。"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from graphlib import CycleError as StdCycleError, TopologicalSorter

from autodev.models import MilestoneWavePlan, PlanTask, ProjectBlueprint, ProjectPlan, SquadSpec


class CycleError(ValueError):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__("存在循环依赖: " + " -> ".join(cycle))


def topo_sort(graph: Mapping[str, Iterable[str]]) -> list[str]:
    """graph: node -> 前置依赖列表。使用标准库 graphlib 并保持同层字典序确定性。"""
    deps = {n: sorted(set(d) & set(graph)) for n, d in graph.items()}
    ts = TopologicalSorter(deps)
    try:
        ts.prepare()
    except StdCycleError as exc:
        # StdCycleError.args[1] 为逆向环路径，统一转为正向 A -> B -> A
        raw_cycle = list(exc.args[1]) if len(exc.args) > 1 and isinstance(exc.args[1], list) else []
        if raw_cycle:
            raw_cycle.reverse()
            raise CycleError(raw_cycle) from exc
        raise CycleError(sorted(graph)) from exc

    order: list[str] = []
    pool: list[str] = []
    while ts.is_active():
        pool.extend(ts.get_ready())
        if not pool:
            break
        pool.sort()
        node = pool.pop(0)
        order.append(node)
        ts.done(node)
    return order


def waves(graph: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """按依赖层级划分可并行批次。"""
    deps = {n: sorted(set(d) & set(graph)) for n, d in graph.items()}
    ts = TopologicalSorter(deps)
    try:
        ts.prepare()
    except StdCycleError as exc:
        topo_sort(graph)
        raise AssertionError from exc

    batches: list[list[str]] = []
    while ts.is_active():
        ready = sorted(ts.get_ready())
        if not ready:
            break
        batches.append(ready)
        ts.done(*ready)
    return batches


# ---------- 路径工具 ----------

def normalize_path(p: str) -> str:
    raw = p.strip().replace("\\", "/")
    is_dir = raw.endswith("/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/"):
        return ""
    norm = posixpath.normpath(raw)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        return ""
    return norm + "/" if is_dir else norm


def path_allowed(path: str, allowed: Iterable[str]) -> bool:
    path = path.strip().replace("\\", "/")
    for prefix in allowed:
        pre = prefix.rstrip("/")
        if not pre:
            continue
        if path == pre or path.startswith(pre + "/"):
            return True
    return False


def paths_overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


# ---------- 清洗与静态校验 ----------

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,48}$")


def normalize_task(t: PlanTask, known_slice_ids: set[str]) -> PlanTask:
    squad = t.squad.strip() or t.module.strip()
    return t.model_copy(
        update={
            "task_id": t.task_id.strip(),
            "milestone_id": t.milestone_id.strip(),
            "squad": squad,
            "module": t.module.strip(),
            "depends_on": [d.strip() for d in t.depends_on if d.strip()],
            "allowed_paths": [normalize_path(p) for p in t.allowed_paths if p.strip()],
            "verify_cmds": [c.strip() for c in t.verify_cmds if c.strip()],
            "acceptance_criteria": [c.strip() for c in t.acceptance_criteria if c.strip()],
            "doc_slice_ids": [s.strip() for s in t.doc_slice_ids if s.strip() in known_slice_ids],
        }
    )


def normalize_plan(plan: ProjectPlan, known_slice_ids: set[str]) -> ProjectPlan:
    tasks = [normalize_task(t, known_slice_ids) for t in plan.tasks]
    milestones = [
        m.model_copy(update={"milestone_id": m.milestone_id.strip(), "integration_cmd": m.integration_cmd.strip()})
        for m in plan.milestones
    ]
    return plan.model_copy(update={"tasks": tasks, "milestones": milestones})


def normalize_blueprint(bp: ProjectBlueprint, known_slice_ids: set[str]) -> ProjectBlueprint:
    squads = [
        SquadSpec(
            squad_id=s.squad_id.strip(),
            name=s.name.strip(),
            mission=s.mission.strip(),
            owned_paths=[normalize_path(p) for p in s.owned_paths if p.strip()],
            roles=[r.strip() for r in s.roles if r.strip()],
            sandbox_profile=(s.sandbox_profile or "generic-docker").strip(),
        )
        for s in bp.squads
    ]
    milestones = [
        m.model_copy(
            update={
                "milestone_id": m.milestone_id.strip(),
                "integration_cmd": m.integration_cmd.strip(),
                "target_squads": [sq.strip() for sq in m.target_squads if sq.strip()],
                "key_deliverables": [kd.strip() for kd in m.key_deliverables if kd.strip()],
            }
        )
        for m in bp.milestones
    ]
    tasks = [normalize_task(t, known_slice_ids) for t in bp.initial_tasks]
    return bp.model_copy(update={"squads": squads, "milestones": milestones, "initial_tasks": tasks})


def normalize_wave(wave: MilestoneWavePlan, milestone_id: str, known_slice_ids: set[str]) -> MilestoneWavePlan:
    tasks = [
        normalize_task(t.model_copy(update={"milestone_id": milestone_id}), known_slice_ids) for t in wave.tasks
    ]
    return wave.model_copy(update={"milestone_id": milestone_id, "tasks": tasks})


def _validate_task_list(
    tasks_list: list[PlanTask],
    ms_order: dict[str, int],
    protected_paths: list[str],
    existing_ids: dict[str, int] | None = None,
) -> list[str]:
    """校验一批任务的 ID、依赖、路径白名单与验证命令。existing_ids 为已存在任务 -> 所属里程碑序号。"""
    errors: list[str] = []
    existing_ids = existing_ids or {}
    tasks: dict[str, PlanTask] = {}
    for t in tasks_list:
        if not _ID_RE.match(t.task_id):
            errors.append(f"任务 ID 非法: {t.task_id!r}")
        if t.task_id in tasks or t.task_id in existing_ids:
            errors.append(f"任务 ID 重复: {t.task_id}")
        tasks[t.task_id] = t

    for t in tasks_list:
        tid = t.task_id
        if t.milestone_id not in ms_order:
            errors.append(f"{tid}: 引用了不存在的里程碑 {t.milestone_id}")
        cur_seq = ms_order.get(t.milestone_id, 0)
        for d in t.depends_on:
            if d == tid:
                errors.append(f"{tid}: 不能依赖自身")
            elif d in tasks:
                if ms_order.get(tasks[d].milestone_id, 0) > cur_seq:
                    errors.append(f"{tid}: 依赖了更晚里程碑中的任务 {d}")
            elif d in existing_ids:
                if existing_ids[d] > cur_seq:
                    errors.append(f"{tid}: 依赖了更晚里程碑中的任务 {d}")
            else:
                errors.append(f"{tid}: 依赖了不存在的任务 {d}")
        if not t.allowed_paths or any(not p for p in t.allowed_paths):
            errors.append(f"{tid}: allowed_paths 为空或包含非法路径（必须是仓库内相对路径）")
        if t.role not in ("contract", "bootstrap"):
            for p in t.allowed_paths:
                for prot in protected_paths:
                    if p and paths_overlap(p, prot):
                        errors.append(
                            f"{tid}: 非 contract/bootstrap 任务的 allowed_paths 不能包含受保护目录 {prot}（当前 {p}）"
                        )
        if not t.verify_cmds:
            errors.append(f"{tid}: 至少需要一条自动化验证命令 verify_cmds")
        if not t.acceptance_criteria:
            errors.append(f"{tid}: 至少需要一条验收标准 acceptance_criteria")

    graph = {tid: [d for d in t.depends_on if d in tasks] for tid, t in tasks.items()}
    try:
        topo_sort(graph)
    except CycleError as exc:
        errors.append(str(exc))
    return errors


def validate_plan(plan: ProjectPlan, protected_paths: list[str]) -> list[str]:
    errors: list[str] = []
    if not plan.milestones:
        errors.append("至少需要一个里程碑")
    if not plan.tasks:
        errors.append("至少需要一个任务")

    ms_order: dict[str, int] = {}
    for i, m in enumerate(plan.milestones):
        if not _ID_RE.match(m.milestone_id):
            errors.append(f"里程碑 ID 非法: {m.milestone_id!r}")
        if m.milestone_id in ms_order:
            errors.append(f"里程碑 ID 重复: {m.milestone_id}")
        ms_order[m.milestone_id] = i

    errors.extend(_validate_task_list(plan.tasks, ms_order, protected_paths))
    used_ms = {t.milestone_id for t in plan.tasks}
    for m in plan.milestones:
        if m.milestone_id not in used_ms:
            errors.append(f"里程碑 {m.milestone_id} 下没有任何任务")
    return errors


def validate_blueprint(bp: ProjectBlueprint, protected_paths: list[str]) -> list[str]:
    errors: list[str] = []
    if not bp.squads:
        errors.append("至少需要划分一个研发组（squads）")
    if not bp.milestones:
        errors.append("至少需要一个里程碑（milestones）")
    if not bp.initial_tasks:
        errors.append("initial_tasks 不能为空（至少需展开第一个里程碑的任务）")

    squad_ids: set[str] = set()
    for s in bp.squads:
        if not _ID_RE.match(s.squad_id):
            errors.append(f"研发组 ID 非法: {s.squad_id!r}")
        if s.squad_id in squad_ids:
            errors.append(f"研发组 ID 重复: {s.squad_id}")
        squad_ids.add(s.squad_id)
        if not s.owned_paths or any(not p for p in s.owned_paths):
            errors.append(f"研发组 {s.squad_id} 的 owned_paths 为空或包含非法相对路径")

    ms_order: dict[str, int] = {}
    for i, m in enumerate(bp.milestones):
        if not _ID_RE.match(m.milestone_id):
            errors.append(f"里程碑 ID 非法: {m.milestone_id!r}")
        if m.milestone_id in ms_order:
            errors.append(f"里程碑 ID 重复: {m.milestone_id}")
        ms_order[m.milestone_id] = i

    errors.extend(_validate_task_list(bp.initial_tasks, ms_order, protected_paths))
    if bp.milestones:
        first_ms = bp.milestones[0].milestone_id
        if not any(t.milestone_id == first_ms for t in bp.initial_tasks):
            errors.append(f"首个里程碑 {first_ms} 必须在 initial_tasks 中包含至少一个具体任务")
    return errors


def validate_wave(
    wave: MilestoneWavePlan,
    ms_order: dict[str, int],
    existing_ids: dict[str, int],
    protected_paths: list[str],
) -> list[str]:
    errors: list[str] = []
    if not wave.tasks:
        errors.append(f"里程碑 {wave.milestone_id} 滚动展开的任务列表不能为空")
    for t in wave.tasks:
        if t.milestone_id != wave.milestone_id:
            errors.append(f"{t.task_id}: milestone_id 必须为 {wave.milestone_id}")
    errors.extend(_validate_task_list(wave.tasks, ms_order, protected_paths, existing_ids=existing_ids))
    return errors


def parallel_overlap_warnings(tasks_list: Iterable[PlanTask] | ProjectPlan) -> list[str]:
    items = tasks_list.tasks if isinstance(tasks_list, ProjectPlan) else list(tasks_list)
    tasks = {t.task_id: t for t in items}
    ancestors: dict[str, set[str]] = {}

    def anc(tid: str) -> set[str]:
        if tid not in ancestors:
            ancestors[tid] = set()
            for d in tasks[tid].depends_on:
                if d in tasks:
                    ancestors[tid] |= {d} | anc(d)
        return ancestors[tid]

    warnings: list[str] = []
    ids = sorted(tasks)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            ta, tb = tasks[a], tasks[b]
            if ta.milestone_id != tb.milestone_id or a in anc(b) or b in anc(a):
                continue
            for pa in ta.allowed_paths:
                for pb in tb.allowed_paths:
                    if pa and pb and paths_overlap(pa, pb):
                        warnings.append(f"{a} 与 {b} 可并行但 allowed_paths 重叠（{pa} / {pb}）")
    return warnings
