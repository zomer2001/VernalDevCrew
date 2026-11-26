"""详细设计确定性合规校验器（WP-1：校验 REQ 引用存在性、P0 需求双向覆盖、接口 TC 覆盖与 Mermaid 语法）。"""

from __future__ import annotations

import re

from autodev.models import DetailedDesign, RequirementItem

_MERMAID_HEADERS = (
    "sequenceDiagram",
    "flowchart ",
    "flowchart\n",
    "graph ",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
)


def _validate_mermaid_snippet(snippet: str) -> str | None:
    body = snippet.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        if len(lines) >= 2:
            body = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    if not body:
        return "Mermaid 时序图内容为空"
    if not any(body.startswith(h) for h in _MERMAID_HEADERS):
        return f"Mermaid 图缺少合法声明头（需以 sequenceDiagram / flowchart 等开头）：{body.splitlines()[0][:40]}"
    # 检查未闭合方括号或引号
    for idx, ln in enumerate(body.splitlines(), 1):
        if ln.count('"') % 2 != 0:
            return f"Mermaid 第 {idx} 行存在未闭合的双引号：{ln[:60]}"
    return None


def validate_detailed_design(
    dd: DetailedDesign,
    known_requirements: list[RequirementItem] | None = None,
    *,
    enforce_all_p0: bool = True,
) -> list[str]:
    """返回校验错误列表；空列表表示通过。"""
    errors: list[str] = []
    reqs = list(known_requirements or [])
    known_ids = {r.identity for r in reqs}
    p0_ids = {r.identity for r in reqs if r.priority == "P0"}

    if not dd.interfaces:
        errors.append("详细设计至少需要定义一个模块接口 (interfaces)")
    if not dd.test_cases:
        errors.append("详细设计至少需要定义一条测试用例 (test_cases)")

    iface_covered_reqs: set[str] = set()
    tc_covered_reqs: set[str] = set()
    tc_texts = " ".join(tc.description for tc in dd.test_cases)

    for tc in dd.test_cases:
        if not re.match(r"^TC-[A-Za-z0-9_-]+$", tc.tc_id):
            errors.append(f"测试用例编号 `{tc.tc_id}` 格式不规范（应为 TC-xxx）")
        for rid in tc.req_ids:
            tc_covered_reqs.add(rid)
            if known_ids and rid not in known_ids:
                errors.append(f"测试用例 `{tc.tc_id}` 引用了不存在的需求编号 `{rid}`")

    for iface in dd.interfaces:
        for rid in iface.req_ids:
            iface_covered_reqs.add(rid)
            if known_ids and rid not in known_ids:
                errors.append(f"接口 `{iface.name}` 引用了不存在的需求编号 `{rid}`")
        # 每个接口至少有一个 TC（通过关联相同 REQ 或在 TC 描述中提及接口名）
        linked_by_req = bool(set(iface.req_ids) & tc_covered_reqs)
        linked_by_name = iface.name in tc_texts
        if not linked_by_req and not linked_by_name:
            errors.append(f"接口 `{iface.name}` 缺少对应的测试用例覆盖（请为其关联的 REQ 或接口名补充 TC）")

    if enforce_all_p0 and p0_ids:
        missing_iface = sorted(p0_ids - iface_covered_reqs)
        if missing_iface:
            errors.append(f"以下 P0 级核心需求未被任何详细设计接口覆盖：{', '.join(missing_iface)}")
        missing_tc = sorted(p0_ids - tc_covered_reqs)
        if missing_tc:
            errors.append(f"以下 P0 级核心需求缺少详细设计测试用例 (TC) 覆盖：{', '.join(missing_tc)}")

    for seq in dd.sequences_mermaid:
        m_err = _validate_mermaid_snippet(seq)
        if m_err:
            errors.append(m_err)

    return errors
