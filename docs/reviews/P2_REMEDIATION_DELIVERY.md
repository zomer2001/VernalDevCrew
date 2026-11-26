# P2 整改与性能优化交付记录（R1–R7）

**日期**：2026-11-26  
**基线提交**：`0393624`（方案分支 `codex/p2-remediation-plan-20261126`，已包含 `origin/main` `af85705`）  
**实施分支**：`codex/p2-remediation-impl-20261126`  
**实施工作目录**：`workspaces/p2-remediation-impl`  
**隔离试点目录**：`workspaces/p2-remediation-pilot`（从既有验收样例独立复制，原 `standard-delivery-p2/workspaces/p2-ui-20261126` 保持零字节变更）

> **结论边界声明**：本文严格区分 **功能整改与闭环验证（R1–R6）** 与 **性能诊断与修复（R7）**。本仓库可独立实施的 R1、R2（Vernal 侧）、R3、R4、R5、R7 代码修改、自动化回归与隔离试点验证已全部落地并通过（`pytest` 308/308、`node --test` 14/14、`git diff --check` 零告警）。受外部预编译二进制缺陷阻塞（`agentflow-runner 0.10.2` 未向 Codex 传递 `agentflow-gate` MCP 工具审批策略、`ghcr.io/tencent/loopforge:0.4.0` 服务端缺少 `run_telemetry.adapter` 列且 `validate` 节点外围控制面耗时约 77.8s），**在线原生 Codex 无人值守端到端跑通（G5 实跑）与 R7 原生校验外围耗时 ≤ 5s 目标尚未达成**，均如实保留为外部阻塞项，不以本地验证或 Mock 替代真实候选闭环声明。

---

## 一、脱敏运行基线与隔离核对

| 维度 | 实测基线 | 备注 |
| --- | --- | --- |
| 方案基线 / 上游基线 | `0393624` / `origin/main` `af85705` | `af85705` 为 `0393624` 祖先，无未合入差异 |
| Python / Node 环境 | Python 3.14.3（pytest 9.2.0） / Node v22.22.0 | 沿用现有 `.venv`，未全局重装依赖 |
| LoopForge Server 镜像 | `ghcr.io/tencent/loopforge:0.4.0` (`sha256:87b6a3e3cba201d03f8f8d2292981f576021fc78c7c42e82f78329abbf4bda4`) | 外部镜像，本仓库无其 Go 源码 |
| Local Runner 二进制 | `agentflow-runner 0.10.2` (`sha256:3fe4cf526cab26b926c75cca8a8ae0f73f37304cb2e0ed4fb0fc699b51c11040`) | 外部预编译二进制 |
| Gate MCP 二进制 | `agentflow-gate-mcp-darwin-arm64` (`sha256:84f726073e5d02c5c584c8032ad78fb5c9e08d35d919a2941e6171f4d58aa1d9`) | 外部预编译二进制 |
| 本机 Codex CLI | `codex-cli 0.171.0-alpha.13.2`（`/Applications/ChatGPT.app/.../CodexCLI.app/Contents/MacOS/codex`） | 未修改 `~/.codex/config.toml` |
| 隔离保护核对 | `DeepSeekHarness`（`autodev/harnesses/cli_generic.py`）、`upstream/v1.4.4/*`、方案分支、`main`、`standard-delivery-p2`、`p2-ui-20261126` 全部 0 修改 | `p2-ui-20261126/.autodev/state.db` SHA-256 核验未变化 |

---

## 二、功能整改与闭环结果（R1–R6）

### 汇总表

