import {api, toast} from "./js/api.js";

const titles = {overview:"架构总控", council:"架构圆桌", squads:"研发组与蓝图", runtime:"底座与收件箱"};
const compactTitles = {overview:"总控", council:"圆桌", squads:"蓝图"};
const icons = {
  overview:"M3 3h6v6H3z M15 15h6v6h-6z M6 9v9h9",
  council:"M8 6a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M3 21v-2a8 8 0 0 1 16 0v2",
  squads:"M3 4h7v7H3z M14 4h7v7h-7z M3 15h7v6H3z M14 15h7v6h-7z",
};

function element(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text) el.textContent = text;
  return el;
}

async function mount(rail, nav, main) {
  const response = await fetch("/vernal-static/index.html");
  if (!response.ok) throw Error("无法载入 Vernal 工作台");
  const template = new DOMParser().parseFromString(await response.text(), "text/html");
  const stage = element("section");
  stage.id = "vernalStage";
  const toolbar = element("div", "vernal-toolbar");
  const tabs = element("nav", "vernal-subtabs");
  let switchView;
  let reloadProjectContext;
  let pendingView;

  function tab(key, navigation) {
    const button = element("button", navigation ? "rail-btn vernal-nav" : "v-tab-btn");
    button.dataset.vernalTab = key;
    button.title = titles[key];
    button.setAttribute("aria-label", titles[key]);
    if (navigation) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "19");
      svg.setAttribute("height", "19");
      const shape = document.createElementNS(svg.namespaceURI, "path");
      shape.setAttribute("d", icons[key]);
      shape.setAttribute("stroke", "currentColor");
      shape.setAttribute("stroke-width", "1.7");
      shape.setAttribute("stroke-linecap", "round");
      shape.setAttribute("stroke-linejoin", "round");
      shape.setAttribute("fill", "none");
      svg.append(shape);
      button.append(svg);
      button.append(element("span", "vernal-short-label", compactTitles[key]));
    }
    button.append(element("span", navigation ? "vernal-full-label" : "", titles[key]));
    if (navigation && key !== "squads") {
      const badge = element("small", "vernal-rail-badge");
      badge.id = key === "overview" ? "vernal-task-badge" : "vernal-council-badge";
      button.append(badge);
    }
    button.addEventListener("click", () => activate(key));
    return button;
  }

  for (const key of Object.keys(titles)) tabs.append(tab(key, false));
  toolbar.append(tabs, template.querySelector(".topbar-left"), template.querySelector(".topbar-right"));
  const panels = element("div", "vernal-body");
  for (const panel of template.querySelectorAll(".content > section")) {
    panel.classList.remove("view");
    panel.classList.add("v-subview");
    panels.append(panel);
  }
  stage.append(
    toolbar,
    template.querySelector("#agent-config-form"),
    template.querySelector("#wish-form"),
    template.querySelector("#goal-bar"),
    panels,
    template.querySelector(".sidebar-footer"),
    template.querySelector("#drawer"),
    template.querySelector("#toast")
  );
  main.append(stage);

  const brand = element("button", "vernal-rail-brand");
  brand.title = "Vernal · 架构总控";
  const logo = document.querySelector("#bootLoader svg, #platformRail svg");
  if (logo) brand.append(logo.cloneNode(true));
  brand.append(element("strong", "vernal-brand-name", "Vernal"));
  rail.prepend(brand);
  brand.addEventListener("click", () => activate("overview"));

  nav.prepend(tab("overview", true), tab("council", true), tab("squads", true), element("hr", "vernal-rail-sep"));

  const pill = element("button", "vernal-topbar-pill");
  pill.id = "vernalTopbarPill";
  const dot = element("i", "vdot warn");
  const pillText = element("span", "", "检查执行底座");
  pill.append(dot, pillText);
  document.querySelector(".topbar .top-actions")?.before(pill);

  const context = document.querySelector(".topbar .page-context span");
  if (context) context.textContent = "／ Vernal · 多智能体架构研发平台";

  document.addEventListener("vernal-state", (e) => {
    const tb = document.getElementById("vernal-task-badge");
    const cb = document.getElementById("vernal-council-badge");
    if (tb) tb.textContent = e.detail.tasks.length || "";
    if (cb) cb.textContent = e.detail.council_count || "";
  });

  ({switchView, reloadProjectContext} = await import("./js/main.js"));

  function activate(key) {
    if (!switchView) {
      pendingView = key;
      return;
    }
    document.body.classList.remove("management-mode");
    document.body.classList.add("vernal-mode");
    nav.querySelectorAll(".nav[data-view].active").forEach((el) => el.classList.remove("active"));
    const title = document.getElementById("pageTitle");
    if (title) title.textContent = titles[key];
    switchView(key);
  }

  document.addEventListener("vernal-jump-native", (e) => {
    const targetView = e.detail?.view || "issues";
    document.body.classList.remove("vernal-mode");
    nav.querySelectorAll(".vernal-nav.active").forEach((el) => el.classList.remove("active"));
    const nativeBtn = document.querySelector(`.nav[data-view="${targetView}"]`);
    if (nativeBtn) nativeBtn.click();
  });

  document.addEventListener(
    "click",
    (e) => {
      if (
        e.target.closest(".nav[data-view], [data-jump], #railSettingsEntry, .management-back") &&
        !e.target.closest("[data-vernal-tab], #vernalStage")
      ) {
        document.body.classList.remove("vernal-mode");
        nav.querySelectorAll(".vernal-nav.active").forEach((el) => el.classList.remove("active"));
      }
    },
    true
  );

  // 跟随顶栏全局 #projectSwitcher 自动切换 Vernal 架构工作区与标准 SOP
  const switcher = document.getElementById("projectSwitcher");
  async function syncSelectedProject(projectId, projectName, notify = false) {
    if (!projectId) return;
    try {
      const res = await api("/vernal-api/project/select", {
        method: "POST",
        body: JSON.stringify({project_id: projectId, project_name: projectName || projectId}),
      });
      if (res.switched && reloadProjectContext) {
        await reloadProjectContext();
        if (notify) toast(`已同步切换至项目「${projectName || projectId}」工作区`);
      }
    } catch (err) {
      if (notify) toast(err.message);
    }
  }
  switcher?.addEventListener("change", () => {
    const opt = switcher.selectedOptions?.[0];
    syncSelectedProject(switcher.value, opt?.textContent?.trim() || switcher.value, true);
  });

  if (pendingView) activate(pendingView);
  else if (!location.hash || location.hash === "#/" || location.hash.startsWith("#/vernal")) activate("overview");

  let bound = false;
  async function bind(silent) {
    try {
      const status = await api("/vernal-api/runtime/status");
      bound = Boolean(status.loopforge.authenticated && status.loopforge.project_id && status.loopforge.workflow_id);
      if (status.loopforge.reachable && !bound) {
        const result = await api("/vernal-api/runtime/bind", {method: "POST"});
        bound = Boolean(result.bound);
        if (bound && reloadProjectContext) await reloadProjectContext();
      }
    } catch (err) {
      if (!silent) toast(err.message);
    }
    dot.classList.toggle("warn", !bound);
    pillText.textContent = bound ? "Vernal 架构中枢 · 已连接" : "点击绑定登录态";
    if (bound && switcher?.value) {
      const opt = switcher.selectedOptions?.[0];
      await syncSelectedProject(switcher.value, opt?.textContent?.trim() || switcher.value, false);
    }
  }

  pill.addEventListener("click", () => (bound ? activate("overview") : bind(false)));
  await bind(true);
}

function ready() {
  const rail = document.querySelector("#platformRail");
  const nav = rail?.querySelector(".rail-nav");
  const main = document.querySelector(".shell-main > main");
  if (!nav || !main) return false;
  mount(rail, nav, main).catch((err) => console.error("Vernal", err));
  return true;
}

if (!ready()) {
  const observer = new MutationObserver(() => {
    if (ready()) observer.disconnect();
  });
  observer.observe(document.body, {subtree: true, childList: true});
}
