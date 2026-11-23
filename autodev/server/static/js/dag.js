import { $, esc, fmtElapsed } from "./api.js";

function nodeColors(status) {
  if (status === "APPROVED" || status === "MERGED")
    return { fill: "#ecfdf5", stroke: "#10b981", tag: "#059669", title: "#0f172a" };
  if (status === "FUSED")
    return { fill: "#fff1f2", stroke: "#e11d48", tag: "#be123c", title: "#0f172a" };
  if (["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(status))
    return { fill: "#eff6ff", stroke: "#2563eb", tag: "#1d4ed8", title: "#0f172a" };
  if (status === "READY")
    return { fill: "#ffffff", stroke: "#3b82f6", tag: "#2563eb", title: "#0f172a" };
  return { fill: "#ffffff", stroke: "#cbd5e1", tag: "#64748b", title: "#334155" };
}

export function renderDAG(state, onSelectTask) {
  const canvas = $("dag-canvas");
  if (!canvas || !state) return;
  const tasks = state.tasks || [];
  const milestones = state.milestones || [];

  if (!tasks.length && !milestones.length) {
    canvas.innerHTML = `<div class="muted" style="padding:18px;text-align:center">尚未生成规划图。点击右上角「摄入资料」→「圆桌与立项」自动生成架构任务流程图。</div>`;
    return;
  }

  const byId = new Map(tasks.map((t) => [t.task_id, t]));
  const msOrder = new Map(milestones.map((m, i) => [m.milestone_id, i]));
  const memo = new Map();
  const depthOf = (tid, stack = new Set()) => {
    if (memo.has(tid)) return memo.get(tid);
    if (stack.has(tid)) return 0;
    stack.add(tid);
    const t = byId.get(tid);
    let d = 0;
    for (const dep of t?.depends_on || []) {
      if (byId.has(dep)) d = Math.max(d, 1 + depthOf(dep, stack));
    }
    memo.set(tid, d);
    return d;
  };

  // 构建列：已展开任务按 (milestone, depth) 分列；未展开的滚动里程碑以占位节点展示在右侧
  const cols = [];
  const colIndex = new Map();
  for (const t of tasks) {
    const k = `${String(msOrder.get(t.milestone_id) ?? 0).padStart(3, "0")}-${String(depthOf(t.task_id)).padStart(3, "0")}`;
    if (!colIndex.has(k)) {
      colIndex.set(k, cols.length);
      cols.push({ key: k, milestoneId: t.milestone_id, items: [] });
    }
    cols[colIndex.get(k)].items.push({ kind: "task", task: t });
  }
  cols.sort((a, b) => a.key.localeCompare(b.key));

  for (const m of milestones) {
    if (m.planned === false && !tasks.some((t) => t.milestone_id === m.milestone_id)) {
      cols.push({ key: `future-${m.milestone_id}`, milestoneId: m.milestone_id, items: [{ kind: "rolling", ms: m }] });
    }
  }

  const nodeW = 204, nodeH = 62, gapX = 56, gapY = 16, padX = 18, padY = 34;
  const pos = new Map();
  let maxRows = 1;
  cols.forEach((col, ci) => {
    maxRows = Math.max(maxRows, col.items.length);
    col.x = padX + ci * (nodeW + gapX);
    col.items.forEach((item, ri) => {
      const y = padY + ri * (nodeH + gapY);
      if (item.kind === "task") pos.set(item.task.task_id, { x: col.x, y });
      else item.pos = { x: col.x, y };
    });
  });

  const width = Math.max(600, padX * 2 + cols.length * nodeW + Math.max(0, cols.length - 1) * gapX);
  const height = padY + maxRows * nodeH + Math.max(0, maxRows - 1) * gapY + 18;

  const colHeaders = cols
    .map(
      (c) =>
        `<text x="${c.x + 2}" y="18" fill="#64748b" font-size="11" font-family="ui-monospace, monospace" font-weight="600">${esc(c.milestoneId)}</text>`
    )
    .join("");

  const edges = [];
  for (const t of tasks) {
    const to = pos.get(t.task_id);
    for (const dep of t.depends_on || []) {
      const from = pos.get(dep);
      if (!from || !to) continue;
      const x1 = from.x + nodeW, y1 = from.y + nodeH / 2;
      const x2 = to.x, y2 = to.y + nodeH / 2;
      const cx = (x1 + x2) / 2;
      const done = ["APPROVED", "MERGED"].includes(byId.get(dep)?.status);
      edges.push(
        `<path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2 - 5},${y2}" fill="none" stroke="${done ? "#10b981" : "#94a3b8"}" stroke-width="${done ? 2 : 1.5}" marker-end="url(#arr-${done ? "ok" : "dim"})"/>`
      );
    }
  }

  const nodeEls = [];
  for (const col of cols) {
    for (const item of col.items) {
      if (item.kind === "rolling") {
        const { x, y } = item.pos;
        const m = item.ms;
        const titleShort = m.title.length > 14 ? m.title.slice(0, 14) + "…" : m.title;
        nodeEls.push(
          `<g transform="translate(${x},${y})">
            <rect width="${nodeW}" height="${nodeH}" rx="8" fill="#f8fafc" stroke="#94a3b8" stroke-dasharray="5 4" stroke-width="1.4"/>
            <text x="11" y="21" fill="#64748b" font-size="11" font-family="ui-monospace, monospace" font-weight="600">${esc(m.milestone_id)} · 待滚动展开</text>
            <text x="11" y="40" fill="#334155" font-size="12.5" font-weight="600">${esc(titleShort)}</text>
            <text x="11" y="54" fill="#64748b" font-size="10.5">前序合入后总监自动拆单</text>
          </g>`
        );
        continue;
      }
      const t = item.task;
      const p = pos.get(t.task_id);
      const c = nodeColors(t.status);
      const active = ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status);
      const elap = active && t.active_started_at ? ` · ${fmtElapsed(t.active_started_at)}` : "";
      const titleShort = t.title.length > 14 ? t.title.slice(0, 14) + "…" : t.title;
      const sqLabel = (t.squad_name || t.squad || t.module || "").slice(0, 12);
      nodeEls.push(
        `<g class="dag-node" data-task="${esc(t.task_id)}" transform="translate(${p.x},${p.y})">
          <rect class="node-box" width="${nodeW}" height="${nodeH}" rx="8" fill="${c.fill}" stroke="${c.stroke}" stroke-width="1.5"/>
          ${active ? `<circle class="pulse-dot" cx="${nodeW - 14}" cy="16" r="4" fill="#2563eb"/>` : ""}
          <text x="11" y="20" fill="${c.tag}" font-size="11" font-family="ui-monospace, monospace" font-weight="700">${esc(t.task_id)} · ${esc(t.status)}${esc(elap)}</text>
          <text x="11" y="38" fill="${c.title}" font-size="12.5" font-weight="600">${esc(titleShort)}</text>
          <text x="11" y="54" fill="#64748b" font-size="10.5">${esc(sqLabel)} · ${esc(t.role)}</text>
        </g>`
      );
    }
  }

  canvas.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <defs>
      <marker id="arr-dim" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#94a3b8"/>
      </marker>
      <marker id="arr-ok" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#10b981"/>
      </marker>
    </defs>
    ${colHeaders}
    ${edges.join("")}
    ${nodeEls.join("")}
  </svg>`;

  canvas.querySelectorAll(".dag-node").forEach((g) => {
    g.addEventListener("click", () => onSelectTask(g.dataset.task));
  });
}
