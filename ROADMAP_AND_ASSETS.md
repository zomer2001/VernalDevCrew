# Vernal 平台资产扩充与演进路线备忘录

> 本文档整理了 Vernal 下一步可引入的 **GitHub 热门开源 Skill / MCP 插件**、**公网开源研发 SOP 复用方案**、**平台待完善能力清单（P0～P3）**，以及在麒麟系统上接入 **`CodeBuddy`** 与 **`DeepSeek Harness (dsh)`** 的具体适配位置，供后续按需自行扩展。

---

## 一、SOP 设计原则：预置固定黄金流水线 + 模型按卡选配

在 Vernal + LoopForge 双层架构中，务必区分两层图：

1. **项目级任务图（Vernal 顶层 DAG）**：决定先做哪个模块、再做哪个模块（如 `T-001 定契约 → T-002 写引擎 → T-003 做界面`）。每个项目业务不同，**由圆桌与总监模型每次动态生成**。
2. **单卡工序流水线（LoopForge 底层 SOP / Workflow）**：决定单张任务卡拿到手后经历哪几道标准工序。
   - **不要每次手写**，也**不要让大模型自由画 SOP 节点图**（极易漏掉产物打包脚本或编出死循环审批边）。
   - **最佳实践（菜单点菜模式）**：在平台代码库中预置 **4 套固定黄金 SOP 模板**，总监模型拆出任务卡时只负责给每张卡打上类型标签（`sop_lane`），自动路由到对应现成 SOP 并挂载对应 Skill。

### 推荐预置的 4 套固定黄金 SOP 模板

| SOP 模板 ID | 适用任务卡片 | 标准节点工序 | 默认挂载资产建议 |
| :--- | :--- | :--- | :--- |
| **`vernal-contract-sop`** | 接口定义、Schema、架构文档卡 | `编写契约与样例` → `JSON/语法静态校验脚本` → `架构一致性快审` | 接口规范 Skill |
| **`vernal-backend-tdd-sop`** | 核心引擎、后端 API、算法逻辑卡 | `先写失败单测与实现（Coder）` → `打包校验脚本` → `独立测试与安全审查（Reviewer）` | `test-driven-development` + `context7` |
| **`vernal-frontend-ui-sop`** | 网页界面、交互组件、小游戏前端卡 | `UI 编码与自测（Coder）` → `打包校验脚本` → `无头浏览器真机点按与截图审查（Reviewer）` | `frontend-design` + `playwright-mcp` |
| **`vernal-bugfix-sop`** | 存量项目排障、线上/集成 Bug 修复卡 | `复现报错与定位根因` → `最小补丁与回归测试` → `打包与防劣化审查` | `systematic-debugging` + `serena` |

---

## 二、公网可直接复用的 4 大开源研发 SOP 经验库

无需从零发明提示词和验收表，以下 4 个 GitHub 高星项目的工序可直接提炼进上述 4 套模板：

1. **`obra/superpowers`（战术级 TDD 与排障纪律标杆）**
   - **核心可抄经验**：强行抑制大模型“拿到需求直接狂写代码”的冲动，固化 `确认规格 → 写失败测试（Red） → 写最小实现（Green） → 双阶段审查（先查规格符合度、再查代码质量）`。
   - **复用位置**：直接抽取出它的红绿灯 TDD 约束和根因排查铁律，塞进 `vernal-coder` 的角色规则与 Skill。
2. **`bmad-code-org/BMAD-METHOD`（敏捷多角色检查单 Checklist）**
   - **核心可抄经验**：拥有极完善的**角色交接检查表（Checklists）**——明确规定架构师派发的开发卡必须包含哪些边界条件、QA 审查官打回（`rework`）时必须按什么结构列出必改项。
   - **复用位置**：用来精炼 `vernal-reviewer` 的 `review-report.md` 审查打回模板。
3. **`geekan/MetaGPT`（`Code = SOP(Team)` 结构化交接鼻祖）**
   - **核心可抄经验**：上下游角色禁止靠自然语言闲聊传递需求，强制以结构化产物（PRD / 接口定义 / 验收用例）交接；其增量迭代 SOP 对需求变更场景很有参考价值。
4. **`github/spec-kit` & `SWE-agent`（存量老代码改造五步法）**
   - **核心可抄经验**：针对几万行存量代码的标准动作：`符号检索定位（Locate） → 写最小复现脚本（Reproduce） → 小步补丁（Patch） → 原有测试全量回归（Regression） → 评审`。

---

## 三、推荐引入的 GitHub 热门 Skill 与 MCP 插件（平台军火库）

