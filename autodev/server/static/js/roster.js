import { $, ROLE_NAMES, esc, fmtElapsed } from "./api.js";

export function renderGoalBar(state) {
  const bar = $("goal-bar");
  if (!bar || !state) return;
  const ms = state.milestones || [];
  const merged = ms.filter((m) => m.status === "MERGED").length;
  const steps = [
    { label: `1. 资料切片 (${state.slices || 0})`, done: (state.slices || 0) > 0, active: state.status === "CREATED" },
    { label: `2. 架构圆桌评审 (${state.council_count || 0})`, done: Boolean(state.has_prd), active: state.planning && !state.has_prd },
    {
      label: `3. 总监立项分組 (${(state.squads || []).length}组)`,
      done: ["PLANNED", "APPROVED", "RUNNING", "STALLED", "COMPLETED"].includes(state.status),
      active: state.planning && Boolean(state.has_prd),
    },
    {
      label: `4. 多组滚动研发 (${merged}/${ms.length} 里程碑)`,
      done: state.status === "COMPLETED",
      active: ["APPROVED", "RUNNING", "STALLED"].includes(state.status),
    },
    { label: "5. 全链路交付", done: state.status === "COMPLETED", active: false },
  ];
  bar.innerHTML = steps
    .map(
      (s, i) =>
        `<span class="gstep ${s.done ? "done" : s.active ? "active" : ""}">${s.done ? "✓ " : ""}${esc(s.label)}</span>` +
        (i < steps.length - 1 ? `<span class="garrow">→</span>` : "")
    )
    .join("");
}

export function renderRoster(state, onSelectTask) {
  const grid = $("roster-grid");
  const sum = $("roster-summary");
  if (!grid || !state) return;
  const slots = state.worker_slots || [];
  const workingCount = slots.filter((s) => s.status === "WORKING").length;
  sum.textContent = `当前在岗：${workingCount} / ${slots.length} 个工位 · 点击活跃工位直接查看实时步骤与 Diff`;
  grid.innerHTML = slots
    .map((s) => {
      const working = s.status === "WORKING";
      const engCls = s.harness === "codex" ? "codex" : s.harness === "opencode" ? "opencode" : "other";
      const roleCn = ROLE_NAMES[s.role] || s.role || "";
      const elap = working && s.started_at ? `<span class="elapsed" data-start="${esc(s.started_at)}">${fmtElapsed(s.started_at)}</span>` : "";
      const clickAttr = working && s.task_id ? `data-slot-task="${esc(s.task_id)}"` : "";
      return `<div class="slot-card ${esc(s.slot_kind)} ${working ? "working" : "idle"}" ${clickAttr}>
        <div class="slot-head">
          <span class="slot-title">${esc(s.slot_label)}</span>
          <span>${elap} <span class="status-pill ${working ? "RUNNING" : ""}">${working ? "WORKING" : "IDLE"}</span></span>
        </div>
        ${
          working
            ? `<div class="slot-who">
                <span>${esc(s.squad || "")} · ${esc(roleCn)}</span>
                <span class="eng-badge ${engCls}">${esc(s.harness || "?")}/${esc(s.model || "default")}</span>
              </div>
              <div class="muted" style="font-size:11.5px;margin-top:2px">任务 <code>${esc(s.task_id || "-")}</code>${s.worktree_path ? " · 独立 worktree" : ""}</div>
              <div class="slot-action" title="${esc(s.current_action || "")}">${esc(s.current_action || "运行中…")}</div>`
            : `<div class="muted" style="font-size:12px">空闲待命（等待编排中枢派单）</div>`
        }
      </div>`;
    })
    .join("");
  grid.querySelectorAll("[data-slot-task]").forEach((el) => {
    el.addEventListener("click", () => onSelectTask(el.dataset.slotTask, "trace"));
  });
}

