# Vernal 现存代码问题审计、产品收敛与体验审美重构任务书（供 Codex 直接执行）

> **文档定位**：本文档是对当前仓库（`autodev/` 约 1.7 万行 Python + `autodev/server/static/` 约 6200 行前端代码）的**全面体检报告与先行整顿施工图**。
> 在叠加标准化七阶段新能力（见 [CODEX_PIPELINE_PLATFORM_SPEC.md](CODEX_PIPELINE_PLATFORM_SPEC.md)）与远景升级（见 [ROADMAP_V2.md](ROADMAP_V2.md)）之前，**必须优先清理本文档列出的真实功能错误、安全隐患、产品概念分裂、反人类交互与糟糕的视觉/架构债**，否则新功能将建立在错位字段和分裂双壳之上。
>
> **全局红线**：
> 1. 不魔改、不重编译上游 `ghcr.io/tencent/loopforge:0.4.0` 镜像源码；
> 2. SQLite Schema 变更 100% 幂等向后兼容；
> 3. 严守 LoopForge CSP（禁内联事件、禁 `eval`、禁公网 CDN）；
> 4. 改完后既有 108 项自动化测试 + 本文新增回归测试必须全绿。

---

## 第一部分：现存问题全量审计清单（4 大类 · 24 项实证缺陷）

### 一、功能错误、状态机破损与安全漏洞（Functional Bugs & Security）

#### B-01【P0 · 前端致命字段错配】演进主线把 `milestone_id` 错写成 `.id`，导致波次编号空白、进度恒为 `0/N`
- **位置**：`autodev/server/static/js/steering.js:80-81, 304, 309, 318, 324` vs `autodev/db.py:267, 291`。
- **实证**：后端 `Milestone.to_dict()` 输出的字段是 `milestone_id`（`dag.js:29`、`runtime.js:151` 均用 `m.milestone_id`），不存在 `.id` 字段。
- **后果**：
  1. 总控页「项目演进主线」上所有里程碑波次节点显示为 `执行波次 · `（编号空白），且 `tasks.filter(t => t.milestone_id === waveMs.id)` 变成 `t.milestone_id === undefined`，匹配永远为 0，进度永远显示 `0/N 单通过`；
  2. 点击「排查集成或熔断报错」预设按钮时，`failedMs[0]?.id` 恒为 `undefined`，无法自动带出失败里程碑号；
  3. `steering.js:291-315` 按数组下标 `milestones[idx]` 把第 `idx` 次转向评审与第 `idx` 个里程碑强行配对——若 M1 内开了 2 次评审，第 2 次评审后面会错误挂上尚未启动的 M2。

#### B-02【P0 · 前端致命字段错配】读取根本不存在的 `state.slices_count`，导致主按钮不高亮且启动台误遮挡
- **位置**：`autodev/server/static/js/main.js:175`、`autodev/server/static/js/launcher.js:493` vs `autodev/engine.py:271`。
- **实证**：后端 `engine.snapshot()` 返回的键名是 `"slices": len(...)`（`roster.js:9` 用 `state.slices`），全库从未定义 `slices_count`。
- **后果**：
  1. 从零新建项目（`scratch`）填入需求并完成切片后，`state.slices_count > 0` 恒为 `false`，顶栏「开始圆桌与立项」按钮永远不会变为蓝色主按钮（`btn-primary`），用户失去下一步视觉引导；
  2. `launcher.js:493` 判断空项目时 `!(state.slices_count > 0)` 恒为 `true`，只要状态仍为 `CREATED` 就自动弹出全屏启动台遮挡页面。

#### B-03【P0 · LoopForge 远端模式核心断链】`_execute_remote` 从不写入 `task_runs` 表，导致默认容器模式下抽屉审查页空白、Token 统计漏记 95%、计时器失效
- **位置**：`autodev/scheduler.py:702-800` vs `autodev/scheduler.py:453, 555, 622` vs `autodev/server/static/js/drawer.js:198-261`。
- **实证**：全库仅本地路径 `_execute_task` 调用 `db.start_run` / `finish_run` 记录 `CODE`、`VERIFY`、`REVIEW` 三阶段；默认旗舰模式（`runtime_backend = "loopforge"`）走 `_execute_remote` + `LoopForgeBridge`，**对业务开发任务 0 次写入 `task_runs`**，也从未拉取保存 LoopForge `review` 节点产出的 `review-report.md`。
- **后果**（在默认 `./vernal.sh` 容器模式下 100% 复现）：
  1. 点开任意已完成/进行中任务的监视器抽屉，切到 **「门禁与审查」Tab**，永远显示 *「尚未进入门禁或审查阶段。」*；切到 **「提示词与命令」Tab**，永远显示 *「暂无调用记录。」*；
  2. 研发组看板卡片（`roster.js:94, 111`）每张任务卡永远显示 **`0 tok`**，顶栏总 Token 只统计了立项圆桌，漏掉了占大头的全部编码与审查消耗；
  3. `engine.py:233` 依赖 `latest_run_by_task()` 计算 `active_started_at`，导致 LoopForge 模式下 DAG 节点和任务卡上的实时运行读秒计时器全部不显示。