| 修改包 | 提交 | 复现情况 | 本仓库自动化回归 | 真实 / 隔离验证 | 剩余阻塞 |
| --- | --- | --- | --- | --- | --- |
| **R1** | `2cfccfc` | 已复现（`run-1795681157951651640` 日志与二进制核验） | 42 passed（含并发幂等、旧轮拒收、权限拒绝禁自动批准） | 已查清 `/api/executors`、`agentflow-runner 0.10.2` 与 `codex` 二进制符号 | **外部阻塞**：需上游 `agentflow-runner`/LoopForge 为 Codex 注入 `agentflow-gate` MCP 授权或支持受控完成协议 |
| **R2** | `60cf221` | 已复现（实时 `HTTP 400` MySQL 1054 错误） | 21 passed（`test_r2_schema_diagnostics.py` + `test_file_delivery.py`） | 实时验证 `run_telemetry` 仅 3 列；`doctor.sh` 语法检查通过 | **外部阻塞**：需上游 `ghcr.io/tencent/loopforge` 修复 `QueryRunTelemetry` / `adapterSQLFilter` 查询或官方迁移 |
| **R3** | `b0fd590` | 已复现（代码链路核实与隔离 DB 验证） | 69 passed + 11 Node passed | 只读校验既有 `p2-ui-20261126` 正确识别 round 5 `PENDING` / `content_current=True` / `PERMISSION_DENIED`；隔离试点验证 `CHECKLIST_STALE` 阻断 | 无仓库内阻塞 |
| **R4** | `dcc7b23` | 已复现（`plan`/`analyze` 缺失 `checklist` 来源与发现表） | 78 passed + 12 Node passed | 只读校验既有 `p2-ui-20261126` 暴露 7 条 `CHK-5-01..07`；隔离试点验证发现入 `plan`/`analyze`、`NFR-001` 编译覆检与显式预设升级 `1.0.0 -> 1.1.0` | 无仓库内阻塞 |
| **R5** | `5482455` + 隔离试点 | 已复现（既有 7 条 WARNING + JS UTF-16 `.length` 计数不一致） | 19 passed + 8 Node passed（含 5001/10001 emoji 码点边界） | 在 `p2-remediation-pilot` 发布 `spec` rev 4 逐项落实 W1–W7 与 `NFR-001`，保留 Q-01/Q-02 确认原文与历史 rev 1–3；真实跑通 A01–A11 | 3 项业务承诺待负责人确认（B1/B2/B3，见下） |
| **R6** | 隔离试点验证 | 区分演示试点与 Vernal 自举 | 隔离试点 `pytest` 6 passed + `node --test` 2 passed；`FileExchange` 摘要一致 | 完成隔离演示试点 G0–G4 真实平台引擎流转与 G5/G6 本地候选验证（`b11f2524...`） | **外部阻塞**：受 R1 外部 Runner 阻塞，未完成原生 Codex 无人值守 G5；未建 Vernal 隔离源码自举试点；未由负责人做正式产品签收 |

---

### R1：完成回调与审批策略冲突（`2cfccfc`）

- **复现与实际根因**：
  1. 实查 LoopForge `GET /api/executors` 显示 `codex` 参数为 `["exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check"]`，`codex exec` 非交互模式有效 `approval_policy=never`。
  2. 实查 `agentflow-runner 0.10.2` 二进制确认其向 Codex 注册 `mcp_servers.agentflow-gate`（工具 `submit_gate_result` 写入 `AGENTFLOW_GATE_RESULT_PATH`，无 `readOnlyHint`），但二进制中 **0 处**传递 Codex MCP 工具授权键（实查 `CodexCLI.app/Contents/MacOS/codex` 确认其原生支持 `default_tools_approval_mode` / `mcp_tool_approval_policy` 配置键）。
  3. Codex 拒绝调用 `submit_gate_result`（`MCP tool call requires approval, but approval policy is never`），LoopForge 0.4.0 额外恢复会话重试 1 次（单节点浪费 45–129s）后停在 `waiting_approval`；旧版 `bridge.py` 随后调用 `approve_run()` 绕过了显式权限拒绝。
