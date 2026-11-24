"""为规划 / 编码 / 返工 / 审查组装精准上下文。"""

from __future__ import annotations

import json
from pathlib import Path

from autodev.config import Settings
from autodev.db import DocSlice, Task

PLANNER_FULLTEXT_BUDGET = 60_000
CONTRACT_BUDGET = 30_000
SLICE_BUDGET = 18_000
FILE_DELIVERY_STAGES = (
    "文件交付模式下，编码和独立审查在本次任务工作区核对清单、代码及测试。"
    "任务审查通过后，平台集成并验证交付文件。对于原地接管的本机项目，平台在里程碑合并时检查原目录冲突并写回。"
    "将用户要求的原目录验收列在此次写回之后。"
    "任务卡的通过条件应能在当前工位独立完成。记录实际使用的执行器和测试命令，"
    "仅在用户明确指定时把某个命名沙箱列为必需环境。"
)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) or "- （无）"


def _slices_block(slices: list[DocSlice], budget: int) -> str:
    parts, used = [], 0
    for s in slices:
        body = s.content
        if used + len(body) > budget:
            body = body[: max(0, budget - used)] + "\n…（已截断，可在 inputs/ 中阅读原文）"
        parts.append(f"### {s.slice_id} · {s.heading_path}\n\n{body}")
        used += len(body)
        if used >= budget:
            break
    return "\n\n".join(parts) or "（无关联切片）"


# ---------------- 规划 ----------------

def build_planner_prompt(
    slices: list[DocSlice], toolchains: dict[str, str], settings: Settings, extra: str = ""
) -> str:
    catalog = "\n".join(
        f"| {s.slice_id} | {s.source_file} | {s.heading_path} | {s.char_count} | {s.summary} |" for s in slices
    )
    total = sum(s.char_count for s in slices)
    if total <= PLANNER_FULLTEXT_BUDGET:
        fulltext = "## 资料全文（按切片）\n\n" + _slices_block(slices, PLANNER_FULLTEXT_BUDGET)
    else:
        fulltext = (
            f"## 资料全文\n\n资料总长 {total} 字符，超出单次投喂预算。请按需使用只读命令（如 `cat`、`sed -n`）"
            "阅读当前目录 inputs/ 下的原始文件，重点阅读与模块边界、接口契约相关的章节。"
        )
    tools = _bullets([f"{k}: {v}" for k, v in toolchains.items()])
    protected = ", ".join(settings.protected_paths)
    extra_block = f"\n## 负责人补充要求\n\n{extra}\n" if extra.strip() else ""
    return f"""你是资深软件架构师兼研发经理。你的任务：把输入资料拆解为一份可由多个编码智能体并行完成的研发计划。
你处于只读模式，不要修改任何文件。最终只输出符合给定 JSON Schema 的 JSON。

## 资料切片索引

| slice_id | 来源文件 | 标题路径 | 字数 | 摘要 |
|---|---|---|---|---|
{catalog}

{fulltext}

## 本机可用工具链

{tools}

重要约束：编码智能体运行在**无网络**沙箱中，不能安装任何新依赖（pip/npm/go get 都不可用）。
因此技术栈只能使用上面的工具链及其标准库；若资料没有指定技术栈，默认使用 Python 3 标准库 + unittest。
{extra_block}
## 规划规则

1. **模块与目录**：识别目标系统的模块划分，在 modules 中给出每个模块的名称、目录（如 `backend/`）与职责。
   跨模块共享的数据结构、接口定义、错误码必须放在受保护的契约目录（{protected}）下。
2. **里程碑**：按执行顺序列出。第一个里程碑必须是“契约与基础骨架”，其中包含 role=contract 的任务，
   负责在 {protected} 中写出可被导入/编译的接口定义及其最小测试。milestone_id 形如 `M1-contracts`、`M2-core`。
3. **任务粒度与精简原则**：
   - 按系统真实体量自适应控制任务总数：小型应用/单页小游戏/轻量命令行工具总计拆 **3～6 张高内聚功能卡**即可（每个模块开发任务直接自带对应单元测试，不要把测试拆成多张碎卡）；中大型多模块系统再按模块展开。
   - **严禁生成纯文书核账单或沙箱无法执行的物理真机测试单**：平台底层代码已自动校验路径白名单与 SHA-256 摘要，且每单自带独立审查，禁止排“核对哈希/写验收回执”的任务；禁止把需要 iOS/Android 真机或桌面 GUI 手工点击的事项拆成任务。
   - **标题与描述说人话**：`title`（10～18 字）必须用人类一眼能懂的产品功能语言（如“实现棋盘合并与计分逻辑”），不要写生僻的内部测试黑话；`description` 先用 1～2 句大白话讲清交付效果，再列出契约函数名与技术细节。
4. **依赖**：depends_on 只能引用同一里程碑或更早里程碑中的任务，不得成环。只有真正需要读取上游产物时才加依赖，
   以便尽可能并行。
5. **allowed_paths**：任务允许新增或修改的相对路径前缀（目录以 `/` 结尾，或具体文件路径），必须包含该任务的测试文件。
   非 contract 任务绝对不能包含 {protected}。同一里程碑中可并行的任务，allowed_paths 不要重叠（例如每个任务用自己的测试文件）。
   需要被多个任务修改的公共文件（如包的 `__init__.py`），应由最早的任务创建，后续任务尽量不改。
6. **verify_cmds**：在仓库根目录执行的 shell 命令，退出码 0 表示通过。**必须与本卡 `allowed_paths` 严格自洽**：
   - 若 `verify_cmds` 运行 `unittest discover` / `pytest` / `node --test`，则本卡 `allowed_paths` 必须同时包含对应的测试文件（严禁在白名单不含测试文件的纯实现卡上跑 `unittest discover`，Python 3.12 找不到用例会返回退出码 5 导致死锁！）；
   - 若本卡确实不含测试文件，仅使用针对白名单文件的语法/导入自检命令（如 `python3 -c "import sorting"` 或 `node --check ...`）。
7. **integration_cmd**：该里程碑全部任务合并后，在仓库根目录运行的跨模块回归命令（通常是运行全部测试）；没有可留空字符串。
   {FILE_DELIVERY_STAGES}
8. **acceptance_criteria**：可被审查员逐条核对的具体验收条件。
9. **doc_slice_ids**：每个任务关联 1~3 个最相关的切片 ID（必须来自上表）。
10. **priority**：数字越小越优先，用于同批次排序。
11. **agents_md**：将写入目标仓库根目录 AGENTS.md 的中文内容，包括：项目简介、技术栈、目录结构、构建与测试命令、
    编码规范、“契约目录只读、不要执行 git commit”等纪律。
12. **summary / tech_stack**：一段话概括系统与拆解思路；技术栈简述。
"""