#### B-04【P0 · 状态机并发破坏】`steering.py` 转向变更单直接改写运行中任务状态，且同时退回上下游任务时破坏 DAG 依赖阻塞
- **位置**：`autodev/steering.py:474, 481, 531-565`。
- **实证与后果**：
  1. **运行中覆盖（Lost Cancel / Reopen）**：若 `reopen_tasks` 或 `cancel_tasks` 命中正在执行的任务（`IN_PROGRESS` / `VERIFYING` / `REVIEWING`），`_apply_local` 只改 SQLite 状态为 `READY` 或 `CANCELLED`，既未取消 `Scheduler.running_tasks` 协程，也未中止远端 LoopForge Run；原协程跑完后会把状态覆写回 `APPROVED`；
  2. **破坏拓扑顺序**：`539-547` 行无条件将所有退回任务置为 `TaskStatus.READY`。若上游契约单 `T-001` 与依赖它的 `T-002` 同时被退回重做，两单会**同时变成 `READY` 并被并发派发**，`T-002` 基于旧契约白跑甚至引发合并冲突；
  3. **泳道硬编码错误**：`474, 481` 行为新增任务建 LoopForge Issue 时写死 `"backend"` 泳道，未调用 `bootstrap.classify_lane(task.role, task.allowed_paths, task.title)`，导致新增前端/契约/修复卡全部挂错 SOP。

#### B-05【P0 · 未捕获异常致 HTTP 500】收件箱审批接口转发转向拍板时漏写异常捕获
- **位置**：`autodev/server/app.py:2525-2528, 2544-2547`。
- **实证**：`approve_steering_checkpoint` / `reject_steering_checkpoint` 在草案代数冲突或远端异常时抛 `LoopForgeError(..., 409/502/503)` 或 `ValueError`，但 `/vernal-api/runtime/runs/{run_id}/approve` 与 `/reject` 未包 `try...except`，一旦冲突直接返回未处理的 **500 Internal Server Error**。

#### B-06【P0 · 安全与多用户隔离漏洞】全局 `default_slot_ref[0]` 串台、SSRF、CSRF 与明文凭据落盘
- **位置**：`autodev/server/app.py:240-245, 518-542, 1181, 1440, 1647, 1768, 2028-2055, 2249`。
- **实证与后果**：
  1. **跨项目串台**：任何未带 `X-Vernal-Project` 头的请求调 `/project/select` 或 `/project/create` 都会改写进程级 `default_slot_ref[0]`；其他未带头的请求（如直接打开链接、部分脚本或 `#projectSwitcher` 未加载完时的请求）会静默落到最后一次被人切换的项目上；
  2. **SSRF 与 Token 泄露**：`project/create (git_repo)` 允许克隆任意内网 HTTP/SSH 地址（如 `http://169.254.169.254`、`http://mysql:3306`）；`/vernal-api/runtime/config` 允许普通成员修改 `loopforge_url` 为任意攻击者地址并附带 `loopforge_token` 发请求，直接窃取凭据；
  3. **缺少统一 CSRF 校验**：中间件对 Cookie 鉴权的写接口未校验 `Origin`/`Referer` 与 `Host` 一致性（仅 2 个接口单独写了校验）；
  4. **明文 Token 写进 `config.toml`**：`_persist_runtime_config` 把 `loopforge_token` 明文写入工作区 `config.toml`（未限 `0600` 权限，且容易被误纳入备份或日志）。

