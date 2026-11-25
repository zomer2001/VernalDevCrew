import { $, actorName, api, esc, projectQuerySuffix, toast } from "./api.js";

function deliveryHistory(state, milestoneId) {
  return (state.deliveries || [])
    .filter((d) => d.milestone_id === milestoneId)
    .map((d) => {
      const version = Number(d.version_no);
      if (d.collab_mode === "files") {
        return `<a class="btn btn-sm" download href="/vernal-api/milestones/${encodeURIComponent(milestoneId)}/files.zip?version=${version}${projectQuerySuffix("&")}">下载 v${version} ZIP</a>`;
      }
      return `<div class="delivery-row">v${version} <code>${esc(d.archive_branch)}</code> · <code>${esc(d.rolling_branch)}</code>
      ${d.compare_url ? `<a href="${esc(d.compare_url)}" target="_blank" rel="noopener">查看合并请求</a>` : ""}</div>`;
    })
    .join("");
}

export function updateInboxBadges(count) {
  const txt = count > 0 ? String(count) : "";
  for (const id of ["nav-inbox-cnt", "vernal-inbox-badge", "vernal-subtab-inbox-badge"]) {
    const el = document.getElementById(id) || $(id);
    if (!el) continue;
    el.textContent = txt;
    el.classList.toggle("hidden", count === 0);
  }
}

