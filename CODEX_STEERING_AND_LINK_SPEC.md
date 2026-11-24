# Vernal 下一阶段架构升级与一致性收口工程任务书（供 Codex 直接执行）

> **文档定位**：本方案同时收口两件事：
> 1. **彻底根治阶段评审（Steering）、局部返工与二次交付的 8 项状态机与并发一致性隐患**（消除驳回旧草案回魂、远端审批 503 脑裂、并发建双份任务、取消任务编号复用、派单假死、里程碑卡 `STALLED`、二次交付分支冲突及输入框丢字）；
> 2. **打通跨平台（Windows 原生 / WSL / macOS · 个人本机 / 团队网页）优雅一致的使用路径**——建立一等公民「版本化交付账本（`milestone_deliveries`）」与轻量跨平台端侧伴侣 **`vernal link`**，在不魔改 LoopForge 上游 Go 源码的前提下，让无 Docker、无 WSL 的 Windows / Mac 成员也能直接改本机目录或复用本机已登录 CLI。

---

## 0. 全局工程红线（动工前必读）

1. **不魔改、不重编译 LoopForge 上游 Go 源码**（`ghcr.io/tencent/loopforge:0.4.0`）：说明一下，LoopForge 官方镜像 `scripts/build-binaries.sh` 仅预编译了 `linux` 与 `darwin` 的 `agentflow-runner`，不存在 `windows/amd64` 原生二进制。因此 Windows 原生算力与目录接入统一通过 Vernal 网关侧协议与 `vernal link` 实现。
2. **SQLite 迁移必须 100% 幂等向后兼容**：已有工作区的 `.autodev/state.db` 启动时不得报错；新增表使用 `CREATE TABLE IF NOT EXISTS`，已有表加列在 `Database._init_schema()` 中检查 `PRAGMA table_info(...)` 后按需 `ALTER TABLE`。
3. **前端严格遵守 LoopForge CSP**：严禁在 HTML 字符串中写 `onclick=` / `onchange=` 等内联事件；修改 `autodev/server/static/styles.css` 后必须执行 `.venv/bin/python scripts/build_vernal_stage_css.py` 重新生成 `vernal-stage.css`，并同步递增 `autodev/server/proxy.py` 与 `autodev/server/static/vernal-overlay.css` 中的 `OVERLAY_VERSION`。
4. **保持既有 94 项 Pytest 全绿**，不破坏 `files` 与 `git` 两种既有协作模式。

---

## WP-1（P0）：带版本代数的演进评审状态机与原子锁（根治旧草案回魂 / 503 脑裂 / 并发双写 / 派单假死）

**涉及文件**：`autodev/db.py`、`autodev/steering.py`、`autodev/server/app.py`

### 1.1 数据库 Schema 扩展（`autodev/db.py`）
在 `evolution_checkpoints` 表幂等补齐以下字段：
- `plan_revision INTEGER NOT NULL DEFAULT 0`：当前有效草案代数（每次从远端读到新的合法草案时 `+1`）；
- `min_director_rework INTEGER NOT NULL DEFAULT 0`：驳回水位线，仅接纳 `director_plan` 节点的 `reworkCount >= min_director_rework` 的产物；
- `draft_artifact_id TEXT`：最近采纳的 `steering-plan.json` 产物 ID 或内容 SHA-256 指纹（防同一旧产物重复采纳）；
- `rejected_plans_json TEXT NOT NULL DEFAULT '[]'`：历次被驳回草案的归档数组；
- `last_error TEXT`：派单或同步失败的具体原因。

同时在 `tasks` 表幂等新增：
- `source_checkpoint_id INTEGER`、`source_item_idx INTEGER`，并建唯一索引 `CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_checkpoint_item ON tasks(source_checkpoint_id, source_item_idx) WHERE source_checkpoint_id IS NOT NULL`（从数据库层彻底杜绝同一次评审重复插入任务）。

### 1.2 评审全生命周期状态机
状态集合收敛为：`DISPATCHING` → `DISPATCH_FAILED` | `RUNNING` → `AWAITING_APPROVAL` → `SYNC_PENDING` | `APPLYING` → `APPLIED` | `FAILED`。

