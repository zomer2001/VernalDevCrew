import { $, actorName, api, fmtElapsed, toast, root } from "./api.js";
import { bindCouncilActions, refreshCouncil } from "./council.js";
import { renderDAG } from "./dag.js";
import { handleLiveDrawerEvent, initDrawer, openReportModal, openTaskDrawer } from "./drawer.js";
import { renderDocsAndPlan, renderGoalBar, renderRoster, renderSquadColumns } from "./roster.js";
import { refreshInbox, refreshRuntimePanel } from "./runtime.js";

import { createTimeline } from "./timeline.js";

let state = null;
let slices = [];
let planDoc = null;
let lastEventId = 0;
let activeView = "overview";
const timeline = createTimeline(openTaskDrawer);

$("actor-input").value = localStorage.getItem("autodev_actor") || "owner";
$("actor-input").addEventListener("input", () => localStorage.setItem("autodev_actor", actorName()));

export function switchView(view) {
  activeView = view;
  root().querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  root().querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  const stage = document.getElementById("vernalStage");
  if (stage) {
    const visible = {squads: ["squads", "docs"], runtime: ["runtime", "inbox"]}[view] || [view];
    for (const panel of stage.querySelectorAll(".v-subview")) panel.classList.toggle("active", visible.includes(panel.id.slice(5)));
    for (const tab of document.querySelectorAll("[data-vernal-tab]")) tab.classList.toggle("active", tab.dataset.vernalTab === view);
  }
  if (!state) return;
  if (view === "council") refreshCouncil(openTaskDrawer);
  if (view === "inbox" || (stage && view === "runtime")) refreshInbox(state, refresh, openTaskDrawer, openReportModal);
  if (view === "runtime") refreshRuntimePanel(refresh);
  if (view === "docs" || (stage && view === "squads")) refreshDocs();
}

async function refreshDocs() {
  const [s, p] = await Promise.all([api("/vernal-api/slices"), api("/vernal-api/plan")]);
  slices = s;
  planDoc = p;
  renderDocsAndPlan(state, planDoc, slices, openTaskDrawer);
}

async function refresh() {
  state = await api("/vernal-api/state");
  renderAll();
  document.dispatchEvent(new CustomEvent("vernal-state", {detail:state}));
}