#### B-07【P1 · 圆桌「插话重裁」假闭环陷阱】插话只改纪要，不更新蓝图与任务图
- **位置**：`autodev/server/app.py:1263-1297`、`autodev/council.py:320`、`autodev/server/static/js/council.js:115-131`。
- **实证**：立项阶段 `engine.plan()` 会依次跑 `run_council` → `plan_blueprint`；但用户在圆桌页输入意见点击「插话并重新裁决」（或点「发起新一轮圆桌评审」）后，后端只更新了 `council_messages` 中的 ADR 纪要，**完全没有重新触发 `plan_blueprint`**，也没有标记当前蓝图过期！用户看到纪要改了，去点「批准蓝图」，实际执行的仍然是插话前的旧研发组与旧任务卡。

#### B-08【P1 · 容器镜像漏装 PDF/DOCX 依赖 & 以 root 裸跑】
- **位置**：`Dockerfile:17, 29`、`deploy/loopforge/Dockerfile.sandbox:3`、`pyproject.toml:17`、`index.html:157`、`roster.js:173`。
- **实证**：UI 空状态明确提示放入 `pdf/docx`，但 `Dockerfile` 只装了 `project.dependencies`，未装 `optional-dependencies.docs`（`pypdf`、`python-docx`），容器模式下解析 PDF/DOCX 必崩，且报错提示用户运行容器内根本不存在的 `uv pip install`；启动台文件选择框 `accept` 也漏了 `.pdf,.docx`；两个容器均以 `root` 身份运行。

#### B-09【P1 · 三处配置默认值自相矛盾 + 硬编码不存在的 `gpt-6.2-sol`】
- **位置**：`autodev/config.py:92-134` vs `autodev/config.py:166-204` vs `README.md:17-25`。
- **实证**：
  - `Settings` 类默认 `runtime_backend="local"`、`planning_mode="classic"`、`enable_council=False`、`harness="codex"`，而 `DEFAULT_CONFIG_TOML` 却是 `"loopforge"`、`"goal"`、`true`、`"auto"`！若 `seed_eng` 来自缺省 `Settings()`，启动台勾选「创建后直接发起多智能体圆桌」（`app.py:1816`）实际会跑进无圆桌的 `classic` 模式；
  - README 称默认是「文件交接模式」，`config.py` 两处默认却都是 `"git"`；
  - 全局默认模型硬编码为 `model = "gpt-6.2-sol"`（`config.py:106, 179`），切换 OpenCode / DeepSeek / Claude 时若未手动清空默认模型，会把该无效模型名传给下游 API 导致直接失败。

#### B-10【P1 · 每秒全量销毁重绘 DAG SVG + SSE 无防抖轮询风暴 + 事件循环阻塞】
- **位置**：`autodev/server/static/js/main.js:286-293, 336`、`autodev/server/app.py:646-687`。
- **实证**：
  1. `main.js:291` 在 `setInterval(..., 1000)` 里每秒执行 `renderDAG()` 重写整个 `<svg>` 的 `innerHTML`：用户在节点上点击若跨过 1 秒边界，DOM 被替换导致 `click` 事件直接丢失（点不开抽屉），且 hover 态每秒闪烁；
  2. `main.js:336` 对每条非 `agent.*` 的 SSE 事件无防抖调用 `refresh()` 拉取 `/vernal-api/state`，而 `get_state` 在 async 协程里同步执行 `git current_branch` / `git remote` 子进程和多次 SQLite 查询，事件密集时阻塞整个网关事件循环。

---

### 二、功能不聚焦、概念膨胀与自相矛盾（Unfocused Product & Conceptual Bloat）

#### C-01【P0 · 双系统精神分裂】Vernal 套壳寄生在 LoopForge 前端里，两套重叠概念同时暴露给用户
- **位置**：`autodev/server/static/vernal-overlay.js:87-101, 134-170`、`runtime.js:222-244`。
- **问题**：左侧导航栏上半截是 Vernal 的 `总控 / 圆桌 / 蓝图`，下半截原封不动保留 LoopForge 的 `快速对话 / 需求列表 / 运行记录 / 待我确认 / 统计 / 角色 / 流程 / 设置 / 执行机器`。同一个事物有两套名字、两三个入口：
  - 任务：`Vernal 任务卡 (T-001)` vs `LoopForge 需求列表 (Issue)`；
  - 运行：`Vernal 任务监视器` vs `LoopForge 运行记录 (Run)`；
  - 审批：`总控待拍板卡` vs `Vernal 审批与接管箱` vs `LoopForge 待我确认`（3 处重复！`steering.js:195` 甚至要写括号向用户解释两边点哪个都行）；
  - 统计：Vernal 顶栏 Tokens vs 底座页「运行统计与数据来源对照」卡 vs LoopForge 原生统计（把内部双库架构割裂直接甩给用户去核对）。
