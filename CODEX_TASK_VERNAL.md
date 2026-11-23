# Vernal 系统收尾与端到端联调任务书（供 Codex 直接执行）

## 0. 项目总体目标与边界（必读）

LoopForge 是主项目。Vernal 是这套定制系统的名称。新项目、需求、工作流、运行、审批和统计都沿用 LoopForge 原生对象和入口。旧 Vernal 工程用于提取架构圆桌、任务拆解、研发分组和文件交接等能力，逐项接入对应的 LoopForge 项目与工作流。

每项改造从用户在 LoopForge 中选定的项目出发。需求在该项目的原生「需求列表」可查，实际执行可从相应需求追溯到运行和产物，用量与交付由原生「统计」按其口径呈现。以登录后的真实页面验收。旧工程的历史在迁移时保留原项目、执行时间和来源，历史执行与 LoopForge 新发起的运行分别归因。

本期验收游戏为《碎星航线》。飞船、陨石、护盾和计分的需求见 `examples/orbit-shooter/wish.md`。

### 严格约束（不可偏离）
1. **单一入口、基于原版 LoopForge 前端改造**：
   - 不再把旧版独立看板作为主入口，也不魔改/重编译 LoopForge 的 14 万行 Go 后端源码。
   - 用户只访问 `http://127.0.0.1:8787`（Vernal 同源反向代理网关），底层透明代理 `http://127.0.0.1:8090`（LoopForge 容器）。
   - **页面必须基于原版 LoopForge 前端**：完整保留 LoopForge 原生页面（AI 工作台、统计、需求列表、待我确认、设置、执行机器、流程模板等），同时：
     - 将网页标题与可见品牌文字 `LoopForge` 改为 **`Vernal`**（**保留原有的 Logo 图形符号不变**）；
     - 将整体视觉从 LoopForge 默认紫色（`#7b61ff`）改造为 **Vernal 精密工程蓝图浅色风格**（Cobalt `#1d4ed8` + Teal `#0d9488` + 点阵图纸质感）；
     - 在原版左侧导航栏 `#platformRail .rail-nav` 顶部新增 Vernal 自研模块入口：**架构总控 & DAG 流程图**、**多智能体架构评审圆桌（Council + ADR）**、**研发组领地与蓝图**，并在视图内展示**工位实况席**与**实时事件流**。
2. **真实执行走 LoopForge + 本机 Codex**：
   - 拆解出的任务必须真实派发到 LoopForge（创建 Issue + 触发 SOP Run），而不是绕开 LoopForge 跑本地 mock。
   - 复用浏览器同源登录 Cookie 自动签发并保存 `afp_` Token（`POST /vernal-api/runtime/bind`），严禁让用户手动复制粘贴 Token。
   - 通过 LoopForge 的个人执行机（`agentflow-runner`）接入当前 Mac 本机已登录的 Codex CLI（`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`，模型 `gpt-6.2-sol`，`model_reasoning_effort="high"`）。
3. **不涉及 Lattice Mesh 业务复刻，也不做动态生成每组不同 SOP 图**（使用标准交付 SOP，任务差异体现在描述、`allowed_paths` 与 `verify_cmds`）。

---

文件协作与 Git 协作可在立项前选择。文件模式无须初始化 Git、创建远程仓库或合并提交。上游任务交付可核验的文件版本，下游取得该版本，里程碑按测试和交付目录验收。已有工作区继续沿用原配置。两种方式共用规划、审批与运行记录。

## 1. 当前仓库已完成进度（请先核对，勿重复造轮子）

仓库根目录：`/Users/zomeryang/Documents/春天/个人科研/lattice-mesh`（Python 虚拟环境：`.venv/bin/python` / `.venv/bin/pytest`）。

### 已就绪并可直接复用
- **Docker 底座编排**：`deploy/loopforge/compose.yaml` 与 `deploy/loopforge/up.sh`
  - `loopforge` (`ghcr.io/tencent/loopforge:0.4.0`)：`127.0.0.1:8090`
  - `gitea` (`gitea/gitea:1.22`)：`127.0.0.1:3000`
  - `mysql` (`mysql:8.4`)：`127.0.0.1:33315`
- **核心编排大脑**：`autodev/{config,models,dag,db,ingest,gitops,verifier,prompts,council,engine,scheduler,cli}.py`
  - `autodev/config.py` 默认 `DEFAULT_CONFIG_TOML` 已切为 `runtime_backend = "loopforge"`，默认模型 `gpt-6.2-sol` + `high`。
- **LoopForge 客户端与桥接器**：
  - `autodev/loopforge/client.py`：已实现 `auth_current_with_cookie`、`create_token_with_cookie`、`create_project`、`create_issue`、`dispatch_issue`、`get_run` 等。
  - `autodev/loopforge/bridge.py`：已实现自动初始化默认项目 `ensure_project()`、创建 LoopForge Issue + 触发 SOP Run + 2.5s 状态对账与事件写入。