function renderAll() {
  if (!state) return;
  $("project-name").textContent = `${state.name}`;
  const st = $("project-status");
  st.textContent = state.planning ? "PLANNING" : state.status;
  st.className = `status-pill ${state.planning ? "RUNNING" : state.status}`;

  $("side-backend").textContent = state.settings?.runtime_backend || "local";
  $("side-backend").className = `tag ${state.settings?.runtime_backend === "loopforge" ? "ok" : ""}`;

  const rt = state.runtimes || {};
  const renderPill = (elId, info, activeModel = "") => {
    const el = $(elId);
    if (!el || !info) return;
    if (!info.installed) {
      el.textContent = "未装";
      el.className = "tag";
    } else if (info.ready) {
      el.textContent = activeModel || "就绪";
      el.className = "tag ok";
    } else {
      el.textContent = "待配Key";
      el.className = "tag warn";
    }
    el.title = `${info.note || ""}${info.bin ? " · " + info.bin : ""}`;
  };
  renderPill("codex-pill", rt.codex, state.settings?.effective_harness === "codex" ? (state.settings?.model || "就绪") : "就绪");
  renderPill("codebuddy-pill", rt.codebuddy);
  renderPill("opencode-pill", rt.opencode);
  renderPill("dsh-pill", rt.dsh);

  const badgesEl = $("agent-runtime-badges");
  if (badgesEl) {
    const entries = [
      ["codex", "Codex"],
      ["codebuddy", "CodeBuddy"],
      ["opencode", "OpenCode"],
      ["dsh", "DeepSeek Harness"],
      ["claude-code", "Claude Code"],
    ];
    badgesEl.innerHTML = `<span class="muted" style="font-size:12px">本机扫描结果：</span>` + entries.map(([k, label]) => {
      const info = rt[k] || {};
      const cls = !info.installed ? "" : info.ready ? "ok" : "warn";
      const statusTxt = !info.installed ? "未安装" : info.ready ? "已就绪" : "待配凭据";
      const tip = `${info.note || ""}${info.bin ? " (" + info.bin + ")" : ""}`.replace(/"/g, "&quot;");
      return `<span class="tag ${cls}" title="${tip}">${label} · ${statusTxt}</span>`;
    }).join("");
  }
  const effHint = $("cfg-effective-hint");
  if (effHint && state.settings) {
    effHint.textContent = `当前生效编队：总监=${state.settings.effective_harness || "-"} · 开发=${state.settings.effective_coder_harness || "-"} · 审查=${state.settings.effective_qa_harness || "-"}`;
  }

  const u = state.usage_total || { input: 0, cached: 0, output: 0, reasoning: 0 };
  $("usage-pill").textContent = `Tokens ${(u.input + u.output).toLocaleString()} · 缓存 ${u.cached.toLocaleString()}`;

  $("btn-approve").classList.toggle("hidden", state.status !== "PLANNED");
  $("btn-start").classList.toggle(
    "hidden",
    state.scheduler_running || ["CREATED", "INGESTED", "SPECIFIED", "PLANNED"].includes(state.status)
  );
  $("btn-stop").classList.toggle("hidden", !state.scheduler_running);

  const modeSel = $("plan-mode");
  if (modeSel && !modeSel._touched) modeSel.value = state.settings?.planning_mode || "goal";
  $("plan-summary").textContent = state.plan_summary || "尚未生成规划";

  // 左侧导航计数角标
  const cc = $("nav-council-cnt");
  if (cc) {
    cc.textContent = String(state.council_count || 0);
    cc.classList.toggle("hidden", !(state.council_count > 0));
  }
  const tc = $("nav-task-cnt");
  if (tc) {
    tc.textContent = String((state.tasks || []).length);
    tc.classList.toggle("hidden", !(state.tasks || []).length);
  }
  const fused = (state.tasks || []).filter((t) => t.status === "FUSED").length;
  const awMs = (state.milestones || []).filter((m) => m.status === "AWAITING_MERGE").length;
  const localPending = fused + awMs + (state.status === "PLANNED" ? 1 : 0);
  const ic = $("nav-inbox-cnt");
  if (ic && activeView !== "inbox") {
    ic.textContent = String(localPending);
    ic.classList.toggle("hidden", localPending === 0);
  }

  if ($("collaboration-mode")) {
    $("collaboration-mode").value = state.settings.collaboration_mode || "git";
    $("collaboration-mode").disabled = state.planning || state.scheduler_running || state.milestones.length > 0;
  }
  renderGoalBar(state);
  renderDAG(state, openTaskDrawer);
  renderRoster(state, openTaskDrawer);
  renderSquadColumns(state, openTaskDrawer);
  if (activeView === "docs") renderDocsAndPlan(state, planDoc, slices, openTaskDrawer);
  if (activeView === "inbox") refreshInbox(state, refresh, openTaskDrawer, openReportModal);
}

// 导航与按钮事件
root().querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

$("btn-council-trace")?.addEventListener("click", () => openTaskDrawer("_council_director", "trace"));
$("btn-planner-trace")?.addEventListener("click", () => openTaskDrawer("_planner", "trace"));
$("btn-refresh-inbox")?.addEventListener("click", () => refreshInbox(state, refresh, openTaskDrawer, openReportModal));
$("plan-mode")?.addEventListener("change", (e) => {
  e.target._touched = true;
});

$("btn-ingest")?.addEventListener("click", async () => {
  try {
    const r = await api("/vernal-api/ingest", { method: "POST" });
    toast(`已摄入 ${r.slices} 个资料切片`);
    await refresh();
    if (activeView === "docs") refreshDocs();
  } catch (err) {
    toast(`摄入失败：${err.message}`);
  }
});

async function triggerPlan(extra = "") {
  const mode = $("plan-mode")?.value || state?.settings?.planning_mode || "goal";
  try {
    await api("/vernal-api/plan", { method: "POST", body: JSON.stringify({ extra_instructions: extra, mode }) });
    toast(mode === "goal" ? "已启动多智能体评审圆桌与立项蓝图，请在圆桌页或工位席实时观看" : "已启动后台规划");
    refresh();
    if (mode === "goal") switchView("council");
  } catch (err) {
    toast(`启动失败：${err.message}`);
  }
}
$("btn-plan")?.addEventListener("click", () => triggerPlan(""));
$("btn-plan-extra")?.addEventListener("click", () => triggerPlan($("plan-extra")?.value || ""));

$("btn-approve")?.addEventListener("click", async () => {
  try {
    await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: actorName() }) });
    toast("立项蓝图已批准");
    refresh();
  } catch (err) {
    toast(`批准失败：${err.message}`);
  }
});