def build_plan_fix_prompt(base_prompt: str, previous: str, errors: list[str]) -> str:
    return (
        base_prompt
        + "\n\n## 上一版规划未通过校验\n\n上一版输出：\n```json\n"
        + previous[:20_000]
        + "\n```\n\n校验错误：\n"
        + _bullets(errors)
        + "\n\n请修正全部错误后重新输出完整规划 JSON。"
    )


# ---------------- 编码 ----------------

def read_contracts(worktree: Path, protected_paths: list[str], budget: int = CONTRACT_BUDGET) -> str:
    parts, used = [], 0
    for prot in protected_paths:
        base = worktree / prot.rstrip("/")
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                text = f.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = f.relative_to(worktree).as_posix()
            if used + len(text) > budget:
                parts.append(f"#### {rel}\n（内容过长未内联，请自行阅读）")
                continue
            parts.append(f"#### {rel}\n```\n{text}\n```")
            used += len(text)
    return "\n\n".join(parts) or "（契约目录当前为空）"


def _task_header(task: Task) -> str:
    return f"""## 任务 {task.task_id}：{task.title}

- 所属研发组（Squad）：{task.squad or task.module}
- 模块：{task.module}
- 岗位角色：{task.role}

{task.description}

## 验收标准

{_bullets(task.acceptance_criteria)}

## 允许修改的路径（白名单）

{_bullets(task.allowed_paths)}

白名单之外的任何改动都会被调度器自动回滚，并判定本轮失败。

## 验证命令（你结束后调度器会在仓库根目录独立执行，全部退出码为 0 才算通过）

{_bullets([f"`{c}`" for c in task.verify_cmds])}
"""


def _decisions_block(decisions: list[dict] | None) -> str:
    active = [d for d in (decisions or []) if str(d.get("status") or "ACTIVE") == "ACTIVE"]
    if not active:
        return ""
    lines = ["## 项目生效硬规矩（评审拍板红线 · 违背任意一条审查直接打回）", ""]
    for d in active:
        scope = f"（适用：{', '.join(d.get('affected_paths') or [])}）" if d.get("affected_paths") else ""
        lines.append(f"- **[{d.get('decision_id', 'D')}] {d.get('title', '')}**{scope}：{d.get('rule_text', '')}")
    return "\n".join(lines) + "\n\n"


