export const root = () => document.getElementById("vernalStage") || document;
export const $ = id => root().querySelector("#" + CSS.escape(id));

export const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

export const ROLE_NAMES = {
  pm: "产品经理 · PM",
  architect: "契约架构师 · Architect",
  qa_lead: "QA 质量总监 · QA Lead",
  director: "研发总监 · Director",
  planner: "架构规划 · Planner",
  bootstrap: "基线搭建 · Bootstrap",
  contract: "契约架构 · Contract",
  coder: "模块开发 · Coder",
  integrator: "网关装配 · Integrator",
  tester: "系统测试 · Tester",
  reviewer: "独立审查 · Reviewer",
  human: "人类负责人",
};

export function actorName() {
  return $("actor-input")?.value.trim() || "owner";
}

export function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

let _activeProjectId = "";

export function setActiveProjectId(pid) {
  _activeProjectId = String(pid || "").trim();
  try {
    if (_activeProjectId) sessionStorage.setItem("vernal_project_id", _activeProjectId);
    else sessionStorage.removeItem("vernal_project_id");
  } catch {}
}

export function getActiveProjectId() {
  const sw = document.getElementById("projectSwitcher");
  if (sw?.value) return String(sw.value).trim();
  if (_activeProjectId) return _activeProjectId;
  try {
    return sessionStorage.getItem("vernal_project_id") || "";
  } catch {
    return "";
  }
}

export function projectQuerySuffix(sep = "?") {
  const pid = getActiveProjectId();
  return pid ? `${sep}project=${encodeURIComponent(pid)}` : "";
}

export async function api(path, opts = {}) {
  const pid = getActiveProjectId();
  const baseHeaders = { "Content-Type": "application/json" };
  if (pid) baseHeaders["X-Vernal-Project"] = pid;
  const res = await fetch(path, {
    ...opts,
    headers: { ...baseHeaders, ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

export function fmtElapsed(isoStart) {
  if (!isoStart) return "";
  const ms = Date.now() - Date.parse(isoStart);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const sec = Math.floor(ms / 1000);
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}
