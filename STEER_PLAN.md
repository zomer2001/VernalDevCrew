# Vernal 用户使用规范、双轨执行底座与本机一键跑通工程任务书（供 Codex 直接执行）

> **文档定位**：本文档将 Vernal 的真实用户使用场景收敛为清晰的 **「0 怎么登录 · 1 做什么 · 2 怎么做」** 三层产品契约，并拆解为可直接落地的代码工程改造清单。
> **当前最高优先级（P0）**：
> 1. **先彻底跑通并验证「本机一键跑通（Local-First）」体验**（克隆仓库后单次 `./vernal.sh` 启动，无需二次敲命令即可连上本机 CLI 或使用公用沙箱，顺畅新建或修改本机项目）；
> 2. **公用 Docker 沙箱内置 `dsh`（DeepSeek Harness）+ `opencode` 双引擎兜底**（本机未装任何 CLI 的用户，只需配置公用额度池/DeepSeek API Key 即可开箱跑通全流程）；
> 3. 架构上原生兼容「一台物理机部署、多人浏览器登录共享公用池或各自远程挂载本机 CLI」的团队模式（P1）。

---

## 0. 产品使用契约定义（需求冻结）

所有后端状态判定、配置项默认值与前端交互文案，统一对齐以下三个正交维度，删除模糊的中间态：

### 0.1 怎么登录 / 怎么部署（Deployment & Auth）

| 模式 | 启动与登录方式 | 核心体验要求 | 阶段优先级 |
| :--- | :--- | :--- | :--- |
| **模式 A：个人本机版（Local-First）** | 用户 `git clone` 到个人电脑 → 执行**一次** `./vernal.sh [--mount ~/Projects]` → 打开 `http://127.0.0.1:8787` 完成首次注册/登录 | **严禁要求用户登录后再回终端敲第二次 `./vernal.sh`**。首次登录完成后，宿主机后台守护自动完成 `agentflow-runner` 绑定上线，立即可见本机 CLI 与挂载目录 | **P0（本轮必验通）** |
| **模式 B：物理机团队共享版（Shared Server）** | 管理员在物理机执行 `./vernal.sh --host 0.0.0.0` → 团队成员浏览器访问 `http://<物理机IP>:8787` 登录各自账号 | 成员电脑**零安装**即可直接使用物理机上的「公用 Docker 额度池」；若成员想用自己电脑的本地 CLI，在网页点「复制本机 CLI 接入命令」在个人终端跑一行即挂载上线 | **P1（架构须本次一并打通）** |

### 0.2 做什么（Project Source · 统一为 3 个清晰入口）

无论项目初始是空白目录还是存量老代码，按**代码物理位置与交付去向**统一收敛为 3 种项目来源：

1. **`scratch` — 全新项目（从文档 / 一句话想法从 0 到 1）**
   - 输入：网页填写许愿需求或上传 `.md`/`.txt` 架构文档。
   - 交付：平台托管工作区，支持产物 `.zip` 下载（及前端项目静态预览）或一键推送到内置 Gitea / 外部 Git。
2. **`local_folder` — 本机项目（空白文件夹或已有本地代码库，心智完全统一）**
   - 输入：在已挂载的宿主机目录树中点选本机文件夹（空目录 = 从零写进该目录；非空目录 = 自动扫描生成代码概览并在此基础上增量开发）。
   - 交付：改动回写至用户本机目录：
     - 小型纯文件目录（≤ 4 MiB 且未启用 Git 桥接）：保持 `files` 快照 + 里程碑合并冲突检测写回；
     - **中大型目录（> 4 MiB）或本地无可达远端的目录**：自动通过**内置 Gitea 透明桥接（Local Git Bridge）**中转，突破 4 MiB 限制，里程碑合入后安全同步回用户本机目录的独立分支（如 `vernal/<milestone_id>`），绝不暴力覆盖用户未提交的工作区改动。
