import { $, esc, fmtElapsed } from "./api.js";

let _zoom = 1;

function nodeClass(status) {
  if (status === "APPROVED" || status === "MERGED") return "dag-st-done";
  if (status === "CANCELLED") return "dag-st-cancelled";
  if (status === "FUSED") return "dag-st-fused";
  if (status === "AWAITING_HUMAN_REVIEW") return "dag-st-review";
  if (["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(status)) return "dag-st-active";
  if (status === "READY") return "dag-st-ready";
  return "dag-st-blocked";
}

export function renderDAG(state, onSelectTask) {
  const canvas = $("dag-canvas");
  if (!canvas || !state) return;
  const tasks = state.tasks || [];
  const milestones = state.milestones || [];

  if (!tasks.length && !milestones.length) {
    canvas.innerHTML = `<div class="empty-hint">尚未生成任务拓扑图。完成需求输入后点击右上角「开始立项评审」自动拆解模块与依赖关系。</div>`;
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

  const cols = [];
  const colIndex = new Map();
  for (const t of tasks) {
    const k = `${String(msOrder.get(t.milestone_id) ?? 0).padStart(3, "0")}-${String(depthOf(t.task_id)).padStart(3, "0")}`;
    if (!colIndex.has(k)) {
      colIndex.set(k, cols.length);
      cols.push({ key: k, milestoneId: t.milestone_id, colDepth: depthOf(t.task_id), items: [] });
    }
    cols[colIndex.get(k)].items.push({ kind: "task", task: t });
  }
  cols.sort((a, b) => a.key.localeCompare(b.key));

  for (const m of milestones) {
    if (m.planned === false && !tasks.some((t) => t.milestone_id === m.milestone_id)) {
      cols.push({ key: `future-${m.milestone_id}`, milestoneId: m.milestone_id, colDepth: 0, items: [{ kind: "rolling", ms: m }] });
    }
  }

  const nodeW = 228, nodeH = 66, gapX = 58, gapY = 18, padX = 20, padY = 36;
  const pos = new Map();
  const colOfTask = new Map();
  let maxRows = 1;
  cols.forEach((col, ci) => {
    maxRows = Math.max(maxRows, col.items.length);
    col.x = padX + ci * (nodeW + gapX);
    col.items.forEach((item, ri) => {
      const y = padY + ri * (nodeH + gapY);
      if (item.kind === "task") {
        pos.set(item.task.task_id, { x: col.x, y });
        colOfTask.set(item.task.task_id, ci);
      } else {
        item.pos = { x: col.x, y };
      }
    });
  });

  const width = Math.max(640, padX * 2 + cols.length * nodeW + Math.max(0, cols.length - 1) * gapX);
  const height = padY + maxRows * nodeH + Math.max(0, maxRows - 1) * gapY + 22;

  const colHeaders = cols
    .map(
      (c, i) =>
        `<text x="${c.x + 2}" y="20" class="dag-col-head">${esc(c.milestoneId)}${i > 0 && cols[i - 1].milestoneId === c.milestoneId ? ` · W${c.colDepth + 1}` : ""}</text>`
    )
    .join("");

  const edges = [];
  for (const t of tasks) {
    const to = pos.get(t.task_id);
    const toCi = colOfTask.get(t.task_id) ?? 0;
    for (const dep of t.depends_on || []) {
      const from = pos.get(dep);
      if (!from || !to) continue;
      const fromCi = colOfTask.get(dep) ?? 0;
      const x1 = from.x + nodeW, y1 = from.y + nodeH / 2;
      const x2 = to.x, y2 = to.y + nodeH / 2;
      const done = ["APPROVED", "MERGED"].includes(byId.get(dep)?.status);
      // 跨多列连线时向上/下拱起避让中间列节点卡片
      let dPath;
      if (toCi - fromCi > 1 && Math.abs(y1 - y2) < nodeH) {
        const arcY = Math.max(10, Math.min(y1, y2) - 24);
        dPath = `M${x1},${y1} C${x1 + 28},${arcY} ${x2 - 28},${arcY} ${x2 - 5},${y2}`;
      } else {
        const cx = (x1 + x2) / 2;
        dPath = `M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2 - 5},${y2}`;
      }
      edges.push(
        `<path d="${dPath}" class="dag-edge ${done ? "done" : ""}" marker-end="url(#arr-${done ? "ok" : "dim"})"/>`
      );
    }
  }

  const nodeEls = [];
  for (const col of cols) {
    for (const item of col.items) {
      if (item.kind === "rolling") {
        const { x, y } = item.pos;
        const m = item.ms;
        const titleShort = m.title.length > 18 ? m.title.slice(0, 18) + "…" : m.title;
        nodeEls.push(
          `<g class="dag-rolling" transform="translate(${x},${y})">
            <title>${esc(`${m.milestone_id} · ${m.title}\n目标：${m.goal || ""}`)}</title>
            <rect class="node-box rolling" width="${nodeW}" height="${nodeH}" rx="8"/>
            <text x="12" y="21" class="dag-id">${esc(m.milestone_id)} · 待滚动拆解</text>
            <text x="12" y="41" class="dag-title">${esc(titleShort)}</text>
            <text x="12" y="56" class="dag-meta">前序里程碑合入后自动展开</text>
          </g>`
        );
        continue;
      }
      const t = item.task;
      const p = pos.get(t.task_id);
      const cls = nodeClass(t.status);
      const active = ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status);
      const titleShort = t.title.length > 18 ? t.title.slice(0, 18) + "…" : t.title;
      const sqLabel = (t.squad_name || t.squad || t.module || "").slice(0, 14);
      const tip = `${t.task_id} · ${t.title}\n状态：${t.status}\n研发组：${t.squad_name || t.squad || t.module} (${t.role})\n目录：${(t.allowed_paths || []).join(", ")}`;
      nodeEls.push(
        `<g class="dag-node ${cls} ${t.status === "CANCELLED" ? "cancelled" : ""}" data-task="${esc(t.task_id)}" transform="translate(${p.x},${p.y})">
          <title>${esc(tip)}</title>
          <rect class="node-box" width="${nodeW}" height="${nodeH}" rx="8"/>
          ${active ? `<circle class="pulse-dot" cx="${nodeW - 14}" cy="17" r="4"/>` : ""}
          <text x="12" y="21" class="dag-id">${esc(t.task_id)} · ${esc(t.status)}${active && t.active_started_at ? ` · <tspan class="elapsed" data-start="${esc(t.active_started_at)}">${esc(fmtElapsed(t.active_started_at))}</tspan>` : ""}</text>
          <text x="12" y="41" class="dag-title">${esc(titleShort)}</text>
          <text x="12" y="56" class="dag-meta">${esc(sqLabel)} · ${esc(t.role)}</text>
        </g>`
      );
    }
  }

  const scaledW = Math.round(width * _zoom);
  const scaledH = Math.round(height * _zoom);
  canvas.innerHTML = `
    <div class="dag-zoom-bar">
      <button type="button" class="btn btn-xs" data-dag-zoom="out" title="缩小">−</button>
      <button type="button" class="btn btn-xs" data-dag-zoom="reset" title="重置比例">${Math.round(_zoom * 100)}%</button>
      <button type="button" class="btn btn-xs" data-dag-zoom="in" title="放大">＋</button>
    </div>
    <svg width="${scaledW}" height="${scaledH}" viewBox="0 0 ${width} ${height}">
      <defs>
        <marker id="arr-dim" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" class="dag-arrow-dim"/>
        </marker>
        <marker id="arr-ok" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" class="dag-arrow-ok"/>
        </marker>
      </defs>
      ${colHeaders}
      ${edges.join("")}
      ${nodeEls.join("")}
    </svg>`;

  canvas.querySelectorAll(".dag-node").forEach((g) => {
    g.addEventListener("click", () => onSelectTask(g.dataset.task));
  });
  canvas.querySelectorAll("[data-dag-zoom]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.dagZoom;
      _zoom = mode === "in" ? Math.min(1.5, _zoom + 0.15) : mode === "out" ? Math.max(0.65, _zoom - 0.15) : 1;
      renderDAG(state, onSelectTask);
    });
  });
}