def build_coder_prompt(
    task: Task, slices: list[DocSlice], contracts: str, settings: Settings, decisions: list[dict] | None = None
) -> str:
    protected = ", ".join(settings.protected_paths)
    role_guidance = {
        "bootstrap": (
            "你是【基础设施组 · 基线工程师 (bootstrap)】。负责初始化目录结构、公共配置或依赖环境。"
            "若需要创建 Python 虚拟环境供后续断网 worktree 共享，可安装在 `.autodev/shared_venv` 并在仓库内留好配置。"
        ),
        "contract": (
            f"你是【契约架构组 · 契约工程师 (contract)】，负责编写或升版 {protected} 下的公共接口契约，"
            "务必保持接口清晰、类型完备、零业务耦合，可被各业务研发组直接导入。"
        ),
        "integrator": (
            f"你是【网关装配组 · 装配工程师 (integrator)】。各业务组已在各自模块目录完成实现，"
            f"请把各模块挂载到统一入口/路由/DI 容器并跑通跨模块联调测试。{protected} 只读，不得修改。"
        ),
        "tester": (
            f"你是【质量保障组 · 系统测试工程师 (tester)】。请对照产品规格与上游契约 {protected}，"
            "独立编写覆盖正常链路、异常码、边界条件与跨模块协作的自动化测试；不要修改契约文件。"
        ),
    }.get(
        task.role,
        f"你是【{task.squad or task.module} · 模块开发工程师 (coder)】。严格在本组授权目录内开发；{protected} 是冻结契约，只读不得修改。",
    )
    return f"""你是多智能体研发团队中的专职工程工位，在独立工作目录完成原子任务。交付方式为 {settings.collaboration_mode}，当前目录包含本任务的输入版本。

{_decisions_block(decisions)}{_task_header(task)}
## 岗位职责说明

{role_guidance}

## 上游契约（{protected}）

{contracts}

## 相关资料切片（含 PM 规格书）

{_slices_block(slices, SLICE_BUDGET)}

## 工作纪律

1. 先阅读 AGENTS.md、`docs/PRD_SPEC.md`（若存在）和相关已有代码，再动手。
2. 测试驱动：先写覆盖正常路径、边界和异常的测试，再写实现；在本地运行验证命令，直到全部通过。
3. 只修改白名单内的文件。
4. 不要执行 git commit / git push / git checkout 切换分支——提交由调度器以你的工位身份自动署名完成。
5. 不要留 TODO、空实现、`pass` 占位或为迎合测试的硬编码。
6. 如果契约缺少完成任务所必需的字段或接口，不要绕过或私自修改契约：停止并输出 status=NEED_CONTRACT_UPDATE，
   在 notes 中写清楚需要的具体变更（研发总监会自动仲裁并派发契约修订任务）；若规格本身自相矛盾，输出 status=BLOCKED_BY_SPEC。
7. 完成后，最终回复只输出符合 JSON Schema 的汇报（status / summary / files_changed / tests_added / notes）。
"""


def build_fix_prompt(task: Task, feedback: str, attempt_no: int, steer_note: str | None) -> str:
    steer = f"\n## 人工指导意见\n\n{steer_note}\n" if steer_note else ""
    return f"""上一轮工作没有通过调度器的独立检查。这是第 {attempt_no} 轮（最多 {task.max_retries} 轮，超过将熔断交人工处理）。

## 失败原因

{feedback or "（无）"}
{steer}
请在当前工作区继续修复，直到验证命令全部通过：
{_bullets([f"`{c}`" for c in task.verify_cmds])}

之前的全部规则仍然有效（只改白名单 {", ".join(task.allowed_paths)}；不要 git commit；不修改契约）。
完成后最终回复只输出 JSON 汇报。
"""


def build_fallback_context(diffstat: str, feedback: str, steer_note: str | None) -> str:
    steer = f"\n人工指导意见：\n{steer_note}\n" if steer_note else ""
    return f"""

## 续作说明

这个任务之前已经有人做过一部分，当前工作区已包含之前提交的改动（相对基线的 diff 统计）：
```
{diffstat or "（无）"}
```
上一轮失败原因：
{feedback or "（无）"}
{steer}
请在现有基础上继续完成，不要从头重写。
"""


# ---------------- 审查 ----------------

def build_review_prompt(
    task: Task, slices: list[DocSlice], verify_summary: str, diff: str, decisions: list[dict] | None = None
) -> str:
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(task.acceptance_criteria, 1))
    return f"""你是独立代码审查员，处于只读模式。请判断任务分支上的改动是否**真正**满足验收标准。当前目录是该任务的工作树，可阅读任何文件。

{_decisions_block(decisions)}## 任务 {task.task_id}：{task.title}（模块 {task.module}，角色 {task.role}）

{task.description}

## 验收标准

{criteria}

## 允许修改的路径

{_bullets(task.allowed_paths)}

## 调度器已独立执行的验证命令（全部通过）

{verify_summary}

## 相关资料切片

{_slices_block(slices, 8_000)}

## 改动 diff（基线..HEAD）

```diff
{diff}
```

## 审查要点

- 逐条核对验收标准，criteria_check 中每条标准一项，给出 met 与证据（文件/函数/测试名）。
- 测试是否真实覆盖正常、边界和异常路径；是否有空实现、占位代码、伪造返回值或针对测试的硬编码。
- 与契约和资料是否一致（命名、字段、错误码、行为）；有无资源泄漏、并发安全、明显的健壮性问题。
- 纯风格偏好不算阻塞问题。

## 判定

- PASS：全部验收标准满足且没有阻塞问题。
- NEEDS_FIX：存在必须修改的问题，blocking_issues 中给出 file、line（未知填 0）、problem、fix。
- ESCALATE_CONTRACT：问题根源在契约或规格本身，需要人工决策。

只输出符合 JSON Schema 的 JSON。
"""