3. **`git_repo` — 远端 Git 仓库项目（拉取仓库分支协作开发）**
   - 输入：GitHub / GitLab / 内置 Gitea 仓库 URL + 目标分支（+ 仓库认证凭据）。
   - 交付：Runner / Docker 沙箱克隆仓库并在隔离工作分支完成编码与独立审查，最终推送到远端分支 / MR。

### 0.3 怎么做（Execution Pool · 显式拆分为 2 套算力池）

彻底改变当前把“公用容器”和“本机 CLI”混杂在一起的做法，在配置与界面上显式引入 `execution_pool`：

| 执行池标识 | 界面名称 | 实际运行位置 | 凭据与扣费来源 | 适配的 Harness |
| :--- | :--- | :--- | :--- | :--- |
| **`local_runner`** | **连接本机 Harness CLI** | 用户电脑宿主机原生进程 `agentflow-runner`（**不在 Docker 内**，完整可见宿主机 PATH 与编译环境） | 复用用户本机 CLI 登录态或本机环境变量（注：LoopForge 上游对 `codebuddy` 需配个人 Key） | `codex` / `claude-code` / `codebuddy` / `opencode` / `dsh` |
| **`public_docker`** | **使用公用额度池（Docker 沙箱）** | 物理机/服务端 LoopForge 拉起的隔离容器（定制镜像 `Dockerfile.sandbox`） | 管理员在 `.env` 或网页配置的**公用额度池 Key**（如 `DEEPSEEK_API_KEY` / 内网 `BASE_URL`）或用户个人 Key | **预装 `dsh` + `opencode` 双兜底**（兼容 `codebuddy`） |
| **`auto`（默认）** | **智能自动选择** | 优先选用在线且目标 Harness 就绪的 `local_runner`；若无在线本机 Runner，则自动路由至已就绪的 `public_docker` 公用池 | 按实际命中的轨道自动匹配 | 自动匹配已就绪引擎 |

---

## 1. 现状根因定位与工程红线（动工前必读）

### 1.1 当前代码中的 4 个真实卡点（本次必须根治）
1. **为什么另一台装了 `opencode` 的电脑提示“找不到 CLI”**：
   - `autodev/config.py` 的 `probe_agent_runtimes()` 跑在 `vernal` 容器内，永远扫不到宿主机二进制；
   - 宿主机 CLI 依赖 `deploy/loopforge/start-runner.sh` 启动的 `agentflow-runner` 上报，但：① 首次运行 `./vernal.sh` 时用户尚未登录网页，脚本拿不到 `bind-code` 直接退出，未绑上 Runner；② `start-runner.sh` 的 `EXTRA_PATHS` 未覆盖 `nvm` / `fnm` / `volta` / `pnpm` / `mise` 等常见 Node/工具安装路径，导致 `agentflow-runner` 启动环境找不到 `opencode`/`dsh`/`codebuddy`。
2. **公用 Docker 沙箱与本机 Runner 互相锁死**：
   - `autodev/server/project_launcher.py` 的 `merge_runner_runtimes()`（第 393~405 行）写死了 `if not online_runners:` 就将所有 Harness 强制标为 `ready: False`；
   - `autodev/loopforge/bootstrap.py` 的 `ensure_vernal_loopforge_assets()` 写死 `sandboxProfileId = "local"` 且只要有在线 Runner 就强行 `set_default_runner`，导致用户既无法在无 Runner 时使用公用 Docker 沙箱，也无法在有 Runner 时切回公用池。
3. **公用 Docker 沙箱缺少 `dsh` + `opencode` 预装镜像与 DeepSeek 额度池入口**：
   - `deploy/loopforge/compose.yaml` 直接把 `AGENTFLOW_ISOLATED_IMAGE` 指向毛坯 `ghcr.io/tencent/loopforge:0.4.0`，且网页端仅有 `CodeBuddy` Key 保存框，没有公用池 / DeepSeek API Key 配置与注入链路；
   - `autodev/harnesses/cli_generic.py` 中 `DeepSeekHarness.build_cmd()` 漏传模型参数，且默认模型 `gpt-6.2-sol` 未与国产执行器解耦。