export async function refreshInbox(state, onRefresh, onOpenTask, onOpenReport) {
  const el = $("inbox-list");
  const msEl = $("milestone-list");
  const data = await api("/vernal-api/inbox");

  const items = [];
  if (data.plan_pending) {
    items.push(`<div class="adr-card gate-card-warn">
      <div class="task-top"><strong>立项蓝图待批准</strong><span class="status-pill PLANNED">PLANNED</span></div>
      <p class="muted">研发总监已完成模块划分与任务路线图，确认后自动进入流水线调度。</p>
      <div class="btn-group">
        <button type="button" class="btn btn-primary btn-sm" id="inbox-approve-start">批准并启动研发</button>
        <button type="button" class="btn btn-sm" id="inbox-approve-plan">仅批准蓝图</button>
      </div>
    </div>`);
  }

  for (const gate of data.process_gates) {
    items.push(`<div class="adr-card"><strong>${esc(gate.gate_id)}</strong> · ${esc(gate.scope_id)}</div>`);
  }
  for (const review of data.reviews) {
    items.push(`<div class="adr-card"><strong>人工 Diff 审查 #${esc(review.review_id)}</strong> · ${esc(review.scope_id)}</div>`);
  }

  for (const t of data.fused_tasks || []) {
    items.push(`<div class="adr-card gate-card-err">
      <div class="task-top">
        <strong>[异常挂起接管] ${esc(t.task_id)} · ${esc(t.title)}</strong>
        <span class="status-pill FUSED">FUSED (${t.retry_count}/${t.max_retries})</span>
      </div>
      <p class="muted">工作区：<code>${esc(t.worktree_path || "远端沙箱")}</code></p>
      <pre>${esc(t.failure_reason || t.last_feedback || "无详细报错日志")}</pre>
      <textarea id="steer-${esc(t.task_id)}" rows="2" class="full-textarea" placeholder="填写给编码智能体的排障指导意见…"></textarea>
      <div class="inline-form card-mt">
        <button type="button" class="btn btn-primary btn-sm" data-steer="${esc(t.task_id)}" data-vonly="0">指导并继续执行</button>
        <button type="button" class="btn btn-sm" data-steer="${esc(t.task_id)}" data-vonly="1">人工已修改，仅重跑验证</button>
        <button type="button" class="btn btn-sm" data-open-task="${esc(t.task_id)}">查看详情</button>
      </div>
    </div>`);
  }

  for (const m of data.awaiting_milestones || []) {
    items.push(`<div class="adr-card gate-card-ok">
      <div class="task-top"><strong>[里程碑验收签字] ${esc(m.milestone_id)} · ${esc(m.title)}</strong><span class="status-pill AWAITING_MERGE">AWAITING_MERGE</span></div>
      <p class="muted">${esc(m.goal)}</p>
      <div class="btn-group">
        ${deliveryHistory(state, m.milestone_id)}
        <button type="button" class="btn btn-primary btn-sm" data-merge="${esc(m.milestone_id)}">确认验收并交付</button>
        ${m.report_path ? `<button type="button" class="btn btn-sm" data-report="${esc(m.milestone_id)}">查看集成报告</button>` : ""}
      </div>
    </div>`);
  }

  if (el) {
    el.innerHTML = items.length ? items.join("") : `<div class="empty-hint">当前无待处理审批或挂起任务。</div>`;
    $("inbox-approve-plan")?.addEventListener("click", async () => {
      await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: actorName() }) });
      toast("立项蓝图已批准");
      onRefresh();
    });
    $("inbox-approve-start")?.addEventListener("click", async () => {
      await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: actorName() }) });
      await api("/vernal-api/scheduler/start", { method: "POST" });
      toast("蓝图已批准并启动自动研发流水线");
      onRefresh();
    });
    el.querySelectorAll("[data-lf-approve]").forEach((b) => {
      b.addEventListener("click", async () => {
        const rid = b.dataset.lfApprove;
        try {
          await api(`/vernal-api/runtime/runs/${encodeURIComponent(rid)}/approve`, {
            method: "POST",
            body: JSON.stringify({ comment: $(`lf-note-${rid}`)?.value || "", node_id: b.dataset.node }),
          });
          toast(`已批准流程门禁 ${rid.slice(0, 8)}`);
          onRefresh();
        } catch (err) {
          toast(`批准失败：${err.message}`);
        }
      });
    });
    el.querySelectorAll("[data-lf-reject]").forEach((b) => {
      b.addEventListener("click", async () => {
        const rid = b.dataset.lfReject;
        try {
          await api(`/vernal-api/runtime/runs/${encodeURIComponent(rid)}/reject`, {
            method: "POST",
            body: JSON.stringify({ reason: $(`lf-note-${rid}`)?.value || "需修改", target_node: b.dataset.to, node_id: b.dataset.node }),
          });
          toast(`已打回流程门禁 ${rid.slice(0, 8)}`);
          onRefresh();
        } catch (err) {
          toast(`打回失败：${err.message}`);
        }
      });
    });
    el.querySelectorAll("[data-steer]").forEach((b) => {
      b.addEventListener("click", async () => {
        const tid = b.dataset.steer;
        await api(`/vernal-api/tasks/${encodeURIComponent(tid)}/steer`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName(), note: $(`steer-${tid}`)?.value || "", verify_only: b.dataset.vonly === "1" }),
        });
        toast(`${tid} 已恢复就绪队列`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-open-task]").forEach((b) => {
      b.addEventListener("click", () => onOpenTask(b.dataset.openTask, "trace"));
    });
    el.querySelectorAll("[data-merge]").forEach((b) => {
      b.addEventListener("click", async () => {
        const mid = b.dataset.merge;
        if (!window.confirm(`确认通过里程碑 ${mid} 验收并写入正式交付产物 / 分支？`)) return;
        const result = await api(`/vernal-api/milestones/${encodeURIComponent(mid)}/merge`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName() }),
        });
        toast(result.delivery_branch ? `已交付至分支 ${result.delivery_branch}` : `里程碑 ${mid} 已完成合入`);
        onRefresh();
      });
    });
    el.querySelectorAll("[data-report]").forEach((b) => {
      b.addEventListener("click", async () => {
        const r = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.report)}/report`);
        onOpenReport(`集成报告 · ${r.milestone_id}`, r.report);
      });
    });
  }

  if (msEl && state) {
    const msList = state.milestones || [];
    msEl.innerHTML = msList.length
      ? msList
          .map((m) => {
            const matrix = m.commit_matrix_json ? JSON.parse(m.commit_matrix_json) : null;
            const ciBadge = m.ci_status
              ? `<span class="status-pill ${m.ci_status === "SUCCESS" ? "PASS" : m.ci_status === "FAILURE" ? "FAIL" : "RUNNING"}">CI: ${esc(m.ci_status)}</span>`
              : "";
            return `<div class="adr-card">
              <div class="task-top">
                <strong>${esc(m.milestone_id)} · ${esc(m.title)}</strong>
                <span class="btn-group">${ciBadge}<span class="status-pill ${esc(m.status)}">${esc(m.status)}</span></span>
              </div>
              <p class="muted">${esc(m.goal)} · 集成命令：<code>${esc(m.integration_cmd || "无")}</code></p>
              ${m.failure_reason ? `<pre>${esc(m.failure_reason)}</pre>` : ""}
              ${matrix ? `<details class="step-out"><summary>提交指纹矩阵 (${Object.keys(matrix).length} 项)</summary><pre>${esc(JSON.stringify(matrix, null, 2))}</pre></details>` : ""}
              <div class="btn-group card-mt">
                ${m.status === "AWAITING_MERGE" ? `<button type="button" class="btn btn-primary btn-sm" data-m-merge="${esc(m.milestone_id)}">确认验收并交付</button>` : ""}
                ${m.status === "FAILED" ? `<button type="button" class="btn btn-primary btn-sm" data-m-reint="${esc(m.milestone_id)}">重新执行集成</button>` : ""}
                ${m.status === "MERGED" && m.preview_entry ? `<a class="btn btn-sm" target="_blank" rel="noopener" href="/vernal-api/preview/${encodeURIComponent(state.project_key)}/${encodeURIComponent(m.milestone_id)}/${m.preview_entry.split("/").map(encodeURIComponent).join("/")}">静态沙箱预览</a>` : ""}
                ${m.status === "MERGED" && state.settings.collaboration_mode === "files" ? `<details class="inline-details"><summary class="btn btn-sm">推送至 Git</summary>
                  <div class="inline-form card-mt">
                    <input data-export-url type="url" placeholder="留空推送至内置 Gitea，或填外部 HTTPS 地址" />
                    <input data-export-user placeholder="用户名" />
                    <input data-export-token type="password" autocomplete="new-password" placeholder="访问令牌" />
                    <button type="button" class="btn btn-primary btn-sm" data-export-release="${esc(m.milestone_id)}">推送分支</button>
                  </div></details>` : ""}
                ${deliveryHistory(state, m.milestone_id)}
                ${m.delivery_branch ? `<code>交付分支 ${esc(m.delivery_branch)}</code>` : ""}
                ${m.status === "MERGED" && state.settings.collaboration_mode === "files" ? `<a class="btn btn-sm" href="/vernal-api/milestones/${encodeURIComponent(m.milestone_id)}/files.zip${projectQuerySuffix("?")}" download>下载成品包 (ZIP)</a>` : ""}
                ${m.report_path ? `<button type="button" class="btn btn-sm" data-m-report="${esc(m.milestone_id)}">集成报告</button>` : ""}
              </div>
            </div>`;
          })
          .join("")
      : `<div class="muted">尚无里程碑记录。</div>`;
    msEl.querySelectorAll("[data-m-merge]").forEach((b) =>
      b.addEventListener("click", async () => {
        const mid = b.dataset.mMerge;
        if (!window.confirm(`确认通过里程碑 ${mid} 验收并交付？`)) return;
        const result = await api(`/vernal-api/milestones/${encodeURIComponent(mid)}/merge`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName() }),
        });
        toast(result.delivery_branch ? `已交付至分支 ${result.delivery_branch}` : "已完成合入");
        onRefresh();
      })
    );
    msEl.querySelectorAll("[data-m-reint]").forEach((b) =>
      b.addEventListener("click", async () => {
        await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.mReint)}/reintegrate`, {
          method: "POST",
          body: JSON.stringify({ actor: actorName() }),
        });
        toast("已重置集成状态");
        onRefresh();
      })
    );
    msEl.querySelectorAll("[data-export-release]").forEach((button) =>
      button.addEventListener("click", async () => {
        const panel = button.closest("details");
        button.disabled = true;
        try {
          const info = await api(`/vernal-api/milestones/${encodeURIComponent(button.dataset.exportRelease)}/export-git`, {
            method: "POST",
            body: JSON.stringify({
              url: panel.querySelector("[data-export-url]").value.trim(),
              username: panel.querySelector("[data-export-user]").value,
              token: panel.querySelector("[data-export-token]").value,
            }),
          });
          toast(`已推送交付分支 ${info.branch}`);
        } catch (err) {
          toast(err.message);
        } finally {
          panel.querySelector("[data-export-token]").value = "";
          button.disabled = false;
        }
      })
    );
    msEl.querySelectorAll("[data-m-report]").forEach((b) =>
      b.addEventListener("click", async () => {
        const r = await api(`/vernal-api/milestones/${encodeURIComponent(b.dataset.mReport)}/report`);
        onOpenReport(`集成报告 · ${r.milestone_id}`, r.report);
      })
    );
  }
}