export function renderSquadColumns(state, onSelectTask) {
  const el = $("squad-columns");
  if (!el || !state) return;
  const tasks = state.tasks || [];
  if (!tasks.length) {
    el.innerHTML = `<div class="card muted">尚无任务。请先运行「摄入资料」→「圆桌与立项」。</div>`;
    return;
  }
  const squadsById = new Map((state.squads || []).map((s) => [s.squad_id, s]));
  const groups = new Map();
  for (const t of tasks) {
    const k = t.squad || t.module || "default";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(t);
  }
  el.innerHTML = [...groups.entries()]
    .map(([sqId, list]) => {
      const sq = squadsById.get(sqId);
      const heading = sq ? `${sq.name} (${list.length})` : `${sqId} (${list.length})`;
      const sb = sq?.sandbox_profile ? `<span class="tag">${esc(sq.sandbox_profile)}</span>` : "";
      const territory = sq?.owned_paths?.length
        ? `<div class="squad-sub">目录领地：<code>${esc(sq.owned_paths.join(", "))}</code> ${sb}</div>`
        : "";
      const cards = list
        .map((t) => {
          const tok = (t.usage?.input || 0) + (t.usage?.output || 0);
          const active = ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status);
          const elap = active && t.active_started_at ? `<span class="elapsed" data-start="${esc(t.active_started_at)}">${fmtElapsed(t.active_started_at)}</span>` : "";
          const actMsg = t.latest_activity?.message || "";
          const who = active && t.active_agent_label ? `<div class="muted" style="font-size:11.5px;margin-top:3px">👤 ${esc(t.active_agent_label)}</div>` : "";
          const live = active && actMsg ? `<div class="task-live-line" title="${esc(actMsg)}">${esc(actMsg)}</div>` : "";
          const lf = t.loopforge_run_id ? `<span class="tag">LF:${esc(t.loopforge_run_id.slice(0, 8))}</span>` : "";
          return `<div class="task-card ${esc(t.status)}" data-task="${esc(t.task_id)}">
            <div class="task-top">
              <span class="task-id">${esc(t.task_id)} · ${esc(t.milestone_id)}</span>
              <span>${elap} <span class="status-pill ${esc(t.status)}">${esc(t.status)}</span></span>
            </div>
            <div style="font-weight:600">${esc(t.title)}</div>
            ${who}
            ${live}
            <div class="task-meta">
              <span>${esc(ROLE_NAMES[t.role] || t.role)} · 重试 ${t.retry_count}/${t.max_retries} ${lf}</span>
              <span>${tok.toLocaleString()} tok</span>
            </div>
          </div>`;
        })
        .join("");
      return `<div class="squad-col"><h4>${esc(heading)}</h4>${territory}${cards}</div>`;
    })
    .join("");
  el.querySelectorAll(".task-card").forEach((c) => {
    c.addEventListener("click", () => onSelectTask(c.dataset.task, "trace"));
  });
}

export function renderEvents(events, onSelectTask) {
  const el = $("event-list");
  const cnt = $("event-count");
  if (!el) return;
  if (cnt) cnt.textContent = String(events.length);
  const list = events;
  el.innerHTML = list
    .map((ev) => {
      const et = ev.event_type || "";
      let cls = "";
      if (et === "agent.command") cls = "ev-command";
      else if (et === "agent.message" || et.startsWith("council.")) cls = "ev-message";
      else if (et === "agent.file") cls = "ev-file";
      else if (et === "agent.warning" || et.endsWith(".fused") || et.endsWith(".failed")) cls = "ev-warning";
      let link = "";
      if (ev.task_id) {
        link = onSelectTask
          ? ` · <button type="button" class="ev-link" data-ev-task="${esc(ev.task_id)}">${esc(ev.task_id)}</button>`
          : ` · ${esc(ev.task_id)}`;
      }
      const actor = ev.actor && ev.actor !== "system" ? `<strong>${esc(ev.actor)}</strong> ` : "";
      return `<div class="ev-item ${cls}">
        <div class="ev-head">
          <span>${esc(et)}${link}</span>
          <time datetime="${esc(ev.created_at)}">${esc((ev.created_at || "").slice(0, 19).replace("T", " "))}</time>
        </div>
        <div>${actor}${esc(ev.message)}</div>
      </div>`;
    })
    .join("");
  el.querySelectorAll("[data-ev-task]").forEach((a) => {
    a.addEventListener("click", () => onSelectTask(a.dataset.evTask, "trace"));
  });
}