def summarize_verify(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"- `{r.cmd}` → exit {r.exit_code}（{r.duration_sec:.1f}s）")
        tail = "\n".join(r.output.strip().splitlines()[-15:])
        if tail:
            lines.append("  ```\n  " + tail.replace("\n", "\n  ") + "\n  ```")
    return "\n".join(lines) or "（无）"


def plan_to_markdown(plan_dict: dict) -> str:
    out = [f"# 研发计划\n\n{plan_dict.get('summary', '')}\n\n**技术栈**：{plan_dict.get('tech_stack', '')}\n", "## 模块\n"]
    for m in plan_dict.get("modules", []):
        out.append(f"- **{m['name']}** `{m['path']}` — {m['description']}")
    tasks = plan_dict.get("tasks", [])
    for ms in plan_dict.get("milestones", []):
        out.append(f"\n## {ms['milestone_id']} {ms['title']}\n\n{ms['goal']}\n\n集成命令：`{ms['integration_cmd'] or '（无）'}`\n")
        out.append("| 任务 | 模块/角色 | 标题 | 依赖 | 允许路径 | 验证命令 |\n|---|---|---|---|---|---|")
        for t in tasks:
            if t["milestone_id"] != ms["milestone_id"]:
                continue
            out.append(
                f"| {t['task_id']} | {t['module']}/{t['role']} | {t['title']} | {', '.join(t['depends_on']) or '-'} | "
                f"{'<br>'.join(t['allowed_paths'])} | {'<br>'.join(f'`{c}`' for c in t['verify_cmds'])} |"
            )
    return "\n".join(out) + "\n"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------- 多角色 Goal 模式：PM / 研发总监蓝图 / 滚动拆单 / 异常仲裁 ----------------

def build_pm_prompt(slices: list[DocSlice], extra: str = "") -> str:
    catalog = "\n".join(
        f"| {s.slice_id} | {s.source_file} | {s.heading_path} | {s.char_count} | {s.summary} |" for s in slices
    )
    total = sum(s.char_count for s in slices)
    body = (
        _slices_block(slices, PLANNER_FULLTEXT_BUDGET)
        if total <= PLANNER_FULLTEXT_BUDGET
        else f"资料总长 {total} 字符，请按需用只读命令阅读 inputs/ 下的原始文档。"
    )
    extra_block = f"\n## 负责人补充指示\n\n{extra}\n" if extra.strip() else ""
    return f"""你是【产品经理（PM / Spec Refiner）】，处于只读沙箱模式。
你的职责：阅读用户输入的系统架构草案，把未写清楚的**业务状态流转、输入校验边界、并发/幂等规则、全局错误码规范与各模块验收红线**补全，避免后续多个并行研发组各自猜测业务细节。

## 输入资料索引

| slice_id | 来源文件 | 标题路径 | 字数 | 摘要 |
|---|---|---|---|---|
{catalog}

## 资料内容

{body}
{extra_block}
## 输出要求

只输出符合给定 JSON Schema 的 JSON：
- `product_vision`：一句话说明目标产品/系统核心价值与范围边界。
- `core_workflows`：核心业务主流程与状态机流转规则列表。
- `edge_cases`：空输入、重复请求、越权、资源不存在、并发竞争等明确处理约定。
- `error_conventions`：全局统一异常类 / 错误码 / 返回结构约定。
- `global_acceptance_redlines`：所有研发组必须遵守的硬性验收底线。
- `spec_markdown`：一份结构清晰、可直接写入仓库 `docs/PRD_SPEC.md` 供全体研发组与 QA 组查阅的完整中文规格说明书（不要发明与原始架构冲突的庞大额外功能，保持克制、准确、可测试）。
"""