export async function refreshRuntimePanel(onRefresh) {
  const el = $("runtime-panel");
  if (!el) return;
  const st = await api("/vernal-api/runtime/status");
  const lf = st.loopforge || {};
  const gt = st.gitea || {};

  const sopsTable = st.workflows?.length
    ? `<table class="data-table"><thead><tr><th>流水线模板</th><th>标识</th><th>版本</th><th>沙箱</th><th>工序节点数</th></tr></thead><tbody>${st.workflows
        .map(
          (w) => `<tr>
            <td><strong>${esc(w.name)}</strong></td>
            <td><code>${esc(w.id)}</code></td>
            <td>v${esc(w.version || 1)}</td>
            <td><code>${esc(w.sandboxProfileId || "-")}</code></td>
            <td>${(w.nodes || []).length}</td>
          </tr>`
        )
        .join("")}</tbody></table>`
    : `<div class="muted">${lf.has_token ? "暂无已注册的流水线模板。" : "绑定登录态后自动加载标准研发工序模板。"}</div>`;

  const runsHtml = st.recent_runs?.length
    ? `<table class="data-table"><thead><tr><th>运行 ID</th><th>工序模板</th><th>状态</th><th>操作</th></tr></thead><tbody>${st.recent_runs
        .slice(0, 10)
        .map(
          (r) => `<tr>
            <td><code>${esc((r.id || "").slice(0, 14))}</code></td>
            <td>${esc(r.workflowId || "-")}</td>
            <td><span class="status-pill ${esc((r.status || "").toUpperCase())}">${esc(r.status || "-")}</span></td>
            <td><button type="button" class="btn btn-sm" data-lf-retry="${esc(r.id)}">重试</button></td>
          </tr>`
        )
        .join("")}</tbody></table>`
    : `<div class="muted">暂无底层执行记录。</div>`;

  const giteaUrl = gt.url || "http://127.0.0.1:3000";
  const giteaRepoUrl = gt.repo_url || "";
  const giteaAdmin = gt.admin_user || "vernal-local";

  el.innerHTML = `
    <section class="vset-card">
      <div class="vset-card-head">
        <div>
          <h3>底层执行引擎与代码托管服务</h3>
          <p>查看执行底座与本机内置私有 Gitea 服务健康度，按需同步登录态或进入运维诊断</p>
        </div>
        <span class="vset-scope-badge">部署运维</span>
      </div>

      <div class="vset-pool-grid">
        <article class="vset-svc-card">
          <div class="vset-pool-card-head">
            <div class="vset-pool-title-wrap">
              <span class="vset-pool-dot ${lf.reachable ? "ok" : "warn"}"></span>
              <div>
                <h4>Vernal 执行底座</h4>
                <small class="vset-svc-url">${esc(lf.url || "-")}</small>
              </div>
            </div>
            <span class="vset-state-pill ${lf.reachable ? "ok" : "warn"}">${lf.reachable ? "ONLINE" : "OFFLINE"}</span>
          </div>
          <p class="vset-caption">${lf.authenticated ? `已鉴权 · ${st.projects.length} 项目 · ${st.workflows.length} 工序 · 在线 Runner ${st.runners_online_count} 台` : lf.reachable ? "服务在线，待绑定登录态" : "离线（请在终端运行 ./vernal.sh 启动）"}</p>
          <div class="vset-pool-card-foot">
            <button type="button" id="rt-auto-bind" class="btn btn-primary btn-sm">同步当前登录凭据</button>
          </div>
        </article>

        <article class="vset-svc-card">
          <div class="vset-pool-card-head">
            <div class="vset-pool-title-wrap">
              <span class="vset-pool-dot ${gt.reachable ? "ok" : "warn"}"></span>
              <div>
                <h4>内置私有 Gitea 仓库</h4>
                <small class="vset-svc-url">${esc(giteaUrl)}</small>
              </div>
            </div>
            <span class="vset-state-pill ${gt.reachable ? "ok" : "warn"}">${gt.reachable ? "ONLINE" : "OFFLINE"}</span>
          </div>
          <p class="vset-caption">${gt.reachable ? `管理员账号 <code>${esc(giteaAdmin)}</code>（点击下方按钮可直接复制密码并打开登录页，无需去终端查看）` : "离线（随 ./vernal.sh 自动拉起）"}</p>
          <div class="vset-pool-card-foot">
            <button type="button" id="btn-gitea-copy-login" class="btn btn-primary btn-sm">复制密码并打开 Gitea ↗</button>
            <button type="button" id="btn-gitea-reveal" class="btn btn-sm">显示账号密码</button>
            ${giteaRepoUrl ? `<a class="btn btn-sm" href="${esc(giteaRepoUrl)}" target="_blank" rel="noopener">打开当前项目仓库 ↗</a>` : ""}
          </div>
          <div id="gitea-creds-box" class="gitea-creds-box" hidden></div>
        </article>
      </div>

      ${st.auth_error ? `<div class="launcher-error" role="alert">鉴权提示：${esc(st.auth_error)}</div>` : ""}

      <details class="vset-disclosure">
        <summary class="vset-disclosure-summary">
          <div>
            <strong>高级连接参数</strong>
            <small>管理员排查底座地址、默认项目、工序与仓库映射时使用</small>
          </div>
        </summary>
        <div class="vset-disclosure-body">
          <div class="vset-grid-2">
            <label class="vset-field">
              <span>执行底座模式</span>
              <select id="rt-backend">
                <option value="loopforge" ${st.runtime_backend === "loopforge" ? "selected" : ""}>loopforge（标准容器与多 Runner 底座）</option>
                <option value="local" ${st.runtime_backend === "local" ? "selected" : ""}>local（本机离线 Worktree 调试）</option>
              </select>
            </label>
            <label class="vset-field">
              <span>底座服务地址</span>
              <input id="rt-url" type="text" value="${esc(lf.url || "http://127.0.0.1:8090")}" />
            </label>
            <label class="vset-field">
              <span>访问令牌 (afp_...)</span>
              <input id="rt-token" type="password" placeholder="${lf.has_token ? "已安全存储于 0600 凭据文件（留空不变）" : "粘贴个人令牌"}" />
            </label>
            <label class="vset-field">
              <span>关联项目标识</span>
              <input id="rt-proj" type="text" value="${esc(lf.project_id || "")}" />
            </label>
            <label class="vset-field">
              <span>默认工序标识</span>
              <input id="rt-wf" type="text" value="${esc(lf.workflow_id || "")}" />
            </label>
            <label class="vset-field">
              <span>关联仓库标识（可选）</span>
              <input id="rt-repo" type="text" value="${esc(lf.repository_id || "")}" />
            </label>
          </div>
          <div class="vset-subfoot">
            <span class="vset-caption">服务令牌由部署级 ./vernal.sh --service-identity 维护</span>
            <button type="button" id="btn-save-runtime" class="btn btn-primary btn-sm">保存连接配置</button>
          </div>
        </div>
      </details>

      <details class="vset-disclosure">
        <summary class="vset-disclosure-summary">
          <div>
            <strong>底层工序模板与最近运行诊断</strong>
            <small>${st.workflows.length} 套模板 · ${st.recent_runs.length} 条近期运行</small>
          </div>
        </summary>
        <div class="vset-disclosure-body vset-stack">
          <div>${sopsTable}</div>
          <div>${runsHtml}</div>
        </div>
      </details>
    </section>
  `;

  $("rt-auto-bind")?.addEventListener("click", async () => {
    try {
      const res = await api("/vernal-api/runtime/bind", { method: "POST" });
      toast(`已同步当前账号 ${res.login} 凭据`);
      await refreshRuntimePanel(onRefresh);
      onRefresh?.();
    } catch (err) {
      toast(`绑定失败：${err.message}`);
    }
  });

  async function fetchGiteaCreds() {
    return await api("/vernal-api/runtime/gitea/credentials", { method: "POST" });
  }

  $("btn-gitea-copy-login")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const creds = await fetchGiteaCreds();
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(creds.password);
        toast(`密码已复制（账号：${creds.username}），正在打开 Gitea 登录页`);
      } else {
        const box = $("gitea-creds-box");
        if (box) {
          box.hidden = false;
          box.innerHTML = `<div>账号：<code>${esc(creds.username)}</code> · 密码：<code>${esc(creds.password)}</code></div>`;
        }
        toast(`请复制下方密码登录（账号：${creds.username}）`);
      }
      window.open(creds.login_url || giteaUrl, "_blank", "noopener");
    } catch (err) {
      toast(`读取 Gitea 凭据失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-gitea-reveal")?.addEventListener("click", async (e) => {
    const box = $("gitea-creds-box");
    const btn = e.currentTarget;
    if (!box) return;
    if (!box.hidden) {
      box.hidden = true;
      box.innerHTML = "";
      btn.textContent = "显示账号密码";
      return;
    }
    btn.disabled = true;
    try {
      const creds = await fetchGiteaCreds();
      box.hidden = false;
      box.innerHTML = `
        <div class="gitea-cred-row">
          <span>账号：<code>${esc(creds.username)}</code></span>
          <button type="button" class="btn btn-xs" data-copy-txt="${esc(creds.username)}">复制账号</button>
        </div>
        <div class="gitea-cred-row">
          <span>密码：<code class="gitea-pw-code">${esc(creds.password)}</code></span>
          <button type="button" class="btn btn-xs" data-copy-txt="${esc(creds.password)}">复制密码</button>
          <a class="btn btn-xs" href="${esc(creds.login_url || giteaUrl)}" target="_blank" rel="noopener">登录页 ↗</a>
        </div>
      `;
      box.querySelectorAll("[data-copy-txt]").forEach((cb) => {
        cb.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(cb.dataset.copyTxt);
            toast("已复制到剪贴板");
          } catch {
            toast("浏览器不支持自动复制，请手动选中复制");
          }
        });
      });
      btn.textContent = "隐藏账号密码";
    } catch (err) {
      toast(`读取 Gitea 凭据失败：${err.message}`);
    } finally {
      btn.disabled = false;
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
      toast("连接配置已保存");
      await refreshRuntimePanel(onRefresh);
      onRefresh();
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
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