4. **本机目录接管受限于 `workspaces/` 默认挂载与 4 MiB 文件上限**：
   - `vernal.sh` 默认仅挂载 `$ROOT/workspaces`，用户选不到 `~/Projects` 等目录；且 `files` 模式超过 4 MiB 直接报错拦截。

### 1.2 严格工程约束（不可偏离）
- **不魔改、不重编译 LoopForge 上游 Go 源码**：全部通过 LoopForge 官方 REST API、环境变量、自定义沙箱镜像层（`FROM ghcr.io/tencent/loopforge:0.4.0`）与 `start-runner.sh` 实现。
- **前端 CSP 零违规**：LoopForge 开启了严格 CSP（禁止内联事件），所有新增 HTML **严禁出现 `onclick=` / `onchange=` 等内联属性**，一律用 `addEventListener` 绑定；修改 `autodev/server/static/styles.css` 后必须运行 `.venv/bin/python scripts/build_vernal_stage_css.py` 重新生成作用域样式，并递增 `autodev/server/proxy.py` 中的静态资源版本号。
- **宿主机免装 Python 原则不变**：宿主机启动脚本（`vernal.sh`、`up.sh`、`start-runner.sh`）保持纯 POSIX/Bash + `curl` 实现，不得在宿主机直接调用 `python3`；SOP 内嵌脚本延续 `python3` / `node` 双运行时兼容写法。
- **不覆盖无关方案文档**：保留仓库现有的 `STEERING_INTEGRATION_PLAN.md` 与数据库既有表结构兼容（SQLite 新增字段一律使用幂等 `ALTER TABLE` 迁移或 `meta` 键值）。

---

## 2. 待执行代码工程任务清单（按 Task 1 → Task 6 顺序落地）

### Task 1：本机一键启动、首次登录自动挂 Runner 与全路径 CLI 探测

**涉及文件**：`vernal.sh`、`deploy/loopforge/start-runner.sh`、新建 `deploy/loopforge/wait-and-bind-runner.sh`

1. **消除“首次启动需跑两次 `./vernal.sh`”的断点**：
   - 新建 `deploy/loopforge/wait-and-bind-runner.sh`（PID 记录于 `deploy/loopforge/.runner/binder.pid`，日志写 `deploy/loopforge/.runner/binder.log`）：
     - 当 `start-runner.sh` 检测到尚无 `state.json` 且 `/vernal-api/runner/bind-code` 返回未绑定（`401` / 空）时，`vernal.sh` 自动在后台拉起该守护脚本（若已在运行则幂等跳过）；
     - 守护脚本每 3 秒轮询一次 `http://127.0.0.1:${VERNAL_PORT}/vernal-api/runner/bind-code?format=raw`（最长等待 30 分钟），一旦用户在浏览器完成首次登录触发 `/vernal-api/runtime/bind`，立即拿到一次性绑定码并调用 `./deploy/loopforge/start-runner.sh` 完成本机 Runner 上线，随后守护进程自行退出；
     - 在 `./vernal.sh --stop` 中一并清理 `binder.pid` 进程。
2. **扩充宿主机 CLI 路径扫描（根治另一台电脑找不到 `opencode` / `dsh` / `codebuddy`）**：
   - 在 `deploy/loopforge/start-runner.sh` 构造 `PATH` 时，除现有目录外，自动探测并前置追加以下常见安装路径（存在即加入）：
     - Node 版本管理器：`"$HOME/.nvm/versions/node"/*/bin`（按版本号倒序取最新）、`"$HOME/.fnm/aliases/default/bin"`、`"$HOME/.local/share/fnm/aliases/default/bin"`、`"$HOME/.volta/bin"`、`"$HOME/.asdf/shims"`、`"$HOME/.local/share/mise/shims"`
     - 包管理器全局 bin：`"$HOME/Library/pnpm"`、`"$HOME/.local/share/pnpm"`、`"$HOME/.bun/bin"`、`"$HOME/.cargo/bin"`、`"/usr/local/go/bin"`、`"$HOME/go/bin"`
     - 各 CLI 专属目录：`"$HOME/.opencode/bin"`、`"$HOME/.dsh/bin"`、`"$HOME/.codebuddy/bin"`、`"$HOME/.claude/local"`
   - 打印每个扫描到的 CLI 的**绝对路径 + 版本号**到终端与 `runner.log`，若之前已有旧 `agentflow-runner` 进程但 `PATH` 发生变化，支持 `./vernal.sh --restart-runner` 重启刷新。