def build_blueprint_prompt(
    slices: list[DocSlice], toolchains: dict[str, str], settings: Settings, extra: str = ""
) -> str:
    catalog = "\n".join(
        f"| {s.slice_id} | {s.source_file} | {s.heading_path} | {s.char_count} | {s.summary} |" for s in slices
    )
    total = sum(s.char_count for s in slices)
    fulltext = (
        "## 资料与 PM 规格全文\n\n" + _slices_block(slices, PLANNER_FULLTEXT_BUDGET)
        if total <= PLANNER_FULLTEXT_BUDGET
        else "## 资料获取\n\n资料较长，请按需使用只读命令阅读 `inputs/` 与 `docs/PRD_SPEC.md`。"
    )
    tools = _bullets([f"{k}: {v}" for k, v in toolchains.items()])
    protected = ", ".join(settings.protected_paths)
    extra_block = f"\n## 负责人补充要求\n\n{extra}\n" if extra.strip() else ""
    return f"""你是【研发总监 / 总架构师（Tech Director）】，处于只读模式。
你要把整个目标架构立项为**多研发组（Squads）协同 + 分里程碑滚动排期（Rolling-Wave）**的工程蓝图。

## 资料切片索引（含 PM 规格书 PRD-S01）

| slice_id | 来源文件 | 标题路径 | 字数 | 摘要 |
|---|---|---|---|---|
{catalog}

{fulltext}

## 本机可用工具链

{tools}

技术栈约束：默认优先使用本机现有工具链与标准库（如 Python 3 标准库 + unittest）；仅当资料明确要求第三方依赖时才规划 `M0-bootstrap`（role=bootstrap 可联网初始化 `.autodev/shared_venv`），其余简单/中型架构直接从 `M1-contracts` 起步即可。
{extra_block}
## 立项蓝图设计规则（极其重要）

{FILE_DELIVERY_STAGES}

0. **按项目真实体量自适应分档（杀鸡绝不用牛刀！）**：
   - **精简敏捷档**（适用于网页小游戏、单体小工具、MVP 原型，总代码文件约 3～12 个）：
     - 研发组控制在 **2～3 个**（例如 `squad-contract` 契约组 + `squad-core` 核心逻辑组 + `squad-web` 界面与集成组，**不要**单独设交付协调组 `squad-release`，也无需把测试拆成专门一堆碎卡）；
     - 里程碑控制在 **2 个**（如 `M1-foundation` 契约与核心引擎 → `M2-product` 完整界面交互与端到端验收）；
     - **全项目总任务卡控制在 4～6 张**（每个里程碑 **2～3 张高内聚功能卡**！工程师 `coder` 写功能时顺手把对应单元测试一并写好跑通，**严禁**为 1 个模块拆出 3～4 张细碎的回归测试卡！）。
   - **标准多域工程档**（仅当输入明确为大型多微服务、跨语言异构 SDK 或复杂存储后端时启用）：
     - 划分 4～6 个业务/契约/网关/QA 研发组，2～4 个里程碑，按业务域并行拆卡。
1. **铁律：严禁两类无效/越界废单**：
   - **严禁“文书核账/哈希对账/写交付证明”类任务**：Vernal 平台底层代码（`FileExchange` 与里程碑集成器）已在毫秒级 100% 自动校验路径白名单、SHA-256 哈希与 `integration_cmd`，且 LoopForge 每单 SOP 已内置独立 `Reviewer`。**绝对禁止**生成类似“核对上游产物回执、核验白名单与哈希、汇总测试入口、撰写阶段验收报告文档”的纯流程工单！每一张任务卡都必须直接产出业务代码、前端界面或核心自动化测试。
   - **严禁把需要物理真机/跨系统桌面手点的测试拆给 AI**：执行环境为无 GUI 的命令行沙箱，没有 Windows/macOS 真人浏览器手点环境，也没有 Android/iOS 手机。凡涉及多设备真机试玩的说明只写进最终 `README.md`，**严禁拆成 DAG 任务卡**！
2. **铁律：任务标题与描述必须“说人话”（面向人类负责人可读）**：
   - `title`（10～18 个字）必须用**直观的产品/功能大白话**，让人扫一眼流程图就知道做完这张卡多了什么能力（✅ 好示例：`定义游戏状态契约与共享常量`、`实现核心碰撞计分引擎与单元测试`、`完成可交互游戏界面与最高分存档`；❌ 严禁写成：`交付两个独立同帧夹具并验证跨档边界`、`汇合双环境导出一致性`）。
   - `description` 第一句先用自然白话讲清业务交付物，随后再写需要对接的契约接口与技术约束。
3. **划分研发组（`squads`）与物理领地隔离**：
   - 每个研发组的 `owned_paths` 边界清晰、互不重叠（如 `contracts/`、`engine/`、`web/`、`tests/`）。
4. **制定全局里程碑路线图（`milestones`）与首轮展开策略（`initial_tasks`）**：
   - 每个里程碑写清简洁人话 `title`、`goal`、`target_squads`、`key_deliverables` 与命令行可自动执行的 `integration_cmd`。
   - **只在 `initial_tasks` 中拆出第 1 个里程碑的具体任务**（精简档拆 **2～3 张卡**即可）！后续第 2..N 个里程碑留待前序合入后结合真实代码树滚动细化。
   - 任务编号形如 `T-001`、`T-002`，非 `contract`/`bootstrap` 任务的 `allowed_paths` 严禁包含 `{protected}`。
   - **单卡 `verify_cmds` 与 `allowed_paths` 必须自洽**：若 `verify_cmds` 运行测试发现命令（如 `unittest discover` / `pytest`），本卡 `allowed_paths` 必须包含对应测试文件（推荐实现与单测同卡交付）；若本卡不含测试文件，只配针对该卡文件的导入/语法自检命令，严禁产生“白名单不让写测试却跑 `unittest discover` 报 0 用例退出码 5”的死锁。
5. **`agents_md`**：生成写入仓库根目录 `AGENTS.md` 的简明中文协作规范。

只输出符合给定 JSON Schema 的 JSON。
"""