- **收敛方向**：LoopForge 必须退居幕后作为**无头执行底座（Headless Runtime）**。常规导航栏隐藏 LoopForge 原生业务项，只保留 Vernal 一套统一研发流水线概念；LoopForge 原生排障页面统一收进管理员专属的「设置 → 底层引擎诊断」折叠入口。

#### C-02【P1 · 需求入口 4 处割裂，评审机制 3 套重叠】
- **位置**：`index.html:88-92, 110, 298, 446, 525, 599`。
- **问题**：
  1. **4 个需求入口**：① `＋ 新建/接管项目` 弹窗；② 顶栏 `补充需求` 展开框（运行中提交只改 `inputs/*.md`，**不触发任何重规划**，纯属误导！）；③ `更多 → 手动重新摄入资料`；④ 左侧原生 `快速对话`。
  2. **两套「四专家开会」各自为政**：立项时叫 `架构评审圆桌`（`council.py`，独立视图），运行中叫 `拿当前进展与问题讨论 / 阶段转向评审`（`steering.py`，总控弹窗 + 演进主线），底层两套角色 ID、两套 SOP、两套表、两套 UI，用户完全困惑什么时候去「圆桌」插话、什么时候点「拿进展讨论」。

#### C-03【P1 · 24 种底层运行模式组合直接抛给用户】
- **位置**：`index.html:103, 145, 350, 600`、`runtime.js:308`。
- **问题**：规划模式（`Goal` / `Classic`）× 协作方式（`files` / `git`）× 执行底座（`local` / `loopforge`）× 执行池（`auto` / `local_runner` / `public_docker`）共 24 种组合全暴露在界面上；甚至选了 `Classic` 模式后，顶栏主按钮依然叫 `启动 Goal`（`index.html:94`）。普通研发人员只需选「项目来源」与「流程模板」，其余底层实现应自动决策或收进管理员高级设置。

#### C-04【P2 · 历史命名包袱与玩具化文案】
- **位置**：`index.html:13, 141, 447, 569`、`scheduler.py:123, 125`、`USAGE_GUIDE.md:93`。
- **问题**：左上角品牌徽标写着字母 **`A`**（`AutoDev` 遗留）、收件箱写 `AutoDev 熔断接管任务`、Web 接口抛出的错误让网页用户去敲命令行 `autodev approve-plan`、Git 分支混用 `autodev/T-001` 与 `vernal/milestone-M1`；默认占位符写着游戏名 `碎星航线`、文档写“直接运行/游玩！”，与实验室严肃复杂软件工程平台定位严重违和。

---

### 三、不合适的用户使用体验（Broken User Flows & Poor Usability）

#### U-01【P0 · Overlay 导航栏丢页面、丢红色待办角标、硬拼不相关视图】
- **位置**：`vernal-overlay.js:3, 53-57, 80, 100, 112-117`、`main.js:28-31`。
- **实证**：
  1. 左侧主导航（`vernal-overlay.js:100`）只挂载了 `overview`、`council`、`squads` **3 个按钮**，漏掉了第 4 个 `runtime`（「底座与收件箱」），导致用户在左侧栏根本找不到审批收件箱和成品下载入口；
  2. 原独立版 `#nav-inbox-cnt` 红色待办角标位于被丢弃的 `.sidebar` 内；Overlay 注入的 `vernal-task-badge` 显示的是总任务数 `state.tasks.length`，导致**熔断待接管、待审批、待合并时没有任何醒目红点提醒**；
  3. `main.js:29` 把 `squads + docs` 硬拼在一页、把高频使用的 `inbox`（审批与交付下载）和低频运维的 `runtime`（底座 Token/SOP 表单）硬拼在一页；
  4. `vernal-overlay.js:80` 把原本窄侧栏底部的 `.sidebar-footer` 直接裸挂在全宽页面最底部，6 行状态条在页面底端变形拉宽。

