"""规范化软件工程文档模板渲染器（WP-1：支持 default [arc42/MADR/EARS] 与 gbt8567 [GB/T 8567] 双体系）。"""

from __future__ import annotations

from typing import Any

from autodev.models import CouncilADR, CouncilConsensus, DetailedDesign, ProductSpec


def render_srs_markdown(spec: ProductSpec, *, profile: str = "default", project_name: str = "") -> str:
    title_prefix = "软件需求规格说明书（GB/T 8567）" if profile == "gbt8567" else "软件需求规格说明书 (SRS · EARS)"
    lines = [
        f"# {title_prefix}{f' — {project_name}' if project_name else ''}",
        "",
        "## 1. 引言与产品愿景" if profile == "gbt8567" else "## 1. 产品愿景与范围",
        "",
        spec.product_vision.strip(),
        "",
        "## 2. 核心业务流程",
        "",
    ]
    for idx, wf in enumerate(spec.core_workflows, 1):
        lines.append(f"{idx}. {wf}")

    lines += ["", "## 3. 功能性需求规定（EARS）" if profile == "gbt8567" else "## 3. 功能需求清单 (REQ)", ""]
    if spec.requirements:
        for req in spec.requirements:
            lines.append(f"### {req.req_id} [{req.priority}] {req.title}")
            lines.append(f"- **规范表述**：{req.statement}")
            if req.acceptance:
                lines.append("- **验收判据**：")
                for ac in req.acceptance:
                    lines.append(f"  - {ac}")
            lines.append("")
    elif spec.spec_markdown.strip():
        lines.append(spec.spec_markdown.strip())
        lines.append("")

    if spec.nfrs:
        lines += ["## 4. 非功能性需求与质量属性 (NFR)", ""]
        for nfr in spec.nfrs:
            lines.append(f"- **{nfr.req_id} [{nfr.priority}] {nfr.title}**：{nfr.statement}")
        lines.append("")

    lines += ["## 5. 异常边界与错误码约定", ""]
    for ec in spec.edge_cases:
        lines.append(f"- 边界场景：{ec}")
    for err in spec.error_conventions:
        lines.append(f"- 错误约定：{err}")

    lines += ["", "## 6. 全局验收红线", ""]
    for rl in spec.global_acceptance_redlines:
        lines.append(f"- {rl}")

    return "\n".join(lines).strip() + "\n"


def render_architecture_markdown(
    consensus: CouncilConsensus | None,
    blueprint: dict[str, Any] | None,
    *,
    profile: str = "default",
    project_name: str = "",
) -> str:
    header = "软件体系结构设计说明（GB/T 8567）" if profile == "gbt8567" else "系统架构设计说明书 (ADD · arc42)"
    lines = [f"# {header}{f' — {project_name}' if project_name else ''}", ""]

    lines += ["## 1. 架构概览与设计目标", ""]
    if consensus:
        lines.append(consensus.executive_summary.strip())
    elif blueprint and blueprint.get("summary"):
        lines.append(str(blueprint["summary"]))
    else:
        lines.append("待完成架构评审生成。")

    squads = (blueprint or {}).get("squads") or []
    lines += ["", "## 2. 模块/容器分解视图 (C4 Container)", ""]
    if squads:
        lines.append("```mermaid")
        lines.append("flowchart TD")
        for sq in squads:
            sid = str(sq.get("squad_id") or "mod").replace("-", "_")
            sname = str(sq.get("name") or sid).replace('"', "'")
            lines.append(f'  {sid}["{sname}"]')
        lines.append("```")
        lines.append("")
        lines.append("| 研发组 / 子系统 | 职责说明 | 物理目录边界 |")
        lines.append("|---|---|---|")
        for sq in squads:
            paths = ", ".join(sq.get("owned_paths") or [])
            lines.append(f"| {sq.get('name', sq.get('squad_id'))} | {sq.get('mission', '')} | `{paths}` |")
        lines.append("")

    lines += ["## 3. 跨模块契约与接口治理策略", ""]
    lines.append((consensus.contract_strategy if consensus else "公共契约集中维护于 `contracts/` 目录。").strip())
    lines.append("")

    if consensus and consensus.adrs:
        lines += ["## 4. 关键架构决策索引 (ADR)", ""]
        for adr in consensus.adrs:
            lines.append(f"- **{adr.adr_id} {adr.title}**：{adr.decision}（详见 `docs/adr/{adr.adr_id}.md`）")
        lines.append("")

    if consensus and consensus.open_risks:
        lines += ["## 5. 技术风险与缓解措施", ""]
        for r in consensus.open_risks:
            lines.append(f"- {r}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_adr_madr(adr: CouncilADR) -> str:
    return (
        f"# {adr.adr_id}：{adr.title}\n\n"
        f"- **状态**：已批准 (Accepted)\n"
        f"- **影响研发组**：{', '.join(adr.affected_squads) or '全局'}\n\n"
        f"## 决策结论 (Decision)\n\n{adr.decision.strip()}\n\n"
        f"## 决策依据与权衡 (Rationale)\n\n{adr.rationale.strip()}\n"
    )


def render_detailed_design_markdown(dd: DetailedDesign, *, profile: str = "default") -> str:
    heading = "软件详细设计说明（GB/T 8567）" if profile == "gbt8567" else "模块详细设计说明书 (Detailed Design)"
    lines = [
        f"# {heading} — {dd.squad_id} ({dd.milestone_id})",
        "",
        "## 1. 模块职责边界",
        "",
    ]
    for r in dd.responsibilities:
        lines.append(f"- {r}")

    lines += ["", "## 2. 对外接口详细设计", ""]
    for iface in dd.interfaces:
        lines.append(f"### 接口-{iface.name}")
        lines.append(f"- **函数/端点签名**：`{iface.signature}`")
        lines.append(f"- **输入参数**：{iface.inputs}")
        lines.append(f"- **输出结构**：{iface.outputs}")
        lines.append(f"- **异常与错误码**：{', '.join(iface.errors) or '无'}")
        lines.append(f"- **关联需求**：{', '.join(iface.req_ids) or '-'}")
        lines.append("")

    lines += ["## 3. 数据结构与存储模型", ""]
    for dm in dd.data_models:
        lines.append(f"- {dm}")

    lines += ["", "## 4. 关键交互时序图", ""]
    for seq in dd.sequences_mermaid:
        clean = seq.strip()
        if not clean.startswith("```"):
            lines.append("```mermaid")
            lines.append(clean)
            lines.append("```")
        else:
            lines.append(clean)
        lines.append("")

    lines += ["## 5. 异常处理与容错约定", ""]
    for eh in dd.error_handling:
        lines.append(f"- {eh}")

    lines += ["", "## 6. 配套测试用例设计 (TC)", "", "| 用例编号 | 类型 | 关联需求 | 测试场景说明 |", "|---|---|---|---|"]
    for tc in dd.test_cases:
        lines.append(f"| `{tc.tc_id}` | {tc.kind} | {', '.join(tc.req_ids)} | {tc.description} |")

    if dd.design_markdown.strip():
        lines += ["", "## 7. 补充设计说明", "", dd.design_markdown.strip()]

    return "\n".join(lines).strip() + "\n"