1. **分步持久化防派单假死（`start_steering_review`）**：
   - 创建记录时初始状态为 `DISPATCHING`（而非 `RUNNING`）。
   - 调用 `client.create_issue(...)` 成功后**立即落库 `loopforge_issue_id`**；随后调用 `client.dispatch_issue(...)` 成功后**立即落库 `loopforge_run_id` 并将状态更新为 `RUNNING`**。
   - 任一步骤抛出 `LoopForgeError` / 网络异常时，捕获并更新状态为 `DISPATCH_FAILED`、写入 `last_error`，再向上抛出 HTTP 错误（前端绝不再悬挂假 `RUNNING`）。
   - 新增 `POST /vernal-api/steering/{checkpoint_id}/retry`：对 `DISPATCH_FAILED` 记录，若已有 `loopforge_issue_id` 则直接补调 `dispatch_issue`，否则重建工单并派发。

2. **驳回即作废旧草案（`reject_steering_checkpoint`）**：
   - 读取远端 Run 当前 `director_plan` 节点的 `reworkCount`（记为 `cur_rework`）。
   - 调用 `client.reject_run(run_id, reason=..., target_node="director_plan", node_id="human_gate")` **成功返回后**：
     - 将当前非空 `draft_plan` 追加进 `rejected_plans_json`；
     - **将 `draft_plan` 置空为 `{}`**，`min_director_rework = cur_rework + 1`，状态更新为 `RUNNING`。

3. **严格按代数对账，禁止回退旧草案（`reconcile_steering_checkpoint`）**：
   - 提取 `steering-plan.json` 时，校验 `director_plan` 节点的当前 `reworkCount >= cp["min_director_rework"]` 且产物指纹不同于 `rejected_plans_json` 中已作废版本。
   - 新草案有效时：递增 `plan_revision` 并保存 `draft_plan` 与 `draft_artifact_id`。
   - **关键红线**：**删除“读不到远端产物时回退使用旧 `cp['draft_plan']` 去执行 `apply`”的逻辑**！若远端 Run 状态已变为 `succeeded`，但本地尚未拿到满足 `min_director_rework` 的非空 `draft_plan`（例如产物读取瞬时超时），将状态置为 `SYNC_PENDING` 等待下一轮轮询重试拉取，**绝不执行旧草案或空草案**。

4. **两阶段远端确认 + SQLite CAS 单次执行锁（`approve_steering_checkpoint` & `apply_steering_plan`）**：
   - `SteeringApproveRequest` 增加可选字段 `expected_revision: int | None = None`；若传入且与数据库当前 `plan_revision` 不一致，返回 `HTTP 409`（提示草案已更新，请确认最新版）。
   - **禁止吞掉 `approve_run` 异常**：调用 `client.approve_run(...)` 若抛 `LoopForgeError`（如 `503`），先调一次 `client.get_run(run_id)` 核实——仅当远端 Run 已明确是 `succeeded` 时继续；否则直接将异常转为 `HTTP 502/503` 抛给调用方，本地保持 `AWAITING_APPROVAL` 不动。
   - **CAS 原子抢占锁**：`apply_steering_plan` 入口在单事务内执行：
     `UPDATE evolution_checkpoints SET status='APPLYING' WHERE checkpoint_id=? AND status IN ('AWAITING_APPROVAL', 'RUNNING', 'SYNC_PENDING')`
     - 若 `cursor.rowcount == 0`：说明另一协程（如后台对账与手动批准并发到达）正在或已经应用，等待/重读该 checkpoint 并直接返回，确保 `apply_steering_plan` 副作用**严格只执行一次（Exactly-Once）**。
     - 若中途发生未预期异常，将状态回退至 `SYNC_PENDING` 并记录 `last_error`。

---

## WP-2（P0）：任务 `CANCELLED` 终态、单调发号器与局部返工里程碑重聚（根治编号复用与卡 `STALLED`）

**涉及文件**：`autodev/models.py`、`autodev/db.py`、`autodev/steering.py`、`autodev/scheduler.py`、`autodev/engine.py`