$("btn-start")?.addEventListener("click", async () => {
  try {
    await api("/vernal-api/scheduler/start", { method: "POST" });
    toast("Goal 调度器已启动");
    refresh();
  } catch (err) {
    toast(`启动失败：${err.message}`);
  }
});

$("btn-stop")?.addEventListener("click", async () => {
  await api("/vernal-api/scheduler/stop", { method: "POST" });
  toast("已发送停止信号");
});

setInterval(() => {
  root().querySelectorAll(".elapsed[data-start]").forEach((el) => {
    el.textContent = fmtElapsed(el.dataset.start);
  });
  if (state && (state.tasks || []).some((t) => ["IN_PROGRESS", "VERIFYING", "REVIEWING"].includes(t.status))) {
    renderDAG(state, openTaskDrawer);
  }
}, 1000);

let activeEventSource = null;

function connectSSE() {
  if (activeEventSource) activeEventSource.close();
  const es = new EventSource(`/vernal-api/events?after=${lastEventId}`);
  activeEventSource = es;
  es.onopen = () => {
    timeline.setConnected(true);
    $("sse-indicator").classList.add("live");
    $("sse-text").textContent = "已连接";
  };
  es.onerror = () => {
    timeline.setConnected(false);
    $("sse-indicator").classList.remove("live");
    $("sse-text").textContent = "重连中";
  };
  es.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.event_id > lastEventId) {
        lastEventId = ev.event_id;
        timeline.append(ev);
        handleLiveDrawerEvent(ev);
        if (ev.task_id && state?.tasks) {
          const t = state.tasks.find((x) => x.task_id === ev.task_id);
          if (t) {
            t.latest_activity = { event_type: ev.event_type, actor: ev.actor, message: ev.message, created_at: ev.created_at };
            renderSquadColumns(state, openTaskDrawer);
          }
        }
        if (ev.task_id && state?.worker_slots) {
          const slot = state.worker_slots.find((s) => s.task_id === ev.task_id && s.status === "WORKING");
          if (slot) {
            slot.current_action = ev.message;
            renderRoster(state, openTaskDrawer);
          }
        }
        if (ev.event_type.startsWith("council.") && activeView === "council") {
          refreshCouncil(openTaskDrawer);
        }
        if (!ev.event_type.startsWith("agent.")) refresh();
      }
    } catch {}
  };
}

export async function reloadProjectContext() {
  await refresh();
  lastEventId = state.recent_events?.at(-1)?.event_id || 0;
  timeline.start(state);
  connectSSE();
  if (activeView === "council") await refreshCouncil(openTaskDrawer);
  if (activeView === "docs" || activeView === "squads") await refreshDocs();
  if (activeView === "runtime") {
    await refreshInbox(state, refresh, openTaskDrawer, openReportModal);
    await refreshRuntimePanel(refresh);
  }
}

