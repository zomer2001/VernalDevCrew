"""多智能体架构评审圆桌（Architecture Council）：2 轮结构化收敛 + ADR 决策纪要 + 人类插话补充。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from autodev import gitops
from autodev.db import DocSlice, now_iso
from autodev.ingest import summarize
from autodev.models import CouncilConsensus, CouncilOpinion, ProjectStatus
from autodev.prompts import (
    build_council_consensus_prompt,
    build_council_opinion_prompt,
    build_plan_fix_prompt,
)

if TYPE_CHECKING:
    from autodev.engine import Engine

ROUND1_ROLES: tuple[tuple[str, str], ...] = (
    ("pm", "产品经理"),
    ("architect", "契约与异构架构师"),
    ("qa_lead", "QA 质量总监"),
)


async def run_council(
    engine: "Engine",
    *,
    extra_instructions: str = "",
    clear_history: bool = True,
) -> CouncilConsensus:
    from autodev.engine import OpsError, PlanningError, current_actor, current_slot, current_task

    slices = [s for s in engine.db.list_slices() if s.slice_id != "PRD-S01"]
    if not slices:
        raise OpsError("还没有资料切片，请先运行 ingest")

    if clear_history:
        engine.db.clear_council_messages()
    engine.db.ensure_slots(engine.settings.max_concurrency)

    human_notes = [
        m["headline"] for m in engine.db.list_council_messages() if m.get("kind") == "human"
    ]
    engine.db.add_event(
        "council.started",
        "多智能体架构评审圆桌启动（第 1 轮：PM / 契约架构师 / QA 总监独立评审 → 第 2 轮：研发总监裁决 ADR）",
        task_id="_council",
        actor="Orchestrator",
    )

    sem = asyncio.Semaphore(max(1, engine.settings.max_concurrency))
    free_slots = [f"slot-{i}" for i in range(1, engine.settings.max_concurrency + 1)]
    slot_lock = asyncio.Lock()

    async def _run_specialist(role: str, cn_name: str) -> dict[str, Any]:
        async with sem:
            async with slot_lock:
                slot_id = free_slots.pop(0) if free_slots else "command"
            ident = engine.agent_identity(role, "架构评审圆桌")
            engine.db.update_slot(
                slot_id,
                status="WORKING",
                squad="架构评审圆桌",
                role=role,
                harness=ident["harness"],
                model=ident["model"],
                task_id=f"_council_{role}",
                current_action=f"{cn_name}正在审阅架构文档并起草评审意见…",
                started_at=now_iso(),
            )
            t_tok = current_task.set(f"_council_{role}")
            s_tok = current_slot.set(slot_id)
            a_tok = current_actor.set(ident["label"])
            prompt = build_council_opinion_prompt(role, slices, extra_instructions, human_notes)
            log_dir = engine.ws.runs_dir / "_council" / f"r1-{role}"
            run_id = engine.db.start_run(f"_council_{role}", 1, f"COUNCIL:{role}", ident["harness"], agent_label=ident["label"])
            try:
                res = await engine.harness.run_structured_readonly(
                    prompt,
                    schema_path=engine.schemas["council_opinion"],
                    cwd=engine.ws.root,
                    log_dir=log_dir,
                    role=role,
                )
                opinion: CouncilOpinion | None = None
                err = res.error
                if res.ok and res.structured:
                    try:
                        opinion = CouncilOpinion.model_validate(res.structured)
                    except ValidationError as exc:
                        err = str(exc)
                engine.db.finish_run(
                    run_id,
                    session_id=res.session_id,
                    exit_code=res.exit_code,
                    input_tokens=res.usage.input_tokens,
                    cached_tokens=res.usage.cached_tokens,
                    output_tokens=res.usage.output_tokens,
                    reasoning_tokens=res.usage.reasoning_tokens,
                    verdict="PASS" if opinion else "ERROR",
                    summary_log=(opinion.headline if opinion else err)[:1000],
                    log_path=str(log_dir),
                )
                if opinion is None:
                    raise PlanningError([f"圆桌角色 {cn_name} 输出无效：{err}"])
                msg = engine.db.add_council_message(
                    round_no=1,
                    speaker_role=role,
                    speaker_name=cn_name,
                    harness=ident["harness"],
                    model=ident["model"],
                    kind="proposal",
                    headline=opinion.headline,
                    body=opinion.model_dump(),
                )
                engine.db.add_event(
                    "council.message",
                    f"[{cn_name}] {opinion.headline}",
                    task_id=f"_council_{role}",
                    actor=ident["label"],
                    payload={"msg_id": msg["msg_id"], "role": role, "round": 1},
                )
                return msg
            finally:
                engine.db.release_slot(slot_id)
                async with slot_lock:
                    if slot_id != "command" and slot_id not in free_slots:
                        free_slots.append(slot_id)
                        free_slots.sort()
                current_actor.reset(a_tok)
                current_slot.reset(s_tok)
                current_task.reset(t_tok)

    opinions = list(await asyncio.gather(*[_run_specialist(r, cn) for r, cn in ROUND1_ROLES]))
    return await synthesize_council(engine, opinions=opinions, extra_instructions=extra_instructions)


async def synthesize_council(
    engine: "Engine",
    *,
    opinions: list[dict[str, Any]] | None = None,
    extra_instructions: str = "",
    max_attempts: int = 2,
) -> CouncilConsensus:
    from autodev.engine import PlanningError, current_actor, current_slot, current_task

    slices = [s for s in engine.db.list_slices() if s.slice_id != "PRD-S01"]
    all_msgs = engine.db.list_council_messages()
    if opinions is None:
        opinions = [m for m in all_msgs if m.get("kind") == "proposal"]
    human_notes = [m["headline"] for m in all_msgs if m.get("kind") == "human"]
    next_round = max((int(m.get("round_no") or 1) for m in all_msgs), default=1) + 1

    ident = engine._occupy_command_slot("director", "_council_director", "研发总监正在综合各方争议并拍板 ADR 决策纪要…")
    t_tok = current_task.set("_council_director")
    s_tok = current_slot.set("command")
    a_tok = current_actor.set(ident["label"])
    base_prompt = build_council_consensus_prompt(slices, opinions, human_notes=human_notes, extra=extra_instructions)
    prompt = base_prompt
    errors: list[str] = []

    try:
        for attempt in range(1, max_attempts + 1):
            log_dir = engine.ws.runs_dir / "_council" / f"r{next_round}-director-{attempt}"
            run_id = engine.db.start_run("_council_director", attempt, "COUNCIL:CONSENSUS", ident["harness"], agent_label=ident["label"])
            res = await engine.harness.run_structured_readonly(
                prompt,
                schema_path=engine.schemas["council_consensus"],
                cwd=engine.ws.root,
                log_dir=log_dir,
                role="director",
            )
            consensus: CouncilConsensus | None = None
            if res.ok and res.structured:
                try:
                    consensus = CouncilConsensus.model_validate(res.structured)
                except ValidationError as exc:
                    errors = [str(exc)]
            else:
                errors = [res.error or "总监未返回合法圆桌共识 JSON"]
            engine.db.finish_run(
                run_id,
                session_id=res.session_id,
                exit_code=res.exit_code,
                input_tokens=res.usage.input_tokens,
                cached_tokens=res.usage.cached_tokens,
                output_tokens=res.usage.output_tokens,
                reasoning_tokens=res.usage.reasoning_tokens,
                verdict="PASS" if consensus else "INVALID",
                summary_log=(consensus.executive_summary if consensus else "; ".join(errors))[:1000],
                log_path=str(log_dir),
            )
            if consensus is not None:
                engine.db.add_council_message(
                    round_no=next_round,
                    speaker_role="director",
                    speaker_name="研发总监（最终裁决）",
                    harness=ident["harness"],
                    model=ident["model"],
                    kind="consensus",
                    headline=consensus.executive_summary,
                    body=consensus.model_dump(),
                )
                # 同步落盘 PRD_SPEC.md 与 prd_spec.json 供后续蓝图与所有研发组使用
                compat_prd = {
                    "product_vision": consensus.executive_summary,
                    "core_workflows": consensus.resolved_debates,
                    "edge_cases": consensus.open_risks,
                    "error_conventions": [consensus.contract_strategy],
                    "global_acceptance_redlines": [f"{a.adr_id} {a.title}: {a.decision}" for a in consensus.adrs],
                    "spec_markdown": consensus.spec_markdown,
                    "adrs": [a.model_dump() for a in consensus.adrs],
                }
                engine.ws.prd_json_path.write_text(json.dumps(compat_prd, ensure_ascii=False, indent=2), "utf-8")
                engine.ws.prd_md_path.parent.mkdir(parents=True, exist_ok=True)
                engine.ws.prd_md_path.write_text(consensus.spec_markdown.strip() + "\n", "utf-8")
                engine.record_documents(
                    ["docs/PRD_SPEC.md"],
                    "autodev(council): publish architecture council consensus & ADRs",
                    author_name=ident["author_name"],
                    author_email=ident["author_email"],
                )
                engine.db.upsert_slice(
                    DocSlice(
                        slice_id="PRD-S01",
                        source_file="docs/PRD_SPEC.md",
                        heading_path="PRD_SPEC / 架构评审圆桌共识与 ADR 决策纪要",
                        summary=summarize(consensus.spec_markdown),
                        content=consensus.spec_markdown[:12_000],
                        char_count=len(consensus.spec_markdown),
                    )
                )
                engine.db.meta_set("prd_vision", consensus.executive_summary)
                engine.set_status(ProjectStatus.SPECIFIED)
                engine.db.add_event(
                    "council.consensus",
                    f"圆桌拍板完成：形成 {len(consensus.adrs)} 项 ADR 决策，裁决 {len(consensus.resolved_debates)} 项分歧",
                    task_id="_council_director",
                    actor=ident["label"],
                )
                return consensus
            prompt = build_plan_fix_prompt(base_prompt, res.final_message or "", errors)
    finally:
        engine.db.release_slot("command")
        current_actor.reset(a_tok)
        current_slot.reset(s_tok)
        current_task.reset(t_tok)
    raise PlanningError(errors)


async def interject_council(engine: "Engine", actor: str, note: str) -> CouncilConsensus:
    from autodev.engine import OpsError

    note = note.strip()
    if not note:
        raise OpsError("插话内容不能为空")
    all_msgs = engine.db.list_council_messages()
    cur_round = max((int(m.get("round_no") or 1) for m in all_msgs), default=1)
    engine.db.add_council_message(
        round_no=cur_round,
        speaker_role="human",
        speaker_name=actor or "负责人",
        kind="human",
        headline=note,
        body={"note": note},
    )
    engine.db.add_event("council.human", f"负责人插话：{note}", task_id="_council", actor=actor or "owner")
    return await synthesize_council(engine)