### 2.1 引入一等公民 `TaskStatus.CANCELLED`（禁止物理删除任务）
1. 在 `autodev/models.py` 的 `TaskStatus` 枚举中新增 `CANCELLED = "CANCELLED"`。
2. 在 `autodev/steering.py` 处理 `cancel_tasks` 时：
   - **删除 `DELETE FROM tasks WHERE task_id=?`**，改为 `engine.db.update_task(tid, status=TaskStatus.CANCELLED, assigned_slot=None, active_agent_label=None, steer_note=f"[评审 #{round_no} 已取消]")`。
   - 原任务在 `runs` 与 `events` 中的历史完整保留，不再污染后续任何任务。
   - 下游依赖清理保持不变（从其他任务的 `depends_on` 中剔除已取消任务并按需解锁 `BLOCKED -> READY`）。

### 2.2 持久化单调递增任务发号器 `db.next_task_id()`
1. 在 `autodev/db.py` 实现原子方法 `next_task_id() -> str`：
   - 若 `meta["task_seq"]` 不存在，扫描 `tasks.task_id`、`runs.task_id`、`events.task_id` 中所有形如 `T-(\d+)` 的数字最大值初始化 `seq`；
   - 在写锁/事务内将 `seq + 1` 写回 `meta["task_seq"]` 并返回 `f"T-{seq + 1:03d}"`。
2. `steering.py` 新增任务（及 `engine.py` 动态补丁单如适用）统一调用 `engine.db.next_task_id()`，确保任务编号**终生只增不复用**。

### 2.3 修复局部返工完成后的里程碑重聚判定（消除卡 `STALLED`）
当前 `autodev/scheduler.py` 第 158 行写死 `all(t.status == TaskStatus.APPROVED for t in ms_tasks)`，导致 `M1` 首次合入后（保留任务为 `MERGED`、返工任务跑完变为 `APPROVED`）永远无法再次进入集成，最终把项目打成 `STALLED`。修改如下：
1. 在 `scheduler.py` 主循环（`ms.status == MilestoneStatus.PENDING` 分支）：
   - 定义有效任务 `active_tasks = [t for t in ms_tasks if t.status != TaskStatus.CANCELLED]`；
   - 若 `not active_tasks`（该里程碑任务全被取消），直接将里程碑标为 `MERGED` 并继续下一里程碑；
   - 若 `active_tasks` 非空、`not self.running_tasks`，且 `all(t.status in (TaskStatus.APPROVED, TaskStatus.MERGED) for t in active_tasks)`，立即调用 `await self._integrate_milestone(ms)`！
2. 在 `_integrate_files(ms)` 与 `_integrate_milestone(ms)`（Git 模式）中：
   - 统一先过滤 `tasks = [t for t in self.db.list_tasks(milestone_id=ms.milestone_id) if t.status != TaskStatus.CANCELLED]`；
   - Git 模式下第 912 行 `if not gitops.is_ancestor(int_wt, sha):` 已天然兼容已在 `base_branch` 里的 `MERGED` 任务，只需确保保留任务的 `candidate_commit_sha` 未被清空；
   - `files` 模式下 `FileExchange.baseline(...)` 确保包含已 `MERGED` 保留任务与最新 `APPROVED` 返工任务的合成快照。

---

## WP-3（P0）：版本化交付账本（`milestone_deliveries`）与二次交付无损推进

**涉及文件**：`autodev/db.py`、`autodev/local_git.py`、`autodev/local_git_bridge.py`、`autodev/engine.py`、`autodev/server/app.py`

### 3.1 新增 `milestone_deliveries` 表（`autodev/db.py`）
```sql
CREATE TABLE IF NOT EXISTS milestone_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    collab_mode TEXT NOT NULL,          -- 'git' | 'files'
    commit_sha TEXT,                    -- Git 交付提交 SHA
    prev_commit_sha TEXT,               -- 上一版成功交付的 SHA（用于精确 lease）
    artifact_digest TEXT,               -- Files 交付快照摘要
    rolling_branch TEXT,                -- 如 vernal/<pid>/M1
    archive_branch TEXT,                -- 如 vernal/<pid>/M1/v2（不可变归档引用）
    compare_url TEXT,                   -- 远端 GitHub/Gitea Compare 或 PR 链接
    created_at TEXT NOT NULL,
    UNIQUE(milestone_id, version_no)
);
```
提供 `db.record_milestone_delivery(...)` 与 `db.list_milestone_deliveries(milestone_id=None)`，并将 `deliveries` 列表随 `engine.snapshot()` 返回前端。

