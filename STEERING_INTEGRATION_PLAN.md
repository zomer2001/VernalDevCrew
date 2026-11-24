# Vernal × LoopForge 深度融合方案：原生并发评审 SOP、动态全局规则与演进节点主线

> 本文档用于直接交接给其他 AI / 开发者继续实现。**第 1 步（底层表结构、LoopForge 客户端与原生并发评审 SOP 模板）已落盘完成**，直接从第 2 步接着做即可。

---

## 一、核心设计思想（为什么要这样改）

- **单次会议 & 单张工单的微观流程 → 100% 下沉为 LoopForge 原生 SOP 与全局 Rule**：
  - 不再由 Vernal 在外层散装调 4 次接口模拟圆桌；直接用 LoopForge 原生 **6 节点并发 DAG SOP**（`pm_opinion` / `arch_opinion` / `qa_opinion` 三分支并发 → `director_plan` 汇合 → `validate_plan` 校验 → `human_gate` 原生人工审批门禁，驳回自动 `rejectTo: director_plan`）。
  - 每次评审拍板生效的规矩，直接通过 `PUT /api/rules` 写入 LoopForge 项目级全局规则（`global: true`），由 LoopForge 运行时自动注入所有编码节点、审查节点和对话角色。
- **跨工单、跨阶段的宏观演进 → 由 Vernal 负责粘合**：
  - 随时基于“当前真实进展 + 报错 + 现行规矩 + 人类新想法”发起新一轮评审 SOP；
  - 审批通过后（无论在 Vernal 还是在 LoopForge 原生「待我确认」点击），原子同步三件事：更新全局 Rule、仅对坏掉的旧卡局部 `reopen` 返工（保留其余已通过任务不动）、增删重排后续工单；
  - 主页呈现一条清晰的「项目演进节点主线（Checkpoint Timeline）」。

---

## 二、已完成并落盘的第 1 步（请勿重复改，可直接复用）

1. **[autodev/db.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/db.py)**
   - 已新增两张 SQLite 表：`project_decisions`、`evolution_checkpoints`。
   - 已新增方法：`list_decisions(active_only=False)`、`upsert_decision(...)`、`revoke_decision(...)`、`create_checkpoint(...)`、`update_checkpoint(...)`、`get_checkpoint(...)`、`list_checkpoints()`。
2. **[autodev/loopforge/client.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/client.py)**
   - `update_issue()` 已支持可选 `status` 参数（可用于关闭取消的 Issue，传 `status="closed"`）。
   - `list_pending_approvals()` 已同时识别 LoopForge 的 `waiting` 和 `waiting_approval` 状态。
3. **[autodev/loopforge/bootstrap.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/bootstrap.py)**
   - 已新增 `VALIDATE_STEERING_SHELL_SCRIPT`（Python / Node 双运行时校验 `steering-plan.json` 并自动兜底生成 `steering-summary.md`）。
   - 已新增 `review_workflow_id(project_id)`（`<pid>-vernal-review-sop`）、`decisions_rule_id(project_id)`（`<pid>-vernal-decisions-rule`）、`build_decisions_rule()`、`sync_decisions_rule()`。
   - `build_vernal_agents()` 已把 `decisions_rule_id(project_id)` 挂入 `coder`（含所有泳道）和 `reviewer` 的 `rules`，并新增 4 个独立角色 ID（确保 LoopForge 并发不互斥）：
     - `<pid>-vernal-review-pm`（写 `opinions/pm.json`）
     - `<pid>-vernal-review-arch`（写 `opinions/arch.json`）
     - `<pid>-vernal-review-qa`（写 `opinions/qa.json`）
     - `<pid>-vernal-review-director`（读三方意见，写 `steering-plan.json` 和 `steering-summary.md`）
   - 已新增 `build_review_workflow()` 并在 `ensure_vernal_loopforge_assets()` 中幂等注册发布。

---

## 三、待实现清单（第 2 ~ 5 步）

### 第 2 步：新建 `autodev/steering.py`（进展打包 + 转向变更单执行引擎）

