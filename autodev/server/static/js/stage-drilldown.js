import { $, ROLE_NAMES, api, esc, toast } from "./api.js";

const PROJECT_STATUS_CN = {
  CREATED: "已创建",
  INGESTED: "已摄入需求",
  SPECIFIED: "规格已定义",
  PLANNED: "蓝图待批准",
  APPROVED: "蓝图已批 · 待启动",
  RUNNING: "自动研发中",
  STALLED: "存在异常待处理",
  COMPLETED: "全部交付完成",
};

const TASK_STATUS_CN = {
  BLOCKED: "等待前置",
  READY: "就绪待派发",
  IN_PROGRESS: "编码执行中",
  VERIFYING: "自动化验证中",
  REVIEWING: "独立审查中",
  AWAITING_HUMAN_REVIEW: "待人工审查",
  APPROVED: "已通过",
  MERGED: "已合入",
  FUSED: "异常熔断",
  CANCELLED: "已取消",
};

const POOL_LABELS = {
  local_runner: "本机 Runner",
  public_docker: "公用 Docker 沙箱",
  auto: "智能自动",
};

const NODE_STATUS_ICON = {
  succeeded: "✓",
  running: "●",
  waiting: "●",
  waiting_approval: "⏸",
  failed: "✕",
  cancelled: "✕",
  skipped: "–",
  pending: "○",
};

const NODE_STATUS_CN = {
  succeeded: "已完成",
  running: "执行中",
  waiting: "等待中",
  waiting_approval: "待签字",
  failed: "失败",
  cancelled: "已取消",
  skipped: "已跳过",
  pending: "待执行",
};

const SPEAKER_CN = {
  pm: "产品经理 · PM",
  architect: "契约与架构师",
  qa_lead: "QA 质量总监",
  director: "研发总监裁决",
  human: "负责人意见",
};

const dd = {
  processData: null,
  selectedStageId: null,
  userPinnedStage: false,
  expandedTasks: new Set(),
  userToggledTasks: new Set(),
  expandedNodes: new Set(),
  userToggledNodes: new Set(),
  showPlatformNodes: new Set(),
  streamCache: new Map(), // taskId -> { data, fetchedAt, loading, sig }
  extraCache: { slices: null, council: null, plan: null, loadingStage: "" },
  lastState: null,
  lastLive: null,
  opts: {},
};