3. **新增直观目录挂载参数 `--mount <dir>` 与一键体检 `--doctor`**：
   - `./vernal.sh --mount <宿主机代码根目录>`：解析绝对路径并校验目录存在后，将其持久化写入 `deploy/loopforge/.env` 的 `VERNAL_HOST_PROJECTS_DIR='<abs_path>'`，后续直接执行 `./vernal.sh` 自动沿用该挂载根，无需每次手传环境变量；
   - `./vernal.sh --doctor`：纯 Shell 一次性检查并清晰打印 5 项状态：① Docker 守护进程与 4 个容器健康度；② 公用沙箱镜像是否就绪及内置 `dsh`/`opencode` 版本；③ 公用额度池 Key 是否已配置；④ 本机 `agentflow-runner` 进程与控制面在线心跳；⑤ 宿主机扫描到的 `codex / codebuddy / opencode / dsh / claude` 路径与凭据状态。

---

### Task 2：公用 Docker 沙箱兜底镜像（`dsh` + `opencode`）与公用额度池注入

**涉及文件**：新建 `deploy/loopforge/Dockerfile.sandbox`、`deploy/loopforge/compose.yaml`、`deploy/loopforge/up.sh`、`vernal.sh`、`autodev/server/app.py`

1. **构建定制公用沙箱镜像 `deploy/loopforge/Dockerfile.sandbox`**：
   - 基座：`FROM ${LOOPFORGE_IMAGE:-ghcr.io/tencent/loopforge:0.4.0}`（确保同时兼容 `linux/amd64` 与 `linux/arm64` 麒麟系统）；
   - 在镜像中确保 `python3`、`node`、`npm`、`git`、`curl` 可用，并全局预装 **`@deepseek-ai/dsh`**（命令 `dsh`）与 **`opencode-ai`**（命令 `opencode`，按目标架构安装对应 npm 包或官方二进制；若某包在特定架构源不可用需给出明确构建日志与回退安装脚本）；
   - 镜像构建末尾执行 `opencode --version` 与 `dsh --version` 冒烟校验，确保可执行文件位于 `/usr/local/bin`（所有沙箱用户可读可执行）；
   - 预置容器启动/环境初始化脚本（或在全局配置模板中支持从环境变量 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` 自动生成 `~/.dsh/` 与 `~/.local/share/opencode/auth.json` 配置），使沙箱无需交互式登录即可非交互执行任务。
2. **接入 `compose.yaml` 与公用额度池环境变量透传**：
   - 在 `deploy/loopforge/compose.yaml` 中：
     - 将 `loopforge` 服务的 `AGENTFLOW_ISOLATED_IMAGE` 改为 `${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}`；
     - 在 `up.sh` / `vernal.sh` 启动时，若本地不存在 `autodev-runtime-sandbox:latest` 或 `Dockerfile.sandbox` 指纹变化，自动执行 `docker build -f deploy/loopforge/Dockerfile.sandbox -t autodev-runtime-sandbox:latest deploy/loopforge`；
     - 支持从 `deploy/loopforge/.env` 读取公用额度池配置（`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`VERNAL_PUBLIC_DEFAULT_MODEL`、`VERNAL_PUBLIC_REASONING_MODEL`）并透传给 `vernal` 与 `loopforge` 服务（同时核实 LoopForge 0.4.0 创建沙箱容器时的环境变量注入方式：若 LoopForge 隔离容器默认不转发全部宿主环境变量，通过自定义沙箱镜像内的环境文件挂载或 wrapper 脚本安全注入）。