1. **数据结构约定（`steering-plan.json`）**：
   ```json
   {
     "summary": "大白话总结本次为什么改、保留什么、调整什么",
     "decisions_upsert": [
       {"decision_id": "D-01", "title": "短标题", "rule_text": "可核验硬规矩", "rationale": "原因", "affected_paths": ["src/"]}
     ],
     "decisions_revoke": [
       {"decision_id": "D-02", "reason": "废止原因"}
     ],
     "reopen_tasks": [
       {"task_id": "T-003", "reason": "仅局部退回该任务的具体问题与修复指引"}
     ],
     "cancel_tasks": ["T-006"],
     "new_tasks": [
       {
         "milestone_id": "M2",
         "title": "人话标题",
         "description": "目标与实现要求",
         "role": "coder",
         "squad": "core",
         "depends_on": [],
         "allowed_paths": ["src/new_mod.py", "tests/test_new_mod.py"],
         "verify_cmds": ["python3 -m unittest tests/test_new_mod.py"],
         "acceptance_criteria": ["..."]
       }
     ]
   }
   ```

2. **需实现的核心函数**：
   - `sync_council_adrs_to_decisions(engine, consensus)`：
     - 在 [autodev/council.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/council.py) 的 `synthesize_council()` 收敛成功后调用；
     - 把 `consensus.adrs` 写入 `engine.db.upsert_decision(...)`，记录一条 `INITIAL` checkpoint，若为 `loopforge` 模式则调用 `sync_decisions_rule()` 同步至 LoopForge 全局 Rule。
   - `build_review_context_markdown(engine, topic, human_note="", kind="REVIEW") -> str`：
     - 收集：当前生效 `project_decisions`、所有里程碑与任务状态（含 `retry_count`、`failure_reason`、`allowed_paths`、`loopforge_issue_id`）、最近集成失败日志（`.autodev/runs/_integration/*.log`）、资料切片摘要、以及人类本次输入的 `topic` / `human_note`。
     - 若总长度 > 40 KB，把大上下文落盘至 `.autodev/baselines/review-{checkpoint_id}.json` 并通过 `build_restore_command` 附带带 HMAC 签名的拉取命令。
   - `start_steering_review(engine, *, topic, human_note="", kind="REVIEW", actor="owner") -> dict`：
     - 插入一条 `evolution_checkpoints`（`status="RUNNING"`）。
     - **`loopforge` 模式**：先调 `ensure_vernal_loopforge_assets()`，创建一条绑定 `review_workflow_id(pid)` 的 LoopForge Issue（标题 `[评审 #{round_no}] {topic}`），调用 `dispatch_issue()`，把 `loopforge_issue_id` / `loopforge_run_id` 写回 checkpoint。
     - **`local` 离线模式**：复用 `engine.harness.run_structured_readonly` 产出 `steering-plan.json` 草案，将 checkpoint 置为 `AWAITING_APPROVAL`（保证离线测试和单机模式同样可用）。
   - `reconcile_steering_checkpoint(engine, checkpoint_id) -> dict`：
     - 查询 LoopForge `get_run(run_id)`：
       - 对非 `approval` 节点且仅卡在 `submit_gate_result` 的状态自动放行（**严禁自动放行 `human_gate`**）；
       - 尝试从 Run 产物提取 `opinion-pm.json`、`opinion-arch.json`、`opinion-qa.json` 与 `steering-plan.json`（兜底用 `extract_run_text` + `extract_json`）；
       - 当 `human_gate` 处于 `waiting` / `waiting_approval` 时，更新 checkpoint 为 `AWAITING_APPROVAL`（写入 `opinions` 与 `draft_plan`）并派发 SSE `steering.awaiting_approval`；
       - **双向同步关键点**：如果用户在 LoopForge 原生「待我确认」里直接点了通过，Run 状态会变成 `succeeded`——`reconcile` 检测到 `succeeded` 且 checkpoint 尚未 `APPLIED` 时，**自动调用 `apply_steering_plan()`**！
       - 若 Run 变成 `running`（例如用户在 LoopForge 原生 UI 点了驳回重议），把 checkpoint 更新回 `RUNNING`；若 `failed` / `cancelled` 则更新为 `FAILED`。
   - `approve_steering_checkpoint(engine, checkpoint_id, *, actor="owner", comment="", override_plan=None) -> dict`：
     - `loopforge` 模式下若 Run 正在 `human_gate` 等待，先调 `client.approve_run(run_id, comment=..., node_id="human_gate")`，再调用 `apply_steering_plan()`。
   - `reject_steering_checkpoint(engine, checkpoint_id, *, actor="owner", reason="") -> dict`：
     - `loopforge` 模式下调 `client.reject_run(run_id, reason=reason, target_node="director_plan", node_id="human_gate")`，将 checkpoint 重置为 `RUNNING` 并追加反馈到 `human_note`。
   - `apply_steering_plan(engine, checkpoint_id, plan, *, actor="owner") -> dict`：
     - 幂等保护：若 checkpoint 已是 `APPLIED` 直接返回；
     - ① 执行 `decisions_upsert` / `decisions_revoke`，递增 `meta["decisions_rule_version"]`，`loopforge` 模式下调 `sync_decisions_rule()` 更新全局 Rule；
     - ② **局部退回（`reopen_tasks`）**：仅重置点名任务为 `TaskStatus.READY`、`retry_count=0`、`arbitration_count=0`、`steer_note="[评审 #N 局部返工] ..."`，清空 `failure_reason`，并将其所属里程碑从 `FAILED`/`AWAITING_MERGE`/`MERGED` 恢复为 `PENDING`；若该任务绑定了 `loopforge_issue_id`，调 `client.reopen_issue(issue_id)`。**其他已 `APPROVED`/`MERGED` 的任务严禁改动**；
     - ③ **取消任务（`cancel_tasks`）**：仅取消尚未 `MERGED` 的任务（从活跃任务中移除并清理其他任务 `depends_on` 中的引用防死锁）；若绑定了 `loopforge_issue_id`，调 `client.update_issue(..., status="closed")`；
     - ④ **新增任务（`new_tasks`）**：顺延分配 `T-xxx` 编号，自动补建不存在的目标里程碑，写入数据库；`loopforge` 模式下可同步在 LoopForge 创建对应 `todo` Issue 写入 `loopforge_issue_id`；
     - ⑤ 标记 checkpoint 为 `APPLIED` 并写入 `applied_summary`；若项目状态为 `PLANNED`/`STALLED`/`COMPLETED` 且有待跑任务，置为 `APPROVED`。