initDrawer();
bindCouncilActions(refresh, openTaskDrawer);
refresh().then(() => {
  lastEventId = state.recent_events.at(-1)?.event_id || 0;
  timeline.start(state);
  connectSSE();
  switchView(activeView);
}).catch(err => toast(err.message));

function populateAgentConfigForm() {
  if (!state?.settings || !$("agent-config-form")) return;
  const s = state.settings;
  $("cfg-harness").value = s.harness || "auto";
  $("cfg-model").value = s.model || "";
  $("cfg-concurrency").value = String(s.max_concurrency || 2);
  $("cfg-director-harness").value = s.director_harness || "";
  $("cfg-coder-harness").value = s.raw_coder_harness ?? "";
  $("cfg-qa-harness").value = s.qa_harness || "";
  $("cfg-director-model").value = s.director_model || "";
  $("cfg-coder-model").value = s.coder_model || "";
  $("cfg-qa-model").value = s.qa_model || "";
}

$("btn-agent-toggle")?.addEventListener("click", () => {
  const form = $("agent-config-form");
  if (!form) return;
  const opening = form.hidden;
  if (opening) {
    populateAgentConfigForm();
    if ($("wish-form")) $("wish-form").hidden = true;
  }
  form.hidden = !opening;
});
$("btn-agent-close")?.addEventListener("click", () => {
  if ($("agent-config-form")) $("agent-config-form").hidden = true;
});
$("btn-agent-rescan")?.addEventListener("click", async () => {
  try {
    const res = await api("/vernal-api/agent/rescan", { method: "POST" });
    toast(`扫描完成，推荐执行器：${res.preferred_harness}`);
    await refresh();
  } catch (err) {
    toast(`扫描失败：${err.message}`);
  }
});
$("agent-config-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const btn = $("btn-agent-save");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/vernal-api/agent/config", {
      method: "POST",
      body: JSON.stringify({
        harness: $("cfg-harness").value,
        model: $("cfg-model").value,
        max_concurrency: Number($("cfg-concurrency").value || 2),
        director_harness: $("cfg-director-harness").value,
        coder_harness: $("cfg-coder-harness").value,
        qa_harness: $("cfg-qa-harness").value,
        director_model: $("cfg-director-model").value,
        coder_model: $("cfg-coder-model").value,
        qa_model: $("cfg-qa-model").value,
      }),
    });
    toast(
      `已保存编队设置（总监=${res.effective.director_harness}, 开发=${res.effective.coder_harness}, 审查=${res.effective.qa_harness}）`
      + (res.synced_loopforge ? " · 已同步到底层 LoopForge 角色" : "")
    );
    await refresh();
    if (activeView === "runtime") await refreshRuntimePanel(refresh);
    $("agent-config-form").hidden = true;
  } catch (err) {
    toast(`保存失败：${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("btn-wish-toggle")?.addEventListener("click", () => {
  const wf = $("wish-form");
  if (!wf) return;
  const opening = wf.hidden;
  if (opening && $("agent-config-form")) $("agent-config-form").hidden = true;
  wf.hidden = !opening;
});
$("wish-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = $("btn-wish-submit");
  submit.disabled = true;
  try {
    const result = await api("/vernal-api/wish", {method:"POST", body:JSON.stringify({
      title:$("wish-title").value, content:$("wish-content").value,
    })});
    toast("已保存需求并完成切片，共 " + result.slices + " 段");
    await refresh();
    $("wish-form").hidden = true;
  } catch (err) { toast(err.message); } finally { submit.disabled = false; }
});
$("collaboration-mode")?.addEventListener("change", async event => {
  const mode = event.target.value;
  try {
    await api("/vernal-api/runtime/config", {method:"POST", body:JSON.stringify({collaboration_mode:mode})});
    await refresh();
  } catch (err) { event.target.value = state.settings.collaboration_mode; toast(err.message); }
});
