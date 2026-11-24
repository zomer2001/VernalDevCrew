import { $, actorName, api, esc, projectQuerySuffix, toast } from "./api.js";

function deliveryHistory(state, milestoneId) {
  return (state.deliveries || []).filter(d => d.milestone_id === milestoneId).map(d => {
    const version = Number(d.version_no);
    if (d.collab_mode === "files") {
      return `<a class="btn btn-sm" download href="/vernal-api/milestones/${encodeURIComponent(milestoneId)}/files.zip?version=${version}${projectQuerySuffix("&")}">下载 v${version} ZIP</a>`;
    }
    return `<div>v${version} <code>${esc(d.archive_branch)}</code> · <code>${esc(d.rolling_branch)}</code>
      ${d.compare_url ? `<a href="${esc(d.compare_url)}" target="_blank" rel="noopener">去创建或查看合并请求</a>` : ""}</div>`;
  }).join("");
}

export async function refreshInbox(state, onRefresh, onOpenTask, onOpenReport) {
  const el = $("inbox-list");
  const msEl = $("milestone-list");
  const data = await api("/vernal-api/runtime/approvals");

  const items = [];
  if (data.plan_pending) {
    items.push(`<div class="adr-card" style="border-left:4px solid #d97706">
      <div class="task-top"><strong>立项蓝图待批准</strong><span class="status-pill PLANNED">PLANNED</span></div>
      <p class="muted" style="margin:4px 0 8px">研发总监已完成研发组划分与里程碑路线图，确认后即可启动全自动滚动研发。</p>
      <button class="btn btn-primary btn-sm" id="inbox-approve-plan">批准立项蓝图</button>
    </div>`);
  }

  for (const g of data.loopforge_gates || []) {
    items.push(`<div class="adr-card" style="border-left:4px solid #2563eb">
      <div class="task-top">
        <strong>[LoopForge 门禁] ${esc(g.workflow_name)} · ${esc(g.node_name)}</strong>
        <span class="status-pill RUNNING">Run ${esc(g.run_id.slice(0, 10))}</span>
      </div>
      <p class="muted" style="margin:4px 0 8px">Issue <code>${esc(g.issue_id || "-")}</code> · 等待人工审阅产物后放行或退回。</p>
      <div class="inline-form">
        <input id="lf-note-${esc(g.run_id)}" type="text" placeholder="审批意见或打回修改原因…" />
        <button class="btn btn-primary btn-sm" data-lf-approve="${esc(g.run_id)}" data-node="${esc(g.node_id)}">批准放行</button>
        <button class="btn btn-danger btn-sm" data-lf-reject="${esc(g.run_id)}" data-node="${esc(g.node_id)}" data-to="${esc(g.reject_to)}">打回返工</button>
      </div>
    </div>`);
  }

  for (const t of data.fused_tasks || []) {
    items.push(`<div class="adr-card" style="border-left:4px solid #e11d48">
      <div class="task-top">
        <strong>[熔断接管] ${esc(t.task_id)} · ${esc(t.title)}</strong>
        <span class="status-pill FUSED">FUSED (${t.retry_count}/${t.max_retries})</span>
      </div>
      <p class="muted" style="margin:4px 0">Worktree：<code>${esc(t.worktree_path || "未创建")}</code></p>
      <pre>${esc(t.failure_reason || t.last_feedback || "无详细日志")}</pre>
      <textarea id="steer-${esc(t.task_id)}" rows="2" style="width:100%" placeholder="输入给智能体的排错提示…"></textarea>
      <div class="inline-form" style="margin-top:6px">
        <button class="btn btn-primary btn-sm" data-steer="${esc(t.task_id)}" data-vonly="0">点拨并续跑</button>
        <button class="btn btn-sm" data-steer="${esc(t.task_id)}" data-vonly="1">我已手工改好，仅重新验证</button>
        <button class="btn btn-sm" data-open-task="${esc(t.task_id)}">打开监视器</button>
      </div>
    </div>`);
  }

  for (const m of data.awaiting_milestones || []) {
    items.push(`<div class="adr-card" style="border-left:4px solid #059669">
      <div class="task-top"><strong>[里程碑合并签字] ${esc(m.milestone_id)} · ${esc(m.title)}</strong><span class="status-pill AWAITING_MERGE">AWAITING_MERGE</span></div>
      <p class="muted" style="margin:4px 0 8px">${esc(m.goal)}</p>
      <div class="btn-group">
        ${deliveryHistory(state, m.milestone_id)}
        <button class="btn btn-primary btn-sm" data-merge="${esc(m.milestone_id)}">确认交付</button>
        ${m.report_path ? `<button class="btn btn-sm" data-report="${esc(m.milestone_id)}">查看交付报告</button>` : ""}
      </div>
    </div>`);
  }

  const totalPending =
    (data.plan_pending ? 1 : 0) +
    (data.loopforge_gates?.length || 0) +
    (data.fused_tasks?.length || 0) +
    (data.awaiting_milestones?.length || 0);
  const badge = $("nav-inbox-cnt");
  if (badge) {
    badge.textContent = String(totalPending);
    badge.classList.toggle("hidden", totalPending === 0);
  }

  if (el) {
    el.innerHTML = items.length ? items.join("") : `<div class="muted" style="padding:12px">当前没有待审批门禁或熔断任务，一切运转正常。</div>`;
    $("inbox-approve-plan")?.addEventListener("click", async () => {
      await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: actorName() }) });
      toast("立项蓝图已批准");
      onRefresh();
    });
    el.querySelectorAll("[data-lf-approve]").forEach((b) => {
      b.addEventListener("click", async () => {
        const rid = b.dataset.lfApprove;
        await api(`/vernal-api/runtime/runs/${encodeURIComponent(rid)}/approve`, {
          method: "POST",
          body: JSON.stringify({ comment: $(`lf-note-${rid}`)?.value || "", node_id: b.dataset.node }),
        });
        toast(`已批准 LoopForge Run ${rid}`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-lf-reject]").forEach((b) => {
      b.addEventListener("click", async () => {
        const rid = b.dataset.lfReject;
        await api(`/vernal-api/runtime/runs/${encodeURIComponent(rid)}/reject`, {
          method: "POST",
          body: JSON.stringify({ reason: $(`lf-note-${rid}`)?.value || "需修改", target_node: b.dataset.to, node_id: b.dataset.node }),
        });
        toast(`已打回 LoopForge Run ${rid}`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-steer]").forEach((b) => {
      b.addEventListener("click", async () => {
        const tid = b.dataset.steer;
        await api(`/vernal-api/tasks/${encodeURIComponent(tid)}/steer`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName(), note: $(`steer-${tid}`)?.value || "", verify_only: b.dataset.vonly === "1" }),
        });
        toast(`${tid} 已恢复就绪`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-open-task]").forEach((b) => {
      b.addEventListener("click", () => onOpenTask(b.dataset.openTask, "trace"));
    });
    el.querySelectorAll("[data-merge]").forEach((b) => {
      b.addEventListener("click", async () => {
        const result = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.merge)}/merge`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName() }),
        });
        toast(result.delivery_branch ? `已交付到分支 ${result.delivery_branch}` : `里程碑 ${b.dataset.merge} 已合入主干`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-report]").forEach((b) => {
      b.addEventListener("click", async () => {
        const r = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.report)}/report`);
        onOpenReport(`交付报告 · ${r.milestone_id}`, r.report);
      });
    });
  }

  if (msEl && state) {
    const msList = state.milestones || [];
    msEl.innerHTML = msList.length
      ? msList
          .map((m) => {
            const matrix = m.commit_matrix_json ? JSON.parse(m.commit_matrix_json) : null;
            return `<div class="adr-card">
              <div class="task-top"><strong>${esc(m.milestone_id)} · ${esc(m.title)}</strong><span class="status-pill ${esc(m.status)}">${esc(m.status)}</span></div>
              <p class="muted" style="margin:4px 0">${esc(m.goal)} · 集成命令：<code>${esc(m.integration_cmd || "无")}</code></p>
              ${m.failure_reason ? `<pre>${esc(m.failure_reason)}</pre>` : ""}
              ${matrix ? `<pre>Commit Matrix: ${esc(JSON.stringify(matrix, null, 2))}</pre>` : ""}
              <div class="btn-group">
                ${m.status === "AWAITING_MERGE" ? `<button class="btn btn-primary btn-sm" data-m-merge="${esc(m.milestone_id)}">确认交付</button>` : ""}
                ${m.status === "FAILED" ? `<button class="btn btn-primary btn-sm" data-m-reint="${esc(m.milestone_id)}">重新集成</button>` : ""}
                ${m.status === "MERGED" && m.preview_entry ? `<a class="btn btn-sm" target="_blank" rel="noopener" href="/vernal-api/preview/${encodeURIComponent(state.project_key)}/${encodeURIComponent(m.milestone_id)}/${m.preview_entry.split("/").map(encodeURIComponent).join("/")}">静态预览</a>` : ""}
                ${m.status === "MERGED" && state.settings.collaboration_mode === "files" ? `<details><summary>推送 Git</summary>
                  <input data-export-url type="url" placeholder="留空使用内置 Gitea，或填写 HTTPS 仓库地址" />
                  <input data-export-user placeholder="远端用户名" /><input data-export-token type="password" autocomplete="new-password" placeholder="远端访问令牌" />
                  <button type="button" class="btn btn-sm" data-export-release="${esc(m.milestone_id)}">推送已验收版本</button></details>` : ""}
                ${deliveryHistory(state, m.milestone_id)}
                ${m.delivery_branch ? `<code>交付分支 ${esc(m.delivery_branch)}</code>` : ""}
                ${m.status === "MERGED" && state.settings.collaboration_mode === "files" ? `<a class="btn btn-sm" href="/vernal-api/milestones/${encodeURIComponent(m.milestone_id)}/files.zip${projectQuerySuffix("?")}" download>下载交付文件</a>` : ""}
                ${m.report_path ? `<button class="btn btn-sm" data-m-report="${esc(m.milestone_id)}">交付报告</button>` : ""}
              </div>
            </div>`;
          })
          .join("")
      : `<div class="muted">尚无里程碑。</div>`;
    msEl.querySelectorAll("[data-m-merge]").forEach((b) =>
      b.addEventListener("click", async () => {
        const result = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.mMerge)}/merge`, { method: "POST", body: JSON.stringify({ actor: actorName() }) });
        toast(result.delivery_branch ? `已交付到分支 ${result.delivery_branch}` : "已合入主干");
        onRefresh();
      })
    );
    msEl.querySelectorAll("[data-m-reint]").forEach((b) =>
      b.addEventListener("click", async () => {
        await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.mReint)}/reintegrate`, { method: "POST", body: JSON.stringify({ actor: actorName() }) });
        toast("已重置集成");
        onRefresh();
      })
    );
    msEl.querySelectorAll("[data-export-release]").forEach((button) => button.addEventListener("click", async () => {
      const panel = button.closest("details");
      button.disabled = true;
      try {
        const info = await api(`/vernal-api/milestones/${encodeURIComponent(button.dataset.exportRelease)}/export-git`,
          {method: "POST", body: JSON.stringify({url: panel.querySelector("[data-export-url]").value.trim(),
           username: panel.querySelector("[data-export-user]").value, token: panel.querySelector("[data-export-token]").value})});
        toast(`已推送交付分支 ${info.branch}`);
      } catch (err) { toast(err.message); }
      finally { panel.querySelector("[data-export-token]").value = ""; button.disabled = false; }
    }));
    msEl.querySelectorAll("[data-m-report]").forEach((b) =>
      b.addEventListener("click", async () => {
        const r = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.mReport)}/report`);
        onOpenReport(`交付报告 · ${r.milestone_id}`, r.report);
      })
    );
  }
}

export async function refreshRuntimePanel(onRefresh) {
  const el = $("runtime-panel");
  if (!el) return;
  const [st, an] = await Promise.all([
    api("/vernal-api/runtime/status"),
    api("/vernal-api/runtime/analytics").catch((err) => ({ loopforge_ok: false, loopforge_error: err.message, local_tasks_count: 0, linked_loopforge_runs: [] })),
  ]);
  const lf = st.loopforge || {};
  const gt = st.gitea || {};
  const linkedRunsHtml = (an.linked_loopforge_runs || []).length
    ? (an.linked_loopforge_runs || [])
        .map(
          (item) => `<button type="button" class="btn btn-sm" data-jump-lf-runs="${esc(item.run_id)}" style="margin:2px 4px 2px 0">${esc(item.task_id)} · ${esc(item.run_id.slice(0, 12))} ↗</button>`
        )
        .join("")
    : `<span class="muted">当前项目尚无关联到 LoopForge 的任务运行记录</span>`;
  const analyticsCardHtml = `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>运行统计与数据来源对照</h3>
          <p class="card-desc">清晰区分本地工作区统计与 LoopForge 控制面统计，避免查询异常被误认为“无数据”</p>
        </div>
        <button type="button" class="btn btn-sm" data-jump-lf-view="analytics">打开 LoopForge 原生统计 ↗</button>
      </div>
      <div class="roster-grid" style="margin-bottom:10px">
        <div class="slot-card">
          <div class="slot-head"><span class="slot-title">Vernal 本地工作区</span><span class="tag ok">${esc(an.workspace || "-")}</span></div>
          <div style="font-size:13px">任务数 <strong>${an.local_tasks_count ?? 0}</strong> · Tokens <strong>${((an.local_usage?.input || 0) + (an.local_usage?.output || 0)).toLocaleString()}</strong></div>
          <div class="muted" style="font-size:11px;margin-top:4px">${esc(an.sources?.local || "本地 SQLite")}</div>
        </div>
        <div class="slot-card ${an.loopforge_ok ? "working" : ""}">
          <div class="slot-head"><span class="slot-title">LoopForge 原生统计</span><span class="tag ${an.loopforge_ok ? "ok" : "warn"}">${an.loopforge_ok ? "已同步" : "未获取"}</span></div>
          <div style="font-size:13px">${an.loopforge_ok ? `总运行数 <strong>${esc(an.loopforge?.totalRuns ?? an.loopforge?.runs ?? st.recent_runs?.length ?? 0)}</strong>` : `<span style="color:#be123c">${esc(an.loopforge_error || "查询失败")}</span>`}</div>
          <div class="muted" style="font-size:11px;margin-top:4px">${esc(an.sources?.loopforge || "LoopForge /api/analytics")}</div>
        </div>
      </div>
      <div style="font-size:12px"><span class="muted">已关联原生 Run 的任务：</span>${linkedRunsHtml}</div>
    </div>`;

  const sopsTable = st.workflows?.length
    ? `<table class="data-table"><thead><tr><th>SOP 名称</th><th>ID</th><th>版本</th><th>沙箱配置</th><th>节点数</th></tr></thead><tbody>${st.workflows
        .map(
          (w) => `<tr>
            <td><strong>${esc(w.name)}</strong>${w.generic ? ` <span class="tag">模板</span>` : ""}</td>
            <td><code>${esc(w.id)}</code></td>
            <td>v${esc(w.version || 1)}</td>
            <td><code>${esc(w.sandboxProfileId || "-")}</code></td>
            <td>${(w.nodes || []).length}</td>
          </tr>`
        )
        .join("")}</tbody></table>`
    : `<div class="muted">${lf.has_token ? "暂无 SOP 数据或鉴权未通过。" : "在上方填入 LoopForge 个人 Bearer Token 后即可拉取全部 SOP 模板、沙箱配置与运行指标。"}</div>`;

  const sandboxesHtml = st.sandboxes?.length
    ? st.sandboxes.map((s) => `<span class="tag">${esc(s.id || s.name)}</span>`).join(" ")
    : `<span class="muted">等待连接后加载</span>`;

  const runsHtml = st.recent_runs?.length
    ? `<table class="data-table"><thead><tr><th>Run ID</th><th>SOP</th><th>状态</th><th>操作</th></tr></thead><tbody>${st.recent_runs
        .slice(0, 12)
        .map(
          (r) => `<tr>
            <td><code>${esc((r.id || "").slice(0, 14))}</code></td>
            <td>${esc(r.workflowId || "-")}</td>
            <td><span class="status-pill ${esc((r.status || "").toUpperCase())}">${esc(r.status || "-")}</span></td>
            <td><button class="btn btn-sm" data-lf-retry="${esc(r.id)}">重试</button></td>
          </tr>`
        )
        .join("")}</tbody></table>`
    : `<div class="muted">暂无 LoopForge 运行记录。</div>`;

  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>运行底座连接状态（LoopForge + Gitea · 方案 A）</h3>
          <p class="card-desc">本机离线模式（Local Worktree）与 LoopForge Docker 沙箱底座无缝切换</p>
        </div>
        <div class="btn-group">
          <button id="rt-auto-bind" class="btn btn-primary btn-sm">⚡ 一键复用当前登录态绑定</button>
          <a class="btn btn-sm" href="${esc(gt.url)}" target="_blank" rel="noopener">打开内网 Gitea ↗</a>
        </div>
      </div>

      <div class="roster-grid" style="margin-bottom:14px">
        <div class="slot-card ${lf.reachable ? "working" : ""}">
          <div class="slot-head"><span class="slot-title">LoopForge 控制面</span><span class="tag ${lf.reachable ? "ok" : "warn"}">${lf.reachable ? "ONLINE" : "OFFLINE"}</span></div>
          <div style="font-weight:600">${esc(lf.url)}</div>
          <div class="muted" style="font-size:12px;margin-top:4px">${lf.authenticated ? `已鉴权 · ${st.projects.length} 个项目 · ${st.workflows.length} 套 SOP · ${st.runners_online_count > 0 ? `在线执行机 ${st.runners_online_count} 台（${esc((st.online_runners || []).map((r) => r.name || r.id).join(", "))}）` : "⚠️ 暂无在线执行机心跳"}` : lf.reachable ? "服务已存活（首次打开注册管理员并在「接入方式 → 我的 Token」生成 afp_ Token）" : "未启动（可运行 ./deploy/loopforge/up.sh）"}</div>
        </div>
        <div class="slot-card ${gt.reachable ? "working" : ""}">
          <div class="slot-head"><span class="slot-title">内网 Gitea 仓库服务</span><span class="tag ${gt.reachable ? "ok" : "warn"}">${gt.reachable ? "ONLINE" : "OFFLINE"}</span></div>
          <div style="font-weight:600">${esc(gt.url)}</div>
          <div class="muted" style="font-size:12px;margin-top:4px">${gt.reachable ? "供 LoopForge 多分支沙箱拉取与合入" : "未启动"}</div>
        </div>
      </div>

      ${st.auth_error ? `<pre style="color:#be123c">LoopForge 鉴权提示：${esc(st.auth_error)}</pre>` : ""}

      <div class="form-grid">
        <label class="field">执行底座模式
          <select id="rt-backend">
            <option value="local" ${st.runtime_backend === "local" ? "selected" : ""}>local（本机 Git Worktree 闭环）</option>
            <option value="loopforge" ${st.runtime_backend === "loopforge" ? "selected" : ""}>loopforge（派发到 LoopForge SOP/沙箱）</option>
          </select>
        </label>
        <label class="field">LoopForge 地址
          <input id="rt-url" type="text" value="${esc(lf.url || "http://127.0.0.1:8090")}" />
        </label>
        <label class="field">LoopForge Bearer Token (afp_...)
          <input id="rt-token" type="password" placeholder="${lf.has_token ? "已配置（留空保持不变）" : "粘贴 afp_ 开头的个人 Token"}" />
        </label>
        <label class="field">绑定项目 ID (project_id)
          <input id="rt-proj" type="text" value="${esc(lf.project_id || "")}" placeholder="${st.projects[0]?.id || "在 LoopForge 创建的项目 ID"}" />
        </label>
        <label class="field">默认 SOP ID (workflow_id)
          <input id="rt-wf" type="text" value="${esc(lf.workflow_id || "")}" placeholder="${st.workflows[0]?.id || "如 generic-backend-delivery"}" />
        </label>
        <label class="field">绑定仓库 ID (repository_id · 可选)
          <input id="rt-repo" type="text" value="${esc(lf.repository_id || "")}" />
        </label>
      </div>
      <div class="inline-form">
        <button id="btn-save-runtime" class="btn btn-primary btn-sm">保存并重连底座</button>
        <span class="muted" style="font-size:12px">可用异构沙箱：${sandboxesHtml}</span>
      </div>
    </div>

    ${analyticsCardHtml}

    <div class="card">
      <div class="card-head"><h3>LoopForge 可用 SOP 流水线模板 (${st.workflows.length})</h3></div>
      ${sopsTable}
    </div>

    <div class="card">
      <div class="card-head"><h3>LoopForge 最近执行记录 (${st.recent_runs.length})</h3></div>
      ${runsHtml}
    </div>
  `;

  $("rt-auto-bind")?.addEventListener("click", async () => {
    try {
      const res = await api("/vernal-api/runtime/bind", { method: "POST" });
      toast(`已自动绑定当前账号 ${res.login}（项目 ${res.project_id || "-"}）`);
      await refreshRuntimePanel(onRefresh);
      onRefresh?.();
    } catch (err) {
      toast(`自动绑定失败：${err.message}`);
    }
  });

  $("btn-save-runtime")?.addEventListener("click", async () => {
    const body = {
      runtime_backend: $("rt-backend").value,
      loopforge_url: $("rt-url").value,
      loopforge_project_id: $("rt-proj").value,
      loopforge_workflow_id: $("rt-wf").value,
      loopforge_repository_id: $("rt-repo").value,
    };
    const tok = $("rt-token").value.trim();
    if (tok) body.loopforge_token = tok;
    try {
      await api("/vernal-api/runtime/config", { method: "POST", body: JSON.stringify(body) });
      toast("底座配置已保存");
      await refreshRuntimePanel(onRefresh);
      onRefresh();
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
  });

  el.querySelectorAll("[data-jump-lf-runs]").forEach((b) => {
    b.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "runs", runId: b.dataset.jumpLfRuns } }));
    });
  });
  el.querySelectorAll("[data-jump-lf-view]").forEach((b) => {
    b.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: b.dataset.jumpLfView } }));
    });
  });

  el.querySelectorAll("[data-lf-retry]").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        await api(`/vernal-api/runtime/runs/${encodeURIComponent(b.dataset.lfRetry)}/retry`, { method: "POST" });
        toast("已触发重试");
        refreshRuntimePanel(onRefresh);
      } catch (err) {
        toast(`重试失败：${err.message}`);
      }
    });
  });
}