export function renderDocsAndPlan(state, planDoc, slices, onSelectTask) {
  const el = $("plan-breakdown");
  const sliceEl = $("slice-list");
  if (sliceEl) {
    sliceEl.innerHTML = slices?.length
      ? slices
          .map(
            (s) => `<details class="adr-card">
              <summary style="cursor:pointer"><strong>${esc(s.slice_id)}</strong> · ${esc(s.heading_path)} <span class="muted">(${s.char_count} 字)</span></summary>
              <p class="muted" style="margin:6px 0">${esc(s.summary)}</p>
              <pre>${esc(s.content)}</pre>
            </details>`
          )
          .join("")
      : `<div class="muted">暂无切片。请把架构 md/proto/pdf/docx 放入 inputs/ 目录后点「摄入资料」。</div>`;
  }
  if (!el || !state) return;

  const squads = state.squads || [];
  const milestones = state.milestones || [];
  const tasks = state.tasks || [];
  const prd = planDoc?.prd || null;
  const plan = planDoc?.plan || null;

  const prdHtml = prd
    ? `<div class="card">
        <div class="card-head">
          <div><h3>产品与架构共识规格书（docs/PRD_SPEC.md）</h3><p class="card-desc">${esc(prd.product_vision || "")}</p></div>
        </div>
        <details>
          <summary style="cursor:pointer" class="muted">展开查看完整规格全文</summary>
          <pre>${esc(prd.spec_markdown || "")}</pre>
        </details>
      </div>`
    : "";

  const squadsHtml = squads.length
    ? `<div class="card">
        <div class="card-head">
          <div><h3>研发组编制与物理领地 (${squads.length} 组)</h3><p class="card-desc">技术栈：${esc(plan?.tech_stack || "-")}</p></div>
        </div>
        <table class="data-table">
          <thead><tr><th>研发组</th><th>沙箱环境</th><th>岗位</th><th>专属目录领地</th><th>职责使命</th></tr></thead>
          <tbody>${squads
            .map(
              (s) => `<tr>
                <td><strong>${esc(s.name)}</strong><div class="muted" style="font-size:11px">${esc(s.squad_id)}</div></td>
                <td><code>${esc(s.sandbox_profile || "generic-docker")}</code></td>
                <td>${esc((s.roles || []).join(", "))}</td>
                <td><code>${esc((s.owned_paths || []).join(", "))}</code></td>
                <td>${esc(s.mission)}</td>
              </tr>`
            )
            .join("")}</tbody>
        </table>
      </div>`
    : "";

  const msHtml = milestones
    .map((m) => {
      const mTasks = tasks.filter((t) => t.milestone_id === m.milestone_id);
      const planned = m.planned !== false && mTasks.length > 0;
      const preview = !planned
        ? `<div class="muted" style="font-size:12px;margin-top:6px">参战组：<code>${esc((m.target_squads || []).join(", ") || "待定")}</code> · 关键交付物：${esc((m.key_deliverables || []).join("；") || m.goal)}</div>`
        : "";
      const rows = mTasks
        .map(
          (t) => `<details class="adr-card" style="margin-top:8px">
            <summary style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:8px">
              <span><strong>${esc(t.task_id)}</strong> [${esc(t.squad_name || t.module)} · ${esc(t.role)}] ${esc(t.title)}</span>
              <span><span class="muted" style="font-size:12px">依赖：${esc((t.depends_on || []).join(",") || "无")}</span> <span class="status-pill ${esc(t.status)}">${esc(t.status)}</span></span>
            </summary>
            <div style="margin-top:8px">
              <p>${esc(t.description)}</p>
              <strong>验收标准：</strong>
              <ul>${(t.acceptance_criteria || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
              <div class="cmsg-grid">
                <div><strong>允许修改路径</strong><pre>${esc((t.allowed_paths || []).join("\n"))}</pre></div>
                <div><strong>验证门禁命令</strong><pre>${esc((t.verify_cmds || []).join("\n"))}</pre></div>
              </div>
              <button class="btn btn-sm" data-open-task="${esc(t.task_id)}">打开实时监视器</button>
            </div>
          </details>`
        )
        .join("");
      return `<div class="card">
        <div class="card-head">
          <div>
            <h3>${esc(m.milestone_id)} · ${esc(m.title)}</h3>
            <p class="card-desc">${esc(m.goal)} · 集成命令：<code>${esc(m.integration_cmd || "无")}</code></p>
          </div>
          <span class="status-pill ${esc(m.status)}">${planned ? `${mTasks.length} 单 · ${m.status}` : "待滚动展开"}</span>
        </div>
        ${preview}
        ${rows}
      </div>`;
    })
    .join("");

  el.innerHTML = prdHtml + squadsHtml + msHtml;
  el.querySelectorAll("[data-open-task]").forEach((btn) => {
    btn.addEventListener("click", () => onSelectTask(btn.dataset.openTask, "trace"));
  });
}