#### U-02【P1 · 顶栏塞满 10 个按钮、多个蓝色主按钮打架、高危操作零确认、身份全叫 `owner`】
- **位置**：`index.html:87-107`、`main.js:20, 173-183, 258-284`。
- **问题**：
  1. 顶栏右侧平铺 9 个按钮 + `更多` 菜单，`＋ 新建/接管项目`、`开始圆桌与立项` / `批准蓝图` / `启动 Goal` 会同时出现 **2 个蓝色 `btn-primary`**，视觉焦点分裂；
  2. 点完「批准蓝图」后什么都不发生，还得再手动点一次旁边的「启动 Goal」才能跑起来，平白多一步断裂操作；
  3. 停止调度、覆盖重做立项规划、合并里程碑写回原目录等高危动作**全部无二次确认弹窗**；
  4. 操作人姓名输入框藏在 `更多` 菜单里，默认硬编码 `"owner"`（`main.js:20`），未自动绑定当前 LoopForge 登录账号，导致多人协作时所有操作记录全叫 `owner`。

#### U-03【P1 · 「编队与模型」18 控件巨型内联表单顶翻主视图，模型全靠盲敲】
- **位置**：`index.html:338-444`。
- **问题**：点「编队与模型」直接在总控画布上方展开包含 18 个控件的长表单，把流程图顶出首屏；公用池管理员配置与个人角色编队混为一谈；模型输入框无候选列表，placeholder 写着 `gpt-6.2-sol` 误导用户。

#### U-04【P1 · 长耗时操作无进度感知，目录栏塞满 120 字说明】
- **位置**：`launcher.js:120, 452-483`。
- **问题**：克隆大仓库或导入数百个文件时仅按钮变灰，无阶段提示；目录状态栏把命令行参数说明和 4 MiB 规则浓缩成一段百字长句塞在狭小区域。

---

### 四、糟糕的视觉审美与前后端工程债（Visual Aesthetics & Architecture Debt）

#### A-01【P0 · 深色模式（Dark Mode）半残废，切换后白底浅字彻底不可读】
- **位置**：`vernal-overlay.css:33-41` vs `vernal-overlay.css:241, 278, 308, 334` & `styles.css:2-20, 557`。
- **实证**：`vernal-overlay.css` 只给宿主顶栏和登录页写了 `html[data-theme="dark"]`，而 `#vernalStage` 画布、工具条、卡片、弹窗背景全部写死 `#f8fafc` / `#ffffff`；用户在宿主顶栏切深色主题后，外黑内白、局部继承浅色文字叠在白底上，界面直接崩坏。

#### A-02【P1 · 双重主色冲突、33 处 `!important`、14 种零散字号、Emoji 充当图标】
- **位置**：`styles.css`（1072 行）、`vernal-overlay.css`（399 行）、`vernal-stage.css`（1001 行生成文件）。
- **实证**：
  1. 两套蓝色打架：`styles.css` 用 `--primary: #2563eb` + 蓝紫渐变 `#4f46e5`，`vernal-overlay.css` 用 `#1d4ed8` + 蓝青渐变 `#0d9488`，并靠 **33 处 `!important`** 强行覆盖宿主样式；
  2. 字号失控：混用 `9.5px, 10.5px, 11px, 11.5px, 12px, 12.5px, 13px, 13.5px, 14px, 14.5px, 15px, 15.5px, 20px, 22px` 共 14 种字号及 50+ 处 HTML/JS 内联 `style="..."`；
  3. 全界面用 `💬🔑🌿📁📦📄⚡👤⚖️🧠⌘✎⚠⏸🎉` 等 Emoji 当图标，在麒麟 Linux 等缺少 Apple Color Emoji 的系统上渲染成黑白线框或乱码方块；
  4. 标题与提示文案过度口号化、冗长啰嗦；DAG 节点把标题硬截断为 14 字且无完整悬浮提示，跨列连线直接穿过中间节点卡片背后。

#### A-03【P1 · 后端 2673 行单闭包巨石 `app.py` + 正则拼凑 TOML】
- **位置**：`autodev/server/app.py:215-2673`（尤其 `2226-2309` 行）。
- **实证**：45+ 个路由、鉴权、槽位、SSE、反向代理全挤在 `create_app()` 一个闭包函数内；`_persist_runtime_config` / `_persist_agent_and_scheduler_config` 用 `re.sub` + `json.dumps` 手工拼写 TOML（一旦存在注释、多行数组或新配置节极易写坏文件）。

---

## 第二部分：工程修复与重构执行方案（按 Fix-1 → Fix-6 顺序落地）

### Fix-1（P0 · 必先执行）：修复前端致命错配、LoopForge 运行账本断链与转向并发 Bug

