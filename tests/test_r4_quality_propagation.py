"""R4 regression tests: quality findings propagation, fingerprinting, NFR coverage, and explicit preset upgrade."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.models import SpecFinding, SpecTaskDraft
from autodev.server.app import create_app
from autodev.speckit import adapter, assets, compiler, missions, service
from autodev.speckit.parser import render_tasks
from test_speckit import BRIEF, PLAN, SPEC, approve, draft, eng, save


def _seven_findings() -> list[dict]:
    return [
        SpecFinding(severity="WARNING", message=f"提醒 W{i}：检查项 REQ-001", requirement_ids=["REQ-001"]).model_dump()
        for i in range(1, 8)
    ]


def test_r4_seven_findings_enter_plan_and_analyze_and_survive_last_step(eng, monkeypatch):
    save(eng, "constitution", "# 项目宪章\n交付须有验收依据。\n")
    save(eng, "spec", SPEC)
    checklist_rev = service.publish_revision(
        eng, "001-save", "checklist", "# 规格质量检查\n共 7 条提醒。\n",
        expected_rev_id=0, author="owner", summary="检查规格质量", findings=_seven_findings(),
    )
    approve(eng)

    qr = service.quality_review(eng, "001-save")
    assert qr["status"] == "CURRENT"
    assert qr["is_current"] is True
    assert qr["counts"] == {"CRITICAL": 0, "WARNING": 7, "INFO": 0, "total": 7}
    finding_ids = [f["id"] for f in qr["findings"]]
    assert finding_ids == [f"CHK-{checklist_rev['rev_id']}-{i:02d}" for i in range(1, 8)]

    plan_table = "\n## 检查发现处理\n| 发现标识 | 处理位置 | 验证方式 | 是否需规格变更 |\n| --- | --- | --- | --- |\n"
    for fid in finding_ids:
        plan_table += f"| {fid} | 接口与数据 | pytest | 否 |\n"
    full_plan = PLAN + plan_table

    seen_prompts: dict[str, str] = {}
    outputs = {
        "plan": full_plan,
        "tasks": render_tasks([draft()]),
        "analyze": "# 一致性检查\n已逐条核验 7 条发现。\n",
    }

    async def fake_readonly(prompt, *, schema_path, **kwargs):
        schema = json.loads(schema_path.read_text("utf-8"))
        step = schema["x-vernal-step"]
        seen_prompts[step] = prompt
        if step == "plan":
            assert schema["x-vernal-require-findings-ids"] == finding_ids
        return SimpleNamespace(
            ok=True,
            structured=dict(step=step, content=outputs[step], clarifications=[], findings=[], handoff_brief=BRIEF),
            session_id=f"s-{step}",
            usage=SimpleNamespace(input_tokens=10, cached_tokens=0, output_tokens=5, reasoning_tokens=0),
        )

    monkeypatch.setattr(eng.harness, "run_structured_readonly", fake_readonly)

    asyncio.run(adapter.run_step(eng, "001-save", "plan"))
    asyncio.run(adapter.run_step(eng, "001-save", "tasks"))
    asyncio.run(adapter.run_step(eng, "001-save", "analyze"))

    # All 7 findings and stable IDs entered plan and analyze prompts
    for fid in finding_ids:
        assert fid in seen_prompts["plan"]
        assert fid in seen_prompts["analyze"]
    assert "提醒 W7" in seen_prompts["plan"]
    assert "提醒 W7" in seen_prompts["analyze"]

    # last_step is now "analyze", yet quality_review still exposes all 7 immutable findings!
    last_step = json.loads(eng.db.meta_get("speckit.last_step:001-save"))
    assert last_step["step"] == "analyze"
    qr_after = service.quality_review(eng, "001-save")
    assert qr_after["counts"]["WARNING"] == 7
    assert [f["id"] for f in qr_after["findings"]] == finding_ids

    # API exposes quality_review cleanly
    with TestClient(create_app(eng, proxy_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))) as client:
        resp = client.get("/vernal-api/specifications")
        assert resp.status_code == 200
        feat = resp.json()["features"][0]
        assert feat["quality_review"]["counts"]["WARNING"] == 7
        assert feat["quality_review"]["is_current"] is True

    # Compiled package binds checklist SHA + findings SHA
    pkgs = compiler.compile_tasks(eng, "001-save")
    assert len(pkgs) == 1
    assert pkgs[0].checklist_rev_id == checklist_rev["rev_id"]
    assert pkgs[0].checklist_sha == checklist_rev["content_sha"]
    assert pkgs[0].checklist_findings_sha == qr["findings_sha"]


def test_r4_checklist_invalidation_missing_metadata_and_idempotence(eng, monkeypatch):
    save(eng, "constitution", "# 项目宪章\n交付须有验收依据。\n")
    save(eng, "spec", SPEC)
    f1 = [SpecFinding(severity="WARNING", message="提醒一", requirement_ids=["REQ-001"]).model_dump()]
    chk1 = service.publish_revision(
        eng, "001-save", "checklist", "# 规格质量检查\n第一版。\n",
        expected_rev_id=0, author="owner", summary="检查", findings=f1,
    )
    approve(eng)

    # Identical checklist + identical findings does NOT create new revision or invalidate downstream
    same = service.publish_revision(
        eng, "001-save", "checklist", "# 规格质量检查\n第一版。\n",
        expected_rev_id=chk1["rev_id"], author="owner", summary="重复检查", findings=f1,
    )
    assert same["rev_id"] == chk1["rev_id"]

    # Plan without handling table is rejected
    with pytest.raises(ValueError, match="检查发现处理"):
        service.publish_revision(eng, "001-save", "plan", PLAN, expected_rev_id=0, author="owner")

    # Plan with handling table succeeds
    plan_ok = PLAN + f"\n## 检查发现处理\n| 发现标识 | 处理位置 | 验证方式 | 是否需规格变更 |\n| --- | --- | --- | --- |\n| CHK-{chk1['rev_id']}-01 | 接口 | pytest | 否 |\n"
    plan_rev = service.publish_revision(eng, "001-save", "plan", plan_ok, expected_rev_id=0, author="owner")
    tasks_rev = service.publish_revision(eng, "001-save", "tasks", render_tasks([draft()]), expected_rev_id=0, author="owner")
    analyze_rev = service.publish_revision(eng, "001-save", "analyze", "# 一致性\n通过。\n", expected_rev_id=0, author="owner", findings=[])
    assert plan_rev["status"] != "STALE"

    # Publishing a new checklist with different findings invalidates plan, tasks, analyze
    f2 = [SpecFinding(severity="WARNING", message="提醒二", requirement_ids=["REQ-001"]).model_dump()]
    chk2 = service.publish_revision(
        eng, "001-save", "checklist", "# 规格质量检查\n第二版。\n",
        expected_rev_id=chk1["rev_id"], author="owner", summary="复查", findings=f2,
    )
    assert chk2["rev_id"] != chk1["rev_id"]
    assert service.latest(eng, "001-save", "plan")["status"] == "STALE"
    assert service.latest(eng, "001-save", "tasks")["status"] == "STALE"
    assert service.latest(eng, "001-save", "analyze")["status"] == "STALE"
    with pytest.raises(service.SpecConflictError, match="旧规格"):
        compiler.compile_tasks(eng, "001-save")

    # Quality review separates current from history
    qr = service.quality_review(eng, "001-save")
    assert qr["checklist_rev_no"] == 2
    assert len(qr["history"]) == 1
    assert qr["history"][0]["rev_id"] == chk1["rev_id"]

    # Missing findings metadata is NOT treated as empty/passing
    eng.db.meta_set(f"speckit.analysis:{chk2['rev_id']}", None)
    qr_missing = service.quality_review(eng, "001-save")
    assert qr_missing["status"] == "MISSING_METADATA"
    assert qr_missing["is_current"] is False
    assert qr_missing["metadata_present"] is False
    with pytest.raises(service.SpecConflictError, match="缺少结构化发现"):
        asyncio.run(adapter.run_step(eng, "001-save", "plan"))


def test_r4_applicable_nfr_enters_compiler_p0_coverage_and_explicit_asset_upgrade(eng):
    save(eng, "constitution", "# 项目宪章\n兼容基线由适用功能的规格显式登记 NFR。\n")
    spec_with_nfr = SPEC.replace(
        "## 边界情况",
        "### NFR-001 [P0] 既有回答格式兼容基线\n"
        "要求：首版以 schema_version=1 作为兼容基线，读取既有合法记录时不丢失内容。\n"
        "验收：\n- 读取 schema_version=1 的已保存记录字段完整。\n\n"
        "## 边界情况",
    )
    save(eng, "spec", spec_with_nfr)
    service.publish_revision(eng, "001-save", "checklist", "# 检查\n通过。\n", expected_rev_id=0, author="owner", findings=[])
    approve(eng)
    service.publish_revision(eng, "001-save", "plan", PLAN, expected_rev_id=0, author="owner")

    # Task missing NFR-001 fails compiler P0 coverage check!
    service.publish_revision(eng, "001-save", "tasks", render_tasks([draft()]), expected_rev_id=0, author="owner")
    service.publish_revision(eng, "001-save", "analyze", "# 一致性\n待补。\n", expected_rev_id=0, author="owner", findings=[])
    with pytest.raises(ValueError, match="NFR-001"):
        compiler.compile_tasks(eng, "001-save")

    # Task covering both REQ-001 and NFR-001 compiles cleanly
    d2 = SpecTaskDraft(**{**draft().model_dump(), "requirement_ids": ["REQ-001", "NFR-001"]})
    t_rev = service.latest(eng, "001-save", "tasks")
    service.publish_revision(eng, "001-save", "tasks", render_tasks([d2]), expected_rev_id=t_rev["rev_id"], author="owner")
    a_rev = service.latest(eng, "001-save", "analyze")
    service.publish_revision(eng, "001-save", "analyze", "# 一致性\n已覆盖 REQ-001 与 NFR-001。\n", expected_rev_id=a_rev["rev_id"], author="owner", findings=[])
    pkgs = compiler.compile_tasks(eng, "001-save")
    assert set(pkgs[0].task.requirement_ids) == {"001-save:REQ-001", "001-save:NFR-001"}

    # Verify manifest bundle & explicit preset upgrade (immutable old lock untouched)
    manifest = assets.verify_bundle(assets.ASSET_ROOT)
    assert manifest["presets"]["vernal-core"]["version"] == "1.1.0"
    assert manifest["presets"]["vernal-lite"]["version"] == "1.1.0"
    old_lock, old_dir = assets.load_assets(eng)
    assert old_lock["preset_id"] == "vernal-core"
    assert old_lock["preset_version"] == "1.1.0"

    upgraded = assets.upgrade_assets(eng, "vernal-lite", actor="owner", reason="局部低风险特性显式切换轻量预设")
    assert upgraded["changed"] is True
    assert upgraded["current"]["preset_id"] == "vernal-lite"
    assert old_dir.is_dir()  # prior immutable directory untouched
    hist = json.loads(eng.db.meta_get("speckit.assets.history"))
    assert len(hist) == 1 and hist[0]["from"]["preset_id"] == "vernal-core" and hist[0]["to"]["preset_id"] == "vernal-lite"