### 3.2 Git 模式双轨交付：精确 Lease 滚动分支 + 不可变 `v1/v2` 分支
修改 `autodev/local_git.py` 的 `publish(...)` 与 `autodev/local_git_bridge.py` 的 `push_remote_branch(...)` / `deliver(...)`：
1. 计算本次版本号 `version_no = len(prev_deliveries) + 1`，上一版提交 `prev_sha = prev_deliveries[-1]["commit_sha"] if prev_deliveries else ""`。
2. **第一轨（必成功·不可变版本分支）**：推送 `archive_branch = f"{prefix}{milestone_id}/v{version_no}"`（使用空 lease `--force-with-lease=refs/heads/{archive_branch}:`，因 `v{N}` 每次递增，绝不冲突，永久保留每一版供用户 `git diff v1..v2`）。
3. **第二轨（便捷合入·滚动交付分支）**：目标为 `rolling_branch = f"{prefix}{milestone_id}"`：
   - 若 `version_no == 1`（无前序交付）：使用空 lease `--force-with-lease=refs/heads/{rolling_branch}:`；
   - 若 `version_no > 1` 且有 `prev_sha`：使用**精确前序 SHA 租约** `--force-with-lease=refs/heads/{rolling_branch}:{prev_sha}`（远端桥接同理：若 `ls-remote` 返回的远端当前 SHA 等于 `prev_sha`，允许更新至新 `commit`）！
   - **优雅降级**：若用户在 `v1` 交付后自己在 `vernal/<pid>/M1` 分支上追加了手工提交导致租约不匹配，**不要让 `merge_milestone` 失败回滚**！捕获该分支的租约不匹配，保留已成功推送的 `archive_branch`（`vernal/<pid>/M1/v2`），写入事件提示：“检测到交付分支存在人工新提交，已保留人工提交并将第 2 版单独发布至 `vernal/<pid>/M1/v2`”。
4. **远端 Compare / PR 链接生成**：若项目绑定了 GitHub / GitLab / Gitea HTTPS URL，自动推导 `compare_url`（如 GitHub/Gitea `.../compare/<base>...<branch>`）存入 `milestone_deliveries.compare_url`。

### 3.3 Files 原地接管模式（`local_folder`）二次写回修复
当前 `autodev/engine.py` 第 961 行每次合并都拿 `exchange.baseline([])`（即 `t=0` 初始导入快照）做磁盘冲突比对——这会导致 `M1` 第 2 版写回时，把第 1 版由平台自己写回的文件误判为“用户手工修改冲突”！
- **修复**：若 `milestone_deliveries`（或 `meta["last_inplace_digest"]`）中存在上一次成功写回原目录的快照 `last_synced_digest`，则以 `exchange.get(last_synced_digest)` 作为三路比对的 `prev_files` 基线；写回成功后更新 `meta["last_inplace_digest"] = key`。
- 支持按具体版本号下载历史 ZIP：`GET /vernal-api/milestones/{milestone_id}/files.zip?version={version_no}`。

---

## WP-4（P0）：前端审批草稿防丢字、异常重试与多版本交付可视化

**涉及文件**：`autodev/server/static/js/steering.js`、`autodev/server/static/js/dag.js`、`autodev/server/static/js/runtime.js`、`autodev/server/static/styles.css`