- **同源反向代理与 API 路由隔离**：
  - `autodev/server/proxy.py`：已实现 `LoopForgeProxy`，重写 `Origin`/`Referer` 通过 LoopForge 同源校验，透传多条 `Set-Cookie` 与 SSE 流，拦截 `/`、`/index.html`、`/login.html`、`/native-chat/*` 替换 `LoopForge -> Vernal` 并注入：
    - `<link rel="stylesheet" href="/vernal-static/vernal-overlay.css?v=20261122v1">`
    - `<script type="module" src="/vernal-static/vernal-overlay.js?v=20261122v1"></script>`
  - `autodev/server/app.py`：所有 Vernal 自有接口已迁移至 `/vernal-api/*`（含 `POST /vernal-api/runtime/bind` 与 `GET/POST /vernal-api/wish`），静态资源挂载于 `/vernal-static/*`，其余路径全部透传给 `LoopForgeProxy`。
- **前端样式与子模块**：
  - `autodev/server/static/js/{api,dag,council,roster,drawer,runtime}.js`：请求路径已全部改为 `/vernal-api/*`；`runtime.js` 已加入 `#rt-auto-bind` 一键绑定按钮。
  - `autodev/server/static/vernal-stage.css`（由 `scripts/build_vernal_stage_css.py` 生成，全选择器限定在 `#vernalStage` 作用域内，不污染 LoopForge 原生 DOM）。
  - `autodev/server/static/vernal-overlay.css`：已完成全局蓝图配色覆盖、登录页点阵背景、顶栏蓝青渐变识别条、左侧栏 `.vernal-rail-brand` / `.rail-btn.vernal-nav`、`body.vernal-mode` 视图切换规则。

---

## 2. 待 Codex 完成的具体任务清单（按顺序执行）

### Task 1：实现 `autodev/server/static/vernal-overlay.js`（核心前端挂载脚本）

新建 ES Module 文件 `autodev/server/static/vernal-overlay.js`，复用 `./js/{api,dag,council,roster,drawer,runtime}.js`，在原版 LoopForge `index.html` 加载后执行以下逻辑：

1. **CSP 合规要求**：
   - LoopForge 启用了 `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`。
   - **禁止**在注入的 HTML 字符串里写 `onclick="..."` 内联事件处理器，所有事件一律通过 `addEventListener` 绑定！
2. **品牌文案替换（保留原符号图标）**：
   - 将顶栏 `.topbar .page-context span` 文案改为 `／ Vernal · 多智能体架构研发平台`。
   - 在 `#platformRail` 最顶部插入 `.vernal-rail-brand` 按钮（可复用 `#bootLoader svg` 的图形路径作为符号，旁配 `<span class="vernal-rail-brand-text"><strong>Vernal</strong><small>ARCH STUDIO</small></span>`），点击切到 Vernal 总控视图。
3. **在 `#platformRail .rail-nav` 顶部注入 3 个 Vernal 导航按钮 + 分隔线 `.vernal-rail-sep`**：
   - `data-vernal-tab="overview"`：**架构总控**（SVG 流程图图标，含任务数角标 `#vernal-task-badge`）
   - `data-vernal-tab="council"`：**架构圆桌**（SVG 多人圆桌图标，含发言数角标 `#vernal-council-badge`）
   - `data-vernal-tab="squads"`：**研发组与蓝图**（SVG 分组网格图标）
4. **在 `.topbar .top-actions` 前注入运行状态胶囊 `#vernalTopbarPill.vernal-topbar-pill`**：
   - 显示 `<i class="vdot"></i><span>Vernal 架构中枢 · 已连接</span>`，若尚未绑定 LoopForge 登录态则显示 `.vdot.warn` + `点击绑定登录态`，点击自动触发 `POST /vernal-api/runtime/bind` 或切回 `overview`。