> **底座利好**：经核查，`LoopForge 0.4.0` 的 `agentflow-runner` 已原生内置 `SKILL.md` 目录树下发机制（支持自动物化到 `.codex/skills`、`.claude/`、`.codebuddy/`、`.dsh/`）以及内置浏览器/MCP 桥接能力。

### 1. 必装开源 Skill（纯文本/脚本资产，零额外常驻内存）

| 来源仓库 | Skill 名称 | 挂给哪个角色 | 解决什么痛点 |
| :--- | :--- | :--- | :--- |
| **`anthropics/skills`** | `frontend-design` | 前端研发组 Coder | 根治 AI 生成网页千篇一律的“廉价蓝紫渐变塑料感”，大幅提升界面排版与配色质感 |
| **`anthropics/skills`** | `webapp-testing` | 前端/集成组 & QA | 指导智能体用 Playwright 自动拉起本地静态服务、抓控制台报错与 DOM 状态 |
| **`obra/superpowers`** | `test-driven-development` | 后端/引擎组 Coder | 强制先写测试再写实现，杜绝“写完代码补个永远返回 True 的假测试” |
| **`obra/superpowers`** | `systematic-debugging` | 全体 Coder | 遇到测试报错禁止盲猜乱改，强制先打印日志定位根因再改代码，防止连续重试熔断 |
| **`trailofbits/skills` / OWASP** | `security-review` | `vernal-reviewer` | 让审查官聚焦检查路径越界、硬编码密钥、注入风险与空壳占位代码 |

### 2. 必装开源 MCP 插件（给智能体装“真眼睛”和“实时字典”）

| 来源仓库 | MCP 插件 | 挂给哪个角色 | 解决什么痛点 |
| :--- | :--- | :--- | :--- |
| **`microsoft/playwright-mcp`** | Playwright 浏览器驱动（LoopForge 亦内置 `@playwright/mcp` 浏览器支持） | 前端 Coder / `vernal-reviewer` | 真正打开无头浏览器点按钮、玩两步游戏、看 Console 有无红字报错，彻底解决“语法没错但页面白屏/点不动” |
| **`upstash/context7`** | Context7 实时文档检索 | 全体 Coder（联网沙箱时） | 实时拉取第三方库最新官方文档，治好模型按两年前旧版 API 瞎写代码的幻觉 |
| **`oraios/serena`** | Serena LSP 符号级代码导航 | 全体 Coder / 架构师 | 接手几万行真实老仓库时精准查找函数定义与引用，节省大量 Token 且防止漏改调用方 |

### 3. 以后自己往代码里加时，改哪几个地方？

1. **存放资产文件**：在仓库新建 `autodev/assets/skills/<skill-name>/SKILL.md` 目录存放精选开源 Skill。
2. **通过 API 自动注入底座**：
   - 在 [client.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/client.py) 补两个调用 LoopForge `/api/skills` 与 `/api/mcp-servers` 的 `PUT` 方法；
   - 在 [bootstrap.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/bootstrap.py) 的 `build_vernal_agents()` 中，把原先空的 `"skills": []` 和 `"mcpServers": []` 填上对应资产 ID，并在 `build_vernal_workflows()` 中注册上述 4 条分泳道 SOP。
3. **让总监按卡路由 SOP**：
   - 在 [bridge.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/bridge.py) 派发工单创建 Issue 时，根据任务卡的 `role`（如 `contract` / `coder` / `integrator`）或涉及文件后缀（如 `.html/.css/.tsx`），自动选择对应的 `workflowId` 派单即可。

---

## 四、平台下一步演进总清单（按推荐落地顺序）

1. ✅【已完成】**多 Harness 全自动扫描与网页可视化切换（开箱即用 & 麒麟迁移前置项）**
   - **本机自动探测**：在 [config.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/config.py) 与 [start-runner.sh](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/deploy/loopforge/start-runner.sh) 中同时扫描宿主机已安装的 `codex`、`opencode`、`codebuddy`、`dsh`（DeepSeek Harness），谁已安装就绪就自动挂载并选为默认执行器（不再写死只认 `codex`）；同时补齐顶层 `codebuddy` / `dsh`（或 DeepSeek API 直连）适配器。
   - **网页点选免改文件**：在 Vernal「总控」顶栏新增「智能体编队与模型设置」弹窗，直接在网页下拉框修改各角色（总监 / 开发 / 审查）的 Harness、模型与并发数，自动写回 `config.toml` 并同步到底层 LoopForge 角色。
2. ✅【已完成】**内置资产军火库与多泳道 SOP 自动分流**
   - 预置精选开源 Skill（`frontend-design`、`test-driven-development`、`systematic-debugging`、`security-review`）+ Playwright MCP + 4 套专项 SOP，实现前端卡自动走浏览器视觉验收、后端卡自动走 TDD 验收。
