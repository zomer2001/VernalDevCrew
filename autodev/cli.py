"""AutoDev Hub 命令行入口（Typer + Rich）。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from autodev.config import (
    Settings,
    Workspace,
    WorkspaceNotFound,
    detect_toolchains,
    probe_agent_runtimes,
)
from autodev.engine import Engine, OpsError, PlanningError, init_workspace

app = typer.Typer(help="Vernal — 多智能体自主协同研发平台", no_args_is_help=True)
console = Console()


def _load_engine(directory: Path | None) -> Engine:
    try:
        ws = Workspace.find(directory)
    except WorkspaceNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    return Engine(ws)


@app.command()
def link(
    server: str = typer.Argument(..., help="团队 Vernal 网关地址"),
    code: str = typer.Option("", "--code", help="首次接入使用网页签发的一次性配对码"),
    directory: Path = typer.Option(Path("."), "--dir", help="要接入的本机项目目录"),
    with_worker: bool = typer.Option(True, "--with-worker/--no-worker", help="复用本机个人 CLI"),
):
    """将本机目录连接到团队工作台，持续接收版本交付。"""
    from autodev.host_link import run_link
    try:
        asyncio.run(run_link(server, code, directory, with_worker))
    except KeyboardInterrupt:
        pass


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="目标研发仓库目录"),
    name: str | None = typer.Option(None, "--name", help="项目显示名称"),
    collaboration: str | None = typer.Option(None, "--collaboration", help="文件交接 files 或版本库协作 git"),
) -> None:
    """初始化目标研发工作区（创建 .autodev/ 与 inputs/）。"""
    ws = init_workspace(path, name=name, collaboration_mode=collaboration)
    console.print(f"[green]工作区已初始化[/green]：{ws.root}")
    console.print(f"下一步：将架构文档放入 [bold]{ws.inputs_dir}[/bold]，然后运行 [cyan]autodev ingest[/cyan]")


@app.command()
def doctor(
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录（可选）"),
) -> None:
    """检查本机 git、codex、opencode 与工具链状态。"""
    settings = Settings()
    ws: Workspace | None = None
    try:
        ws = Workspace.find(directory)
        settings = ws.load_settings()
    except WorkspaceNotFound:
        pass

    table = Table(title="AutoDev 环境自检")
    table.add_column("检查项", style="bold")
    table.add_column("状态")
    table.add_column("详情")

    git_bin = shutil.which("git")
    if git_bin:
        ver = subprocess.run([git_bin, "--version"], capture_output=True, text=True).stdout.strip()
        table.add_row("git", "[green]OK[/green]", f"{ver} ({git_bin})")
    else:
        table.add_row("git", "[red]MISSING[/red]", "未找到 git")

    runtimes = probe_agent_runtimes(settings)
    cx = runtimes["codex"]
    if cx["installed"]:
        table.add_row("codex", "[green]OK[/green]", f"{cx['version']} ({cx['bin']}) · {cx['note']}")
    else:
        table.add_row("codex", "[yellow]MISSING[/yellow]", cx["note"])

    oc = runtimes["opencode"]
    if oc["installed"]:
        tag = "[green]READY[/green]" if oc["ready"] else "[yellow]NO API KEY[/yellow]"
        table.add_row("opencode", tag, f"{oc['version']} ({oc['bin']}) · {oc['note']}")
    else:
        table.add_row("opencode", "[dim]OPTIONAL[/dim]", "未安装（可 brew install opencode）")

    tools = detect_toolchains()
    table.add_row(
        "可用工具链",
        "[green]OK[/green]" if tools else "[yellow]NONE[/yellow]",
        ", ".join(f"{k} ({v})" for k, v in tools.items()) or "无",
    )

    if ws is not None:
        table.add_row(
            "当前工作区",
            "[green]OK[/green]",
            f"{ws.root}（harness={settings.harness}, concurrency={settings.max_concurrency}）",
        )
    else:
        table.add_row("当前工作区", "[dim]NONE[/dim]", "当前目录未初始化（可运行 autodev init）")

    console.print(table)


@app.command()
def ingest(
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """解析并切分 inputs/ 下的架构资料。"""
    eng = _load_engine(directory)
    try:
        count = eng.ingest()
    except OpsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print(f"[green]摄入完成[/green]：共 {count} 个切片，索引位于 {eng.ws.catalog_path}")


@app.command()
def plan(
    extra: str = typer.Option("", "--extra", "-e", help="给 PM / 研发总监的补充要求"),
    mode: str | None = typer.Option(None, "--mode", "-m", help="规划模式：goal（PM+总监分组滚动）| classic（单次拆单）"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """运行规划（goal 模式：PM 规格补全 → 总监分组蓝图；classic 模式：单次拆解 DAG）。"""
    eng = _load_engine(directory)
    eng.db.listeners.append(lambda ev: console.print(f"[dim]{ev['event_type']}[/dim] [{ev['actor']}] {ev['message']}"))
    try:
        result = asyncio.run(eng.plan(extra_instructions=extra, mode=mode))
    except (OpsError, PlanningError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    task_count = len(result.initial_tasks) if hasattr(result, "initial_tasks") else len(result.tasks)
    console.print(
        f"[green]立项规划完成[/green]：{len(result.milestones)} 个里程碑，首轮展开 {task_count} 个任务。\n"
        f"查看详细蓝图：[bold]{eng.ws.plan_md_path}[/bold]"
    )
    if eng.settings.require_plan_approval:
        console.print("确认无误后运行 [cyan]autodev approve-plan[/cyan]，再运行 [cyan]autodev run[/cyan]。")


@app.command("approve-plan")
def approve_plan(
    actor: str = typer.Option("owner", "--actor", help="审批人姓名"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """批准当前研发计划，允许调度器启动。"""
    eng = _load_engine(directory)
    try:
        eng.approve_plan(actor=actor)
    except OpsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print("[green]规划已批准[/green]，可运行 [cyan]autodev run[/cyan] 开始研发。")


@app.command()
def run(
    watch: bool = typer.Option(False, "--watch", "-w", help="无就绪任务时不退出，持续等待人工接管或合并"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """运行 DAG 调度器，并行开发、验证、审查并集成里程碑。"""
    eng = _load_engine(directory)
    eng.db.listeners.append(
        lambda ev: console.print(
            f"[dim]{ev['created_at'][11:19]}[/dim] [cyan]{ev['event_type']}[/cyan]"
            + (f" [{ev['task_id']}]" if ev.get("task_id") else "")
            + f" {ev['message']}"
        )
    )
    from autodev.scheduler import Scheduler
    sched = Scheduler(eng)
    try:
        final = asyncio.run(sched.run(watch=watch))
    except KeyboardInterrupt:
        console.print("\n[yellow]已收到中断信号，正在清理...[/yellow]")
        raise typer.Exit(130)
    except OpsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print(f"调度结束，项目状态：[bold]{final.value}[/bold]")


@app.command()
def status(
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """查看项目、里程碑与各任务当前状态。"""
    eng = _load_engine(directory)
    try:
        snap = eng.snapshot()
    finally:
        eng.db.close()

    u = snap["usage_total"]
    console.print(
        f"[bold]{snap['name']}[/bold] ({snap['root']}) · 状态：[cyan]{snap['status']}[/cyan] · "
        f"切片：{snap['slices']} · Tokens：in {u['input']:,} / cache {u['cached']:,} / out {u['output']:,}"
    )

    if snap["milestones"]:
        mt = Table(title="里程碑路线图")
        mt.add_column("ID", style="bold")
        mt.add_column("标题")
        mt.add_column("排期")
        mt.add_column("状态")
        mt.add_column("报告")
        for m in snap["milestones"]:
            mt.add_row(
                m["milestone_id"],
                m["title"],
                "已展开" if m.get("planned", True) else "待滚动展开",
                m["status"],
                m["report_path"] or "-",
            )
        console.print(mt)

    if snap["tasks"]:
        tt = Table(title="任务 DAG")
        tt.add_column("ID", style="bold")
        tt.add_column("里程碑")
        tt.add_column("研发组 / 角色")
        tt.add_column("状态")
        tt.add_column("重试/仲裁")
        tt.add_column("依赖")
        tt.add_column("标题")
        for t in snap["tasks"]:
            tt.add_row(
                t["task_id"],
                t["milestone_id"],
                f"{t.get('squad_name') or t['module']} / {t['role']}",
                t["status"],
                f"{t['retry_count']}/{t['max_retries']} · 仲{t.get('arbitration_count', 0)}",
                ",".join(t["depends_on"]) or "-",
                t["title"],
            )
        console.print(tt)


@app.command()
def takeover(
    task_id: str = typer.Argument(..., help="熔断任务 ID，如 T-003"),
    note: str = typer.Option("", "--note", "-n", help="给智能体的排错指导意见"),
    verify_only: bool = typer.Option(False, "--verify-only", help="手工改好 worktree 后跳过编码直接重新验证"),
    actor: str = typer.Option("owner", "--actor", help="接管人姓名"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """接管熔断（FUSED）任务：提供指导意见续跑，或手工修复后重新验证。"""
    eng = _load_engine(directory)
    try:
        eng.steer(task_id, note=note, actor=actor, verify_only=verify_only)
    except OpsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print(f"[green]{task_id} 已重置为 READY[/green]，运行 [cyan]autodev run[/cyan] 继续执行。")


@app.command()
def merge(
    milestone_id: str = typer.Argument(..., help="等待合并的里程碑 ID"),
    actor: str = typer.Option("owner", "--actor", help="签字审批人"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """批准并将里程碑集成分支合入主干。"""
    eng = _load_engine(directory)
    try:
        eng.merge_milestone(milestone_id, actor=actor)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print(f"[green]里程碑 {milestone_id} 已合入主干[/green]")


@app.command()
def guard(
    task_id: str = typer.Option(..., "--task", "-t", help="要校验的任务 ID（供本地或 LoopForge Script 节点调用）"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="目标工作区/仓库根目录"),
    worktree: Path | None = typer.Option(None, "--worktree", "-w", help="被检查的 git 工作区路径（默认当前目录）"),
) -> None:
    """独立运行确定性目录白名单与契约保护门禁（退出码 0=通过，1=越界并已自动回滚）。"""
    from autodev import verifier

    eng = _load_engine(directory)
    try:
        task = eng.db.get_task(task_id)
        if task is None:
            console.print(f"[red]任务不存在：{task_id}[/red]")
            raise typer.Exit(2)
        target_dir = (worktree or Path.cwd()).resolve()
        protected = [] if task.role in ("contract", "bootstrap") else eng.settings.protected_paths
        res = verifier.guard_worktree(target_dir, task.allowed_paths, protected)
        if not res.ok:
            console.print(f"[red]门禁拦截失败（越界文件已回滚）：[/red]\n{res.describe()}")
            raise typer.Exit(1)
        console.print(f"[green]门禁通过[/green]：{task_id} 全部 {len(res.changed)} 处改动均落在授权路径内。")
    finally:
        eng.db.close()


@app.command()
def council(
    extra: str = typer.Option("", "--extra", "-e", help="给圆桌会议的补充要求"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="工作区目录"),
) -> None:
    """单独启动多智能体架构评审圆桌（PM / 架构师 / QA 总监 → 研发总监拍板 ADR）。"""
    from autodev.council import run_council

    eng = _load_engine(directory)
    eng.db.listeners.append(lambda ev: console.print(f"[dim]{ev['event_type']}[/dim] [{ev['actor']}] {ev['message']}"))
    try:
        cons = asyncio.run(run_council(eng, extra_instructions=extra))
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        eng.db.close()
    console.print(f"[green]架构评审圆桌拍板完成[/green]：形成 {len(cons.adrs)} 项 ADR，规格书已写入 {eng.ws.prd_md_path}")


def _resolve_serve_engine(directory: Path | None) -> Engine:
    if directory is not None:
        return _load_engine(directory)
    try:
        return Engine(Workspace.find(None))
    except WorkspaceNotFound:
        ws_root = Path.cwd() / "workspaces"
        ws_root.mkdir(parents=True, exist_ok=True)
        for entry in sorted(ws_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                cand = Workspace(entry)
                if cand.config_path.is_file() and cand.db_path.is_file():
                    try:
                        if Settings.load(cand.config_path).loopforge_project_id:
                            return Engine(cand)
                    except Exception:
                        continue
        default_ws = ws_root / "vernal-default"
        if (default_ws / ".autodev" / "state.db").is_file():
            return Engine(Workspace(default_ws))
        return Engine(init_workspace(default_ws, name="Vernal Workspace", collaboration_mode="files"))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（局域网共享可填 0.0.0.0）"),
    port: int = typer.Option(8787, "--port", "-p", help="监听端口"),
    directory: Path | None = typer.Option(None, "--dir", "-C", help="初始工作区目录（省略时自动挂载 workspaces/ 并随顶栏项目切换）"),
) -> None:
    """启动统一 Vernal 平台网关（同源代理 LoopForge 前端与运行时）。"""
    import uvicorn

    from autodev.server.app import create_app

    eng = _resolve_serve_engine(directory)
    fastapi_app = create_app(eng)
    console.print(f"[green]Vernal 平台已启动[/green]：http://{host}:{port} （当前挂载工作区：{eng.ws.root.name}，随页面项目切换自动联动）")
    try:
        uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")
    finally:
        eng.db.close()


if __name__ == "__main__":
    app()