def build_wave_prompt(
    milestone_dict: dict,
    all_milestones: list[dict],
    squads: list[dict],
    slices: list[DocSlice],
    repo_files: list[str],
    contracts: str,
    completed_tasks: list[Task],
    next_task_seq: int,
    settings: Settings,
) -> str:
    protected = ", ".join(settings.protected_paths)
    squad_lines = [
        f"- `{s['squad_id']}`（{s['name']}）：领地 `{', '.join(s['owned_paths'])}` · 角色 {','.join(s['roles'])} · {s['mission']}"
        for s in squads
    ]
    done_lines = [
        f"- `{t.task_id}` [{t.squad}/{t.role}] {t.title}（已合入）" for t in completed_tasks
    ] or ["- （尚无）"]
    future_ms = [
        f"- `{m['milestone_id']}` {m['title']}：{m['goal']}" for m in all_milestones
    ]
    return f"""你是【研发总监（Tech Director · 滚动排期）】，处于只读模式。
前序里程碑已合入 `main` 分支。现在请结合仓库中**已经真实存在的代码与冻结契约**，专门把当前里程碑 `{milestone_dict['milestone_id']}` 拆解为分派给各研发组的并行原子任务。

## 当前待展开里程碑：{milestone_dict['milestone_id']} · {milestone_dict['title']}

- 阶段目标：{milestone_dict['goal']}
- 参战研发组：{', '.join(milestone_dict.get('target_squads') or []) or '按需指派'}
- 关键交付物：{'; '.join(milestone_dict.get('key_deliverables') or []) or '见阶段目标'}
- 阶段集成命令：`{milestone_dict.get('integration_cmd') or '（无）'}`
- {FILE_DELIVERY_STAGES}

## 全局里程碑路线参考

{chr(10).join(future_ms)}

## 研发组编制与目录领地

{chr(10).join(squad_lines)}

## 已完成的前序任务

{chr(10).join(done_lines)}

## 当前 `main` 分支已有文件列表

{_bullets(repo_files)}

## 当前已冻结的上游契约（{protected}）

{contracts}

## 产品规格与架构切片参考

{_slices_block(slices, 28_000)}

## 拆单规则（精简、高内聚、说人话）

1. 只拆解 `{milestone_dict['milestone_id']}` 这一阶段的任务，`milestone_id` 统一填 `{milestone_dict['milestone_id']}`，任务 ID 从 `T-{next_task_seq:03d}` 顺延编号。
2. **严格控制单阶段卡片数量，杜绝碎片化与叠床架屋**：
   - 小型应用/小游戏单阶段控制在 **2～3 张高内聚功能卡**（最多不超过 4 张）；开发任务（`coder`）在实现模块功能时**直接一并编写并通过该模块的单元测试**，严禁把测试拆成三四张细碎的回归卡！需要跨模块联调时最多在末尾加 **1 张** 端到端自动化验证卡。
   - **严禁生成“文书核账/核对 SHA-256 哈希/核对导出副本/写阶段验收证明报告”的纯流程废单**（平台底层已自动完成哈希、白名单及 `integration_cmd` 校验，每单 SOP 已含独立审查）。
   - **严禁生成需要 Windows/macOS 真人浏览器手点或 Android/iOS 物理手机实测的任务卡**！所有 `verify_cmds` 必须在本机无界面命令行下直接执行通过，且**必须与本卡 `allowed_paths` 自洽**（不得在白名单不含测试文件的卡片上运行 `unittest discover` / `pytest`，避免 0 测试退出码 5 死锁）。
3. **标题与描述必须说人话**：
   - `title`（10～18 字）必须用清晰直观的产品功能大白话（如 `实现飞船操控、碰撞计分与最高分存档`），禁止使用生涩的单元测试夹具行话；
   - `description` 先用 1～2 句自然语言说明用户可见功能或核心产出，再附具体契约接口与技术要求。
4. **并行无冲突与契约保护**：
   - 同批次并行任务的 `allowed_paths` 互不重叠；`depends_on` 严禁成环；非 `contract` 任务的 `allowed_paths` 严禁包含 `{protected}`。

只输出符合给定 JSON Schema 的 JSON。
"""