- **修改文件**：
  - 新增 [`autodev/loopforge/completion.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/completion.py)：统一 `CompletionCategory`、`evaluate_waiting_node()` 与 SQLite 持久化 `CompletionLedger`（覆盖 `run_id`/`node_id`/`attempt`/`rework_count`/`artifact_digest` 幂等键及单调高水位旧轮拒收）。
  - 修改 [`autodev/loopforge/bridge.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/bridge.py)、[`autodev/steering.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/steering.py)、[`autodev/loopforge/bootstrap.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/bootstrap.py)、[`autodev/harnesses/loopforge_harness.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/harnesses/loopforge_harness.py)、[`deploy/loopforge/start-runner.sh`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/deploy/loopforge/start-runner.sh)。
  - 回归测试：`tests/test_file_delivery.py`、`tests/test_loopforge_harness.py`、`tests/test_steering_and_global_rules.py`。
  - 未修改 `autodev/harnesses/cli_generic.py`（`DeepSeekHarness`）。
- **核心行为约束**：显式权限拒绝立即返回 `CALLBACK_PERMISSION_DENIED` 并阻断，**绝不调用 `approve_run()`**；人工审批门（`human_gate`/`approval`）、未知节点类型、非零退出码、缺失/旧轮产物、无证据报错串均拒绝自动通过；审查 `# REJECT` 严格触发 `reject_run()`。
- **生效方式与回退**：需重启 Vernal Python 后端；`start-runner.sh --probe` 即刻生效。上游 Runner 修复需由 LoopForge 官方发布或配置支持（严禁通过改 `~/.codex/config.toml` 或 `--dangerously-bypass-approvals-and-sandbox` 绕过）。

---

### R2：Adapter 数据库错误诊断与边界保护（`60cf221`）

- **复现与实际根因**：
  1. 实时请求 `GET http://127.0.0.1:8090/api/runs/run-1795681157951651640/telemetry?limit=100&adapter=live&after=0`（由 LoopForge 内嵌前端 `app/observability.js` 自动发出），稳定复现 `HTTP 400`：`{"error":"Error 1054 (42S22): Unknown column 'adapter' in 'where clause'"}`。
  2. 实时只读查询 MySQL 8.4 `loopforge` 库确认：`run_telemetry`（44,228 行，91.84 MB）与 `run_logs`（42,438 行，76.83 MB）仅有 3 列 `(run_id varchar(64), seq bigint, payload json)`，无 `adapter` 列。
  3. 实查容器 `/app/bin/agentflow` 符号确认 `(*RunsRepo).QueryRunTelemetry` -> `adapterSQLFilter` 生成了 `AND adapter=?` / `SELECT ... adapter ... FROM run_telemetry`，而未通过 JSON 字段读取或随版本提供官方迁移。
  4. 遵守禁令：未执行猜测性 `ALTER TABLE`，未删除 `?adapter=` 过滤，未清库或删卷。
- **修改文件**：
  - [`autodev/loopforge/client.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/client.py)：新增 `check_telemetry_schema(run_id)`，将服务存活与 `run_telemetry.adapter` Schema 兼容解耦。
  - [`autodev/server/routers/runtime.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/routers/runtime.py)：`/vernal-api/runtime/status` 仅基于当前项目 Run 暴露 `loopforge.schema_ready` 与 `loopforge.schema_diagnostics`。
  - [`autodev/server/proxy.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/proxy.py)：透传 `?adapter=` 参数与上游 400/500 错误（不伪造空 200），命中 MySQL 1054 `adapter` 缺列时附加响应头 `X-Vernal-Upstream-Diagnostic: upstream-schema-column-missing:run_telemetry.adapter`。
  - [`deploy/loopforge/doctor.sh`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/deploy/loopforge/doctor.sh)：新增 `INFORMATION_SCHEMA.COLUMNS` 只读检查项。
  - 新增回归：`tests/test_r2_schema_diagnostics.py`。
- **生效方式**：重启 Vernal Python 后端；外部库表与查询根因须等待上游 `ghcr.io/tencent/loopforge` 镜像升级。

---

### R3：分离文档内容有效性与审批前置条件（`b0fd590`）

- **实际根因**：旧版 `_hydrate_gate_row()` 与 `target_is_current()` 把内容哈希校验与审批前置条件（检查运行中、未答问题、质量检查缺失/过期/严重发现、前置条件未核验）混在一起，导致检查运行中或未达前置条件时误把门禁状态覆写为 `STALE`（文档过期），误导页面提示重建文档。
- **修改文件**：
  - [`autodev/speckit/missions.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/missions.py)：新增只读 `approval_readiness()` 与 `_update_lease_step()`；`assert_ready_for_approval()` 改为薄包装；`view()` 在存在 `CRITICAL` 或缺失发现元数据时返回 `CHECKLIST_BLOCKED` 而非 `WAITING_APPROVAL`。
  - [`autodev/process/targets.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/process/targets.py)：新增 `evaluate_document_content()`、`content_is_current()`、`evaluate_gate_record()`；区分 `PENDING` 决策前检查（含运行中阻塞）与已 `PASSED/WAIVED` 有效性的实质阻塞（允许批准后的 `plan`/`tasks`/`analyze` 与编码派发正常进行）；保留 `target_is_current()` fail-closed 语义。
  - [`autodev/process/runner.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/process/runner.py)：`_hydrate_gate_row()` 在 `content_current=True` 且仅前置条件未满足时保持 `status="PENDING"`，透出 `content_current`、`ready_for_decision`、`can_approve`、`approval_valid`、`blockers`；`decide_gate()` 事务内二次核验，旧页/直接 HTTP/并发提交均无法绕过。
  - [`autodev/server/routers/process.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/routers/process.py)：路由层 `_overlay_gate_permissions()` 叠加 RBAC 角色权限（`can_approve`、`can_waive`、`required_roles`、`PERMISSION_DENIED`）。
  - [`autodev/server/static/js/pipeline.js`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/static/js/pipeline.js) & [`autodev/server/static/js/missions.js`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/static/js/missions.js)：按阻塞码分别呈现操作指引。
  - 回归测试：`tests/test_process_gates.py`、`tests/test_missions.py`、`tests/test_pipeline_ui.mjs`。
- **生效方式**：重启 Vernal 后端并强刷浏览器（已递增 `OVERLAY_VERSION = "20261126p2v1"`）；无需数据库迁移。

---

### R4：质量发现向 `plan`/`analyze` 传播与预设显式升级（`dcc7b23`）

- **实际根因**：`adapter.run_step()` 在 `plan`/`analyze` 中未注入当前 `checklist` 及 `speckit.analysis:<rev_id>` 结构化发现；`input_fingerprint()` 与 `publish_revision()` 未将 `checklist` 纳入依赖签名与下游失效传播；`missions.js` 缺失独立于 `last_step` 的质量检查折叠区；旧版预设 v1.0.0 易导致宪章与规格重复维护业务 REQ/NFR。
- **修改文件**：
  - [`autodev/speckit/service.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/service.py)：新增 `checklist_findings_digest()`（区分缺失 `__MISSING__` 与空数组）和 `quality_review()`（生成稳定不可变 ID `CHK-<rev_id>-<idx:02d>`，当前与历史分离）；将 `checklist` 加入 `plan`/`tasks`/`analyze` 的 `input_fingerprint()`；`publish_revision()` 增加 `checklist -> ["plan", "tasks", "analyze"]` 下游失效传播与内容/发现完全一致时的幂等复用，并在当前有未结发现时校验 `plan` 必须包含 `## 检查发现处理` 及全部 `CHK-...` 标识。
  - [`autodev/speckit/adapter.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/adapter.py)、[`validate_output.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/validate_output.py)、[`validate_output.cjs`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/validate_output.cjs)：将当前清单与结构化发现表传入 `plan` 和 `analyze`，通过 `x-vernal-require-findings-ids` 在 Python 与 Node 双校验器强校验 `## 检查发现处理`。
  - [`autodev/models.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/models.py) & [`autodev/speckit/compiler.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/compiler.py)：`CompiledWorkPackage` 新增 `checklist_rev_id`/`checklist_sha`/`checklist_findings_sha` 冻结绑定；`compile_tasks()` 与 `assert_package_current()` 核验清单有效性与适用 P0 `NFR` 覆盖。
  - [`autodev/server/routers/speckit.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/routers/speckit.py)、[`autodev/speckit/missions.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/missions.py)、[`autodev/server/static/js/missions.js`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/static/js/missions.js)：接口独立返回 `quality_review`；前端渲染折叠区 `#mission-quality-review`；新增 `POST /vernal-api/specifications/upgrade-assets`。
  - 预设与清单升级：[`vernal-core.json`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/assets/speckit/presets/vernal-core.json) v1.1.0（SHA-256 `16ba816a...`）、[`vernal-lite.json`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/assets/speckit/presets/vernal-lite.json) v1.1.0（SHA-256 `3264e02a...`）、[`manifest.json`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/assets/speckit/manifest.json)、[`autodev/speckit/assets.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/assets.py) 新增 `upgrade_assets()`（保留旧不可变资产目录，写审计历史 `speckit.assets.history`）。
  - 新增回归：`tests/test_r4_quality_propagation.py`、`tests/test_speckit_step_output.py`、`tests/test_missions_ui.mjs`。
- **生效与迁移**：新项目直接锁定 v1.1.0；既有项目通过 `POST /vernal-api/specifications/upgrade-assets` 或 `POST /vernal-api/missions/{feature}/convert-profile` 显式升级，不原地篡改旧资产锁目录。

---

### R5：试点七条提醒（W1–W7）与输入契约处理（`5482455` + 隔离试点）

1. **Vernal 本体修复（A09 Unicode 码点一致）**：
   - 修复 [`autodev/server/static/js/missions.js`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/static/js/missions.js) 中原 `answer.length > 10000` 按 UTF-16 码元计数的偏差，改为 `codepointLength()`（`Array.from(s).length`）与 Python `len()` 严格一致（5,001 个 `😀` 在两端均为 5,001 码点通过，10,001 个均拒绝）；补齐 `tests/test_missions_ui.mjs` 与 `tests/test_missions.py` 回归，并验证整轮原子保存与 CAS 冲突检测保留完好。
2. **隔离试点 `p2-remediation-pilot` 逐项落实 W1–W7**：
   - 经 `service.publish_revision()` 发布 `spec` 第 4 版（`rev_id=6`，SHA `7aaf7eeb...`），**逐字保留 `## 澄清 Q-01 · 1` 与 `## 澄清 Q-02 · 2` 确认原文及历史 rev 1–3**：
     - **W1**：补齐 `REQ-001/002/005` 成功晚于持久化写入反馈、拒绝/写入失败不显示成功且不覆盖旧值。
     - **W2**：在 `REQ-002` 增加已有记录状态下全空与仅说明提交的独立验收（A04）。
     - **W3**：明确“首次保存”= 该题无已保存记录（无效提交被拒后仍可首次保存），统一缺省/`null -> ""`、纯空白保留原文、10000 Unicode 码点上限及逐字相等口径。
     - **W4**：在 `REQ-003` 覆盖重复提交与响应丢失后逻辑重试（A06），通过原子唯一约束去重。
     - **W5**：区分 `empty` / `saved` / `validation_error` / `save_error` / `read_error`（读取失败不伪装为无记录），接口契约写入 `plan`。
     - **W6**：在规格 `## 需求` 中显式登记可解析的 `### NFR-001 [P0] 首版格式基线与后续兼容约束`（`schema_version=1`，首版无历史旧格式样本，迁移测试明确记 N/A）。
     - **W7**：在 `plan` 建立 5 条 P0 `REQ` + `NFR-001` 与接口、工作包及 `A01–A11` 自动化用例的双向映射表。
   - **已采用的可逆工程默认细节**：稳定 `question_id` + 归属 `option_id` 校验；SQLite `PRIMARY KEY(question_id)` + `ON CONFLICT DO UPDATE` 原子去重；显式判别返回结构。
   - **集中待负责人确认的业务承诺（不代作决定）**：
     - **B1（持久化期限）**：首版仅承诺单机工作区持久化并在同一使用环境刷新读回；关闭浏览器重启或跨会话的长期保留策略是否沿用工作区数据库待确认。
     - **B2（跨设备范围）**：当前列为 Non-Goal（不做跨设备同步），若需多端漫游须另立需求。
     - **B3（正式签收）**：正式产品规格审批与发布签收须由负责人在产品权限门禁中亲自完成。

---

### R6：P2 后半段隔离试点验证与边界说明

- **严格区分两类试点**：
  1. **独立演示试点（`workspaces/p2-remediation-pilot`）**：已完成真实 Vernal 引擎的 **G0**（基线登记 + 显式升级至 `vernal-lite@1.1.0` / `quick-prototype`）、**G1**（`spec` rev 4 + `checklist` rev 2，保留 rev 1 的 7 条历史发现）、**G2**（隔离 RBAC `decide_gate` round 6 `PASSED`）、**G3/G4**（`plan` rev 8、`tasks` rev 9、`analyze` rev 10、冻结编译并幂等发布 `SK-001-initial-answer-save`，`compiled_task_hash = 11d2a9539236e088db453b5db66bfda963d37080ca3898595bebc9bd26833783`，绑定 `checklist_sha = 7c5a9d31...` 并覆盖 `REQ-001..005` + `NFR-001`）、以及 **G5/G6 本地参考候选与 A01–A11 真实测试**（`pytest tests/test_answer_save_a01_a11.py` 6 passed、`node --test tests/test_answer_ui_a09.mjs` 2 passed，经 `FileExchange.patch` 冻结并核对 `candidate_digest = b11f2524bda61560b041e421189155b6f7dc7689bcdb8122412bf105d8209f8a`，测过版本与交付对象摘要一致）。
  2. **Vernal 自身增量开发试点**：尚未另建绑定 Vernal 隔离源码副本的存量项目，**不将上述独立演示试点成果混记为 Vernal 平台自举完成**。
- **未完成的外部依赖项（诚实保留）**：
  - 受 R1 外部 `agentflow-runner 0.10.2` MCP 审批策略缺陷阻塞，未执行原生 LoopForge + Codex 的无人值守 `plan`/`tasks`/`analyze`/编码/独立审查实跑；待上游 Runner 解除 R1 阻塞后，即可复用当前检查点补跑 G5 实机闭环。

---

## 三、性能诊断与整改结果（R7，独立报告）

### 3.1 既有真实运行分段取证（`p2-ui-20261126` / `run-1795681157951651640`）

通过只读关联既有 `task_runs`、MySQL `runner_tasks` 与 `run_logs`，明确拆分 `checklist` 总耗时 **462s**（16:19:17 → 16:26:59）：

| 阶段 / 节点 | 观测时长 | 证据与归因 |
| --- | --- | --- |
| 派发至 `draft` 开始（控制面准备） | ~41s | LoopForge 0.4.0 外部控制面（输入准备与沙盒检查） |
| `draft` 第 1 次尝试（模型 + 工具） | 214.2s | 含 ~40s 分 5 次 Shell 读取 41.8 KB 膨胀 `PLAN_PROMPT.md`，及 ~70s 扫描 `/opt/homebrew` 与 `pip cache` 搜索 `ajv`/`jsonschema` |
| `draft` 权限拒绝后无效会话恢复重试 | **129.3s** | Codex 拒绝 `submit_gate_result` 后 LoopForge 0.4.0 自动恢复会话再撞拒绝 |
| `validate` 脚本节点（总观测 78s） | 脚本实际 **0.170s** + 控制面 **~77.8s** | `runner_tasks` `rt-1795681582418274092-validate-script` 实测执行仅 170ms（log #49→#51 仅 5ms）；前置 +41.5s 准备与后置 +36.6s 收尾/产物发布位于 LoopForge 0.4.0 外部控制面（未知内部细项标为 `unknown_upstream_control_plane`） |
| Token 统计偏差（实证） | **2× 重复累计 + 评估记 0** | LoopForge API 返回 `run.usage`（548,157 in）与 `nodes.draft.usage`（548,157 in）完全一致，`extract_run_usage()` 直接相加记为 `1,096,314`；`triage.assess()` 未向 `finish_run()` 传用量导致实耗 `437,087 in / 4,440 out` 在 DB 记为 `0` |

### 3.2 已实施的 R7 代码修改（`9773371`）

- **P7-1（完成协议与无效重试）**：与 R1 共用统一分类策略，显式权限拒绝立即终止不再浪费业务侧轮询/误批准；提示词明确告知工具被拒时正常落盘退出。
- **P7-2（确定性校验 3× 同产物双运行时回放）**：使用真实 `run-1795681157951651640` 产物（9,555 B）与 Schema（4,519 B）在隔离目录各热回放 3 次：
  - Python `validate_output.py`：`[0.0457s, 0.0277s, 0.0280s]`，中位数 **0.0280s**，退出码 `[0, 0, 0]`（满足 `< 1s`）。
  - Node `validate_output.cjs`：`[0.0580s, 0.0559s, 0.0565s]`，中位数 **0.0565s**，退出码 `[0, 0, 0]`（满足 `< 1s` 且结果一致）。
- **P7-3 & P7-6（按步骤精简上下文、去重与签名绑定）**：
  - [`autodev/speckit/assets.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/assets.py)、[`triage.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/triage.py)、[`adapter.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/adapter.py)、[`loopforge_harness.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/harnesses/loopforge_harness.py)：用平台维护的中文步骤方法要点替代 10–38 KB 原始英文上游 CLI 控制脚本；去重重复嵌入的 wish/slice；外置 `PLAN_PROMPT.md` 不再重复整份 `.vernal-schema.json`；明确告知下游脚本自动做 Schema 校验，禁止搜索 `ajv`/`jsonschema`；将 `PROMPT_ASSEMBLY_VERSION` 纳入派发签名；缓存规则摘要跳过同内容冗余 `PUT /api/rules`。
  - **同类真实试点输入体积对比（优化前 `p2-ui-20261126` vs 优化后隔离试点）**：

| 步骤 | 优化前 `PLAN_PROMPT.md` | 优化后 `PLAN_PROMPT.md` | 降幅 | 优化前 Bundle | 优化后 Bundle | 降幅 |
| --- | --- | --- | --- | --- | --- | --- |
| `intake` | 54,292 B | 15,881 B | **-70.7%** | 58,940 B | 19,346 B | **-67.2%** |
| `constitution` | 20,106 B | 10,304 B | **-48.8%** | 26,605 B | 15,997 B | **-39.9%** |
| `checklist` | 41,812 B | 18,763 B | **-55.1%** | 48,845 B | 24,574 B | **-49.7%** |

  > 注：优化后测量基于已扩充 W1–W7 与 `NFR-001` 的更大规格第 4 版，且全部质量发现、用户回答原文与强制原则零丢失。

- **P7-4（按步骤/角色/风险可配置推理强度，单独记录）**：
  - [`autodev/config.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/config.py) & [`autodev/loopforge/bootstrap.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/bootstrap.py)：新增 `reasoning_effort_policy`（默认 `adaptive`）及分角色覆盖 `effort_for()`；`director`/`coder`/`reviewer`/高风险及 `plan`/`tasks`/`analyze`/`constitution` 保持 `high`，轻量 `pm`/`qa_lead` 评估与清单默认 `medium`；不修改 `Settings.codex_extra_args` 默认值与 `~/.codex/config.toml`。
  - 实机 `medium` vs `high` 耗时与质量单独对照试验因外部 R1 Runner 阻塞尚未开展，后续解除阻塞后须在同类输入下单独采样对照（小样本不报 p95）。
- **P7-5（轻量建议与实际锁定配置显式对齐）**：
  - [`autodev/speckit/missions.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/missions.py)、[`routers/speckit.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/routers/speckit.py)、[`missions.js`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/server/static/js/missions.js)：新增 `config_alignment` 明确展示推荐 vs 实际锁定差异原因，新增 `convert_profile()` 及 `POST /vernal-api/missions/{feature}/convert-profile` 显式转换入口（零任务且非自定义流程时可用，保留旧不可变资产锁、原规格/回答及阻断型 `srs_approval` 门禁）。
- **P7-7（用量去重、未知标识与单调阶段计时）**：
  - [`autodev/harnesses/base.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/harnesses/base.py)、[`bridge.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/loopforge/bridge.py)、[`loopforge_harness.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/harnesses/loopforge_harness.py)、[`triage.py`](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl/autodev/speckit/triage.py)：`extract_run_usage()` 优先采用 Run 级汇总、缺失时才对节点求和，新增 `telemetry_known` 与 `source` 区分未知与真实零值；`triage.assess()` 成功与失败路径均持久化 Token 用量与 `session_id`；`poll_loopforge_run()` 与 `LoopForgeHarness` 在每次运行的 `log_dir/timing.json` 落盘单调阶段耗时、节点状态跃迁与 `unknown_intervals`。

### 3.3 R7 未达成项（如实保留）

1. **原生 `validate` 节点非脚本开销中位数 ≤ 5s 目标未达成**：经实测脚本自身仅 0.170s，剩余 ~77.8s 位于外部 `ghcr.io/tencent/loopforge:0.4.0` 控制面（输入物化/沙盒镜像检查/节点收尾与产物发布）。本仓库无权修改外部镜像，须由上游 LoopForge 优化本地脚本节点执行路径（复用已就绪 Run 沙盒、消除无意义的 `sandbox.image.building` 探测等待）。
2. **实机端到端耗时前后实测及 `medium` vs `high` 单独对照未采集**：受外部 R1 Runner MCP 审批策略阻塞，未发起大规模在线模型实跑，避免浪费无效重试。

---

## 四、全量回归验证结果

在 `/Users/zomeryang/Documents/春天/个人科研/lattice-mesh/workspaces/p2-remediation-impl` 执行（2026-11-26）：

```bash
/Users/zomeryang/Documents/春天/个人科研/lattice-mesh/.venv/bin/python -m pytest
# -> 308 passed, 1 warning in 118.07s (较基线 284 项净增 24 项针对性回归)

node --test tests/*.mjs
# -> 14 pass, 0 fail (较基线 8 项净增 6 项前端行为回归)

git diff --check
# -> exit 0，无空白/格式问题
```

---

## 五、分层生效与后续升级指引

| 层级 | 改动范围 | 生效方式（避免无谓全局重启） |
| --- | --- | --- |
| **前端静态资源** | `pipeline.js`、`missions.js`、`proxy.py` (`OVERLAY_VERSION=20261126p2v1`) | 重启 Vernal Web 服务后浏览器刷新自动命中新版本参数 |
| **Vernal Python 后台** | `autodev/loopforge/*`、`autodev/process/*`、`autodev/speckit/*`、`autodev/server/*`、`autodev/config.py`、`autodev/models.py`、`autodev/assets/speckit/*` | 重启 Vernal 后端进程（无需库表迁移；既有项目资产锁需显式调用升级/配置转换接口） |
| **本地运维脚本** | `deploy/loopforge/start-runner.sh`、`deploy/loopforge/doctor.sh` | 直接执行即可生效，无需重启容器 |
| **外部 LoopForge Server & Runner** | `ghcr.io/tencent/loopforge:0.4.0`、`agentflow-runner 0.10.2` | **待上游发布**：① 为 Codex 节点注入 `agentflow-gate` MCP 授权或支持受控完成协议（解除 R1/R6 实跑阻塞）；② 修复 `run_telemetry.adapter` 查询/官方迁移（解除 R2 观测告警）；③ 消除本地脚本节点 ~77.8s 控制面准备/收尾开销（达成 R7 ≤ 5s 目标） |