3. **网页/前端成品「一键在线沙箱试玩」**
   - 在文件交接模式下，除了下载 `.zip` 产物包外，新增静态文件沙箱预览路由（如 `/vernal-preview/<milestone_id>/`），在里程碑卡片旁一键弹窗直接试玩生成的网页或游戏（免去手动敲 `python3 -m http.server`）。
4. **收尾遗留半成品项目或开源发布整理**
   - 按需续跑收尾之前暂停的 `orbit-shooter` 项目；若准备推上 GitHub 开源，检查 `.gitignore` 清理测试痕迹并补充英文/中文 Quickstart。
5. **存量老代码库（Brownfield）自动扫描摸底 & 预算护栏**
   - 圆桌开会前自动扫描现有代码目录与函数签名生成《Repo Map》喂给圆桌专家；在总控顶栏展示累计 Token/轮次消耗并支持设置预算上限。

---

## 五、麒麟（Kylin Linux）接入 `CodeBuddy` 与 `DeepSeek Harness` 适配指南

平台分为**「底层编码执行层（LoopForge）」**和**「顶层圆桌规划层（Vernal Python）」**两段，在麒麟系统上使用 **`CodeBuddy`**（`@tencent-ai/codebuddy-code`）和 **`DeepSeek Harness`**（`@deepseek-ai/dsh`，命令 `dsh`）时的现状与改造点如下：

### 1. 底层编码与审查执行层（LoopForge + `agentflow-runner`）—— **已原生支持**
- `LoopForge 0.4.0` 的 `agentflow-runner` 二进制中**已经原生内置**了 `CodeBuddy`（`provider_codebuddy.go`）和 `DeepSeek Harness`（`agent_cli.go` + `dshStream`）两个一等公民执行器，连 MCP 审批门禁桥接与 `.codebuddy/`、`.dsh/` 技能挂载都已写好。
- **仅需微调 1 行白名单**：打开 [bootstrap.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/loopforge/bootstrap.py) 的 `_executor_id_for()` 函数，将 `dsh` / `deepseek` 加入允许透传的执行器列表（目前写了 `("codex", "opencode", "claude", "codebuddy")`），或者直接在 `http://127.0.0.1:8787` 左侧「设置 → 角色配置」界面把 `vernal-coder` / `vernal-reviewer` 的执行器下拉框选为 `CodeBuddy` 或 `DeepSeek Harness` 即可直接跑任务。

### 2. 顶层圆桌会议与拆蓝图层（Vernal `autodev/harnesses/`）—— **按需二选一适配**
顶层召开 4 人圆桌会议、输出 `blueprint.json` / `plan.json` 是由 Vernal 的 Python 引擎直接调用实现的，目前 [autodev/harnesses/__init__.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/harnesses/__init__.py) 仅内置了 `codex` 与 `opencode` 两个 CLI 适配器：

- **方案 A（零改代码过渡用法）**：
  - 在麒麟电脑安装 `opencode` 并填入同一个 `DEEPSEEK_API_KEY`，在 `.autodev/config.toml` 设 `harness = "opencode"` 负责顶层开圆桌和拆蓝图；
  - 同时设 `coder_harness = "codebuddy"`（或在网页角色设置中绑定 `CodeBuddy` / `DeepSeek Harness`），让 95% 的实际写代码与审查工作交由 `CodeBuddy` 和 `DeepSeek Harness` 执行。
- **方案 B（彻底原生化改造，推荐，约 100 行代码）**：
  - **做法 1（加 CLI 适配器）**：参照 [opencode_cli.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/harnesses/opencode_cli.py)，在 `autodev/harnesses/` 下新增 `codebuddy_cli.py` 与 `dsh_cli.py`（实现 `run_structured_readonly` 拼接 JSON Schema 调起非交互命令），并在 [autodev/harnesses/__init__.py](file:///Users/zomeryang/Documents/春天/个人科研/lattice-mesh/autodev/harnesses/__init__.py) 的 `_build_single_harness()` 注册 `"codebuddy"` 与 `"dsh"`。
  - **做法 2（更轻更快：顶层直接走 DeepSeek HTTP API）**：因为顶层圆桌辩论和拆 JSON 蓝图只读不改文件，直接在 `autodev/harnesses/` 加一个 60 行的 `openai_compat_api.py`（直接 POST DeepSeek 官方或内网 vLLM 的 `/v1/chat/completions` 接口并开启 JSON 输出），速度比拉起任何 CLI 快数倍；底层写代码则 100% 交给 LoopForge 原生的 `CodeBuddy` + `DeepSeek Harness`。