def build_director_arbitration_prompt(
    task: Task, reason: str, contracts: str, diffstat: str, next_patch_id: str, settings: Settings
) -> str:
    protected = ", ".join(settings.protected_paths)
    return f"""你是【研发总监（Tech Director · 异常仲裁官）】，处于只读模式。
下属工位在执行任务 `{task.task_id}` 时触发升级/连续失败阻塞。请诊断根因并做出仲裁决策：

## 阻塞任务信息

- 任务：`{task.task_id}` — {task.title}
- 研发组 / 角色：`{task.squad}` / `{task.role}`
- 描述：{task.description}
- 允许修改路径：{', '.join(task.allowed_paths)}
- 验证命令：{', '.join(task.verify_cmds)}
- 已仲裁次数：{task.arbitration_count}（最多自动仲裁 {settings.director_max_arbitrations} 次）

## 阻塞 / 失败详情

```
{reason[:8000]}
```

## 当前工作区 Diff 统计

```
{diffstat or "（尚无提交改动）"}
```

## 当前契约内容（{protected}）

{contracts}

## 三种仲裁动作（`action`）

1. **`STEER_RETRY`**：问题出在实现逻辑、测试写法或路径误改，不需要改 `{protected}` 契约。
   - 在 `steer_note` 中给出具体到文件、函数与边界条件的修复方案，让原工位重置重试计数继续修；
   - `contract_patch` 各字段填空字符串 `""` 或空数组 `[]`。
2. **`PATCH_CONTRACT`**：下游任务确实因为 `{protected}` 缺少必要字段/方法/异常定义而无法完成（例如发出了合理的 `NEED_CONTRACT_UPDATE`）。
   - 系统会自动新建契约升版任务 `{next_patch_id}`（`role="contract"`），跑通合入当前基线后再自动唤醒 `{task.task_id}`！
   - 请在 `contract_patch` 中填好该契约修订任务的 `title`、`description`、`allowed_paths`（必须在 `{protected}` 及契约测试内）、`verify_cmds`、`acceptance_criteria`；并在 `steer_note` 写明契约更新后 `{task.task_id}` 应如何对接。
3. **`ESCALATE_HUMAN`**：原始需求本身存在重大业务矛盾，必须由人类负责人拍板。

只输出符合给定 JSON Schema 的 JSON。
"""


def blueprint_to_markdown(bp_dict: dict, tasks: list[dict] | None = None) -> str:
    out = [
        f"# Goal 研发立项蓝图\n\n{bp_dict.get('summary', '')}\n\n**技术栈**：{bp_dict.get('tech_stack', '')}\n",
        "## 研发组编制与目录领地（Squads）\n",
        "| 组 ID | 研发组名称 | 角色 | 专属领地 (owned_paths) | 职责使命 |",
        "|---|---|---|---|---|",
    ]
    for s in bp_dict.get("squads", []):
        out.append(
            f"| `{s['squad_id']}` | **{s['name']}** | {','.join(s.get('roles', []))} | "
            f"`{', '.join(s.get('owned_paths', []))}` | {s['mission']} |"
        )
    all_tasks = tasks if tasks is not None else bp_dict.get("initial_tasks", [])
    out.append("\n## 里程碑滚动路线图（Milestones）\n")
    for m in bp_dict.get("milestones", []):
        m_tasks = [t for t in all_tasks if t["milestone_id"] == m["milestone_id"]]
        state_tag = f"已细化 {len(m_tasks)} 个任务" if m_tasks else "待前序合入后由研发总监滚动细化"
        out.append(
            f"\n### {m['milestone_id']} · {m['title']}（{state_tag}）\n\n"
            f"- **目标**：{m['goal']}\n"
            f"- **参战研发组**：{', '.join(m.get('target_squads', [])) or '-'}\n"
            f"- **关键交付物**：{'；'.join(m.get('key_deliverables', [])) or '-'}\n"
            f"- **集成验证**：`{m.get('integration_cmd') or '（无）'}`\n"
        )
        if m_tasks:
            out.append("| 任务 | 研发组/角色 | 标题 | 依赖 | 允许路径 | 验证命令 |\n|---|---|---|---|---|---|")
            for t in m_tasks:
                out.append(
                    f"| {t['task_id']} | {t.get('squad') or t['module']}/{t['role']} | {t['title']} | "
                    f"{', '.join(t['depends_on']) or '-'} | {'<br>'.join(t['allowed_paths'])} | "
                    f"{'<br>'.join(f'`{c}`' for c in t['verify_cmds'])} |"
                )
    return "\n".join(out) + "\n"


# ---------------- 多智能体架构评审圆桌（Architecture Council · 2 轮收敛） ----------------

_COUNCIL_ROLE_PROFILES: dict[str, tuple[str, str]] = {
    "pm": (
        "产品经理（PM）",
        "聚焦核心业务流、用户/调用方场景、状态机流转、空值/幂等/权限边界、统一错误码与业务验收红线。不陷入底层代码细节。",
    ),
    "architect": (
        "契约与异构架构师（Contract & Polyglot Architect）",
        "聚焦系统模块边界、跨语言/异构组件间的共享接口契约（Proto/OpenAPI/Schema/异常体系）、避免并行修改公共文件的领地划分方案，以及各研发组应绑定的执行环境/沙箱类型（如 generic-docker / sandbox-go / sandbox-python / sandbox-rust / sandbox-node）。",
    ),
    "qa_lead": (
        "QA 质量总监（QA Lead）",
        "聚焦务实、可全自动在无界面命令行中秒级运行的契约与核心功能验证方案。注意：严禁要求物理真机（iOS/Android）或跨系统桌面人工手点作为自动化阻塞门禁，严禁建议繁琐的人工哈希对账文书流程；坚持小项目精简测试、大项目分层回归。",
    ),
}


