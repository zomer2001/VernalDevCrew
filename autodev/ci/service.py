"""持续集成与交付服务：隔离 CI 分支/工作流发布、双通道原子对账（轮询 + 签名回调）、失败自动返工与受控 CD 部署（WP-3）。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
from typing import TYPE_CHECKING, Any

import httpx

from autodev import gitops
from autodev.artifacts import FileExchange, materialize
from autodev.ci.detect import render_vernal_ci_workflow, write_ci_workflow_non_destructive
from autodev.ci.providers.base import CIHandle, CIProvider
from autodev.ci.providers.external import GitLabCIProvider, JenkinsCIProvider, WebhookCIProvider
from autodev.ci.providers.gitea import GiteaCIProvider
from autodev.db import Database, Milestone, now_iso
from autodev.loopforge.bridge import sign_context_sig, verify_context_sig
from autodev.models import MilestoneStatus, PlanTask, TaskStatus
from autodev.process.runner import decide_gate, ensure_gate_record, load_template
from autodev.security import ensure_ci_signing_key, get_ci_signing_key, load_workspace_credentials

if TYPE_CHECKING:
    from autodev.engine import Engine

_TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


class CISignatureError(PermissionError):
    """CI 回调签名校验失败（映射为 HTTP 403）。"""


def _hydrate_ci_run(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["summary"] = json.loads(d.pop("summary_json", None) or "{}")
    return d


def list_ci_runs(db: Database, milestone_id: str | None = None) -> list[dict[str, Any]]:
    if milestone_id:
        rows = db._query("SELECT * FROM ci_runs WHERE milestone_id=? ORDER BY ci_run_id DESC", (milestone_id,))
    else:
        rows = db._query("SELECT * FROM ci_runs ORDER BY ci_run_id DESC")
    return [_hydrate_ci_run(r) for r in rows]


def get_ci_run(db: Database, ci_run_id: int) -> dict[str, Any] | None:
    rows = db._query("SELECT * FROM ci_runs WHERE ci_run_id=?", (ci_run_id,))
    return _hydrate_ci_run(rows[0]) if rows else None


def build_ci_provider(
    engine: "Engine",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CIProvider:
    s = engine.settings
    creds = load_workspace_credentials(engine.ws.state_dir)
    ci_token = creds.get("ci_token", "")
    provider_name = (s.ci_provider or "gitea").strip().lower()
    if provider_name == "none":
        provider_name = "gitea"
    if provider_name == "webhook":
        return WebhookCIProvider(s.ci_url, token=ci_token, transport=transport)
    if provider_name == "gitlab":
        return GitLabCIProvider(s.ci_url, token=ci_token, transport=transport)
    if provider_name == "jenkins":
        return JenkinsCIProvider(s.ci_url, token=ci_token, transport=transport)
    return GiteaCIProvider(s.ci_url or s.gitea_url, token=ci_token, transport=transport)


def ci_gate_enabled(engine: "Engine") -> bool:
    tpl = load_template(engine.ws, engine.settings)
    deliv = next((s for s in tpl.stages if s.id == "integration_delivery" and s.enabled), None)
    has_tpl_ci = bool(deliv and any(g.kind == "ci" and g.blocking for g in deliv.gates))
    explicit_prov = (engine.settings.ci_provider or "none").strip().lower()
    return has_tpl_ci or (explicit_prov not in ("", "none"))


def make_callback_url(engine: "Engine", milestone_id: str, round_no: int) -> tuple[str, str]:
    pid = engine.settings.loopforge_project_id or engine.ws.root.name
    secret = ensure_ci_signing_key(engine.ws, engine.settings)
    sig = sign_context_sig(secret, pid, f"ci:{milestone_id}:{round_no}")
    base = engine.settings.vernal_public_url.rstrip("/")
    return f"{base}/vernal-api/ci/callback/{pid}/{milestone_id}/{round_no}/{sig}", sig


def _resolve_ci_remote_info(engine: "Engine", repo_dir: Path) -> dict[str, Any]:
    explicit_remote = (engine.settings.ci_git_remote or engine.db.meta_get("ci_git_remote") or "").strip()
    gitea_mirror = engine.db.meta_get("gitea_mirror") == "1"
    private_remote = (engine.db.meta_get("private_remote") or "").strip()
    git_remote = explicit_remote
    if not git_remote and not gitea_mirror and not private_remote:
        if (repo_dir / ".git").exists():
            out = gitops.git(["remote"], repo_dir, check=False).stdout.split()
            if "ci" in out:
                git_remote = "ci"
            elif "origin" in out:
                git_remote = "origin"
    return {
        "repo_dir": str(repo_dir),
        "git_remote": git_remote,
        "gitea_mirror": gitea_mirror,
        "private_remote": private_remote,
    }


def prepare_isolated_ci_commit(
    engine: "Engine",
    ms: Milestone,
    round_no: int,
    candidate_ref: str,
    *,
    callback_url: str = "",
) -> tuple[str, str, Path, str]:
    """在独立隔离分支 `vernal/ci/<ms>-r<round>` 上非破坏性写入 vernal-ci.yml 并生成真实 40 位 Git Commit SHA。"""
    ref = f"vernal/ci/{ms.milestone_id}-r{round_no}"
    tasks = [t for t in engine.db.list_tasks(milestone_id=ms.milestone_id) if t.status != TaskStatus.CANCELLED]

    if engine.settings.collaboration_mode == "files":
        exchange = FileExchange(engine.ws, engine.db)
        files: dict[str, str] = {}
        if candidate_ref:
            files = exchange.get(candidate_ref)
            if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
                raise ValueError("CI 候选必须是完整文件快照")
        elif tasks:
            files = exchange.baseline([t.task_id for t in tasks])

        ci_repo = engine.ws.state_dir / "ci-repo" / ms.milestone_id / str(round_no)
        ci_repo.mkdir(parents=True, exist_ok=True)
        if not (ci_repo / ".git").exists():
            gitops.git(["init", "-q", "-b", "main"], ci_repo)
            gitops.git(
                ["-c", "user.name=Vernal CI", "-c", "user.email=vernal-ci@localhost", "commit", "--allow-empty", "-qm", "vernal(ci): init"],
                ci_repo,
            )

        gitops.git(["checkout", "-q", "-B", ref], ci_repo)
        for child in list(ci_repo.iterdir()):
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

        materialize(files, ci_repo)
        wf_yaml = render_vernal_ci_workflow(ci_repo, ms, tasks, callback_url=callback_url)
        wf_path = write_ci_workflow_non_destructive(ci_repo, wf_yaml)
        gitops.git(["add", "-A", "--", "."], ci_repo)
        gitops.git(["add", "-f", "--", ".gitea/workflows/vernal-ci.yml"], ci_repo)
        gitops.git(
            [
                "-c",
                "user.name=Vernal CI",
                "-c",
                "user.email=vernal-ci@localhost",
                "commit",
                "--allow-empty",
                "-qm",
                f"vernal(ci): publish files snapshot for {ms.milestone_id} round {round_no}",
            ],
            ci_repo,
        )
        ci_sha = gitops.head_sha(ci_repo)
        return ref, ci_sha, ci_repo, str(wf_path)

    # Git 协作模式：从当前里程碑集成分支/提交切出临时隔离 worktree，不污染集成分支或主分支
    root = engine.ws.root
    gitops.ensure_repo(root, engine.settings.base_branch)
    base_rev = candidate_ref.strip()
    if base_rev and gitops.git(["rev-parse", "--verify", "--quiet", f"{base_rev}^{{commit}}"], root, check=False).returncode:
        raise ValueError("无法检出指定的 CI 候选提交")
    if not base_rev:
        base_rev = ms.branch_name if (ms.branch_name and gitops.branch_exists(root, ms.branch_name)) else engine.settings.base_branch

    ci_wt = engine.ws.integration_dir / f"{ms.milestone_id}-ci-r{round_no}"
    gitops.remove_worktree(root, ci_wt)
    gitops.delete_branch(root, ref)
    gitops.create_worktree(root, ci_wt, ref, base_rev)
    try:
        wf_yaml = render_vernal_ci_workflow(ci_wt, ms, tasks, callback_url=callback_url)
        wf_path = write_ci_workflow_non_destructive(ci_wt, wf_yaml)
        gitops.git(["add", "-f", "--", ".gitea/workflows/vernal-ci.yml"], ci_wt)
        gitops.git(
            [
                "-c",
                "user.name=Vernal CI",
                "-c",
                "user.email=vernal-ci@localhost",
                "commit",
                "--allow-empty",
                "-qm",
                f"vernal(ci): publish workflow for {ms.milestone_id} round {round_no}",
            ],
            ci_wt,
        )
        ci_sha = gitops.head_sha(ci_wt)
    finally:
        gitops.remove_worktree(root, ci_wt)
    return ref, ci_sha, root, str(wf_path)


async def trigger_milestone_ci(
    engine: "Engine",
    ms: Milestone,
    candidate_ref: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    from autodev.review.service import current_milestone_candidate_ref

    db = engine.db
    resolved_candidate = (candidate_ref or current_milestone_candidate_ref(engine, ms.milestone_id)).strip()
    pid = engine.settings.loopforge_project_id or engine.ws.root.name
    provider = build_ci_provider(engine, transport=transport)
    with db.transaction():
        active = db._query("SELECT * FROM ci_runs WHERE milestone_id=? AND status IN ('QUEUED','RUNNING') "
                           "ORDER BY round DESC LIMIT 1", (ms.milestone_id,))
        if active and active[0]["candidate_ref"] == resolved_candidate:
            return _hydrate_ci_run(active[0])
        if resolved_candidate:
            db.meta_set(f"candidate_ref:{ms.milestone_id}", resolved_candidate)
        round_no = db._query("SELECT COALESCE(MAX(round),0)+1 n FROM ci_runs WHERE milestone_id=?",
                             (ms.milestone_id,))[0]["n"]
        ts = now_iso()
        ci_id = db._exec(
            "INSERT INTO ci_runs(milestone_id,round,provider,ref,commit_sha,candidate_ref,status,started_at,created_at)"
            " VALUES(?,?,?,'','',?,'QUEUED',?,?)",
            (ms.milestone_id, round_no, provider.name, resolved_candidate, ts, ts)).lastrowid
        db.update_milestone(ms.milestone_id, status=MilestoneStatus.CI_RUNNING, failure_reason=None)
        ensure_gate_record(
            db,
            stage_id="integration_delivery",
            gate_id="milestone_ci",
            kind="ci",
            scope_type="milestone",
            scope_id=ms.milestone_id,
            summary=f"里程碑 {ms.milestone_id} 第 {round_no} 轮 CI 正在执行",
            new_round_if_failed=True,
            force_new_round=True,
        )
    cb_url, _ = make_callback_url(engine, ms.milestone_id, round_no)
    try:
        ref, ci_sha, repo_dir, wf_path = await asyncio.to_thread(
            prepare_isolated_ci_commit,
            engine,
            ms,
            round_no,
            resolved_candidate,
            callback_url=cb_url,
        )
        db._exec("UPDATE ci_runs SET ref=?,commit_sha=? WHERE ci_run_id=?", (ref, ci_sha, ci_id))
        remote_ctx = _resolve_ci_remote_info(engine, repo_dir)

        if provider.name != "gitea" and remote_ctx.get("git_remote"):
            await asyncio.to_thread(
                gitops.git,
                ["push", "--force", "--", str(remote_ctx["git_remote"]), f"{ci_sha}:refs/heads/{ref}"],
                repo_dir,
            )

        handle = await provider.trigger(
            pid,
            ref,
            ci_sha,
            {
                "milestone_id": ms.milestone_id,
                "round": round_no,
                "callback_url": cb_url,
                "candidate_ref": resolved_candidate,
                "workflow_path": wf_path,
                **remote_ctx,
            },
        )

    except Exception as exc:
        _finalize_ci_outcome(engine, get_ci_run(db, ci_id), final_status="FAILURE", summary={"reason": str(exc)})
        raise
    db._exec("UPDATE ci_runs SET external_id=?,url=?,status=CASE WHEN status='QUEUED' THEN 'RUNNING' "
             "ELSE status END WHERE ci_run_id=?", (handle.external_id or "", handle.url, ci_id))
    db.add_event(
        "ci.triggered",
        f"已发布并触发里程碑 {ms.milestone_id} 第 {round_no} 轮持续集成（provider={provider.name}, ref={ref}, sha={ci_sha[:10]}）",
        actor="CIEngine",
        payload={"ci_run_id": ci_id, "milestone_id": ms.milestone_id, "round": round_no, "ref": ref, "commit_sha": ci_sha},
    )
    return get_ci_run(db, ci_id) or {}


def _finalize_ci_outcome(
    engine: "Engine",
    run_row: dict[str, Any],
    *,
    final_status: str,
    summary: dict[str, Any],
    log_tail: str = "",
    url: str = "",
) -> dict[str, Any]:
    from autodev.review.policy import effective_review_policy
    from autodev.review.service import current_milestone_candidate_ref, open_review

    db = engine.db
    ci_id = int(run_row["ci_run_id"])
    ms_id = str(run_row["milestone_id"])
    round_no = int(run_row["round"])
    run_cand = str(run_row.get("candidate_ref") or "").strip()
    ts = now_iso()

    should_open_ms_review = False
    should_auto_merge = False
    current_cand = ""

    with db.transaction():
        # 1. CAS 原子声明结案当前 ci_run_id：并发回调与轮询仅赢家可执行后续状态机迁移
        claim = db._exec(
            "UPDATE ci_runs SET status=?, url=?, summary_json=?, log_tail=?, finished_at=?"
            " WHERE ci_run_id=? AND status IN ('QUEUED', 'RUNNING')",
            (
                final_status,
                url or run_row.get("url") or "",
                json.dumps(summary, ensure_ascii=False),
                log_tail or run_row.get("log_tail") or "",
                ts,
                ci_id,
            ),
        )
        if claim.rowcount != 1:
            return get_ci_run(db, ci_id) or run_row

        # 2. 校验是否为当前里程碑最新轮次、当前候选产物且里程碑仍处于 CI_RUNNING
        max_r_row = db._query("SELECT COALESCE(MAX(round), 0) AS max_r FROM ci_runs WHERE milestone_id=?", (ms_id,))
        latest_round = int(max_r_row[0]["max_r"] or 0) if max_r_row else round_no
        ms = db.get_milestone(ms_id)
        current_cand = current_milestone_candidate_ref(engine, ms_id)

        is_stale = (
            round_no != latest_round
            or ms is None
            or ms.status != MilestoneStatus.CI_RUNNING
            or bool(run_cand and current_cand and run_cand != current_cand)
        )
        if is_stale:
            db.add_event(
                "ci.stale_ignored",
                f"忽略过时 CI 轮次结果（{ms_id} round={round_no}, latest={latest_round}）",
                actor="CIEngine",
            )
            return get_ci_run(db, ci_id) or run_row

        gate_rec = ensure_gate_record(
            db,
            stage_id="integration_delivery",
            gate_id="milestone_ci",
            kind="ci",
            scope_type="milestone",
            scope_id=ms_id,
        )
        if gate_rec["status"] == "PENDING":
            decide_gate(
                db,
                int(gate_rec["record_id"]),
                "pass" if final_status == "SUCCESS" else "fail",
                actor="CIEngine",
                comment=f"第 {round_no} 轮 CI 结果：{final_status}",
                evidence=summary,
            )

        if final_status == "SUCCESS":
            db.update_milestone(ms_id, status=MilestoneStatus.AWAITING_MERGE, failure_reason=None)
            db.add_event("ci.passed", f"里程碑 {ms_id} 第 {round_no} 轮 CI 通过", actor="CIEngine")
            if effective_review_policy(engine.ws, engine.settings) == "milestone":
                should_open_ms_review = True
            elif not engine.settings.require_merge_approval:
                should_auto_merge = True
        else:
            # FAILURE / TIMEOUT 自动创建 bugfix 返工单或超限熔断
            max_rounds = max(1, int(engine.settings.ci_max_fix_rounds or 3))
            failed_step = str(summary.get("failed_step") or summary.get("reason") or final_status)
            if round_no >= max_rounds:
                reason = f"持续集成连续 {round_no} 轮未通过（{failed_step}），已达自动修复上限 ({max_rounds})，请人工介入排查"
                db.update_milestone(ms_id, status=MilestoneStatus.FAILED, failure_reason=reason)
                db.add_event("ci.fused", reason, actor="CIEngine")
            else:
                ms_tasks = [t for t in db.list_tasks(milestone_id=ms_id) if t.status != TaskStatus.CANCELLED]
                union_paths: list[str] = []
                seen_p: set[str] = set()
                union_verify: list[str] = []
                seen_v: set[str] = set()
                for t in ms_tasks:
                    for p in t.allowed_paths:
                        if p not in seen_p:
                            seen_p.add(p)
                            union_paths.append(p)
                    for v in t.verify_cmds:
                        if v not in seen_v:
                            seen_v.add(v)
                            union_verify.append(v)

                fix_tid = db.next_task_id()
                tail_snippet = "\n".join((log_tail or "").splitlines()[-150:]) or json.dumps(summary, ensure_ascii=False)
                fix_task = PlanTask(
                    task_id=fix_tid,
                    milestone_id=ms_id,
                    squad=ms_tasks[-1].squad if ms_tasks else "bugfix",
                    module=ms_tasks[-1].module if ms_tasks else "ci-fix",
                    role="coder",
                    title=f"修复 CI 失败：{failed_step[:60]}",
                    description=(
                        f"里程碑 {ms_id} 第 {round_no} 轮持续集成失败（状态：{final_status}）。\n"
                        f"请根据以下 CI 失败日志定位根因并补充回归测试：\n\n```\n{tail_snippet[:6000]}\n```"
                    ),
                    acceptance_criteria=["修复 CI 报错根因", "全部关联单元与集成测试通过"],
                    depends_on=[t.task_id for t in ms_tasks if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)],
                    doc_slice_ids=[],
                    allowed_paths=union_paths or ["."],
                    verify_cmds=union_verify or ["true"],
                    priority=5,
                )
                db.insert_single_task(fix_task, engine.settings.max_retries)
                db.update_milestone(ms_id, status=MilestoneStatus.PENDING, failure_reason=None)
                db.add_event(
                    "ci.autofix_dispatched",
                    f"第 {round_no} 轮 CI 未通过（{failed_step}），已自动创建缺陷修复任务 {fix_tid}",
                    task_id=fix_tid,
                    actor="CIEngine",
                )

    if should_open_ms_review:
        open_review(
            engine,
            scope_type="milestone",
            scope_id=ms_id,
            milestone_id=ms_id,
            head_ref=current_cand or run_cand or str(run_row["commit_sha"]),
            base_ref=engine.settings.base_branch if engine.settings.collaboration_mode == "git" else "",
            risk_reasons=["持续集成已通过，按策略要求执行里程碑合入前整体代码审查 (policy=milestone)"],
        )
    elif should_auto_merge:
        engine.merge_milestone(ms_id, actor="CIEngine")

    return get_ci_run(db, ci_id) or {}


async def poll_ci_run(
    engine: "Engine",
    ci_run_id: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    run_row = get_ci_run(engine.db, ci_run_id)
    if run_row is None:
        raise KeyError(f"CI 记录不存在：{ci_run_id}")
    if run_row["status"] not in ("QUEUED", "RUNNING"):
        return run_row

    # 超时判定
    started_raw = run_row.get("started_at") or run_row.get("created_at")
    if started_raw:
        elapsed = (datetime.now().astimezone() - datetime.fromisoformat(started_raw)).total_seconds()
        if elapsed > float(engine.settings.ci_timeout_sec or 1800):
            return _finalize_ci_outcome(
                engine,
                run_row,
                final_status="TIMEOUT",
                summary={"reason": f"超过超时时间 {engine.settings.ci_timeout_sec}s"},
            )

    if run_row["status"] == "QUEUED" and not run_row.get("external_id"):
        return run_row
    provider = build_ci_provider(engine, transport=transport)
    pid = engine.settings.loopforge_project_id or engine.ws.root.name
    handle = CIHandle(
        provider=run_row["provider"],
        project=pid,
        ref=run_row["ref"],
        commit_sha=run_row["commit_sha"],
        external_id=str(run_row.get("external_id") or ""),
        url=run_row.get("url") or "",
    )
    st = await provider.poll(handle)
    if st.status in ("QUEUED", "RUNNING"):
        engine.db._exec(
            "UPDATE ci_runs SET status=?, url=? WHERE ci_run_id=? AND status IN ('QUEUED', 'RUNNING')",
            (st.status, st.url or run_row.get("url") or "", ci_run_id),
        )
        return get_ci_run(engine.db, ci_run_id) or run_row

    tail = ""
    if st.status == "FAILURE":
        tail = await provider.fetch_log_tail(handle, lines=150)
    return _finalize_ci_outcome(engine, run_row, final_status=st.status, summary=st.summary, log_tail=tail, url=st.url)


def _parse_strict_bool(value):
    if isinstance(value, (str, int, bool)):
        normalized = str(value).strip().lower()
        if normalized in ("true", "1"):
            return True
        if normalized in ("false", "0"):
            return False
    raise ValueError("passed 只接受布尔值或 true、false、1、0")


def handle_ci_callback(
    engine: "Engine",
    project_id: str,
    milestone_id: str,
    round_no: int,
    sig: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    expected_pid = engine.settings.loopforge_project_id or engine.ws.root.name
    if project_id not in {expected_pid, engine.ws.root.name}:
        raise KeyError(f"未知项目：{project_id}")

    expected_secret = get_ci_signing_key(engine.ws, engine.settings)
    keys = {expected_secret, *engine.settings.verification_keys()}
    import time
    from autodev.security import load_workspace_credentials
    if engine.settings.legacy_signing_until > time.time():
        keys.add(load_workspace_credentials(engine.ws.state_dir).get("ci_webhook_secret", ""))
    if not any(key and verify_context_sig(key, project_id, f"ci:{milestone_id}:{round_no}", sig) for key in keys):
        raise CISignatureError("CI 回调签名校验未通过")

    rows = engine.db._query(
        "SELECT * FROM ci_runs WHERE milestone_id=? AND round=? ORDER BY ci_run_id DESC LIMIT 1",
        (milestone_id, round_no),
    )
    if not rows:
        raise KeyError(f"未找到对应的 CI 运行记录：{milestone_id} round {round_no}")
    run_row = _hydrate_ci_run(rows[0])
    if run_row["status"] in ("SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED"):
        return run_row

    passed = _parse_strict_bool(payload.get("passed", str(payload.get("status") or "").upper() == "SUCCESS"))
    log_tail = str(payload.get("log_tail") or "")
    return _finalize_ci_outcome(
        engine,
        run_row,
        final_status="SUCCESS" if passed else "FAILURE",
        summary=payload,
        log_tail=log_tail,
    )


def write_release_notes_and_tag(engine: "Engine", milestone_id: str, version_no: int, commit_sha: str | None = None) -> str:
    """里程碑合入后打版本标签并生成结构化 Release Notes。"""
    tag_name = f"vernal-{milestone_id}-v{version_no}"
    if engine.settings.collaboration_mode == "git" and (engine.ws.root / ".git").exists():
        target = commit_sha or "HEAD"
        current = gitops.git(["rev-parse","--verify","--quiet","refs/tags/" + tag_name], engine.ws.root, check=False)
        if current.returncode:
            gitops.git(["tag", tag_name, target], engine.ws.root)
        elif current.stdout.strip() != gitops.git(["rev-parse", target], engine.ws.root).stdout.strip():
            raise ValueError("发布标签已绑定其他提交")
        from autodev import local_git_bridge
        root = engine.ws.root
        ref = "refs/tags/" + tag_name
        sha = gitops.head_sha(root, target)
        access = engine.db.meta_get("private_remote")
        if access:
            local_git_bridge.publish_private_ref(root, access, ref, sha)
        elif engine.db.meta_get("gitea_mirror") == "1":
            internal, _ = local_git_bridge.urls(engine.settings.loopforge_project_id or root.name)
            local_git_bridge.push_once(root, internal, sha, ref)
        source = engine.db.meta_get("git_delivery_source")
        if source or (not access and gitops.has_remote(root, "origin")):
            local_git_bridge._push_with_lease(lambda args: gitops.git(args, root).stdout,
                                              source or "origin", ref, sha, "")

    ms = engine.db.get_milestone(milestone_id)
    tasks = [t for t in engine.db.list_tasks(milestone_id=milestone_id) if t.status != TaskStatus.CANCELLED]
    ci_list = list_ci_runs(engine.db, milestone_id)
    latest_ci = ci_list[0] if ci_list else None

    notes = [
        f"# Release Notes · {tag_name}",
        "",
        f"- **里程碑**：{milestone_id} — {ms.title if ms else ''}",
        f"- **交付目标**：{ms.goal if ms else ''}",
        f"- **基线提交**：`{(commit_sha or '-')[:12]}`",
        f"- **CI 状态**：`{latest_ci['status'] if latest_ci else 'SKIPPED'}`",
        "",
        "## 完成任务清单",
    ]
    for t in tasks:
        notes.append(f"- `{t.task_id}` [{t.role}] {t.title}")

    rel_dir = engine.ws.reports_dir / "releases"
    rel_dir.mkdir(parents=True, exist_ok=True)
    out = rel_dir / f"{tag_name}.md"
    out.write_text("\n".join(notes) + "\n", "utf-8")
    return tag_name


def resolve_controlled_deploy_cmd(engine: "Engine", target: str) -> str:
    """仅从受控服务端配置（process.yaml 的 deliver 段或 config.toml 的 deploy_cmd）读取部署命令。"""
    tpl = load_template(engine.ws, engine.settings)
    deliver_cfg = tpl.deliver if isinstance(tpl.deliver, dict) else {}
    targets_map = deliver_cfg.get("targets")
    if isinstance(targets_map, dict):
        target_entry = targets_map.get(target)
        if isinstance(target_entry, dict) and str(target_entry.get("deploy_cmd") or "").strip():
            return str(target_entry["deploy_cmd"]).strip()
        if isinstance(target_entry, str) and target_entry.strip():
            return target_entry.strip()
    if str(deliver_cfg.get("deploy_cmd") or "").strip():
        return str(deliver_cfg["deploy_cmd"]).strip()
    return (engine.settings.deploy_cmd or "").strip()


async def trigger_deployment(
    engine: "Engine",
    milestone_id: str,
    *,
    target: str = "staging",
    version_no: int | None = None,
    actor: str = "",
) -> dict[str, Any]:
    clean_actor = (actor or "").strip()
    if not clean_actor or clean_actor.lower() in ("anonymous", "guest", "unknown", "system"):
        raise ValueError("必须提供有效的部署操作者身份 (actor)")

    # 若工作区启用了成员 RBAC 表，校验操作者具备部署权限（admin / architect / developer）
    from autodev.governance.rbac import check_role_permission
    check_role_permission(engine.db, clean_actor, ("admin", "architect", "developer"),
                          project=engine.settings.loopforge_project_id or engine.ws.root.name)

    clean_target = (target or "staging").strip()
    if not _TARGET_NAME_RE.fullmatch(clean_target):
        raise ValueError(f"非法的部署环境标识：{target}")

    deliveries = engine.db.list_milestone_deliveries(milestone_id)
    if not deliveries:
        raise ValueError(f"里程碑 {milestone_id} 尚无已合入的交付版本，禁止部署")
    by_version = {int(d["version_no"]): d for d in deliveries}
    ver = int(version_no if version_no is not None else deliveries[-1]["version_no"])
    if ver not in by_version:
        raise ValueError(f"里程碑 {milestone_id} 不存在已交付版本 v{ver}（当前已交付：{sorted(by_version)}），禁止部署")
    delivery_rec = by_version[ver]

    controlled_cmd = resolve_controlled_deploy_cmd(engine, clean_target)
    tag_name = f"vernal-{milestone_id}-v{ver}"
    ts = now_iso()

    status = "SUCCESS"
    log_tail = f"已登记已交付版本 {tag_name} 部署至 {clean_target}"
    if controlled_cmd:
        proc = await asyncio.create_subprocess_shell(
            controlled_cmd,
            cwd=str(engine.ws.root),
            env={
                **engine.shared_env(),
                "VERNAL_RELEASE_TAG": tag_name,
                "VERNAL_DEPLOY_TARGET": clean_target,
                "VERNAL_COMMIT_SHA": str(delivery_rec.get("commit_sha") or ""),
                "VERNAL_ARTIFACT_DIGEST": str(delivery_rec.get("artifact_digest") or ""),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        log_tail = "\n".join(out_b.decode("utf-8", errors="replace").splitlines()[-100:])
        status = "SUCCESS" if proc.returncode == 0 else "FAILED"

    cur = engine.db._exec(
        "INSERT INTO deployments(milestone_id, version_no, target, status, actor, log_tail, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?)",
        (milestone_id, ver, clean_target, status, clean_actor, log_tail, ts),
    )
    dep_id = int(cur.lastrowid)
    engine.db.add_event("cd.deployed", f"版本 {tag_name} 部署至 {clean_target}：{status}", actor=clean_actor)
    return dict(engine.db._query("SELECT * FROM deployments WHERE id=?", (dep_id,))[0])


def list_deployments(db: Database, milestone_id: str | None = None) -> list[dict[str, Any]]:
    if milestone_id:
        return [dict(r) for r in db._query("SELECT * FROM deployments WHERE milestone_id=? ORDER BY id DESC", (milestone_id,))]
    return [dict(r) for r in db._query("SELECT * FROM deployments ORDER BY id DESC")]
