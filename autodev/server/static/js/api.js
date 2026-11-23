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

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
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