def build_council_opinion_prompt(
    role: str, slices: list[DocSlice], extra: str = "", prior_human_notes: list[str] | None = None
) -> str:
    title, focus = _COUNCIL_ROLE_PROFILES.get(role, (role, "从你的专业视角给出结构化评审建议。"))
    catalog = "\n".join(
        f"| {s.slice_id} | {s.source_file} | {s.heading_path} | {s.char_count} | {s.summary} |" for s in slices
    )
    total = sum(s.char_count for s in slices)
    body = (
        _slices_block(slices, PLANNER_FULLTEXT_BUDGET)
        if total <= PLANNER_FULLTEXT_BUDGET
        else f"资料总长 {total} 字符，请使用只读命令查阅 `inputs/` 下的文档。"
    )
    notes_block = ""
    if extra.strip() or prior_human_notes:
        items = ([extra.strip()] if extra.strip() else []) + list(prior_human_notes or [])
        notes_block = "\n## 负责人补充指示\n\n" + _bullets(items) + "\n"
    return f"""你正在参加【多智能体架构评审圆桌（Architecture Council）· 第 1 轮独立提案】，担任角色：**{title}**（只读模式）。

## 你的评审职责

{focus}
{FILE_DELIVERY_STAGES}

## 输入资料索引

| slice_id | 来源文件 | 标题路径 | 字数 | 摘要 |
|---|---|---|---|---|
{catalog}

## 资料内容

{body}
{notes_block}
## 输出要求（严格 JSON）

请保持简明、高信息密度、可落地，输出符合 JSON Schema 的 JSON：
- `headline`：一句话概括你的核心立场与关键判断。
- `proposals`：3～6 条具体可落地的工程提案。
- `concerns`：2～5 条你发现的风险、模糊点或潜在坑（请直接指出，不要客套）。
- `questions`：1～4 个需要研发总监或其他角色在第 2 轮拍板澄清的关键分歧/问题。
- `recommended_sandboxes`：你建议为本系统准备的沙箱/工具链标签列表（如 `["python3", "generic-docker"]`）。
"""


def build_council_consensus_prompt(
    slices: list[DocSlice],
    opinions: list[dict],
    human_notes: list[str] | None = None,
    extra: str = "",
) -> str:
    blocks = []
    for op in opinions:
        b = op.get("body") or {}
        blocks.append(
            f"### {op.get('speaker_name')}（{op.get('speaker_role')}）\n"
            f"- 核心立场：{op.get('headline', '')}\n"
            f"- 提案：\n{_bullets(b.get('proposals') or [])}\n"
            f"- 风险与质疑：\n{_bullets(b.get('concerns') or [])}\n"
            f"- 抛给圆桌的问题：\n{_bullets(b.get('questions') or [])}\n"
            f"- 建议沙箱：{', '.join(b.get('recommended_sandboxes') or []) or '默认'}"
        )
    human_block = ""
    all_human = ([extra.strip()] if extra.strip() else []) + list(human_notes or [])
    if all_human:
        human_block = "\n## 人类负责人最新插话 / 裁决要求（最高优先级，必须采纳）\n\n" + _bullets(all_human) + "\n"
    return f"""你是【研发总监（Tech Director）】兼本场【多智能体架构评审圆桌 · 第 2 轮收敛拍板人】（只读模式）。
第 1 轮中，产品经理、契约架构师和 QA 总监已各自提交独立评审意见。请你逐一回应关键争议、消除分歧，形成权威的《架构决策纪要（ADR）》与统一产品技术规格书。
{FILE_DELIVERY_STAGES}

## 第 1 轮各角色意见汇总

{chr(10).join(blocks)}
{human_block}
## 原始输入资料摘要

{_slices_block(slices, 24_000)}

## 拍板输出要求（严格 JSON）

- `executive_summary`：一段话总结本次圆桌达成的架构共识与分工策略。
- `resolved_debates`：逐条说明如何裁决第 1 轮各方提出的分歧与问题（格式：`[争议点] → [最终裁决]`）。
- `open_risks`：剩余需在开发/测试期持续盯防的风险点。
- `contract_strategy`：公共契约如何定义、冻结、跨模块引用及版本演进策略。
- `adrs`：2～6 条结构化架构决策记录（`adr_id` 如 `ADR-001`、`title`、`decision`、`rationale`、`affected_squads`）。
- `spec_markdown`：将写入仓库 `docs/PRD_SPEC.md` 的权威中文《产品与架构共识规格书》（语言清晰直白、避免晦涩行话；坚持规模匹配——小型项目保持轻量架构与命令行可跑通的自动化验收标准，将任何需人类真机试玩的事项明确标注为「交付后人工体验项」而非开发阻塞项）。
"""
