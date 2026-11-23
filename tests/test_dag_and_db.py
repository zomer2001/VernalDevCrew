"""测试 DAG 拓扑排序、环路检测、规划校验与 SQLite 台账基础流转。"""

from __future__ import annotations

from pathlib import Path

import pytest

from autodev.dag import CycleError, normalize_plan, parallel_overlap_warnings, topo_sort, validate_plan, waves
from autodev.db import Database
from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan, TaskStatus, strict_json_schema


def _sample_plan() -> ProjectPlan:
    return ProjectPlan(
        summary="双模块微型键值存储",
        tech_stack="Python 3 标准库 + unittest",
        agents_md="# AGENTS\n契约目录只读。",
        modules=[
            PlanModule(name="contracts", path="contracts/", description="共享接口契约"),
            PlanModule(name="storage", path="storage/", description="存储引擎"),
            PlanModule(name="api", path="api/", description="HTTP/CLI 接口"),
        ],
        milestones=[
            PlanMilestone(milestone_id="M1-contracts", title="契约", goal="定义接口", integration_cmd=""),
            PlanMilestone(milestone_id="M2-impl", title="实现", goal="实现后端与接口", integration_cmd="python3 -m unittest"),
        ],
        tasks=[
            PlanTask(
                task_id="T-001",
                milestone_id="M1-contracts",
                module="contracts",
                role="contract",
                title="定义存储契约",
                description="编写 contracts/store.py",
                acceptance_criteria=["定义 Store 接口与 KeyNotFoundError"],
                depends_on=[],
                doc_slice_ids=["DOC01-S01"],
                allowed_paths=["contracts/", "tests/test_contracts.py"],
                verify_cmds=["python3 -m unittest tests/test_contracts.py"],
                priority=10,
            ),
            PlanTask(
                task_id="T-002",
                milestone_id="M2-impl",
                module="storage",
                role="coder",
                title="实现内存存储",
                description="实现 storage/memory.py",
                acceptance_criteria=["支持 get/set/delete"],
                depends_on=["T-001"],
                doc_slice_ids=["DOC01-S01"],
                allowed_paths=["storage/", "tests/test_storage.py"],
                verify_cmds=["python3 -m unittest tests/test_storage.py"],
                priority=20,
            ),
            PlanTask(
                task_id="T-003",
                milestone_id="M2-impl",
                module="api",
                role="coder",
                title="实现命令接口",
                description="实现 api/handler.py",
                acceptance_criteria=["转发调用到 Store"],
                depends_on=["T-001", "T-002"],
                doc_slice_ids=["DOC01-S02"],
                allowed_paths=["api/", "tests/test_api.py"],
                verify_cmds=["python3 -m unittest tests/test_api.py"],
                priority=30,
            ),
        ],
    )


def test_topo_sort_and_waves() -> None:
    graph = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    assert topo_sort(graph) == ["A", "B", "C", "D"]
    assert waves(graph) == [["A"], ["B", "C"], ["D"]]


def test_cycle_detection() -> None:
    graph = {"T1": ["T3"], "T2": ["T1"], "T3": ["T2"]}
    with pytest.raises(CycleError) as exc:
        topo_sort(graph)
    assert len(exc.value.cycle) >= 3
    assert exc.value.cycle[0] == exc.value.cycle[-1]


def test_validate_plan_passes_and_rejects_violations() -> None:
    plan = normalize_plan(_sample_plan(), {"DOC01-S01", "DOC01-S02"})
    assert validate_plan(plan, ["contracts/"]) == []
    assert parallel_overlap_warnings(plan) == []

    # 非 contract 任务企图修改 contracts/
    bad_task = plan.tasks[1].model_copy(update={"allowed_paths": ["contracts/", "storage/"]})
    bad_plan = plan.model_copy(update={"tasks": [plan.tasks[0], bad_task, plan.tasks[2]]})
    errors = validate_plan(bad_plan, ["contracts/"])
    assert any("受保护目录" in e for e in errors)

    # 缺少 verify_cmds
    no_cmd = plan.tasks[1].model_copy(update={"verify_cmds": []})
    errors2 = validate_plan(plan.model_copy(update={"tasks": [plan.tasks[0], no_cmd, plan.tasks[2]]}), ["contracts/"])
    assert any("verify_cmds" in e for e in errors2)


def test_strict_json_schema_has_no_refs_and_forbids_extra() -> None:
    schema = strict_json_schema(ProjectPlan)
    assert "$defs" not in schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_db_save_plan_and_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    try:
        plan = _sample_plan()
        db.save_plan(plan, max_retries=3)
        tasks = {t.task_id: t for t in db.list_tasks()}
        assert tasks["T-001"].status == TaskStatus.READY
        assert tasks["T-002"].status == TaskStatus.BLOCKED
        assert tasks["T-003"].status == TaskStatus.BLOCKED

        seen: list[dict] = []
        db.listeners.append(seen.append)
        ev = db.add_event("test.ping", "hello", task_id="T-001")
        assert len(seen) == 1
        assert db.events_after(0)[0]["event_id"] == ev["event_id"]
    finally:
        db.close()