3. **新增公用额度池配置 REST 接口（`autodev/server/app.py`）**：
   - `GET /vernal-api/runtime/public-pool`：返回公用 Docker 沙箱可用状态（镜像是否就绪、内置 engines `["dsh", "opencode"]`、各 Key 是否已配置及脱敏前缀如 `sk-****a1b2`、`deepseek_base_url`、推荐默认模型，**严禁返回明文 Key**）；
   - `POST /vernal-api/runtime/public-pool`（限本机回环或已认证管理员）：接收 `{deepseek_api_key, deepseek_base_url, openai_api_key, openai_base_url, default_model, reasoning_model}`，原子更新持久化凭据文件并实时刷新沙箱运行环境与状态缓存，让用户直接在网页填入 Key 即可立即激活公用 Docker 额度池。

---

### Task 3：解耦双轨执行池互锁 Bug + `dsh` / `opencode` 深度适配

**涉及文件**：`autodev/config.py`、`autodev/server/project_launcher.py`、`autodev/loopforge/bootstrap.py`、`autodev/loopforge/bridge.py`、`autodev/harnesses/cli_generic.py`、`autodev/server/app.py`

1. **配置模型升级（`autodev/config.py`）**：
   - 在 `DEFAULT_CONFIG_TOML` 与 `Settings` 中新增：
     - `execution_pool: str = "auto"`（合法值：`"auto"` | `"local_runner"` | `"public_docker"`）；
   - 新增按 Harness 区分的默认模型解析逻辑（优化 `Settings.model_for(role)`）：
     - 当目标执行器为 `dsh` 且未显式指定角色模型（或全局模型仍为初始值 `gpt-6.2-sol`）时：规划/圆桌/架构师/总监默认返回 `deepseek-reasoner`（或 `VERNAL_PUBLIC_REASONING_MODEL`），开发/审查默认返回 `deepseek-chat`（或 `VERNAL_PUBLIC_DEFAULT_MODEL`）；
     - 当目标执行器为 `opencode` 且全局仍为 `gpt-6.2-sol` 时：若已配 DeepSeek 凭据，默认返回 `deepseek/deepseek-chat`（否则留空交由用户本机 `opencode` 默认模型处理），彻底杜绝把 OpenAI 专属模型名传给 `dsh` / `opencode` 导致报错。
2. **重构双轨能力探测（`autodev/server/project_launcher.py`）**：
   - **先核实 LoopForge 0.4.0 真实契约**：对照 `deploy/loopforge/bin/agentflow-runner` 与运行态 `/api/executors`、`/api/sandboxes`、`/api/runners` 返回报文，确认 `opencode` 与 `dsh` 上报的准确 `provider` 字段名（增加别名映射，如 `{"deepseek": "dsh", "deepseek-harness": "dsh", "opencode-cli": "opencode"}`）；
   - 重构 `merge_runner_runtimes()`：
     - 输出中保留兼容顶层 `runtimes`（反映**当前生效 `execution_pool`** 下的真实可用性），并新增 `pools` 字段分别给出两套池子的独立明细：
       - `pools.local_runner`: `{online, runner_names, runtimes: {...}}`
       - `pools.public_docker`: `{available, sandbox_image, key_configured, runtimes: {"dsh": ..., "opencode": ..., "codebuddy": ...}}`
     - **核心修复**：当 `online_runners` 为空时，仅将 `pools.local_runner` 标为离线；若 `execution_pool` 为 `"public_docker"`（或 `"auto"` 且 `pools.public_docker` 已配置好镜像与 API Key），顶层 `dsh` / `opencode` 必须返回 `installed=True, ready=True, source="public_docker"`！
   - `choose_preferred_harness_from_runtimes()`：在命中 `public_docker` 时按 `("dsh", "opencode", "codebuddy")` 顺序自动选择已就绪引擎。
