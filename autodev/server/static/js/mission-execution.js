import { $, api, esc, getActiveProjectId, ROLE_NAMES } from "./api.js";
import { renderStreamItemHtml } from "./stage-drilldown.js";

let context = null;
let generation = 0;
const steps = {intake:"整理需求", constitution:"建立项目约束", checklist:"检查规格质量", plan:"制定技术方案",
  tasks:"拆解实施任务", analyze:"核对交付一致性", clarify:"确认规格决策"};
const statuses = {pending:"等待执行", queued:"排队中", running:"执行中", succeeded:"执行完成",
  failed:"执行失败", cancelled:"已取消", waiting:"等待处理", waiting_approval:"等待审批"};

export function mergeExecutionItems(previous, nodes) {
  const items = new Map(previous.map(item => [item.id, item]));
  for (const node of nodes) for (const item of node.items || []) items.set(item.id, item);
  return [...items.values()].sort((a,b) => a.sequence - b.sequence).slice(-1000);
}

export function mountMissionExecution(feature, refresh) {
  const project = getActiveProjectId();
  if (!context || context.project !== project || context.feature !== feature) {
    context = {project, feature, refresh, runId:"", requestedRun:"", after:0, items:[], generation:++generation};
  }
  context.refresh = refresh;
  context.nextPollAt = 0;
  render();
}

function render() {
  const mount = $("mission-execution");
  if (!mount || !context) return;
  const data = context.data;
  const open = Boolean(context.expanded);
  if (!data?.selected) {
    mount.innerHTML = `<p class="muted">${esc(data?.error || "执行记录会在任务派发后显示")}</p>`;
    return;
  }
  const run = data.selected;
  const recent = context.items.filter(item => item.kind !== "system");
  const last = recent.at(-1) || context.items.at(-1);
  const lastTime = Date.parse(last?.timestamp || "");
  const age = Number.isFinite(lastTime) ? Math.max(0, Math.floor((Date.now() - lastTime)/1000)) : null;
  const activity = age !== null ? `最近活动在 ${age} 秒前` : last ? "已有活动记录，时间未上报" : "等待首条活动记录";
  const elapsed = Number.isFinite(data.timing?.wallMs) ? Math.round(data.timing.wallMs/1000) : null;
  const pool = {local_runner:"本机", public_docker:"公用沙箱"}[run.pool] || "";
  mount.innerHTML = `<div class="mission-question" aria-live="polite">
    <strong>${esc(steps[run.step] || run.step)} · ${esc(statuses[data.run_status] || data.run_status || "状态待同步")}</strong>
    <p>${esc(ROLE_NAMES[run.role] || run.role)} · ${esc(pool)} ${esc(run.executor)} / ${esc(run.model)} · ${run.round ? `修正第 ${Number(run.round)} 轮` : "首次执行"} · ${elapsed === null ? "耗时未上报" : `已用 ${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`}</p>
    <p class="muted">${(data.nodes || []).map(node => `${esc(node.title || node.id)} ${esc(statuses[node.status] || node.status || "状态待同步")}${node.attempt ? `，第 ${Number(node.attempt)} 次执行` : ""}`).join(" · ")}</p>
    <p>${data.error ? esc("执行服务暂时无法同步，保留最近记录。" + data.error) : "执行服务已同步"} · ${activity}</p>
    <label>执行记录 <select id="mission-run-select"><option value="">跟随最新执行</option>${data.runs.map(r => `<option value="${esc(r.run_id)}" ${context.requestedRun===r.run_id?"selected":""}>${esc(steps[r.step] || r.step)} · ${r.round ? `修正第 ${Number(r.round)} 轮` : "首次执行"} · ${esc(r.run_id)}</option>`).join("")}</select></label>
    <div class="vst-feed">${recent.slice(-3).map(renderStreamItemHtml).join("")}</div>
    <details id="mission-execution-details" ${open?"open":""}><summary>查看执行细节与平台记录（${context.items.length} 条${context.items.length===1000?"，最近记录":""}）</summary>
      <div class="vst-feed">${open ? context.items.map(renderStreamItemHtml).join("") : ""}</div></details>
  </div>`;
  $("mission-execution-details")?.addEventListener("toggle", event => {
    if (context.expanded === event.target.open) return;
    context.expanded = event.target.open;
    render();
  });
  $("mission-run-select")?.addEventListener("change", event => {
    context.requestedRun = event.target.value;
    context.runId = ""; context.after = 0; context.items = [];
    context.generation = ++generation;
    context.loading = false;
    pollMissionExecution();
  });
}

export async function pollMissionExecution() {
  const current = context;
  if (!current || current.loading || current.project !== getActiveProjectId() || !$("mission-execution")) return;
  if (Date.now() < (current.nextPollAt || 0)) return;
  const ticket = current.generation;
  const active = () => context === current && ticket === current.generation && current.project === getActiveProjectId();
  current.loading = true;
  try {
    const base = `/vernal-api/missions/${encodeURIComponent(current.feature)}/stream`;
    const query = new URLSearchParams({after:String(current.after)});
    if (current.requestedRun) query.set("run_id", current.requestedRun);
    // A following poll may select a newer Run. Its event cursor must start at zero.
    if (current.runId) query.set("cursor_run_id", current.runId);
    const data = await api(`${base}?${query}`);
    if (!active()) return;
    const changed = current.runId && current.runId !== data.selected?.run_id;
    const statusChanged = current.data?.run_status !== data.run_status;
    if (changed) current.items = [];
    current.runId = data.selected?.run_id || "";
    current.after = data.after || 0;
    current.items = mergeExecutionItems(current.items, data.nodes || []);
    current.data = data;
    current.nextPollAt = Date.now() + (["succeeded", "failed", "cancelled"].includes(data.run_status) && !data.has_more ? 15000 : 4000);
    render();
    if (changed || statusChanged) await current.refresh();
  } catch (error) {
    if (active()) {
      current.data = {...current.data, error:error.message};
      render();
    }
  } finally {
    if (active()) current.loading = false;
  }
}