5. **在 `.shell-main > main` 内挂载 `<section id="vernalStage">`**：
   - 内部必须包含现有子模块所需的精确 DOM ID（务必与 `dag.js` / `council.js` / `roster.js` / `drawer.js` / `runtime.js` 对齐）：
     - 顶部吸顶 `.vernal-toolbar`：
       - 子页切换按钮组 `.vernal-subtabs`（`总控与 DAG` / `架构评审圆桌` / `研发组与蓝图` / `底座与收件箱`）
       - 状态标签 `#project-name`、`#project-status`、`#usage-pill`
       - 操作区：`#actor-input`、`#btn-wish-toggle`（展开/收起许愿输入框）、`#btn-ingest`（摄入资料）、`#btn-plan`（圆桌与立项）、`#btn-approve`（批准蓝图）、`#btn-start`（启动派发）、`#btn-stop`（停止调度）
     - 许愿式需求快速录入卡片 `.card.wish-composer`（可折叠）：
       - `#wish-title`（标题输入，如 `orbit-shooter`）、`#wish-content`（多行需求输入）、`#btn-wish-submit`（调用 `POST /vernal-api/wish`，保存并自动切片，随后可直接点「圆桌与立项」）
     - `#goal-bar.goal-bar`（5 阶段进度条，由 `renderGoalBar(state)` 渲染）
     - 子视图 1 `v-subview` (`overview`)：
       - 点阵蓝图流程图卡片 `#dag-canvas.dag-canvas`（由 `renderDAG` 渲染）
       - 在岗团队工位实况席 `#roster-summary` + `#roster-grid.roster-grid` + `#btn-council-trace` + `#btn-planner-trace`（由 `renderRoster` 渲染）
       - 右侧实时事件流 `#event-count` + `#event-list.event-list`（由 `renderEvents` 渲染，连接 SSE `/vernal-api/events`）
     - 子视图 2 `v-subview` (`council`)：
       - `#btn-run-council`、`#council-stream.council-stream`、`#council-note`、`#btn-council-interject`、`#council-adrs.stack`（由 `bindCouncilActions` / `refreshCouncil` 驱动）
     - 子视图 3 `v-subview` (`squads`)：
       - 研发组领地看板 `#squad-columns.squad-columns`（由 `renderSquadColumns` 渲染）
       - 立项规划与切片拆解 `#plan-summary`、`#plan-mode`、`#plan-extra`、`#btn-plan-extra`、`#plan-breakdown`、`#slice-list`（由 `renderDocsAndPlan` 渲染）
     - 子视图 4 `v-subview` (`runtime`)：
       - 统一审批与里程碑 `#btn-refresh-inbox`、`#inbox-list`、`#milestone-list` + 运行底座面板 `#runtime-panel`（由 `refreshInbox` / `refreshRuntimePanel` 驱动）
     - 实时监视器抽屉 `#drawer.drawer.hidden`（含 `#drawer-title`、`#drawer-live-badge`、`#drawer-subtitle`、`#drawer-close`、`.drawer-tabs` 四个 `.dtab[data-dtab="trace|diff|verify|prompt"]`、`#drawer-body`）与提示框 `#toast.toast.hidden`。
6. **与原版 LoopForge 视图无缝互切**：
   - 点击任一 `.rail-btn.vernal-nav`：`document.body.classList.add("vernal-mode")`、`document.body.classList.remove("management-mode")`，清除原版 `.nav[data-view]` 的 `.active`，高亮当前 Vernal 按钮，并更新 `#pageTitle` 为对应中文标题。
   - 监听整个文档对原版导航元素的点击（如 `.nav[data-view]:not(.vernal-nav)`、`[data-jump]`、`#railSettingsEntry`、`.management-back`）：一旦点击，立即 `document.body.classList.remove("vernal-mode")` 并清除 `.vernal-nav.active`，让原版 LoopForge 的 `switchView()` 100% 正常接管并展示其原生页面！
7. **无感登录态自动绑定**：
   - 页面初始化时先请求 `/vernal-api/runtime/status`。用户登录 LoopForge 后复用同源登录态绑定账号。任务归属取自用户在 LoopForge 中选择的项目，切换项目后按新项目展示需求和运行。当前实现自动绑定默认项目，这部分需要迁移。

---

### Task 2：补齐 WebSocket 代理与离线页 CSP 细节（检查 `autodev/server/proxy.py` 与 `app.py`）

1. 检查 `autodev/server/proxy.py` 中的 `_offline_fallback_html`：去掉内联 `onclick="location.reload()"`，改为纯 `<a href="/">重新连接</a>`，避免任何 CSP 报错。
2. （可选增强）LoopForge 的在线工作区终端使用 WebSocket（如 `/api/runs/{id}/workspace/terminal` 或 `/runners/connect`）。若需要通过 `8787` 使用终端或连接 runner，可在 `app.py` 增加 `@app.websocket("/{full_path:path}")` 双向转发至 `ws://127.0.0.1:8090/{full_path}`（若未装 `websockets` 库，可通过 `uv pip install websockets` 安装，或让本机 `agentflow-runner` 直接连 `http://127.0.0.1:8090`）。
3. 更新 `autodev/cli.py` 中的 CLI 标题与 `serve` 启动提示文案为 `Vernal（基于 LoopForge 执行底座）`，默认端口 `8787`。

---

### Task 3：更新并跑通全部自动化测试（`tests/`）