1. **驳回意见输入框草稿保活与免重绘（`steering.js`）**：
   - 维护模块级字典 `const rejectDrafts = new Map()`（`checkpoint_id -> string`），监听 `#st-reject-reason-input` 的 `input` 事件实时写入。
   - 在 `approvalCard` 上记录 `approvalCard.dataset.renderKey = `${pendingCp.checkpoint_id}:${pendingCp.plan_revision}:${pendingCp.status}:${Object.keys(pendingCp.opinions || {}).sort().join(",")}`。
   - 后台任务 SSE 触发 `renderAll()` 时，若 `approvalCard.dataset.renderKey` 未变且 `!approvalCard.hidden`，**直接 `return` 跳过 `approvalCard.innerHTML` 重建**，100% 保持用户正在输入的文字、光标位置与焦点！即使版本升版重绘，也从 `rejectDrafts.get(pendingCp.checkpoint_id)` 自动恢复已输入草稿。
   - 点击「批准并同步到 LoopForge」时，请求体带上 `expected_revision: pendingCp.plan_revision`。
2. **派单失败重试与同步中提示（`steering.js`）**：
   - 当演进主线存在 `DISPATCH_FAILED` 节点时，节点显示红色状态并在详情区提供「↻ 重试发起并发评审」按钮（调用 `POST /vernal-api/steering/{id}/retry`）；
   - 当节点处于 `SYNC_PENDING` 时，提示“远端已通过，正在同步最新变更单产物…”。
3. **已取消任务与多版本交付记录展示（`dag.js` & `runtime.js`）**：
   - `dag.js` 为 `CANCELLED` 状态任务渲染浅灰删除线样式（不混同于 `FUSED` 或 `READY`）；
   - `runtime.js`（收件箱与里程碑交付区）按 `state.deliveries` 展示 `v1`、`v2` 交付历史标签：Git 模式展示 `vernal/<pid>/M1` 分支名、`v1/v2` 归档分支与「去创建/查看合并请求」链接；Files 模式支持按 `v1` / `v2` 分别下载 `.zip`。
4. 完成后运行 `.venv/bin/python scripts/build_vernal_stage_css.py` 并递增 `OVERLAY_VERSION`。

---

## WP-5（P1）：轻量跨平台端侧伴侣 `vernal link`（免 Docker、免 WSL 打通 Windows / Mac 本机目录与个人 CLI）

**目标**：让没有 Docker、没有 WSL 的 Windows 或 Mac 同事，在访问团队服务器网页时，只需在自己电脑项目目录（如 `D:\Projects\demo`）运行一行命令，就能**直接原地双向同步该目录（无 4 MiB 限制、含二进制）**，并可选**直接调用本机原生 Windows Codex / Claude 个人额度**。

**涉及文件**：新增 `autodev/host_link.py`、`autodev/harnesses/link_worker.py`；扩展 `autodev/cli.py`、`autodev/server/app.py`、`autodev/server/static/js/launcher.js`

### 5.1 协议与安全模型（全出站 HTTPS 长轮询，穿透办公网 NAT）
1. **配对签发**：网页启动台「本机项目」新增第三个子标签 **「跨机 / Windows 本机桥接（`vernal link`）」**：
   - 点击生成一次性 10 分钟有效配对码 `POST /vernal-api/link/create-code` → 返回一行跨平台命令：
     - Python / uvx 环境：`python -m autodev.cli link http://<server>:8787 --code <CODE> --dir "D:\Projects\demo"`
2. **配对握手与目录同步（`autodev/host_link.py`）**：
   - `POST /vernal-api/link/exchange`：兑换专属 `link_id` 与 HMAC `link_token`（绑定当前登录用户与目标项目槽位）。
   - **上传基线**：
     - 若本机目录是 Git 仓库：打包当前 `HEAD`（`git bundle create`）流式上传至服务端，服务端克隆为规范 Git 工作区（原目录未提交改动完全保留）；
     - 若本机目录是普通目录：扫描非忽略文件（含二进制文件 Base64/Tar 流，上限放宽至 200 MiB），在服务端初始化为规范仓库并记录初始 `synced_manifest`。
   - **自动拉回多版本交付**：`vernal link` 守护进程每 2s 轮询 `GET /vernal-api/link/{link_id}/events`：
     - 一旦检测到里程碑产生新交付记录（`v1`、`v2`…）：
       - 本机为 Git 仓库时：下载增量 bundle，`git fetch` 写入本机 `refs/heads/vernal/<pid>/M1` 与 `refs/heads/vernal/<pid>/M1/v{N}`（绝不碰用户当前检出工作区）；
       - 本机为普通目录时：对比上次同步快照做本地三路检查，无冲突文件原子写回 `D:\Projects\demo`，冲突文件写入 `.vernal/conflicts/M1-v{N}/` 并在终端与网页双端告警。