1. **修正前端错配字段与演进主线配对逻辑**（解决 B-01、B-02）：
   - `autodev/server/static/js/steering.js`：将全部 `waveMs.id` / `failedMs[0]?.id` 改为 `.milestone_id`；删除按数组下标 `milestones[idx]` 强行配对的逻辑，改为按各节点 `created_at` / 里程碑阶段顺序排布（或将立项/转向评审挂在对应里程碑之前）。
   - `autodev/server/static/js/main.js:175` 与 `launcher.js:493`：将 `state.slices_count` 改为 `state.slices`（同时在 `engine.snapshot()` 中保留 `"slices"` 并兼容输出 `"slices_count": len(...)` 防回归）。
2. **补齐 LoopForge 远端模式的 `task_runs` 台账与审查报告拉取**（解决 B-03）：
   - 在 `autodev/scheduler.py:_execute_remote` 中：
     - 派发前调用 `db.start_run(task.task_id, attempt, "CODE", coder_ident["harness"], agent_label=coder_ident["label"])`；
     - `bridge.execute_task` 返回后，从 `bridge.client.get_run(run_id)` 解析各节点（`implement`、`package`/`record_mr`、`review`）耗时、状态与 usage tokens（若上游提供则写入，否则保底记录起止时间使实时计时器正常工作），调用 `db.finish_run`；
     - 本地 `_check_files` 执行前后写入 `VERIFY` 阶段 `task_runs` 记录与 `attempt-{attempt}-verify.log`；
     - 通过 `bridge.client.named_artifact(run, "review-report.md")` 拉取远端审查报告落盘到 `runs/{task_id}/attempt-{attempt}-review/last_message.txt`（若内含 JSON verdict 则同步写入 `verdict.json`），并写入 `REVIEW` 阶段 `task_runs` 记录，确保任务抽屉「门禁与审查」与「提示词与命令」Tab 在 LoopForge 模式下完整可看。
3. **修复 `steering.py` 并发覆盖、拓扑依赖与泳道路由**（解决 B-04、B-05）：
   - `autodev/steering.py:_apply_local`：
     - 若被 `reopen_tasks` 或 `cancel_tasks` 命中的任务处于 `ACTIVE_TASK_STATUSES`，先标记 `pending_action="cancel_by_steering"`（并在 `Slot` / `Scheduler` 收割点拦截旧协程写回 `APPROVED`；若有关联 `loopforge_run_id` 则调用 `client.pause_run`）；
     - 收集全部 `reopen_set = {r["task_id"] for r in reopen_tasks}` 后统一计算新状态：若某退回任务的 `depends_on` 中存在未处于 `(APPROVED, MERGED)`（含本轮同批被退回）的上游任务，必须置为 **`TaskStatus.BLOCKED`** 而非 `TaskStatus.READY`；
     - 新增任务创建 LoopForge Issue 时调用 `bootstrap.classify_lane(role, allowed, title)` 选择对应泳道 SOP。
   - `autodev/server/app.py:2525, 2545`：为 `api_lf_approve` / `api_lf_reject` 中的 `approve_steering_checkpoint` / `reject_steering_checkpoint` 补齐 `try ... except (ValueError, LoopForgeError)` 并映射为对应 HTTP 404/409/502/503。

### Fix-2（P0）：安全加固、多项目严格隔离与配置默认值统一

1. **消除全局 `default_slot_ref[0]` 串台**（解决 B-06.1）：
   - 所有 `/vernal-api/*` 项目级接口在存在多个工作区时强制要求 `X-Vernal-Project` 头或 `?project=` 参数（前端 `api.js`、SSE、ZIP 下载、静态预览统一补齐）；`api_select_project` 与 `api_create_project` **禁止修改全局共享的默认槽位影响其他会话**（仅单机 CLI 无头调用且仅有一个工作区时回退初始槽位）。