function fmtDurationSec(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return "";
  const total = Math.max(0, Math.floor(Number(sec)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTimeShort(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString("zh-CN", { hourCycle: "h23" }) : "";
}

function formatRichInline(text) {
  const escaped = esc(text || "");
  return escaped.replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

function resolveDefaultStage(state, processData) {
  const stages = (processData?.stages || []).filter((s) => s.enabled !== false);
  const byId = new Map(stages.map((s) => [s.id, s]));
  const tasks = state?.tasks || [];

  const waiting = stages.find((s) => s.status === "WAITING_GATE");
  if (waiting) return waiting.id;

  const hasActiveTask = tasks.some((t) => ["IN_PROGRESS", "VERIFYING", "REVIEWING", "FUSED"].includes(t.status));
  if (hasActiveTask && byId.has("implementation")) return "implementation";

  const hasHumanReview = tasks.some((t) => t.status === "AWAITING_HUMAN_REVIEW");
  if (hasHumanReview && byId.has("code_review")) return "code_review";

  if ((state?.status === "COMPLETED" || (state?.milestones || []).some((m) => m.status === "AWAITING_MERGE")) && byId.has("integration_delivery")) {
    return "integration_delivery";
  }

  const inProg = stages.find((s) => s.status === "IN_PROGRESS");
  if (inProg) return inProg.id;

  if ((state?.planning || state?.status === "PLANNED") && byId.has("architecture")) return "architecture";
  if ((state?.status === "APPROVED" || state?.status === "RUNNING" || state?.status === "STALLED") && byId.has("implementation")) {
    return "implementation";
  }
  return stages[0]?.id || "implementation";
}

function syncStepperSelection() {
  const stepper = $("pipeline-stepper");
  if (!stepper || !dd.selectedStageId) return;
  stepper.querySelectorAll("[data-pstep-id]").forEach((btn) => {
    const active = btn.dataset.pstepId === dd.selectedStageId;
    btn.classList.toggle("is-selected", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

export function setProcessData(processData) {
  dd.processData = processData;
  if (!dd.userPinnedStage && dd.lastState) {
    dd.selectedStageId = resolveDefaultStage(dd.lastState, processData);
  }
  syncStepperSelection();
  if (dd.lastState) {
    renderStageDrilldown(dd.lastState, dd.lastLive, dd.opts);
  }
}

export function selectStage(stageId, { userInitiated = false } = {}) {
  if (!stageId) return;
  dd.selectedStageId = stageId;
  if (userInitiated) dd.userPinnedStage = true;
  syncStepperSelection();
  if (dd.lastState) {
    renderStageDrilldown(dd.lastState, dd.lastLive, dd.opts);
  }
}

export function resetDrilldownProject() {
  dd.selectedStageId = null;
  dd.userPinnedStage = false;
  dd.expandedTasks.clear();
  dd.userToggledTasks.clear();
  dd.expandedNodes.clear();
  dd.userToggledNodes.clear();
  dd.showPlatformNodes.clear();
  dd.streamCache.clear();
  dd.extraCache = { slices: null, council: null, plan: null, loadingStage: "" };
}

async function fetchTaskStream(taskId, { force = false, silent = false } = {}) {
  const prev = dd.streamCache.get(taskId);
  if (prev?.loading) return;
  if (!force && prev?.data && Date.now() - prev.fetchedAt < 3200) return;

  dd.streamCache.set(taskId, { ...(prev || {}), loading: true });
  if (!silent && !prev?.data) renderCurrent();

  try {
    const data = await api(`/vernal-api/tasks/${encodeURIComponent(taskId)}/stream`);
    const sig = JSON.stringify((data.nodes || []).map((n) => [n.id, n.status, (n.items || []).length, n.error || ""]));
    const changed = sig !== prev?.sig || !prev?.data;
    dd.streamCache.set(taskId, { data, fetchedAt: Date.now(), loading: false, sig });

    // Auto-expand the most informative node once on first stream load
    const nodes = data.nodes || [];
    const runningNode = nodes.find((n) => ["running", "waiting", "waiting_approval", "failed"].includes(n.status));
    const latestWithItems = [...nodes].reverse().find((n) => (n.items || []).some((it) => it.kind !== "system"));
    const preferred = runningNode || latestWithItems || nodes[0];
    if (preferred) {
      const nKey = `${taskId}:${preferred.id}`;
      if (!dd.userToggledNodes.has(nKey)) {
        dd.expandedNodes.add(nKey);
      }
      // If running node is currently stalled/sparse (e.g. package) while implement has rich thoughts, keep running node open
    }
    if (changed) renderCurrent();
  } catch (err) {
    dd.streamCache.set(taskId, {
      ...(prev || {}),
      loading: false,
      fetchedAt: Date.now(),
      error: err.message,
    });
    if (!silent) renderCurrent();
  }
}

async function ensureStageExtraData(stageId) {
  if (dd.extraCache.loadingStage === stageId) return;
  if (stageId === "requirements" && dd.extraCache.slices === null) {
    dd.extraCache.loadingStage = stageId;
    try {
      dd.extraCache.slices = await api("/vernal-api/slices");
    } catch {
      dd.extraCache.slices = [];
    } finally {
      dd.extraCache.loadingStage = "";
      renderCurrent();
    }
  } else if ((stageId === "architecture" || stageId === "detailed_design") && (dd.extraCache.council === null || dd.extraCache.plan === null)) {
    dd.extraCache.loadingStage = stageId;
    try {
      const [c, p] = await Promise.all([api("/vernal-api/council"), api("/vernal-api/plan")]);
      dd.extraCache.council = c;
      dd.extraCache.plan = p;
    } catch {
      dd.extraCache.council = { messages: [] };
      dd.extraCache.plan = {};
    } finally {
      dd.extraCache.loadingStage = "";
      renderCurrent();
    }
  }
}

export function pollOpenTaskStreams() {
  if (!dd.lastState || dd.lastState.workspace_initialized === false) return;
  if (!["implementation", "testing", "code_review"].includes(dd.selectedStageId || "")) return;
  const tasksById = new Map((dd.lastState.tasks || []).map((t) => [t.task_id, t]));
  for (const tid of dd.expandedTasks) {
    const t = tasksById.get(tid);
    if (!t) continue;
    if (["IN_PROGRESS", "VERIFYING", "REVIEWING", "FUSED"].includes(t.status)) {
      fetchTaskStream(tid, { force: true, silent: true });
    }
  }
}

export function renderStreamItemHtml(item) {
  const ts = fmtTimeShort(item.timestamp);
  const tsHtml = ts ? `<time class="vst-time">${esc(ts)}</time>` : "";
  if (item.kind === "thought") {
    return `<div class="vst-item vst-thought">
      <div class="vst-item-head"><span class="vst-badge thought">思考</span><span class="vst-item-title">${esc(item.title || "模型思考")}</span>${tsHtml}</div>
      <div class="vst-prose">${formatRichInline(item.body)}</div>
    </div>`;
  }
  if (item.kind === "message") {
    return `<div class="vst-item vst-message">
      <div class="vst-item-head"><span class="vst-badge message">进展</span><span class="vst-item-title">${esc(item.title || "智能体汇报")}</span>${tsHtml}</div>
      <div class="vst-prose">${formatRichInline(item.body)}</div>
    </div>`;
  }
  if (item.kind === "tool") {
    const st = item.status || "";
    const stCls = st === "failed" ? "err" : st === "in_progress" ? "running" : "ok";
    const stTxt = st === "failed" ? "失败" : st === "in_progress" ? "已发起" : st === "completed" || st === "succeeded" ? "已执行" : "已记录";
    const isShort = (item.body || "").length <= 120 && !(item.body || "").includes("\n");
    return `<details class="vst-item vst-tool" ${isShort ? "" : ""}>
      <summary class="vst-tool-sum">
        <span class="vst-tool-left">
          <span class="vst-badge tool">⌘</span>
          <span class="vst-tool-title">${esc(item.title || "执行命令")}</span>
        </span>
        <span class="vst-tool-right">
          <span class="vst-mini-pill ${stCls}">${stTxt}</span>
          ${tsHtml}
        </span>
      </summary>
      <pre class="vst-code">${esc(item.body || "")}</pre>
    </details>`;
  }
  if (item.kind === "stdout" || item.kind === "stderr") {
    const lines = (item.body || "").split("\n");
    const isErr = item.kind === "stderr";
    if (lines.length <= 6 && (item.body || "").length <= 420) {
      return `<div class="vst-item ${isErr ? "vst-stderr" : "vst-stdout"}">
        <div class="vst-item-head"><span class="vst-badge ${isErr ? "err" : "stdout"}">${isErr ? "诊断" : "输出"}</span><span class="vst-item-title">${esc(item.title || "终端输出")}</span>${tsHtml}</div>
        <pre class="vst-code compact">${esc(item.body || "")}</pre>
      </div>`;
    }
    return `<details class="vst-item ${isErr ? "vst-stderr" : "vst-stdout"}">
      <summary class="vst-tool-sum">
        <span class="vst-tool-left">
          <span class="vst-badge ${isErr ? "err" : "stdout"}">${isErr ? "诊断" : "输出"}</span>
          <span class="vst-tool-title">${esc(item.title || "终端输出")}（${lines.length} 行）</span>
        </span>
        <span class="vst-tool-right">${tsHtml}</span>
      </summary>
      <pre class="vst-code">${esc(item.body || "")}</pre>
    </details>`;
  }
  return `<div class="vst-item vst-system">
    <div class="vst-item-head"><span class="vst-badge system">平台</span><span class="vst-item-title">${esc(item.title || "系统")}</span>${tsHtml}</div>
    <div class="vst-sys-body">${esc(item.body || "")}</div>
  </div>`;
}

function renderNodeAccordionHtml(taskId, node, idx) {
  const nKey = `${taskId}:${node.id}`;
  const isOpen = dd.expandedNodes.has(nKey);
  const nst = node.status || "pending";
  const stepCls =
    nst === "succeeded"
      ? "ok"
      : nst === "running" || nst === "waiting"
        ? "running"
        : nst === "failed" || nst === "cancelled"
          ? "err"
          : nst === "waiting_approval"
            ? "warn"
            : "";
  const icon = NODE_STATUS_ICON[nst] || "○";
  const items = node.items || [];
  const mainItems = items.filter((it) => it.kind !== "system");
  const sysItems = items.filter((it) => it.kind === "system");
  const showSys = dd.showPlatformNodes.has(nKey);
  const visibleItems = showSys ? items : mainItems;

  const thoughtCount = mainItems.filter((it) => it.kind === "thought" || it.kind === "message").length;
  const toolCount = Math.max(Number(node.tool_calls || 0), mainItems.filter((it) => it.kind === "tool").length);
  const metaChips = [];
  if (node.provider) metaChips.push(node.provider);
  if (thoughtCount > 0) metaChips.push(`${thoughtCount} 段思考/汇报`);
  if (toolCount > 0) metaChips.push(`${toolCount} 次命令`);
  if (node.elapsed_sec !== null && node.elapsed_sec !== undefined && nst !== "pending") {
    metaChips.push(fmtDurationSec(node.elapsed_sec));
  }

  let bodyHtml = "";
  if (isOpen) {
    const errBlock = node.error ? `<div class="vdd-inline-alert err"><strong>节点错误：</strong>${esc(node.error)}</div>` : "";
    const outBlock =
      node.output_preview && !mainItems.some((it) => it.body && it.body.includes(node.output_preview.slice(0, 40)))
        ? `<div class="vst-item vst-message"><div class="vst-item-head"><span class="vst-badge message">总结</span><span class="vst-item-title">节点输出摘要</span></div><div class="vst-prose">${formatRichInline(node.output_preview)}</div></div>`
        : "";
    const streamHtml = visibleItems.length
      ? visibleItems.map(renderStreamItemHtml).join("")
      : nst === "pending"
        ? `<div class="vdd-empty-inline">等待前置节点完成后自动进入此步骤…</div>`
        : `<div class="vdd-empty-inline">暂无详细思考/命令记录${sysItems.length ? `（另有 ${sysItems.length} 条平台调度日志可展开查看）` : "，正在同步…"}</div>`;
    const liveFoot =
      nst === "running" || nst === "waiting"
        ? `<div class="vst-live-foot"><i></i>正在实时同步该节点的思考链与命令流（每 4 秒自动刷新）</div>`
        : "";
    const sysToggle = sysItems.length
      ? `<div class="vst-sys-bar"><button type="button" class="btn btn-xs" data-vdd-sys="${esc(nKey)}">${showSys ? `隐藏平台底层事件 (${sysItems.length})` : `显示平台底层事件 (${sysItems.length})`}</button></div>`
      : "";

    bodyHtml = `<div class="vdd-node-body">
      ${errBlock}
      <div class="vst-feed">${streamHtml}${outBlock}</div>
      ${liveFoot}
      ${sysToggle}
    </div>`;
  }

  return `<div class="vdd-node ${stepCls} ${isOpen ? "is-open" : ""}">
    <button type="button" class="vdd-node-head" data-vdd-node="${esc(nKey)}" aria-expanded="${isOpen ? "true" : "false"}">
      <span class="vdd-node-left">
        <span class="vdd-node-icon ${stepCls}">${icon}</span>
        <span class="vdd-node-idx">${idx + 1}.</span>
        <strong class="vdd-node-title">${esc(node.title || node.id)}</strong>
        <span class="vdd-node-state ${stepCls}">${esc(NODE_STATUS_CN[nst] || nst)}</span>
      </span>
      <span class="vdd-node-right">
        ${metaChips.map((c) => `<span class="vdd-meta-chip">${esc(c)}</span>`).join("")}
        <span class="vdd-chevron">${isOpen ? "▾" : "▸"}</span>
      </span>
    </button>
    ${bodyHtml}
  </div>`;
}

function renderTaskRowHtml(t, runInfo) {
  const tid = t.task_id;
  const isOpen = dd.expandedTasks.has(tid);
  const st = t.status || "READY";
  const isActive = ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(st);
  const isDone = ["APPROVED", "MERGED"].includes(st);
  const isErr = st === "FUSED" || Boolean(runInfo?.stalled);
  const stCls = isErr ? "err" : isDone ? "ok" : isActive ? "running" : st === "AWAITING_HUMAN_REVIEW" ? "warn" : "";

  const cache = dd.streamCache.get(tid);
  const nodes = cache?.data?.nodes?.length ? cache.data.nodes : runInfo?.nodes || [];
  const activeNode = nodes.find((n) => ["running", "waiting", "waiting_approval", "failed"].includes(n.status));
  const poolTxt = runInfo?.pool ? POOL_LABELS[runInfo.pool] || runInfo.pool : "";
  const elapsedTxt = runInfo?.elapsed_sec !== null && runInfo?.elapsed_sec !== undefined ? fmtDurationSec(runInfo.elapsed_sec) : "";
  const currentPill = activeNode
    ? `<span class="vdd-cur-node ${activeNode.status === "failed" ? "err" : "running"}">● ${esc(activeNode.title || activeNode.id)}</span>`
    : st === "BLOCKED" && (t.depends_on || []).length
      ? `<span class="vdd-meta-chip">依赖 ${esc(t.depends_on.join(", "))}</span>`
      : "";

  let bodyHtml = "";
  if (isOpen) {
    const stallAlert = runInfo?.stalled
      ? `<div class="vdd-inline-alert warn"><strong>⚠ 异常提醒：</strong><span>${esc(runInfo.stalled_reason || "当前任务长时间无进展")}</span></div>`
      : t.failure_reason
        ? `<div class="vdd-inline-alert err"><strong>失败原因：</strong><span>${esc(t.failure_reason)}</span></div>`
        : "";
    const canRetry = Boolean(runInfo?.stalled || st === "FUSED" || runInfo?.run_status === "failed" || runInfo?.run_status === "cancelled");
    const runId = t.loopforge_run_id || runInfo?.run_id || cache?.data?.run_id || "";
    const actionsHtml = `<div class="vdd-task-toolbar">
      <div class="vdd-task-desc">${esc(t.description || "")}</div>
      <div class="vset-inline-actions">
        ${canRetry ? `<button type="button" class="btn btn-sm btn-primary" data-vdd-restart="${esc(tid)}">一键重试此任务</button>` : ""}
        <button type="button" class="btn btn-sm" data-vdd-refresh-stream="${esc(tid)}">刷新执行流</button>
        <button type="button" class="btn btn-sm" data-vdd-drawer="${esc(tid)}" data-vdd-tab="diff">代码 Diff</button>
        <button type="button" class="btn btn-sm" data-vdd-drawer="${esc(tid)}" data-vdd-tab="verify">验收与门禁</button>
        ${runId ? `<button type="button" class="btn btn-sm" data-vdd-run="${esc(runId)}">原生 Run ↗</button>` : ""}
      </div>
    </div>`;

    let innerLayerHtml = "";
    if (nodes.length > 0) {
      innerLayerHtml = `<div class="vdd-nodes-stack">${nodes.map((n, i) => renderNodeAccordionHtml(tid, n, i)).join("")}</div>`;
    } else if (cache?.loading) {
      innerLayerHtml = `<div class="vdd-empty-inline">正在拉取该任务的 SOP 节点与思考流…</div>`;
    } else if (isActive || isDone || st === "FUSED" || st === "AWAITING_HUMAN_REVIEW") {
      const fallbackEvs = cache?.data?.fallback_events || [];
      innerLayerHtml = fallbackEvs.length
        ? `<div class="vst-feed">${fallbackEvs
            .slice(-12)
            .map((ev) =>
              renderStreamItemHtml({
                kind: ev.event_type === "agent.command" ? "tool" : ev.event_type === "agent.warning" ? "stderr" : "message",
                title: ev.event_type || "事件",
                body: ev.message || "",
                timestamp: ev.created_at || "",
              })
            )
            .join("")}</div>`
        : `<div class="vdd-empty-inline">尚未产生底层节点记录（${esc(t.latest_activity?.message || "等待执行机上报")}）。</div>`;
    } else {
      const criteria = (t.acceptance_criteria || []).map((c) => `<li>${esc(c)}</li>`).join("");
      const paths = (t.allowed_paths || []).join(", ");
      const cmds = (t.verify_cmds || []).join(" && ");
      innerLayerHtml = `<div class="vdd-spec-box">
        ${criteria ? `<div><strong>验收标准</strong><ul>${criteria}</ul></div>` : ""}
        <div class="vdd-spec-meta">
          ${paths ? `<span>允许修改：<code>${esc(paths)}</code></span>` : ""}
          ${cmds ? `<span>验证命令：<code>${esc(cmds)}</code></span>` : ""}
        </div>
      </div>`;
    }

    bodyHtml = `<div class="vdd-task-body">
      ${stallAlert}
      ${actionsHtml}
      ${innerLayerHtml}
    </div>`;
  }

  return `<article class="vdd-task ${stCls} ${isOpen ? "is-open" : ""}">
    <button type="button" class="vdd-task-head" data-vdd-task="${esc(tid)}" aria-expanded="${isOpen ? "true" : "false"}">
      <span class="vdd-task-left">
        <span class="vdd-dot ${stCls}"></span>
        <span class="vmc-task-code">${esc(tid)}</span>
        <strong class="vdd-task-title">${esc(t.title)}</strong>
        ${currentPill}
      </span>
      <span class="vdd-task-right">
        ${poolTxt ? `<span class="vdd-meta-chip">${esc(poolTxt)}</span>` : ""}
        ${elapsedTxt ? `<span class="vmc-elapsed">${esc(elapsedTxt)}</span>` : ""}
        <span class="status-pill ${esc(st)}">${esc(TASK_STATUS_CN[st] || st)}</span>
        <span class="vdd-chevron">${isOpen ? "▾" : "▸"}</span>
      </span>
    </button>
    ${bodyHtml}
  </article>`;
}

function renderImplementationStage(stage, state, liveProgress) {
  const tasks = state.tasks || [];
  const milestones = state.milestones || [];
  const ms = liveProgress?.milestone_summary || null;
  const totalTasks = ms ? ms.tasks_total : tasks.length;
  const doneTasks = ms ? ms.tasks_done : tasks.filter((t) => ["APPROVED", "MERGED"].includes(t.status)).length;
  const activeCount = ms ? ms.tasks_active : tasks.filter((t) => ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status)).length;
  const reviewCount = ms ? ms.tasks_review : tasks.filter((t) => t.status === "AWAITING_HUMAN_REVIEW").length;
  const readyCount = ms ? ms.tasks_ready : tasks.filter((t) => t.status === "READY").length;
  const fusedCount = ms ? ms.tasks_fused : tasks.filter((t) => t.status === "FUSED").length;
  const pct = totalTasks > 0 ? Math.round((doneTasks * 100) / totalTasks) : 0;

  const curMsId = ms?.current_milestone_id || milestones.find((m) => m.status !== "MERGED")?.milestone_id || "";
  const curMsTitle = ms?.current_milestone_title || milestones.find((m) => m.milestone_id === curMsId)?.title || "";
  const schedOn = Boolean(liveProgress ? liveProgress.scheduler_running : state.scheduler_running);
  const autoResumable = Boolean(
    liveProgress?.auto_resumable || (!schedOn && ["APPROVED", "RUNNING", "STALLED"].includes(state.status) && totalTasks > doneTasks)
  );

  let schedHtml = "";
  if (schedOn) {
    schedHtml = `<span class="vmc-sched-pill live"><i></i>调度自动追踪中</span>`;
  } else if (state.status === "COMPLETED") {
    schedHtml = `<span class="vmc-sched-pill done">全部交付完成 ✓</span>`;
  } else if (autoResumable) {
    schedHtml = `<span class="vmc-sched-pill paused">调度已暂停</span><button type="button" class="btn btn-primary btn-sm" data-vdd-resume="1">恢复自动追踪</button>`;
  } else {
    schedHtml = `<span class="vset-scope-badge">${esc(PROJECT_STATUS_CN[state.status] || state.status)}</span>`;
  }

  if (!tasks.length) {
    return `${renderStageHeader(stage, schedHtml, "点击任务可逐层展开 SOP 节点与 AI 实时思考链 / 命令流")}
      <div class="vdd-empty-stage">
        <p>当前尚未拆解研发任务。完成需求输入并点击右上角「开始立项评审」后，任务将在此处按层级展开显示。</p>
      </div>`;
  }

  const runByTask = new Map((liveProgress?.active_runs || []).map((r) => [r.task_id, r]));

  // Auto-expand active or fused tasks once unless the user explicitly collapsed them
  for (const t of tasks) {
    if (["IN_PROGRESS", "VERIFYING", "REVIEWING", "FUSED"].includes(t.status) && !dd.userToggledTasks.has(t.task_id)) {
      dd.expandedTasks.add(t.task_id);
      if (!dd.streamCache.has(t.task_id)) {
        queueMicrotask(() => fetchTaskStream(t.task_id));
      }
    }
  }

  const groups = new Map();
  for (const t of tasks) {
    const mid = t.milestone_id || "M1";
    if (!groups.has(mid)) groups.set(mid, []);
    groups.get(mid).push(t);
  }
  const msById = new Map(milestones.map((m) => [m.milestone_id, m]));
  const tasksHtml = [...groups.entries()]
    .map(([mid, list]) => {
      const mObj = msById.get(mid);
      const groupTitle = groups.size > 1 ? `<div class="vdd-group-head"><strong>${esc(mid)}${mObj ? ` · ${esc(mObj.title)}` : ""}</strong><span>${list.filter((x) => ["APPROVED", "MERGED"].includes(x.status)).length}/${list.length} 完成</span></div>` : "";
      return `${groupTitle}<div class="vdd-task-list">${list.map((t) => renderTaskRowHtml(t, runByTask.get(t.task_id))).join("")}</div>`;
    })
    .join("");

  return `
    ${renderStageHeader(stage, schedHtml, curMsId ? `${curMsId} · ${curMsTitle} · 点击任意任务可逐层展开 SOP 节点与思考链` : "点击任意任务可逐层展开 SOP 节点与思考链")}
    <div class="vdd-progress-strip">
      <div class="vmc-progress-wrap">
        <div class="vmc-progress-track" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
          <div class="vmc-progress-fill" style="width:${pct}%"></div>
        </div>
        <span class="vmc-progress-pct">${pct}%</span>
      </div>
      <div class="vdd-stat-chips">
        <span>已完成 <strong>${doneTasks}/${totalTasks}</strong></span>
        <span class="${activeCount > 0 ? "is-accent" : ""}">执行中 <strong>${activeCount}</strong></span>
        <span class="${reviewCount > 0 ? "is-warn" : ""}">待审查 <strong>${reviewCount}</strong></span>
        <span>就绪排队 <strong>${readyCount}</strong></span>
        ${fusedCount > 0 ? `<span class="is-err">异常熔断 <strong>${fusedCount}</strong></span>` : ""}
      </div>
    </div>
    ${tasksHtml}
  `;
}

function renderNextActionBanner(liveProgress, state) {
  let na = liveProgress?.next_action;
  if (!na) return "";
  if (na.action_kind === "start_plan" && (state.specifications_enabled || state.mission_count)) {
    na = {...na, label:"继续需求与交付", hint:"进入需求面板，确认待回答问题、执行条件和当前规格，再按审批流程继续。", action_kind:"continue_mission"};
  }
  const cls =
    na.bucket === "waiting_approval"
      ? "warn"
      : na.bucket === "stalled"
        ? "err"
        : na.bucket === "completed"
          ? "ok"
          : na.bucket === "running"
            ? "running"
            : "";
  const whoBadge = na.who === "human" ? "下一步由你操作" : na.who === "system" ? "系统执行中" : "已结束";
  let cta = "";
  if (na.action_kind === "continue_mission") {
    cta = `<button type="button" class="btn btn-primary btn-sm" data-vdd-jump-view="council">继续需求交付</button>`;
  } else if (na.action_kind === "approve_plan") {
    cta = `<button type="button" class="btn btn-primary btn-sm" data-vdd-approve-start="1">批准并启动研发</button>`;
  } else if (na.action_kind === "approve_gate" && na.run_id) {
    cta = `<button type="button" class="btn btn-primary btn-sm" data-vdd-run="${esc(na.run_id)}">前往审批节点 ↗</button>`;
  } else if (na.action_kind === "human_review" || na.action_kind === "merge_milestone") {
    cta = `<button type="button" class="btn btn-primary btn-sm" data-vdd-jump-view="inbox">进入审查与交付 →</button>`;
  } else if (na.action_kind === "resume_scheduler") {
    cta = `<button type="button" class="btn btn-primary btn-sm" data-vdd-resume="1">恢复自动追踪</button>`;
  }
  return `<div class="vdd-next-banner ${cls}">
    <div class="vdd-next-left">
      <span class="vdd-next-who">${esc(whoBadge)}</span>
      <strong>${esc(na.label || "")}</strong>
      <span class="vdd-next-hint">${esc(na.hint || "")}</span>
    </div>
    ${cta ? `<div class="vdd-next-cta">${cta}</div>` : ""}
  </div>`;
}

function renderStageHeader(stage, rightHtml, subtitle) {
  const sIdx = stage._idx || 1;
  return `<div class="vdd-stage-head">
    <div>
      <div class="vmc-eyebrow">当前展开阶段 · S${sIdx}</div>
      <h3 class="vmc-title">${esc(stage.name || stage.id)}</h3>
      <p class="vmc-sub">${esc(subtitle || "")}</p>
    </div>
    <div class="vmc-head-right">${rightHtml || ""}</div>
  </div>`;
}

function renderRequirementsStage(stage, state) {
  ensureStageExtraData("requirements");
  const slices = dd.extraCache.slices;
  const right = `<button type="button" class="btn btn-sm" data-vdd-open-wish="1">补充研发需求</button>
    <button type="button" class="btn btn-sm" data-vdd-jump-view="council">打开规范文档中心 →</button>`;
  let body = `<div class="vdd-empty-inline">正在读取需求切片…</div>`;
  if (slices !== null) {
    body = slices.length
      ? `<div class="vdd-task-list">${slices
          .map(
            (s) => `<details class="vdd-simple-acc">
              <summary><strong>${esc(s.slice_id)}</strong> · ${esc(s.heading_path)} <span class="vdd-meta-chip">${s.char_count} 字</span></summary>
              <div class="vdd-simple-body"><p class="muted">${esc(s.summary || "")}</p><pre class="vst-code">${esc(s.content || "")}</pre></div>
            </details>`
          )
          .join("")}</div>`
      : `<div class="vdd-empty-stage"><p>尚无需求切片。点击右上角「补充研发需求」或从项目启动台上传规格文档。</p></div>`;
  }
  return `${renderStageHeader(stage, right, `已摄入 ${state.slices ?? state.slices_count ?? 0} 段原始需求与规格切片`)} ${body}`;
}

function renderArchitectureStage(stage, state) {
  ensureStageExtraData(stage.id);
  const council = dd.extraCache.council;
  const plan = dd.extraCache.plan;
  const right = `<button type="button" class="btn btn-sm" data-vdd-jump-view="council">进入架构圆桌与文档中心 →</button>`;
  const msgs = council?.messages || [];
  const adrs = council?.consensus?.adrs || [];
  let body = "";
  if (council === null) {
    body = `<div class="vdd-empty-inline">正在读取架构圆桌纪要与蓝图…</div>`;
  } else {
    const msgsHtml = msgs.length
      ? msgs
          .map(
            (m) => `<details class="vdd-simple-acc">
              <summary>
                <strong>${esc(SPEAKER_CN[m.speaker_role] || m.speaker_name || m.speaker_role)}</strong>
                <span>${esc(m.headline || "")}</span>
                <span class="vdd-meta-chip">${esc(m.harness || "")}</span>
              </summary>
              <div class="vdd-simple-body"><pre class="vst-code">${esc(typeof m.body === "string" ? m.body : JSON.stringify(m.body, null, 2))}</pre></div>
            </details>`
          )
          .join("")
      : `<div class="vdd-empty-stage"><p>尚未发起架构评审圆桌。点击顶栏「开始立项评审」即可自动启动多角色架构讨论与蓝图拆解。</p></div>`;
    const adrHtml = adrs.length
      ? `<div class="vdd-group-head"><strong>已签发 ADR 架构决策</strong><span>${adrs.length} 条</span></div>
         <div class="vdd-task-list">${adrs
           .map(
             (a) => `<details class="vdd-simple-acc">
               <summary><strong>${esc(a.id || "ADR")}</strong> · ${esc(a.title || "")}</summary>
               <div class="vdd-simple-body"><p>${esc(a.decision || "")}</p>${a.rationale ? `<p class="muted">依据：${esc(a.rationale)}</p>` : ""}</div>
             </details>`
           )
           .join("")}</div>`
      : "";
    const summaryLine = plan?.plan?.summary || state.plan_summary || "";
    body = `${summaryLine ? `<div class="vdd-spec-box"><strong>蓝图摘要：</strong>${esc(summaryLine)}</div>` : ""}
      <div class="vdd-task-list">${msgsHtml}</div>${adrHtml}`;
  }
  return `${renderStageHeader(stage, right, `圆桌发言 ${state.council_count || 0} 条 · 研发组 ${(state.squads || []).length} 个 · 里程碑 ${(state.milestones || []).length} 个`)} ${body}`;
}

function renderReviewStage(stage, state, liveProgress) {
  const tasks = state.tasks || [];
  const reviewTasks = tasks.filter((t) => ["AWAITING_HUMAN_REVIEW", "REVIEWING", "APPROVED", "MERGED"].includes(t.status));
  const runByTask = new Map((liveProgress?.active_runs || []).map((r) => [r.task_id, r]));
  const right = `<button type="button" class="btn btn-sm btn-primary" data-vdd-jump-view="inbox">打开审查与交付工作台 →</button>`;
  const body = reviewTasks.length
    ? `<div class="vdd-task-list">${reviewTasks.map((t) => renderTaskRowHtml(t, runByTask.get(t.task_id))).join("")}</div>`
    : `<div class="vdd-empty-stage"><p>当前暂无进入审查阶段的任务。编码与自测通过后，任务将自动流转至此进行独立 AI 审查或人工 Code Review。</p></div>`;
  return `${renderStageHeader(stage, right, `代码审查策略：${state.settings?.review_policy || "off"} · 点击任务可查看审查记录与代码 Diff`)} ${body}`;
}

function renderDeliveryStage(stage, state) {
  const milestones = state.milestones || [];
  const tasks = state.tasks || [];
  const right = `<button type="button" class="btn btn-sm btn-primary" data-vdd-jump-view="inbox">打开交付与合入中心 →</button>`;
  const body = milestones.length
    ? `<div class="vdd-task-list">${milestones
        .map((m) => {
          const mTasks = tasks.filter((t) => t.milestone_id === m.milestone_id);
          const done = mTasks.filter((t) => ["APPROVED", "MERGED"].includes(t.status)).length;
          return `<details class="vdd-simple-acc" ${m.status === "AWAITING_MERGE" ? "open" : ""}>
            <summary>
              <strong>${esc(m.milestone_id)} · ${esc(m.title)}</strong>
              <span class="vdd-meta-chip">${done}/${mTasks.length} 任务通过</span>
              <span class="status-pill ${esc(m.status)}">${esc(m.status)}</span>
            </summary>
            <div class="vdd-simple-body">
              <p>${esc(m.goal || "")}</p>
              ${m.integration_cmd ? `<p class="muted">集成验收命令：<code>${esc(m.integration_cmd)}</code></p>` : ""}
            </div>
          </details>`;
        })
        .join("")}</div>`
    : `<div class="vdd-empty-stage"><p>尚无里程碑交付计划。</p></div>`;
  return `${renderStageHeader(stage, right, `共 ${milestones.length} 个里程碑，已合入 ${milestones.filter((m) => m.status === "MERGED").length} 个`)} ${body}`;
}

function renderCurrent() {
  if (dd.lastState) renderStageDrilldown(dd.lastState, dd.lastLive, dd.opts);
}

export function renderStageDrilldown(state, liveProgress, opts = {}) {
  dd.lastState = state;
  dd.lastLive = liveProgress;
  dd.opts = { ...dd.opts, ...opts };

  const card = $("stage-drilldown-card");
  if (!card) return;
  if (!state || state.workspace_initialized === false) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const stages = (dd.processData?.stages || []).map((s, idx) => ({ ...s, _idx: idx + 1 }));
  if (!dd.selectedStageId || (stages.length && !stages.some((s) => s.id === dd.selectedStageId))) {
    dd.selectedStageId = resolveDefaultStage(state, dd.processData);
  }
  syncStepperSelection();

  const stageObj = stages.find((s) => s.id === dd.selectedStageId) || {
    id: dd.selectedStageId || "implementation",
    name: "AI 辅助编码与实时执行",
    _idx: 4,
  };

  let html = "";
  if (stageObj.id === "requirements") {
    html = renderRequirementsStage(stageObj, state);
  } else if (stageObj.id === "architecture" || stageObj.id === "detailed_design") {
    html = renderArchitectureStage(stageObj, state);
  } else if (stageObj.id === "code_review") {
    html = renderReviewStage(stageObj, state, liveProgress);
  } else if (stageObj.id === "integration_delivery") {
    html = renderDeliveryStage(stageObj, state);
  } else {
    html = renderImplementationStage(stageObj, state, liveProgress);
  }

  card.innerHTML = renderNextActionBanner(liveProgress, state) + html;

  card.querySelector("[data-vdd-approve-start]")?.addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: localStorage.getItem("autodev_actor") || "owner" }) });
      await api("/vernal-api/scheduler/start", { method: "POST" });
      toast("立项蓝图已批准，研发流水线已启动");
      await dd.opts.onRefresh?.();
    } catch (err) {
      toast(`操作失败：${err.message}`);
    } finally {
      b.disabled = false;
    }
  });

  // Bind interactions
  card.querySelectorAll("[data-vdd-task]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tid = btn.dataset.vddTask;
      dd.userToggledTasks.add(tid);
      if (dd.expandedTasks.has(tid)) {
        dd.expandedTasks.delete(tid);
      } else {
        dd.expandedTasks.add(tid);
        fetchTaskStream(tid);
      }
      renderCurrent();
    });
  });

  card.querySelectorAll("[data-vdd-node]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nKey = btn.dataset.vddNode;
      dd.userToggledNodes.add(nKey);
      if (dd.expandedNodes.has(nKey)) dd.expandedNodes.delete(nKey);
      else dd.expandedNodes.add(nKey);
      renderCurrent();
    });
  });

  card.querySelectorAll("[data-vdd-sys]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nKey = btn.dataset.vddSys;
      if (dd.showPlatformNodes.has(nKey)) dd.showPlatformNodes.delete(nKey);
      else dd.showPlatformNodes.add(nKey);
      renderCurrent();
    });
  });

  card.querySelectorAll("[data-vdd-refresh-stream]").forEach((btn) => {
    btn.addEventListener("click", () => {
      fetchTaskStream(btn.dataset.vddRefreshStream, { force: true });
    });
  });

  card.querySelectorAll("[data-vdd-drawer]").forEach((btn) => {
    btn.addEventListener("click", () => {
      dd.opts.onOpenTask?.(btn.dataset.vddDrawer, btn.dataset.vddTab || "diff");
    });
  });

  card.querySelectorAll("[data-vdd-run]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "runs", runId: btn.dataset.vddRun } }));
    });
  });

  card.querySelectorAll("[data-vdd-jump-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      dd.opts.onSwitchView?.(btn.dataset.vddJumpView);
    });
  });

  card.querySelector("[data-vdd-open-wish]")?.addEventListener("click", () => {
    const wf = $("wish-form");
    if (wf) wf.hidden = false;
  });

  card.querySelector("[data-vdd-resume]")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api("/vernal-api/scheduler/start", { method: "POST" });
      toast("已恢复自动调度与实时进度追踪");
      await dd.opts.onRefresh?.();
    } catch (err) {
      toast(`恢复失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  card.querySelectorAll("[data-vdd-restart]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      const b = e.currentTarget;
      const tid = b.dataset.vddRestart;
      if (!window.confirm(`确认重置并重新派发任务 ${tid} 吗？旧的卡滞运行将被安全暂停，调度器会使用最新工作流重新执行。`)) return;
      b.disabled = true;
      try {
        await api(`/vernal-api/tasks/${encodeURIComponent(tid)}/restart`, { method: "POST" });
        dd.streamCache.delete(tid);
        toast(`已重置任务 ${tid}，调度器正在重新派发`);
        await dd.opts.onRefresh?.();
      } catch (err) {
        toast(`重试失败：${err.message}`);
      } finally {
        b.disabled = false;
      }
    });
  });
}
