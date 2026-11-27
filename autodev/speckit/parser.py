"""Strict, deterministic parsing of the Vernal Chinese Spec Kit preset."""
from __future__ import annotations
import json
import re

from autodev.models import ProductSpec, RequirementItem, SpecTaskDraft, WorkPackageDraft, WorkPackageStep
from autodev.process.targets import normalize_document
from autodev.prompts_style import validate_public_text
from .paths import feature_spec_dir


def section(text: str, name: str) -> str:
    match = re.search(r"^## " + re.escape(name) + r"\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match[1].strip() if match else ""


def bullets(text: str) -> list[str]:
    return [line[2:].strip() for line in text.splitlines() if line.startswith("- ") and line[2:].strip()]


def parse_spec(text: str, feature_id: str) -> ProductSpec:
    feature_spec_dir(feature_id)
    validate_public_text(text)
    text = normalize_document(text)
    for heading in ("目标", "用户场景", "Non-Goals", "需求"):
        if len(re.findall(r"^## " + re.escape(heading) + r"\s*$",text,re.M))!=1 or not section(text, heading):
            raise ValueError(f"功能规格缺少非空章节：{heading}")
    for heading in ("用户场景", "Non-Goals"):
        if not bullets(section(text,heading)):
            raise ValueError(f"{heading} 必须包含明确条目")
    matches = list(re.finditer(r"^### ((?:REQ|NFR)-[0-9]{3,}) \[(P[012])\] ([^\n]+)\n(.*?)(?=^#{1,3} |\Z)", section(text,"需求"), re.M | re.S))
    if not matches:
        raise ValueError("功能规格必须包含 REQ/NFR 编号、优先级、要求及验收条件")
    all_headers = re.findall(r"^### .*", section(text, "需求"), re.M)
    if len(matches) != len(all_headers):
        raise ValueError("需求章节存在无法解析的条目，请使用固定的需求标题格式")
    reqs = []
    for m in matches:
        body = m[4].strip()
        parts = body.split("\n验收：\n", 1)
        if len(parts) != 2 or not parts[0].startswith("要求："):
            raise ValueError(f"{m[1]} 缺少要求或验收条件")
        statement = parts[0][3:].strip()
        acceptance = bullets(parts[1])
        if any(line.strip() and not line.startswith("- ") for line in parts[1].splitlines()):
            raise ValueError(f"{m[1]} 验收条件必须逐条列出")
        if not statement or not acceptance:
            raise ValueError(f"{m[1]} 要求与验收条件不能为空")
        reqs.append(RequirementItem(req_id=m[1], feature_id=feature_id, priority=m[2], title=m[3].strip(),
                                    statement=statement, acceptance=acceptance))
    return ProductSpec(product_vision=section(text, "目标"), core_workflows=bullets(section(text, "用户场景")),
                       edge_cases=bullets(section(text, "边界情况")), error_conventions=bullets(section(text,"错误约定")),
                       global_acceptance_redlines=bullets(section(text, "验收红线")), spec_markdown=text,
                       requirements=[r for r in reqs if r.req_id.startswith("REQ-")],
                       nfrs=[r for r in reqs if r.req_id.startswith("NFR-")])


def render_spec(spec: ProductSpec, *, non_goals: list[str]) -> str:
    out = ["# 功能规格", "", "## 目标", spec.product_vision, "", "## 用户场景"]
    out += [f"- {x}" for x in spec.core_workflows] or ["- 按需求条目完成操作并核对验收结果。"]
    out += ["", "## Non-Goals", *[f"- {x}" for x in non_goals], "", "## 需求"]
    for r in spec.requirements + spec.nfrs:
        out += [f"### {r.req_id} [{r.priority}] {r.title}", f"要求：{r.statement}", "验收：",
                *[f"- {x}" for x in r.acceptance], ""]
    if spec.edge_cases:
        out += ["## 边界情况", *[f"- {x}" for x in spec.edge_cases], ""]
    for heading,values in (("错误约定",spec.error_conventions),("验收红线",spec.global_acceptance_redlines)):
        if values:
            out += ["## " + heading,*[f"- {x}" for x in values],""]
    if spec.spec_markdown.strip():
        out += ["## 迁移原始说明",*["> " + line for line in spec.spec_markdown.splitlines()],""]
    return normalize_document("\n".join(out))


def parse_tasks(text: str) -> list[SpecTaskDraft]:
    """Checklists remain readable; each item carries one validated metadata block."""
    validate_public_text(text)
    pattern = r"^- \[([ xX])\] (T[0-9]{3,}) ([^\n]+)\n```vernal-task\n(.*?)\n```"
    found = list(re.finditer(pattern, text, re.M | re.S))
    if not found or len(found) != len(re.findall(r"^- \[[ xX]\]", text, re.M)):
        raise ValueError("每条任务必须包含 T 编号、标题和紧随的 vernal-task JSON 元数据")
    result = []
    for m in found:
        if m[1] != " ":
            raise ValueError("任务完成状态由独立验证回写，草案不能预先勾选")
        item = SpecTaskDraft.model_validate_json(m[4])
        if item.task_key != m[2] or item.title != m[3].strip():
            raise ValueError("任务标题或编号与元数据不一致")
        result.append(item)
    if len({x.task_key for x in result}) != len(result):
        raise ValueError("任务编号重复")
    return result


def render_tasks(tasks: list[SpecTaskDraft]) -> str:
    return "# 任务草案\n\n" + "\n\n".join(
        f"- [ ] {t.task_key} {t.title}\n```vernal-task\n{json.dumps(t.model_dump(), ensure_ascii=False, indent=2)}\n```"
        for t in tasks) + "\n"


def legacy_drafts_to_packages(drafts: list[SpecTaskDraft]) -> list[WorkPackageDraft]:
    """Read old metadata once; retain the union of all acceptance and verification clauses."""
    groups: dict[str, list[SpecTaskDraft]] = {}
    for draft in drafts:
        groups.setdefault(draft.package_key, []).append(draft)
    packages = []
    for key, items in groups.items():
        first = items[0]
        for field in ("milestone_id", "squad", "module", "role"):
            if any(getattr(item, field) != getattr(first, field) for item in items):
                raise ValueError(f"旧版工作包 {key} 的 {field} 不一致，无法归一化")
        unique = lambda field: list(dict.fromkeys(v for item in items for v in getattr(item, field)))
        packages.append(WorkPackageDraft(
            package_key=key, milestone_id=first.milestone_id, squad=first.squad, module=first.module,
            role=first.role, title=first.title, acceptance_criteria=unique("acceptance_criteria"),
            allowed_paths=unique("allowed_paths"), verify_cmds=unique("verify_cmds"),
            steps=[WorkPackageStep(**{field: getattr(item, field) for field in WorkPackageStep.model_fields})
                   for item in items]))
    return packages


def render_work_packages_markdown(packages: list[WorkPackageDraft]) -> str:
    from .validate_output import render_packages
    return render_packages([package.model_dump() for package in packages])