3. **补强 [autodev/prompts.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/prompts.py)**：
   - 给 `build_coder_prompt()` 与 `build_review_prompt()` 增加可选参数 `decisions: list[dict] | None = None`（默认 `None` 保持兼容），把生效规矩注入提示词，并由 [autodev/scheduler.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/scheduler.py) 传入 `self.db.list_decisions(active_only=True)`，同时给 `format_issue_description()`（[autodev/loopforge/bridge.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/bridge.py)）附带生效规矩 ID 索引。

---

### 第 3 步：接入服务端 API（[autodev/server/app.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/server/app.py)）

- 在 `GET /vernal-api/state` 中增加返回：
  - `decisions`: `eng.db.list_decisions()`
  - `checkpoints`: `eng.db.list_checkpoints()`
  - `decisions_rule_version`: `int(eng.db.meta_get("decisions_rule_version") or 0)`
- 新增 4 个 REST 端点（遵守现有 `X-Vernal-Project` 槽位隔离与 Cookie 鉴权中间件）：
  1. `GET /vernal-api/steering`：返回 `decisions`、`checkpoints`、`rule_version`；若有 `RUNNING`/`AWAITING_APPROVAL` 的 checkpoint，顺手触发一次 `reconcile_steering_checkpoint` 对账。
  2. `POST /vernal-api/steering/start`：入参 `{topic, human_note, kind, actor}`，启动评审 SOP，并起一个轻量的后台轮询任务定期调用 `reconcile_steering_checkpoint` 直到到达 `AWAITING_APPROVAL` / `APPLIED` / `FAILED`。
  3. `POST /vernal-api/steering/{checkpoint_id}/approve`：入参 `{actor, comment, override_plan}`，双向批准并应用变更单。
  4. `POST /vernal-api/steering/{checkpoint_id}/reject`：入参 `{actor, reason}`，驳回至 `director_plan` 重出变更单并恢复后台轮询。
- 在里程碑集成失败卡片（`status == "FAILED"`）增加快捷触发：`kind="INTEGRATION_FIX"`，自动把该里程碑的 `failure_reason` 和 `.autodev/runs/_integration/<ms>.log` 填入 `human_note` 发起定位评审。

---

### 第 4 步：前端「演进节点主线 + 变更预览卡 + 拉会讨论弹窗」

