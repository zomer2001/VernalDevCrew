# Vernal (`vernal`)

**Vernal** 是构建在腾讯开源 `LoopForge`（`ghcr.io/tencent/loopforge:0.4.0`）执行底座之上的多智能体架构驱动研发平台：

- **统一前端与同源网关（`8787`）**：完整保留 LoopForge 原生工作台、需求列表、审批、统计、角色与流程管理页面，无侵入升级为 **Vernal 精密工程蓝图浅色风格**，并在左侧导航栏融合 **架构总控 & DAG 流程图 / 多智能体架构评审圆桌（Council + ADR） / 研发组领地与蓝图**。
- **全链路统一走 LoopForge（宿主机免装 Python）**：
  1. **宏观大脑层（Vernal Brain，运行在容器内）**：许愿/架构资料切片 → 2 轮多智能体架构评审圆桌（PM / 契约架构师 / QA 总监 → 研发总监 ADR 拍板 + 人类插话） → 研发组领地划分与滚动波次 DAG 排期 → 跨组依赖解锁与自动仲裁。圆桌与拆蓝图全部通过 `LoopForgeHarness` 派发至规划专用 SOP（`vernal-plan-sop`）执行。
  2. **微观车间层（LoopForge Asset-as-Code）**：自动向 LoopForge 注入 5 套开源精选 `SKILL.md`、2 套预置 MCP 插件、规划与 4 泳道开发/审查角色与 SOP（后端 / 前端 / 契约 / 修复），并由本机 `agentflow-runner` 驱动你电脑已有的 **Codex / CodeBuddy / OpenCode / DeepSeek Harness (`dsh`) / Claude Code**。

---

## 同事首次使用（零 Python 依赖，只需 Docker + 自己的 Harness）

### 宿主机前置条件
1. **Docker**（macOS 可用 Docker Desktop 或 Colima；Linux / 麒麟系统安装 Docker + `docker compose` 插件）；
2. 任意一款本机已安装并登录的 **Harness CLI**：`codex`、`codebuddy`、`opencode`、`dsh`（DeepSeek Harness）或 `claude`（或者在网页「设置 → 凭据」填自己的模型 API Key 走容器沙箱）；
3. **不需要在宿主机安装 Python 或配置虚拟环境**。

### 两步启动
```bash
git clone <你的仓库地址>
cd <仓库目录>

# 第 1 次启动：自动拉起 LoopForge + MySQL + Gitea + 容器版 Vernal
./vernal.sh
```

1. 浏览器打开 **http://127.0.0.1:8787**，注册/登录你的本地账号（登录后顶栏会自动绑定当前项目并装配标准 Skill、角色与 SOP）；
2. **首次登录完成后，再执行一次 `./vernal.sh`**（或 `./deploy/loopforge/start-runner.sh`），脚本会自动免 Python 签发绑定码并将你电脑上的执行器（Runner）挂入平台；
3. 在「架构总控」顶栏点 **「编队与模型」** 即可网页点选各角色使用的 Harness 与模型，之后按「输入需求 → 圆桌与立项 → 批准蓝图 → 启动 Goal」跑完整研发流程。

> 📖 **完整日常操作、三人协作与麒麟迁移手册请直接阅读：[USAGE_GUIDE.md](USAGE_GUIDE.md)**

---

## 常用命令

| 命令 | 用途 |
|---|---|
| `./vernal.sh` | **默认纯容器模式**：一键拉起底层容器、容器版 Vernal（`8787`）与本机多引擎 Runner（免装 Python） |
| `./vernal.sh --build` | 源码更新后重新构建 Vernal 镜像并启动 |
| `./vernal.sh --stop` | 优雅停止全部容器与本机 Runner（所有数据完好保留） |
| `./vernal.sh --dev` | 开发者本地 `.venv` 调试模式 |
| `./deploy/loopforge/start-runner.sh` | 单独启动或重连本机 `agentflow-runner`（默认并发 3） |
| `.venv/bin/pytest -v` | 开发者运行全量自动化测试（39 项） |