2. **补齐 CSRF、SSRF 防护与凭据权限**（解决 B-06.2~4）：
   - 在 `vernal_project_and_auth_middleware` 中对所有基于 Cookie 鉴权的 `POST/PUT/PATCH/DELETE` 请求统一校验 `Origin`（若无则校验 `Referer`）必须与当前 `Host` 同源；
   - 新增 `autodev/security.py:validate_external_git_url(url)`：默认禁止 `127.0.0.0/8`、`169.254.0.0/16`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`（除非显式匹配管理员配置的 `VERNAL_GITEA_URL` / `VERNAL_GIT_ALLOWLIST`）；`/vernal-api/runtime/config` 修改 `loopforge_url` / `loopforge_token` 限管理员权限；
   - 将 `loopforge_token` 从工作区 `config.toml` 剥离，统一存入权限 `0600` 的 `.autodev/credentials.json`（向后兼容读取旧 `config.toml` 中的值并在首次保存时自动迁移清除）。
3. **统一三处默认配置 & 移除 `gpt-6.2-sol` + 补齐容器依赖**（解决 B-08、B-09）：
   - `Settings` dataclass 默认值与 `DEFAULT_CONFIG_TOML` 对齐：`harness="auto"`、`model=""`（留空走所选执行器默认模型，彻底删除 `gpt-6.2-sol` 硬编码）、`planning_mode="goal"`、`enable_council=True`、`collaboration_mode="files"`、`runtime_backend="loopforge"`（单元测试需本地模式时在 `conftest.py` 显式传 `runtime_backend="local"`）；
   - `Dockerfile` 安装 `pypdf` 与 `python-docx`，新增非特权用户 `vernal`（UID 1000）运行服务；`index.html:157` 附件框补上 `.pdf,.docx`。

### Fix-3（P1）：前后端性能与圆桌真闭环

1. **移除每秒全量重绘 DAG**（解决 B-10.1）：删除 `main.js:290-292` 中 `setInterval` 里的 `renderDAG(...)`，在 SVG 节点耗时 `<text>` 上加 `class="elapsed" data-start="..."`，由已有的每秒轻量文本刷新统一更新。
2. **SSE 刷新合并防抖 + 异步化阻塞调用**（解决 B-10.2）：`main.js` 将 `refresh()` 改为 `scheduleRefresh(150ms)` 合并同一波事件风暴；后端 `get_state` / `api_task_detail` / `api_task_diff` 中的 `gitops.*` 子进程调用移入 `asyncio.to_thread` 或缓存分支名。
3. **圆桌插话与蓝图联动**（解决 B-07）：在 `council.py:interject_council` 收敛出新 ADR 后，若项目尚处于立项未开跑阶段（`status in (SPECIFIED, PLANNED)`），自动接着调用 `engine.plan_blueprint` 刷新研发组与 DAG（或提供「重裁纪要并更新蓝图」默认主动作，并将旧蓝图标记为 `STALE` 禁止直接批准旧蓝图）。

### Fix-4（P1）：产品概念大收敛——隐藏双壳分裂、合并重复入口

1. **LoopForge 退居幕后（解决 C-01）**：
   - 在 `vernal-overlay.css` 的默认模式下隐藏 LoopForge 原生左侧栏的重复业务项（`快速对话 / 需求列表 / 运行记录 / 待我确认 / 统计 / 角色 / 流程`），仅在管理员点击「设置 → 打开底层引擎控制台」时展开；
   - 删除 `runtime.js` 中向用户解释双库差异的「运行统计与数据来源对照」补丁卡，统一由 Vernal 自身完整汇总展示（Fix-1 修好 `task_runs` 后本地统计已完整准确）。
2. **合并需求入口与两套评审会议（解决 C-02、C-03、C-04）**：
   - 删除顶栏误导性的 `补充需求` 独立表单：未立项时统一走首页/启动台需求输入；运行中改需求统一走「变更与评审」（原 `steering`）；
   - 将立项期的「架构圆桌」与运行期的「阶段转向评审」在用户侧统一命名为 **「架构与变更评审」**；隐藏过时的 `Classic 单次拆单模式` 下拉框，顶栏 `启动 Goal` 改为直白的 **「启动自动研发」**（并与「批准蓝图」合并为主按钮 **「批准并启动研发」**，下拉箭头提供「仅批准暂不启动」）；
   - 清理全部 `AutoDev`、`A` 字徽标、`碎星航线`、`游玩`、Web Toast 里的 CLI 命令提示，统一为严肃规范的工程术语；操作人自动取当前 LoopForge 登录用户名（`/vernal-api/state` 返回 `current_user`），取消手工填 `"owner"`。

### Fix-5（P1）：导航重组、顶栏减负与交互防呆

1. **重构独立清晰的平级导航（解决 U-01）**：
   - 严禁再用 `main.js:29` 把两个不相关页面上下硬拼！左侧主导航与子标签一一对应，拆为清晰的业务视图：`总控（流水线与 DAG）`、`评审与蓝图`、`任务看板`、`待办与交付`（含显眼红色待办数字角标 `#vernal-inbox-badge`）、`系统设置`（收纳执行池、编队与模型弹窗/页面、底座连接）；
   - 移除把 `.sidebar-footer` 裸塞到全宽页面底部的做法，改为紧凑的顶栏右侧环境健康胶囊（点击弹出执行器与 Runner 状态浮层）。