- 涉及文件：[index.html](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/server/static/index.html)、[js/main.js](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/server/static/js/main.js)、新建 `js/steering.js`、[styles.css](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/server/static/styles.css)（改完跑 `.venv/bin/python scripts/build_vernal_stage_css.py` 并递增 [proxy.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/server/proxy.py) 的 `OVERLAY_VERSION`）。
- 交互要点（保持 LoopForge 克制居中弹窗风格）：
  1. 顶栏常驻按钮 **`💬 拿进展与问题讨论`** → 打开居中弹窗（大号 Composer `<textarea>` 输入本轮问题/新想法 + 快捷勾选“基于当前失败/进展自动汇总”）→ 调 `POST /vernal-api/steering/start`。
  2. 总控页 DAG 上方新增干净的 **「项目演进主线（Checkpoint Rail）」**：横向节点链展示 `立项评审 #1 (Rule v1)` → `波次执行` → `进展评审 #2 (Rule v2 · 局部返工 T-003)`，点击节点展开详情（三方意见摘要、生效规矩列表、一键跳转对应 LoopForge 原生 Run 图谱）。
  3. 当存在 `status == "AWAITING_APPROVAL"` 的 checkpoint 时，在主线正下方高亮显示 **「待拍板转向变更单预览卡」**：清晰列出“将新增/修改的规矩、将局部退回的旧卡、将取消/新增的工单”，底部提供 **[批准并同步到 LoopForge]** 与 **[写意见打回重议]**。

---

### 第 5 步：专项回归测试与验收标准

新建 `tests/test_steering_and_global_rules.py`，用 `httpx.MockTransport` 模拟 LoopForge（**不调用真实大模型**），覆盖：
1. `ensure_vernal_loopforge_assets()` 正确注册并发布 6 节点并发 `<pid>-vernal-review-sop`（`pm_opinion`/`arch_opinion`/`qa_opinion` 均指向 `director_plan`，且 3 个 `agentId` 互不相同）以及 `global=True` 的 `<pid>-vernal-decisions-rule`。
2. 发起 `POST /vernal-api/steering/start` → 模拟 LoopForge Run 停在 `human_gate (waiting_approval)` 并产出 `steering-plan.json` → `reconcile` 将 checkpoint 置为 `AWAITING_APPROVAL` 且不自动放行 `human_gate`。
3. 模拟在 Vernal 点击驳回（`POST /vernal-api/steering/{id}/reject`）→ 验证调用了 `POST /api/runs/{run_id}/reject` 且 `targetNodeId="director_plan"`。
4. 模拟通过（分别测试在 Vernal 点 `approve` 和在 LoopForge 原生 UI 批准后 Run 变 `succeeded` 经 `reconcile` 自动生效）→ 验证：
   - LoopForge `PUT /api/rules` 收到含最新条文的 `global: true` 全局 Rule；
   - 被点名返工的 `T-003` 被重置为 `READY` 并调用了 `POST /api/issues/{id}/reopen`，而同里程碑已通过的 `T-001`/`T-002` 保持 `APPROVED`/`MERGED` 纹丝不动；
   - 被取消的任务关闭了对应 Issue 且清理了下游 `depends_on`；新增任务写入 DB 并创建对应 Issue。
5. 运行全量 `.venv/bin/pytest -v`（确保原有 56 项 + 新增测试全部 100% 通过）与 `node --check` 前端语法校验。

---

## 四、LoopForge 底层核实过的关键细节（避坑备忘）

- **并发分支互斥规则**（来源：`LoopForge/internal/engine/parallel_exec.go`）：同一 Run 内只有 `agentId` 相同的节点才会串行互斥；我们已拆分为 `vernal-review-pm`/`arch`/`qa` 三个不同 `agentId`，因此 3 节点真并发，各自写 `opinions/{slug}.json` 零文件冲突。
- **汇合语义**（来源：`LoopForge/internal/engine/workflow_graph.go`）：`readyNodes()` 要求目标节点所有入边源节点都为 `succeeded` 才触发，天然支持 3→1 屏障汇合。
- **全局规则生效范围**（来源：`LoopForge/internal/engine/assets.go`）：`RuleDefinition` 设 `global: true` + `workflowIds` 时注入所有匹配 SOP 节点；同时把 `decisions_rule_id` 挂入角色 `agent.rules`，连无 SOP 的对话角色也会自动加载。
- **零 Python 宿主机兼容**：`validate_plan` 节点已采用 `VALIDATE_STEERING_SHELL_SCRIPT`（`python3` / `node` 双运行时），后续新增脚本节点务必延续此规范。