3. **修复派单预检与沙箱路由（`autodev/loopforge/bridge.py` & `bootstrap.py`）**：
   - 修改 `check_runners_online(client, settings)`：
     - 若有效执行池为 `"public_docker"`：直接跳过个人 Runner 在线拦截（改为校验公用沙箱与凭据是否就绪）；
     - 若有效执行池为 `"local_runner"`：无在线 Runner 时返回精准提示（区分“首次未登录绑定”与“Runner 进程未启动”）。
   - 修改 `ensure_vernal_loopforge_assets(client, project_id, settings)`：
     - 先通过 `/api/sandboxes` 与 `/api/runners` 查询服务端可用沙箱 ID（如 `"docker"` / `"isolated"` / `"local"`）与在线个人 Runner；
     - 当有效执行池为 `"public_docker"` 时：将 Vernal 生成的 SOP `sandboxProfileId` 设为服务端 Docker 隔离沙箱 Profile，并确保任务不会被错误强绑到离线的个人 Runner（核实 `PUT /api/runners/default` 传空或 workflow 级沙箱绑定的准确行为）；
     - 当有效执行池为 `"local_runner"` 时：绑定在线个人 Runner 并设为默认执行路由。
4. **补全 `DeepSeekHarness` CLI 适配器（`autodev/harnesses/cli_generic.py`）**：
   - 对照真实 `dsh --help` 参数规范补齐 `DeepSeekHarness.build_cmd()` 的模型参数（如 `--model <model>`，仅在 `model` 非空时传入）；
   - 确保 `DEEPSEEK_API_KEY` 与 `DEEPSEEK_BASE_URL` 正确注入子进程 `env`，并兼容 `dsh --json` 输出中的文本块、`reasoning_content` 与 Token 用量统计。

---

### Task 4：本机项目无缝接管与突破 4 MiB 限制（内置 Gitea 透明桥接）

**涉及文件**：新建 `autodev/local_git_bridge.py`、`autodev/server/app.py`、`autodev/engine.py`、`autodev/server/project_launcher.py`

1. **统一本机项目（空白文件夹 & 已有仓库）的接入逻辑**：
   - 在 `POST /vernal-api/project/create` (`mode == "local_folder"`) 中：
     - 支持选定**完全空白的本机新建目录**（此前空白目录无文件时 `slices == 0`，若用户同时填了 `wish` 或上传了 `docs` 则正常切片立项；若未填则在进入总控后引导输入首条需求，不得报错拦截）；
     - 保留小体积纯文件目录（文本总量 ≤ 4 MiB 且用户选 `files`）的轻量快照 + `merge_milestone` 冲突检测写回能力。
2. **新增 `autodev/local_git_bridge.py`（解决 > 4 MiB 真实本地项目与无远端 Git 目录痛点）**：
   - 触发条件：`mode == "local_folder"` 时，若目录源码超过 `FILE_EXCHANGE_MAX_TOTAL`（4 MiB），或用户选择 `collaboration_mode == "git"` 但该目录没有可被容器/Runner 访问的远端 URL（如无 `origin`，或 `origin` 为仅本机可见的相对路径）：
     1. **自动确保内置 Gitea 可用并签发内部同步凭据**：在内置 Gitea（`settings.gitea_url`，容器内 `http://gitea:3000` / 宿主机 `http://127.0.0.1:3000`）幂等初始化服务账户与名为 `vernal-local/<project_id>` 的私有镜像仓库（若 Gitea 尚未完成首次初始化，通过 Gitea CLI / Admin API 自动完成静默初始化，零人工点网页安装向导）；
     2. **安全基线推送**：若用户本机目录尚无 `.git`，自动初始化 Git 并生成初始基线提交（自动忽略 `node_modules`、`.venv`、`dist`、`target`、`.autodev` 等构建产物）；添加名为 `vernal-bridge` 的 remote（绝不覆盖用户原有的 `origin`！），将当前分支推送到内置 Gitea；
     3. **双地址适配与绑定**：向 LoopForge `PUT /api/repositories` 注册该 Gitea 仓库（注意区分容器内访问地址与宿主机 Runner 访问地址，确保无论 `public_docker` 还是 `local_runner` 都能成功 `git clone` 和 `git push`）；
     4. **里程碑安全回写本机目录**：当里程碑合入完成时，自动从 `vernal-bridge` fetch 最新集成提交到用户本机目录的独立交付分支 `vernal/milestone-<milestone_id>`（若用户当前工作树干净且处于托管新建分支，可选快进更新；若用户工作树有未提交改动，仅落地到 `vernal/milestone-<milestone_id>` 分支并在界面提示 `git merge vernal/milestone-<id>`，**严禁丢失或覆盖用户本地未提交代码**）。

