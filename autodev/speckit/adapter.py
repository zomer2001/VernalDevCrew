"""Spec Kit steps are ordinary LoopForge planning SOP executions, never a second runner."""
from __future__ import annotations
from contextvars import ContextVar
import asyncio
import json

from autodev.models import SpecStepResult, SpecTaskDraft, strict_json_schema
from autodev.process.runner import is_gate_passed
from autodev.prompts_style import COMMUNICATION_STYLE, validate_public_text
from .assets import digest_json, step_assets
from .compiler import compile_tasks, contract_hash
from .paths import source_path
from .service import (SpecConflictError, add_clarifications, assert_no_blocking_questions, assert_source_current,
                      input_fingerprint, latest, list_clarifications, publish_revision, quality_review)

PLANNING_BASELINE = ContextVar("speckit_planning_baseline", default=None)
STEP_NAMES = {"constitution":"建立项目约束","specify":"整理功能规格","clarify":"澄清需求边界",
              "checklist":"检查规格质量","plan":"制定技术方案","tasks":"拆解实施任务","analyze":"核对交付一致性"}


def _format_quality_context(qr: dict, checklist_text: str) -> str:
    lines = [
        f"## 已确认来源：checklist（第 {qr['checklist_rev_no']} 版 · 修订 #{qr['checklist_rev_id']}）",
        checklist_text.strip(),
        "",
        "### 当前结构化质量发现（只读原始记录，含稳定发现标识）",
    ]
    if not qr["findings"]:
        lines.append("- 当前质量检查无未结发现（findings=[]）。")
    else:
        lines.append("| 发现标识 | 严重级别 | 分类 | 位置 | 关联需求 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for f in qr["findings"]:
            reqs = ", ".join(f.get("requirement_ids") or []) or "-"
            msg = str(f.get("message") or "").replace("\n", " ").replace("|", "/")
            tgt = str(f.get("target") or "").replace("\n", " ").replace("|", "/")
            lines.append(f"| {f['id']} | {f['severity']} | {f['category']} | {tgt} | {reqs} | {msg} |")
    return "\n".join(lines)


async def run_step(engine, feature_id: str, step_name: str, preset_id: str = "vernal-core", *,
                   instructions: str = "", actor: str = "owner") -> dict:
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    if not isinstance(engine.harness, LoopForgeHarness):
        raise ValueError("功能规格步骤须使用 LoopForge 规划执行链路，请先绑定执行池")
    if step_name not in STEP_NAMES:
        raise ValueError("不支持的规格步骤")
    assets = step_assets(engine,step_name)
    if assets["versions"]["preset_id"] != preset_id:
        raise ValueError("请求预设与项目锁定预设不同")
    kind = "spec" if step_name in ("specify","clarify") else step_name
    source_path(feature_id,kind)
    if step_name != "constitution" and not latest(engine,"project","constitution"):
        raise ValueError("请先建立项目宪章")
    if step_name in ("plan","tasks","analyze"):
        assert_no_blocking_questions(engine,feature_id)
        if not is_gate_passed(engine.db,"srs_approval","project","project"):
            raise SpecConflictError("请先批准当前功能规格，再生成技术方案和任务")
    needed = {"constitution":[],"specify":["constitution"],"clarify":["constitution","spec"],
              "checklist":["constitution","spec"],"plan":["constitution","spec"],
              "tasks":["constitution","spec","plan"],"analyze":["constitution","spec","plan","tasks"]}[step_name]
    context = []
    for dep in needed:
        rev = latest(engine,feature_id,dep)
        if not rev:
            raise ValueError(f"请先完成 {dep} 步骤")
        assert_source_current(engine,rev)
        if rev["input_sha"] != input_fingerprint(engine,feature_id,dep):
            raise SpecConflictError(f"{dep} 基于旧来源，请先重新确认对应修订")
        context.append(f"## 已确认来源：{dep}\n{rev['content_text']}")
    qr = None
    checklist_rev = latest(engine, feature_id, "checklist")
    has_mission = engine.db.meta_get(f"mission:{feature_id}") is not None
    if step_name in ("plan", "tasks", "analyze") and (checklist_rev or has_mission):
        qr = quality_review(engine, feature_id)
        if qr["status"] == "MISSING":
            raise SpecConflictError("请先完成当前版本规格的质量检查，再进入后续方案或一致性步骤")
        if qr["status"] == "MISSING_METADATA":
            raise SpecConflictError("质量检查缺少结构化发现记录，不能视为无问题，请重新执行质量检查")
        if not qr["is_current"]:
            raise SpecConflictError("质量检查基于旧版本规格，请重新执行质量检查后再继续")
        if qr["counts"]["CRITICAL"] > 0:
            raise SpecConflictError("质量检查存在严重问题（CRITICAL），请先修订规格并重新检查")
        if step_name in ("plan", "analyze") and checklist_rev:
            context.append(_format_quality_context(qr, checklist_rev["content_text"]))
    if step_name == "analyze":
        compile_tasks(engine,feature_id,require_analysis=False)
    current = latest(engine,feature_id,kind)
    if current and kind not in needed:
        context.append("## 当前待修订正文\n" + current["content_text"])
    questions=list_clarifications(engine,feature_id)
    if questions:
        context.append("## 已记录问题，不重复提问；新问题使用未占用编号\n" + "\n".join(
            f"{q['question_key']}：{q['question']}；状态 {q['status']}；回答 {q['answer'] or '尚未回答'}" for q in questions))
    expected = current["rev_id"] if current else 0
    fingerprint = input_fingerprint(engine,feature_id,kind)
    role = "pm" if step_name in ("specify","clarify") else "qa_lead" if step_name in ("checklist","analyze") else "director"
    model = engine.settings.model_for(role)
    executor = engine.settings.harness_for(role)
    if executor in ("dsh","deepseek","deepseek-harness") and not model:
        raise ValueError("DeepSeek 执行器需要明确配置模型，不能依赖默认模型推测")
    signature = digest_json([feature_id,step_name,expected,fingerprint,assets["versions"],instructions])[:20]
    schema_path = engine.ws.state_dir / "schemas" / f"speckit_{step_name}_{feature_id}_{signature}.schema.json"
    schema_path.parent.mkdir(parents=True,exist_ok=True)
    schema = strict_json_schema(SpecStepResult)
    schema["properties"]["step"]["enum"] = [step_name]
    schema["properties"]["clarifications"]["maxItems"] = 3 if step_name == "clarify" else 0
    schema["properties"]["content"]["pattern"] = r"^\s*$" if step_name == "clarify" else r"\S"
    schema["x-vernal-step"] = step_name
    schema["x-vernal-task-schema"] = strict_json_schema(SpecTaskDraft)
    schema["x-vernal-artifact-path"] = source_path(feature_id,kind) if step_name != "clarify" else ""
    if step_name == "plan" and qr and qr["findings"]:
        schema["x-vernal-require-findings-ids"] = [f["id"] for f in qr["findings"]]
    schema_path.write_text(json.dumps(schema,ensure_ascii=False,indent=2),"utf-8")
    findings_guidance = ""
    if step_name == "plan" and qr and qr["findings"]:
        findings_guidance = (
            "\n质量发现处理要求（必须遵守）：当前质量检查共有 "
            f"{len(qr['findings'])} 条发现，方案正文必须包含「## 检查发现处理」章节，用 Markdown 表格逐条列出："
            "发现标识 | 处理位置 | 验证方式 | 是否需规格变更。每条必须覆盖原始发现标识（CHK-...）；"
            "纯工程实现细节可在方案中落实，涉及改变业务承诺或未确认范围的必须标注「是」并退回规格修订重新审批，不得替负责人自行承诺。"
        )
    elif step_name == "analyze" and qr and qr["findings"]:
        findings_guidance = (
            "\n质量发现核验要求（必须遵守）：必须逐条对照当前质量检查的每一条发现标识（CHK-...），"
            "核实规格、方案、接口、任务和验证命令是否真正落实，不能仅凭方案写有「已处理」就放行；"
            "若仍存在影响业务承诺的未决项或未映射的适用 NFR，必须写入 findings。"
        )
    # Read upstream protocol as reference, then constrain it to the platform-owned execution envelope.
    prompt = (f"本次任务：{STEP_NAMES[step_name]}。\n" + COMMUNICATION_STYLE + "\n"
              + "以下上游指令仅作方法参考；其中分支、脚本、扩展、直接提问和步骤切换由平台处理，不执行这些控制操作。\n"
              + assets["upstream_command"] + "\n上游文档结构参考：\n" + assets["template"]
              + "\n本项目格式与范围约束（优先遵循）：\n" + assets["instructions"]
              + findings_guidance
              + "\n" + "\n".join(context)
              + "\n用户输入资料：\n" + "\n".join(s.content for s in engine.db.list_slices() if s.slice_id != "PRD-S01")
              + "\n本次补充：\n" + instructions
              + f"\n仅产出当前步骤 {step_name} 的结果，不执行下一步骤。"
              + ("澄清步骤的 content 必须为空字符串，clarifications 至多三个问题。"
                 if step_name == "clarify" else
                 "content 放完整非空 Markdown 正文；clarifications 必须为空数组，本步骤不得发布澄清问题。未决事项可在正文中标记待确认，不替用户作答。")
              + "findings 无内容时用空数组。不得修改已确认输入。")
    baseline = {"spec_sha":(latest(engine,feature_id) or {}).get("content_sha",""),
                "checklist_sha": (qr or {}).get("checklist_sha", ""),
                "checklist_findings_sha": (qr or {}).get("findings_sha", ""),
                "contract_sha":contract_hash(engine), "compiled_task_hash":"", "asset_versions":assets["versions"]}
    token = PLANNING_BASELINE.set(baseline)
    log_dir = engine.ws.runs_dir / "speckit" / feature_id / step_name / signature
    run_id = engine.db.start_run(f"_speckit_{feature_id}_{step_name}",1,"SPEC_KIT",engine.harness.name)
    engine.db.add_event("speckit.step_started",STEP_NAMES[step_name],actor=actor)
    try:
        result = await engine.harness.run_structured_readonly(prompt,schema_path=schema_path,cwd=engine.ws.root,
                                                            log_dir=log_dir,role=role)
    except (Exception, asyncio.CancelledError) as exc:
        engine.db.finish_run(run_id,session_id="",exit_code=1,verdict="ERROR",summary_log=str(exc) or "执行已取消",log_path=str(log_dir))
        engine.db.add_event("speckit.step_failed",str(exc) or "执行已取消",actor=actor)
        raise
    finally:
        PLANNING_BASELINE.reset(token)
    try:
        if not result.ok or not result.structured:
            raise ValueError(result.error or "规格步骤未返回结构化产物")
        parsed = SpecStepResult.model_validate(result.structured)
        if parsed.step != step_name:
            raise ValueError("规格步骤与返回产物不匹配")
        for value in parsed.handoff_brief.model_dump().values():
            validate_public_text(value)
        with engine.db.transaction():
            now = latest(engine,feature_id,kind)
            if (now["rev_id"] if now else 0) != expected or input_fingerprint(engine,feature_id,kind) != fingerprint:
                raise SpecConflictError("生成期间规格已更新，旧结果已拒收")
            if step_name == "clarify":
                if parsed.content.strip() or len(parsed.clarifications)>3:
                    raise ValueError("澄清步骤只返回至多三个问题，不改写规格")
                add_clarifications(engine,feature_id,expected,parsed.clarifications,actor=actor)
                revision = latest(engine,feature_id)
            else:
                if parsed.clarifications:
                    raise ValueError("澄清问题须通过澄清步骤发布")
                findings_payload = [f.model_dump() for f in parsed.findings] if step_name in ("checklist", "analyze") else None
                revision = publish_revision(engine,feature_id,kind,parsed.content,expected_rev_id=expected,
                                            author=actor,summary=STEP_NAMES[step_name],input_sha=fingerprint,
                                            handoff=parsed.handoff_brief,findings=findings_payload)
                if step_name in ("checklist","analyze") and engine.db.meta_get(f"speckit.analysis:{revision['rev_id']}") is None:
                    engine.db.meta_set(f"speckit.analysis:{revision['rev_id']}",json.dumps(findings_payload,ensure_ascii=False))
            engine.db.meta_set(f"speckit.last_step:{feature_id}",parsed.model_dump_json())
        engine.db.finish_run(run_id,session_id=result.session_id,exit_code=0,input_tokens=result.usage.input_tokens,
                            cached_tokens=result.usage.cached_tokens,output_tokens=result.usage.output_tokens,
                            reasoning_tokens=result.usage.reasoning_tokens,verdict="PASS",summary_log=parsed.handoff_brief.delivered,
                            log_path=str(log_dir))
        engine.db.add_event("speckit.step_completed",parsed.handoff_brief.delivered,actor=actor)
        return {"revision":revision,"handoff_brief":parsed.handoff_brief.model_dump(),"findings":[f.model_dump() for f in parsed.findings]}
    except (ValueError,OSError) as exc:
        engine.db.finish_run(run_id,session_id=result.session_id,exit_code=1,verdict="INVALID",summary_log=str(exc),log_path=str(log_dir),
                            input_tokens=result.usage.input_tokens,cached_tokens=result.usage.cached_tokens,output_tokens=result.usage.output_tokens,
                            reasoning_tokens=result.usage.reasoning_tokens)
        engine.db.add_event("speckit.step_failed",str(exc),actor=actor)
        raise
