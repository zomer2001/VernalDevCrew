import { $, ROLE_NAMES, esc, fmtElapsed } from "./api.js";

export function renderGoalBar(state) {
  const bar = $("goal-bar");
  if (!bar || !state) return;
  // 若已加载标准化七阶段步进条数据，则由 pipeline stepper 主导显示，隐藏旧版简略条
  if (bar.dataset.replacedByStepper === "1") return;
  const ms = state.milestones || [];
  const merged = ms.filter((m) => m.status === "MERGED").length;
  const sliceCnt = state.slices ?? state.slices_count ?? 0;
  const steps = [
    { label: `1. 需求与资料 (${sliceCnt})`, done: sliceCnt > 0, active: state.status === "CREATED" },
    { label: `2. 架构评审 (${state.council_count || 0})`, done: Boolean(state.has_prd), active: state.planning && !state.has_prd },
    {
      label: `3. 蓝图拆解 (${(state.squads || []).length} 组)`,
      done: ["PLANNED", "APPROVED", "RUNNING", "STALLED", "COMPLETED"].includes(state.status),
      active: state.planning && Boolean(state.has_prd),
    },
    {
      label: `4. 并行实现与验证 (${merged}/${ms.length} 里程碑)`,
      done: state.status === "COMPLETED",
      active: ["APPROVED", "RUNNING", "STALLED"].includes(state.status),
    },
    { label: "5. 审查与集成交付", done: state.status === "COMPLETED", active: false },
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
  sum.textContent = `活跃工位：${workingCount} / ${slots.length} · 点击执行中工位查看实时输出与代码改动`;
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
                <span class="eng-badge ${engCls}">${esc(s.harness || "?")}/${esc(s.model || "auto")}</span>
              </div>
              <div class="muted slot-task-line">任务 <code>${esc(s.task_id || "-")}</code>${s.worktree_path ? " · 隔离工作树" : ""}</div>
              <div class="slot-action" title="${esc(s.current_action || "")}">${esc(s.current_action || "运行中…")}</div>`
            : `<div class="muted">空闲待命</div>`
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
    el.innerHTML = `<div class="card empty-hint">尚无研发任务。完成需求输入后点击右上角「开始立项评审」自动划分研发组与任务看板。</div>`;
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
        ? `<div class="squad-sub">职责目录：<code>${esc(sq.owned_paths.join(", "))}</code> ${sb}</div>`
        : "";
      const cards = list
        .map((t) => {
          const tok = (t.usage?.input || 0) + (t.usage?.output || 0);
          const active = ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status);
          const elap = active && t.active_started_at ? `<span class="elapsed" data-start="${esc(t.active_started_at)}">${fmtElapsed(t.active_started_at)}</span>` : "";
          const actMsg = t.latest_activity?.message || "";
          const who = active && t.active_agent_label ? `<div class="muted task-agent-line">${esc(t.active_agent_label)}</div>` : "";
          const live = active && actMsg ? `<div class="task-live-line" title="${esc(actMsg)}">${esc(actMsg)}</div>` : "";
          const lf = t.loopforge_run_id ? `<span class="tag">Run:${esc(t.loopforge_run_id.slice(0, 7))}</span>` : "";
          return `<div class="task-card ${esc(t.status)}" data-task="${esc(t.task_id)}">
            <div class="task-top">
              <span class="task-id">${esc(t.task_id)} · ${esc(t.milestone_id)}</span>
              <span>${elap} <span class="status-pill ${esc(t.status)}">${esc(t.status)}</span></span>
            </div>
            <div class="task-card-title">${esc(t.title)}</div>
            ${who}
            ${live}
            <div class="task-meta">
              <span>${esc(ROLE_NAMES[t.role] || t.role)} · 重试 ${t.retry_count}/${t.max_retries} ${lf}</span>
              <span>${tok > 0 ? tok.toLocaleString() + " tok" : "-"}</span>
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

const EVENT_LABELS = {
  "scheduler.started": "调度启动",
  "scheduler.auto_resumed": "重启自动恢复",
  "scheduler.stopped": "调度暂停",
  "scheduler.error": "调度异常",
  "background.started": "后台任务",
  "orchestrator.dispatched": "任务派单",
  "loopforge.progress": "执行推进",
  "loopforge.approved": "原生审批通过",
  "loopforge.rejected": "原生审批打回",
  "agent.command": "执行命令",
  "agent.message": "智能体汇报",
  "agent.file": "文件变更",
  "agent.warning": "运行提醒",
  "agent.configured": "编队更新",
  "runtime.configured": "底座更新",
  "plan.started": "开始立项",
  "plan.completed": "蓝图完成",
  "plan.approved": "蓝图已批",
  "plan.failed": "立项失败",
  "council.started": "圆桌评审",
  "council.completed": "圆桌收敛",
  "council.failed": "圆桌异常",
  "task.started": "任务启动",
  "task.completed": "任务完成",
  "task.approved": "任务通过",
  "task.restarted": "任务重启",
  "task.fused": "任务熔断",
  "task.failed": "任务失败",
  "task.steered": "人工干预",
  "milestone.integrated": "里程碑集成",
  "milestone.merged": "里程碑合入",
  "steering.started": "阶段转向",
  "steering.approved": "转向批准",
  "review.opened": "待人工审查",
  "review.approved": "审查通过",
  "ci.started": "CI 启动",
  "ci.passed": "CI 通过",
  "ci.failed": "CI 失败",
};

function labelForEvent(et) {
  if (!et) return "系统事件";
  if (EVENT_LABELS[et]) return EVENT_LABELS[et];
  if (et.startsWith("council.")) return "架构圆桌";
  if (et.startsWith("steering.")) return "阶段转向";
  if (et.startsWith("loopforge.")) return "底层引擎";
  if (et.startsWith("milestone.")) return "里程碑";
  if (et.startsWith("task.")) return "任务事件";
  if (et.startsWith("agent.")) return "智能体";
  return et;
}

export function renderEvents(events, onSelectTask) {
  const el = $("event-list");
  const cnt = $("event-count");
  if (!el) return;
  if (cnt) cnt.textContent = String(events.length);
  el.innerHTML = events
    .map((ev) => {
      const et = ev.event_type || "";
      const etLabel = labelForEvent(et);
      let cls = "";
      if (et === "agent.command") cls = "ev-command";
      else if (et === "agent.message" || et.startsWith("council.")) cls = "ev-message";
      else if (et === "agent.file" || et === "task.approved" || et === "milestone.merged" || et === "scheduler.auto_resumed") cls = "ev-file";
      else if (et === "agent.warning" || et.endsWith(".fused") || et.endsWith(".failed") || et === "scheduler.error") cls = "ev-warning";
      let link = "";
      if (ev.task_id) {
        link = onSelectTask
          ? ` · <button type="button" class="ev-link" data-ev-task="${esc(ev.task_id)}">${esc(ev.task_id)}</button>`
          : ` · ${esc(ev.task_id)}`;
      }
      const actor = ev.actor && ev.actor !== "system" ? `<strong>${esc(ev.actor)}</strong> ` : "";
      return `<div class="ev-item ${cls}">
        <div class="ev-head">
          <span title="${esc(et)}"><span class="ev-kind-tag">${esc(etLabel)}</span>${link}</span>
          <time datetime="${esc(ev.created_at)}">${esc((ev.created_at || "").slice(0, 19).replace("T", " "))}</time>
        </div>
        <div class="ev-body">${actor}${esc(ev.message)}</div>
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
              <summary class="details-summary"><strong>${esc(s.slice_id)}</strong> · ${esc(s.heading_path)} <span class="muted">(${s.char_count} 字)</span></summary>
              <p class="muted">${esc(s.summary)}</p>
              <pre>${esc(s.content)}</pre>
            </details>`
          )
          .join("")
      : `<div class="muted">暂无输入切片。支持在新建项目时上传 md / txt / pdf / docx 规格与架构文档。</div>`;
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
          <div><h3>需求与架构规格书（docs/PRD_SPEC.md）</h3><p class="card-desc">${esc(prd.product_vision || "")}</p></div>
        </div>
        <details>
          <summary class="details-summary muted">展开查看完整规格全文</summary>
          <pre>${esc(prd.spec_markdown || "")}</pre>
        </details>
      </div>`
    : "";

  const squadsHtml = squads.length
    ? `<div class="card">
        <div class="card-head">
          <div><h3>研发组划分与目录边界 (${squads.length} 组)</h3><p class="card-desc">技术栈：${esc(plan?.tech_stack || "-")}</p></div>
        </div>
        <table class="data-table">
          <thead><tr><th>研发组</th><th>沙箱</th><th>角色</th><th>负责目录</th><th>职责边界</th></tr></thead>
          <tbody>${squads
            .map(
              (s) => `<tr>
                <td><strong>${esc(s.name)}</strong><div class="muted">${esc(s.squad_id)}</div></td>
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
        ? `<div class="muted">目标研发组：<code>${esc((m.target_squads || []).join(", ") || "待定")}</code> · 关键交付物：${esc((m.key_deliverables || []).join("；") || m.goal)}</div>`
        : "";
      const rows = mTasks
        .map(
          (t) => `<details class="adr-card card-mt">
            <summary class="details-summary task-top">
              <span><strong>${esc(t.task_id)}</strong> [${esc(t.squad_name || t.module)} · ${esc(t.role)}] ${esc(t.title)}</span>
              <span><span class="muted">依赖：${esc((t.depends_on || []).join(",") || "无")}</span> <span class="status-pill ${esc(t.status)}">${esc(t.status)}</span></span>
            </summary>
            <div class="card-mt">
              <p>${esc(t.description)}</p>
              <strong>验收标准：</strong>
              <ul>${(t.acceptance_criteria || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
              <div class="cmsg-grid">
                <div><strong>允许修改路径</strong><pre>${esc((t.allowed_paths || []).join("\n"))}</pre></div>
                <div><strong>验证命令</strong><pre>${esc((t.verify_cmds || []).join("\n"))}</pre></div>
              </div>
              <button type="button" class="btn btn-sm" data-open-task="${esc(t.task_id)}">打开任务监视器</button>
            </div>
          </details>`
        )
        .join("");
      return `<div class="card">
        <div class="card-head">
          <div>
            <h3>${esc(m.milestone_id)} · ${esc(m.title)}</h3>
            <p class="card-desc">${esc(m.goal)} · 集成验证：<code>${esc(m.integration_cmd || "无")}</code></p>
          </div>
          <span class="status-pill ${esc(m.status)}">${planned ? `${mTasks.length} 单 · ${m.status}` : "待滚动拆解"}</span>
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