---

### Task 5：前端「双轨执行池 + 三入口启动台」交互重构（严守 CSP）

**涉及文件**：`autodev/server/static/index.html`、`autodev/server/static/js/main.js`、`autodev/server/static/js/launcher.js`、`autodev/server/static/js/runtime.js`、`autodev/server/static/styles.css`、`autodev/server/proxy.py`

1. **「编队与模型设置」升级为「执行池、凭据与智能体编队」面板（`#agent-config-form`）**：
   - 在面板最顶部新增 **「2. 怎么做：选择算力执行池」** 三选一切换卡（绑定 `POST /vernal-api/agent/config` 新增的 `execution_pool` 字段）：
     - **`auto`（推荐 · 智能自动）**：优先用本机已连上的 CLI，没装则自动走公用 Docker 池；
     - **`local_runner`（连接本机 Harness CLI）**：展示在线 Runner 名称、宿主机扫到的 5 个 CLI 状态胶囊，以及 **「复制本机 CLI 接入命令」** 按钮（调用后端生成带一次性 `bind-code` 和当前服务器地址的单行终端命令，供团队物理机模式下成员一键挂载个人电脑 CLI）+ **「重新扫描」** 按钮；
     - **`public_docker`（公用 Docker 额度池 · `dsh` + `opencode` 兜底）**：展示公用沙箱镜像状态、`dsh` / `opencode` 就绪徽章，以及内嵌的 **「公用额度池 / DeepSeek 凭据配置」** 折叠卡（输入 `DEEPSEEK_API_KEY`、可选 `DEEPSEEK_BASE_URL`、默认模型并一键保存到 `POST /vernal-api/runtime/public-pool`），保留原有的 `CodeBuddy API Key` 配置卡按需展开。
   - 顶栏状态胶囊（`#codex-pill` / `#codebuddy-pill` / `#opencode-pill` / `#dsh-pill`）旁增加一枚醒目的当前执行池标识徽章（如 `🖥️ 本机 CLI` 或 `🐳 公用沙箱`），彻底消除“不知道当前跑在容器还是本机”的困惑。
2. **「项目启动台」三入口对齐（`#project-launcher` & `js/launcher.js`）**：
   - 三个主 Tab 文案与引导严格对应 `0.2` 定义：
     - **Tab 1：全新项目（文档 / 许愿从 0 开始）**
     - **Tab 2：本机项目（空白或已有本地文件夹）** —— 清晰显示当前已挂载的宿主机根目录 `display_root`，旁附一行可复制提示：`如需切换挂载目录：./vernal.sh --mount ~/你的代码目录`；当检测到所选目录 > 4 MiB 时，前端友好提示“将自动启用内置 Gitea 透明桥接，支持大仓库无损增量开发”。
     - **Tab 3：Git 仓库项目（拉取远端分支协作）**
3. **样式构建与缓存刷新**：
   - 所有事件通过 `addEventListener` 注册（**零内联 `onclick`**）；
   - 执行 `.venv/bin/python scripts/build_vernal_stage_css.py` 同步生成 `vernal-stage.css`，并更新 `autodev/server/proxy.py` 中的 `OVERLAY_VERSION` 查询串。

---

### Task 6：全量自动化测试与「本机一键跑通」P0 端到端验收