1. 更新 `tests/test_server_api.py`：
   - 将旧的 `/api/*` 断言改为 `/vernal-api/*`（`/vernal-api/ingest`、`/vernal-api/slices`、`/vernal-api/state`、`/vernal-api/plan`、`/vernal-api/plan/approve`、`/vernal-api/tasks/T-001`、`/vernal-api/wish` 等）。
   - 使用 `httpx.MockTransport` 传入 `create_app(eng, proxy_transport=mock_transport)`，新增反向代理集成测试，验证：
     - `GET /` 返回原版 LoopForge HTML 结构但 `<title>` 已变为 `Vernal`，且成功注入 `vernal-overlay.css` 与 `vernal-overlay.js`；
     - `GET /login.html` 中 `<h1>LoopForge</h1>` 已替换为 `<h1>Vernal</h1>`；
     - `POST /api/projects` 等原版接口被透明转发到上游，且 `Origin: http://testserver` 被正确重写为上游 `Origin: http://127.0.0.1:8090`，`Set-Cookie` 被原样透传；
     - `POST /vernal-api/runtime/bind` 能用浏览器 Cookie 自动换取 `afp_` Token 并写入配置。
2. 检查 `tests/test_council_and_loopforge.py` 中是否有引用 `/api/council` 或 `/api/runtime/*` 的测试，统一更新为 `/vernal-api/*`。
3. 运行：
   ```bash
   .venv/bin/pytest -v
   ```
   确保所有测试 100% 绿灯通过。

---

### Task 4：连接本机 Docker + Codex 执行器，跑通 `orbit-shooter` 端到端真机验证

1. **确认容器栈健康**：
   ```bash
   ./deploy/loopforge/up.sh
   docker compose -f deploy/loopforge/compose.yaml ps
   curl -s http://127.0.0.1:8090/api/health
   ```
   （若 Colima 休眠，先执行 `colima start`。）
2. **启动统一平台 Vernal（端口 8787）**：
   - 将 `workspaces/orbit-shooter/.autodev/config.toml` 中的 `runtime_backend` 设为 `"loopforge"`，`planning_mode = "goal"`，`enable_council = true`。
   - 启动服务：
     ```bash
     .venv/bin/autodev serve -C workspaces/orbit-shooter --port 8787
     ```
   - 验证 `curl -s http://127.0.0.1:8787/login.html` 与 `curl -s http://127.0.0.1:8787/` 均已包含 `Vernal` 标题与 `vernal-overlay` 静态资源且 HTTP 200。
3. **接入本机 Mac Codex 执行机（`agentflow-runner`）**：
   - 检查 `http://127.0.0.1:8090/api/auth/setup` 是否已完成管理员注册（若用户已注册账号但需登录，提示用户在 `http://127.0.0.1:8787` 登录，不要绕过鉴权；若处于全新未初始化状态且用户允许，引导或协助完成初始化）。
   - 在Vernal「设置 → 执行机器」（即原版 LoopForge `#runnersView`）生成绑定码或通过 `/runners/install.sh` 将当前 Mac 主机注册为个人执行机器，使其直接调用本机 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`（复用桌面端登录态，模型 `gpt-6.2-sol`）。
4. **运行 `orbit-shooter` 许愿小游戏全链路验收**：
   - 选择文件交接并在适用的无仓库 SOP 中声明 UTF-8 产物 `vernal-files.json`，文件内容结构为 `{"files":{"web/index.html":"完整内容"}}`。
   - 输入文件已备好：`workspaces/orbit-shooter/inputs/wish.md`（也可直接在 Vernal 页面许愿框提交）。
   - 完整跑通：**资料切片 → 2 轮多智能体架构评审圆桌（PM / 架构师 / QA → 总监 ADR） → 立项蓝图审批 → 派发到 LoopForge SOP 真实执行 → 工位实况席与事件流实时跳动 → 产出可玩的 飞船射击游戏《碎星航线》并通过验证命令**。

---

## 3. 验收核对清单（Definition of Done）

- [ ] `.venv/bin/pytest -v` 全部通过（含新增的代理注入与自动绑卡测试）。
- [ ] 浏览器打开 `http://127.0.0.1:8787`：
  - 登录页与主工作台标题、品牌均为 **Vernal**（原图形符号保留），整体呈现浅色精密工程蓝图风格，与原版紫色 LoopForge 明显区分；
  - 左侧栏顶部可切换 **架构总控 & DAG / 架构圆桌 / 研发组与蓝图**，点击下方 **AI 工作台 / 统计 / 需求列表 / 待我确认 / 设置** 原版页面全部正常可用，零 JS 报错、零 CSP 报错；
  - 登录后自动完成 LoopForge 执行底座绑定，无需手动粘贴 `afp_` Token。
- [ ] 在 LoopForge 原生项目中提交《碎星航线》需求，本机 Codex 执行机完成真实运行，产物可离线游玩。通过原生「需求列表」查到这条需求，通过原生「统计」查到该项目的实际执行记录，并核对需求与运行编号。