2. **顶栏单一主按钮原则与二次确认（解决 U-02、U-03、U-04）**：
   - 顶栏同一时刻**最多只允许 1 个蓝色主操作按钮**（随阶段自动切换：`开始立项评审` → `批准并启动研发` → 运行中显示状态与 `暂停调度` → `确认交付里程碑`）；
   - 「编队与模型」改为居中 Modal 或放入「系统设置」专页，模型输入框增加 `<datalist>` 常用模型推荐（`deepseek-chat`、`deepseek-reasoner` 等）；
   - 为「停止调度」「覆盖重做规划」「写回本机目录 / 推送远端分支」加上简洁的确认对话框；为克隆 Git 仓库和导入目录增加阶段进度文案。

### Fix-6（P1）：统一设计令牌（Design Tokens）、修复深色模式、清退 Emoji 与巨石拆分

1. **统一 CSS 变量与完整深色模式（解决 A-01、A-02）**：
   - 建立单一 `autodev/server/static/styles/tokens.css`，收敛为 1 套品牌主色（`#1d4ed8`）、6 档标准字号（`11 / 12 / 13 / 14 / 16 / 20px`，清除所有 `.5px` 半像素字号與内联 `style`），所有背景、文字、边框、DAG 节点均走语义变量（`--v-bg`、`--v-surface`、`--v-text`、`--v-border`），补齐 `html[data-theme="dark"]` 变量映射，确保浅色/深色切换零白斑；
   - 将全部 Emoji 替换为统一线宽（`1.75px`）的内联单色 SVG 图标组件；精简所有超过 20 字的啰嗦副标题；DAG 节点增加完整标题 `<title>` Tooltip、缩放控件与正交避让连线。
2. **拆分 `autodev/server/app.py` 巨石与规范化 TOML 读写（解决 A-03）**：
   - 新建 `autodev/server/deps.py`（槽位解析、登录态、CSRF、权限）与 `autodev/server/routers/`（`projects.py`、`planning.py`、`tasks.py`、`steering.py`、`runtime.py`、`history.py`），将 `app.py` 压缩到 300 行以内的应用装配入口；
   - 废弃正则替换拼写 `config.toml`，改用结构化序列化输出（带单元测试覆盖特殊字符与多节保持）。

---

## 第三部分：必写回归测试与验收清单（Definition of Done）

- [ ] 新增 `tests/test_audit_fixes.py`，至少覆盖：
  1. `/vernal-api/state` 同时兼容 `slices` 字段，前端脚本静态扫描不再出现 `.slices_count` 或 `waveMs.id` / `failedMs[0]?.id`；
  2. LoopForge 远端执行任务完成后，`task_runs` 表准确生成 `CODE` / `VERIFY` / `REVIEW` 记录，`/vernal-api/tasks/{id}` 返回非空 `runs` 与 `review_verdict` / 审查报告；
  3. `steering` 同时退回存在依赖关系的 `T-001 -> T-002` 时，`T-001` 为 `READY`、`T-002` 必须为 `BLOCKED`；退回/取消运行中任务不会被旧协程覆写回 `APPROVED`；新增前端卡自动路由至 `frontend` 泳道 SOP；
  4. `/vernal-api/runtime/runs/{id}/approve` 遇到重复/过期转向审批时返回 `409` 而非 `500`；
  5. Cookie 跨站伪造 `Origin: https://evil.example` 调用 POST 接口返回 `403`；`git_repo` 传入 `http://169.254.169.254/repo.git` 或未授权内网地址被拦截；A 用户切换项目不会改变 B 用户无头回退槽位；
  6. 圆桌 `interject` 在 `PLANNED` 状态下触发后，蓝图与任务图同步更新（或旧蓝图被阻止直接盲批）。
- [ ] `.venv/bin/pytest -q` 全量测试 100% 通过；重新运行 `scripts/build_vernal_stage_css.py` 并同步递增 `OVERLAY_VERSION`。
- [ ] 浏览器实测浅色与深色两套主题下无白底浅字、无双蓝色冲突、无 Emoji 乱码，左侧导航可直接进入待办收件箱且红点计数准确。