**涉及文件**：新建 `tests/test_execution_pools_and_dsh.py`、新建 `tests/test_local_git_bridge.py`、更新 `README.md` 与 `USAGE_GUIDE.md`

#### 6.1 必过自动化单测与静态检查
1. `tests/test_execution_pools_and_dsh.py`（使用 `httpx.MockTransport`，不依赖外网）：
   - 验证 `online_runners == []` 但公用池已配 `DEEPSEEK_API_KEY` 时：`execution_pool="public_docker"` 与 `execution_pool="auto"` 下 `dsh` 和 `opencode` 均返回 `ready=True`，且 `check_runners_online()` **不拦截**派单；
   - 验证 `execution_pool="local_runner"` 且无在线 Runner 时：明确返回离线拦截提示，不会静默跑进错误环境；
   - 验证 `GET/POST /vernal-api/runtime/public-pool` 写入凭据、掩码脱敏返回、以及非授权跨机访问返回 `401/403`；
   - 验证 `Settings.model_for()` 在 `dsh` / `opencode` 下自动映射 `deepseek-reasoner` / `deepseek-chat`，不泄漏 `gpt-6.2-sol`，且 `DeepSeekHarness.build_cmd()` 正确携带模型参数与环境变量。
2. `tests/test_local_git_bridge.py`：
   - 构造一个 > 4 MiB 的本地目录（或含自定义代码的无 remote 本地目录），验证自动初始化 Git、推送到模拟/本地 bare 仓并安全回写至 `vernal/milestone-M1` 分支，且当用户工作区存在未提交改动时绝不覆盖原文件。
3. 运行全量门禁命令，要求 **100% 通过**：
   ```bash
   .venv/bin/pytest -v
   bash -n vernal.sh deploy/loopforge/up.sh deploy/loopforge/start-runner.sh deploy/loopforge/wait-and-bind-runner.sh
   for f in autodev/server/static/vernal-overlay.js autodev/server/static/js/*.js; do node --check "$f"; done
   ```

#### 6.2 P0 真机端到端验收清单（必须逐项实测跑通后再收工）
1. **体检与单次启动验证**：
   - 清理旧 `.runner/state.json` 模拟新用户克隆环境，执行**单次** `./vernal.sh --mount <本机测试父目录>`；
   - 在浏览器打开 `http://127.0.0.1:8787` 注册/登录，**不再回终端敲任何命令**，等待 ≤ 10 秒观察后台守护自动将本机 `agentflow-runner` 绑定上线，执行 `./vernal.sh --doctor` 确认全部绿灯，本机已安装的 CLI（如 `codex` / `opencode` / `codebuddy` / `dsh`）路径与版本准确显示在网页中。
2. **轨道 2B 验收（公用 Docker 沙箱 `dsh` + `opencode` 兜底）**：
   - 在网页将执行池切换为 **`public_docker`（公用 Docker 额度池）**（甚至可手动停掉本机 `agentflow-runner` 验证零本机依赖），填入有效 `DEEPSEEK_API_KEY`；
   - 分别选用 `dsh` 和 `opencode` 新建一个最小空白项目（如命令行计算器或待办小工具），完整跑通：**许愿切片 → 圆桌评审产出 ADR → 批准蓝图 → 自动编码与独立审查 → 下载 `.zip` 产物并运行通过**。
3. **轨道 2A + 本机项目验收（本机 CLI 改本地文件夹）**：
   - 将执行池切回 **`local_runner`（连接本机 CLI）**，在启动台选择「本机项目」，点选 `<本机测试父目录>` 下的一个本地项目目录，下达一个增量修改需求，完整跑通圆桌、编码、审查，并核实改动已安全落地到该本机目录（小目录原地写回 / 大目录落地 `vernal/milestone-*` 分支）。
4. 最后同步更新 `README.md` 与 `USAGE_GUIDE.md` 的快速开始章节，按「0 怎么登录 · 1 做什么 · 2 怎么做」重写清晰的新人指引。