### 5.2 原生跨平台个人算力桥（免 WSL 复用 Windows Codex）
当用户在 `vernal link` 开启 `--with-worker`（默认开启，若检测到本机装有 `codex / claude / opencode / dsh`）：
- **在 macOS / Linux 上**：`vernal link` 自动拉起并守护官方 `agentflow-runner` 二进制，走既有 `local_runner` 轨道。
- **在 Windows 原生（`sys.platform == "win32"`）上**：
  - `vernal link` 向服务端上报本机探测到的 Windows CLI（自动探测 `%APPDATA%\npm\codex.cmd` 与 `%USERPROFILE%\.codex\auth.json`，并将验证环境兼容映射 `python`）；
  - 新增 `LinkWorkerHarness`（`autodev/harnesses/link_worker.py`）：当项目绑定了在线 Windows `link` Worker 且选用个人执行池时，任务通过 `/vernal-api/link/{link_id}/tasks/claim` 与 `/complete` 分发给同事电脑上的 `vernal link` 进程，在 Windows 本机临时隔离目录调用原生 `codex.cmd` 完成编码与测试后回传补丁，同时向 LoopForge 写入工单状态以便全局看板统一追踪。

---

## WP-6：硬核验收标准与必写回归测试

Codex 完成开发后，必须新增并通过以下端到端自动化测试（以及保持全量 `pytest` 与 `node --check` 零报错）：

### 必测用例 1：`tests/test_rework_v2_and_idempotency.py`（覆盖全部 8 项审查问题）
在单个连贯测试中模拟完整真实故障链：
1. **首版交付**：项目 `M1` 含 `T-001`（数据库底座）与 `T-002`（鉴权），首轮运行完成并调用 `merge_milestone("M1")` → 断言原仓库生成 `vernal/<pid>/M1` 与 `vernal/<pid>/M1/v1`（`version_no == 1`）。
2. **驳回旧草案防回魂**：发起阶段评审 → 产出初版草案（含误伤 `reopen_tasks: ["T-001"]`） → 调用 `/reject` 驳回 → 模拟远端 Run 状态先变为 `succeeded` 但新版 `steering-plan.json` 暂时 `404` 读不到 → 调用对账接口，断言状态为 `SYNC_PENDING`、**绝未应用旧草案**、`T-001` 仍为 `MERGED`。
3. **远端 503 拦截 + 并发批准幂等**：新版草案就绪（仅退回 `T-002`、取消 `T-003`、新增 1 张新单）：
   - 第一次调 `/approve` 时注入远端 LoopForge `/approve` 返回 `HTTP 503` → 断言接口返回错误，本地仍保持 `AWAITING_APPROVAL`，未提前改库；
   - 恢复正常后，用 `asyncio.gather` **同时并发触发**手动 `/approve` 与后台 `reconcile_steering_checkpoint`（模拟重复通知并发到达） → 断言：
     - 仅新增 **1 张**新任务，且因 `T-003` 变为 `TaskStatus.CANCELLED`（未物理删除），新任务编号严格为 **`T-004`（不复用 `T-003`）**；
     - `T-001` 原封不动保持 `MERGED`，仅 `T-002` 变为 `READY`。
4. **局部返工重聚与二次交付**：驱动调度器跑完返工后的 `T-002`（变为 `APPROVED`） → 断言 `M1` **不卡 `STALLED`**，顺利完成二次集成进入 `AWAITING_MERGE` → 再次调用 `merge_milestone("M1")` → 断言：
   - 原仓库 `vernal/<pid>/M1` 通过精确 lease 平滑推进至第二版提交；
   - 同时存在 `vernal/<pid>/M1/v1` 与 `vernal/<pid>/M1/v2` 两个可对比版本，`milestone_deliveries` 表准确记录 `v1`、`v2` 两行。

### 必测用例 2：`tests/test_host_link_sync.py`（覆盖 `vernal link` 跨平台同步与 Windows Worker 契约）
1. 模拟远端非回环客户端通过配对码握手，上传普通目录与 Git 目录基线；
2. 服务端完成 `M1` 的 `v1` 与返工 `v2` 两次交付后，客户端轮询拉回：验证本机 Git 仓库收到 `vernal/<pid>/M1` 与 `v1/v2` 分支，普通目录在保留人工非冲突改动的前提下收到 `v2` 更新，人工修改同文件冲突时准确拦截并落入 `.vernal/conflicts/`；
3. 模拟 Windows `link` Worker 领单执行并回传补丁通过门禁。
