# Vernal (`vernal`)

**Vernal** 是构建在腾讯开源 `LoopForge`（`ghcr.io/tencent/loopforge:0.4.0`）执行底座之上的多智能体架构驱动研发平台：

- **统一前端与同源网关（`8787`）**：完整保留 LoopForge 原生工作台、需求列表、审批、统计、角色与流程管理页面，无侵入升级为 **Vernal 精密工程蓝图浅色风格**，并在左侧导航栏融合 **架构总控 & DAG 流程图 / 多智能体架构评审圆桌（Council + ADR） / 研发组领地与蓝图**。
- **两层协同架构**：
  1. **宏观大脑层（Vernal Brain）**：许愿/架构资料切片 → 2 轮多智能体架构评审圆桌（PM / 契约架构师 / QA 总监 → 研发总监 ADR 拍板 + 人类插话） → 研发组领地划分与滚动波次 DAG 排期 → 跨组依赖解锁与自动仲裁。
  2. **微观车间层（LoopForge Asset-as-Code）**：通过 `autodev/loopforge/bootstrap.py` 自动向 LoopForge 注册标准编码规则、开发/审查角色（`vernal-coder` / `vernal-reviewer`）与标准交付流水线（`vernal-file-sop` / `vernal-git-sop`），并自动挂载本机 `agentflow-runner` 调用 Codex / OpenCode 执行。

---

## 一键启动（macOS / Linux Docker）

```bash
# 自动检查并拉起 LoopForge + MySQL + Gitea 容器栈、挂载本机 Codex 执行器并启动统一网关
./vernal.sh
```

随后直接用浏览器打开统一入口：**http://127.0.0.1:8787**

> 📖 **完整日常操作、三人协作与麒麟迁移手册请直接阅读：[USAGE_GUIDE.md](USAGE_GUIDE.md)**

1. 首次进入在登录页注册/登录管理员账号；
2. 登录后顶栏会自动复用当前同源登录态签发令牌、绑定项目并装配标准角色与 SOP（零手动复制 Token）；
3. 在「架构总控」点击「输入需求」提交粗粒度许愿或放入架构文档 → 点击「圆桌与立项」 → 「批准蓝图」 → 「启动 Goal」，即可观看任务自动派发进 LoopForge SOP 真实执行。

---

## 常用命令

| 命令 | 用途 |
|---|---|
| `./vernal.sh` | 一键拉起底层容器、本机 Runner 与 `http://127.0.0.1:8787` 统一平台 |
| `.venv/bin/vernal serve [--port 8787]` | 单独启动或重启 Vernal 同源反向代理与编排网关 |
| `.venv/bin/vernal doctor` | 检查 `git`、`codex`、`opencode` 与本机工具链状态 |
| `./deploy/loopforge/up.sh` | 单独启动/检查 LoopForge + MySQL 8.4 + Gitea 1.22 容器栈 |
| `./deploy/loopforge/start-runner.sh` | 单独启动或重连本机 `agentflow-runner` 执行器 |
| `.venv/bin/pytest -v` | 运行全量自动化单元测试与反向代理集成测试 |
