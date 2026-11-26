import { api, setActiveProjectId, toast } from "./js/api.js";

const titles = {
  docs: "文档与详设",
  overview: "Vernal 工作台",
  council: "蓝图与圆桌",
  squads: "研发看板",
  inbox: "里程碑与交付",
  runtime: "系统设置",
};

const compactTitles = {
  docs: "文档",
  overview: "工作台",
  council: "架构",
  squads: "看板",
  inbox: "审查",
  runtime: "设置",
};

const icons = {
  docs: "M4 4h16v16H4z M8 9h8 M8 14h8",
  overview: "M3.5 10l7.3-6a1.9 1.9 0 0 1 2.4 0l7.3 6 M5.5 8.4v10.1A1.5 1.5 0 0 0 7 20h3v-5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v5h3a1.5 1.5 0 0 0 1.5-1.5V8.4",
  council: "M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z M12 12l8-4.5 M12 12v9 M12 12L4 7.5",
  squads: "M3 4h7v7H3z M14 4h7v7h-7z M3 14h7v6H3z M14 14h7v6h-7z",
  inbox: "M4 5h16v14H4z M4 13h5l2 3h2l2-3h5",
  runtime: "M12 8.5a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7z M4 12h2 M18 12h2 M12 4v2 M12 18v2 M6.3 6.3l1.4 1.4 M16.3 16.3l1.4 1.4 M17.7 6.3l-1.4 1.4 M7.7 16.3l-1.4 1.4",
};

function element(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text) el.textContent = text;
  return el;
}

async function mount(rail, nav, main) {
  const assetVer = new URL(import.meta.url).searchParams.get("v") || String(Date.now());
  const response = await fetch(`/vernal-static/index.html?v=${encodeURIComponent(assetVer)}`, { cache: "no-store" });
  if (!response.ok) throw Error("无法载入 Vernal 工作台");
  const template = new DOMParser().parseFromString(await response.text(), "text/html");
  const stage = element("section");
  stage.id = "vernalStage";
  stage.classList.add("view");
  const toolbar = element("div", "vernal-toolbar");
  const tabs = element("nav", "vernal-subtabs");
  let switchView;
  let reloadProjectContext;
  let pendingView;

  function tab(key, navigation) {
    const button = element("button", navigation ? "rail-btn vernal-nav" : "v-tab-btn");
    button.type = "button";
    button.dataset.vernalTab = key;
    button.title = titles[key];
    button.setAttribute("aria-label", titles[key]);
    if (navigation) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "18");
      svg.setAttribute("height", "18");
      const shape = document.createElementNS(svg.namespaceURI, "path");
      shape.setAttribute("d", icons[key]);
      shape.setAttribute("stroke", "currentColor");
      shape.setAttribute("stroke-width", "1.75");
      shape.setAttribute("stroke-linecap", "round");
      shape.setAttribute("stroke-linejoin", "round");
      shape.setAttribute("fill", "none");
      svg.append(shape);
      button.append(svg);
      button.append(element("span", "vernal-short-label", compactTitles[key]));
    }
    button.append(element("span", navigation ? "vernal-full-label" : "", titles[key]));

    if (navigation) {
      if (key === "squads") {
        const badge = element("small", "vernal-rail-badge");
        badge.id = "vernal-task-badge";
        button.append(badge);
      } else if (key === "council") {
        const badge = element("small", "vernal-rail-badge");
        badge.id = "vernal-council-badge";
        button.append(badge);
      } else if (key === "inbox") {
        const badge = element("small", "vernal-rail-badge alert hidden");
        badge.id = "vernal-inbox-badge";
        button.append(badge);
      }
    } else if (key === "inbox") {
      const badge = element("small", "vernal-tab-badge hidden");
      badge.id = "vernal-subtab-inbox-badge";
      button.append(badge);
    }

    button.addEventListener("click", () => activate(key));
    return button;
  }

  for (const key of Object.keys(titles).filter(k => k !== "docs")) tabs.append(tab(key, false));
  toolbar.append(tabs, template.querySelector(".topbar-left"), template.querySelector(".topbar-right"));

  const panels = element("div", "vernal-body");
  const guide = element("div", "vernal-body", "此原生项目尚未绑定研发工作区。");
  const setup = element("button", "button", "接入项目目录");
  setup.type = "button";
  setup.addEventListener("click", () => document.dispatchEvent(new CustomEvent("vernal-setup-workspace", { detail: switcher?.value })));
  guide.append(setup);
  guide.hidden = true;
  let boundProject = "";

  panels.append(template.querySelector("#wish-form"), template.querySelector("#goal-bar"));
  for (const panel of template.querySelectorAll(".content > section")) {
    panel.classList.remove("view");
    panel.classList.add("v-subview");
    panels.append(panel);
  }
  // 保留隐藏状态指示器节点以兼容 main.js 的状态同步，不在页面底部裸露挂出侧栏页脚
  const hiddenFoot = template.querySelector(".sidebar-footer");
  if (hiddenFoot) hiddenFoot.hidden = true;

  stage.append(
    toolbar,
    template.querySelector("#project-launcher"),
    template.querySelector("#steering-modal"),
    panels,
    guide,
    hiddenFoot,
    template.querySelector("#drawer"),
    template.querySelector("#toast")
  );
  main.append(stage);

  const brand = element("div", "vernal-rail-brand");
  brand.setAttribute("aria-label", "Vernal · 规范驱动软件研发流水线");
  const logo = document.querySelector("#bootLoader svg");
  if (logo) {
    brand.append(logo.cloneNode(true));
  } else {
    const mark = element("span", "vernal-brand-mark", "V");
    brand.append(mark);
  }
  brand.append(element("strong", "vernal-brand-name", "Vernal"));
  rail.prepend(brand);

  // 第 1 位（小房子图标）：Vernal 工作台；原聊天页（collaboration）顺延至第 2 位改为气泡图标「聊天助手」
  const workspaceTab = tab("overview", true);
  workspaceTab.dataset.tooltip = "Vernal 工作台";
  nav.prepend(workspaceTab);

  const chatNav = nav.querySelector('.nav[data-view="collaboration"], .nav[data-view="home"]');
  if (chatNav) {
    chatNav.dataset.tooltip = "聊天助手";
    chatNav.title = "聊天助手 · 需求孵化与项目问答";
    chatNav.setAttribute("aria-label", "聊天助手");
    const chatSvg = chatNav.querySelector("svg");
    if (chatSvg) {
      chatSvg.setAttribute("viewBox", "0 0 24 24");
      chatSvg.innerHTML =
        '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>';
    }
    const label = chatNav.querySelector(".rail-label");
    if (label) label.textContent = "聊天助手";
  }

  nav.append(tab("council", true), tab("docs", true), tab("inbox", true));

  // 为左侧所有原生图标统一补齐收起态短名称与展开态完整名称
  const shortLabelMap = {
    collaboration: ["助手", "聊天助手"],
    observability: ["统计", "统计"],
    issues: ["需求", "需求列表"],
    approvals: ["待办", "待我确认"],
    settings: ["设置", "设置"],
  };
  rail.querySelectorAll(".rail-btn:not(.vernal-nav)").forEach((btn) => {
    const viewKey = btn.dataset.view || (btn.id === "railSidebarToggle" ? "toggle" : "");
    const existing = btn.querySelector(".rail-label");
    const pair = shortLabelMap[viewKey] || (viewKey === "toggle" ? ["展开", existing?.textContent?.trim() || "展开导航"] : null);
    if (!pair) return;
    if (existing) existing.textContent = pair[1];
    if (!btn.querySelector(".vernal-short-label")) {
      const shortSpan = element("span", "vernal-short-label", pair[0]);
      if (existing) existing.before(shortSpan);
      else btn.append(shortSpan);
    }
  });
  attachObjectTabs(activate);

  const pill = element("button", "vernal-topbar-pill");
  pill.type = "button";
  pill.id = "vernalTopbarPill";
  const dot = element("i", "vdot warn");
  const pillText = element("span", "", "连接执行底座…");
  pill.append(dot, pillText);
  document.querySelector(".topbar .top-actions")?.before(pill);

  const context = document.querySelector(".topbar .page-context span");
  if (context) context.textContent = "／ Vernal · 规范驱动研发流水线";

  document.addEventListener("vernal-state", (e) => {
    const tb = document.getElementById("vernal-task-badge");
    const cb = document.getElementById("vernal-council-badge");
    if (tb) tb.textContent = (e.detail.tasks || []).length || "";
    if (cb) cb.textContent = e.detail.council_count || "";
  });

  if (document.getElementById("projectSwitcher")?.value) setActiveProjectId(document.getElementById("projectSwitcher").value);
  ({ switchView, reloadProjectContext } = await import(`./js/main.js?v=${encodeURIComponent(assetVer)}`));

  function activate(key) {
    if (!switchView) {
      pendingView = key;
      return;
    }
    const normalized = key === "docs" ? "council" : key;
    main.querySelectorAll(":scope > .view.active").forEach(view => view.classList.remove("active"));
    stage.classList.add("active");
    nav.querySelectorAll(".nav[data-view].active").forEach((el) => el.classList.remove("active"));
    const title = document.getElementById("pageTitle");
    if (title) title.textContent = titles[normalized] || titles.overview;
    switchView(normalized);
    if (key === "docs") stage.querySelector("#docs-center-mount")?.scrollIntoView();
  }

  document.addEventListener("vernal-jump-native", (e) => {
    const targetView = e.detail?.view || "issues";
    const runId = e.detail?.runId || "";
    const issueId = e.detail?.issueId || "";
    stage.classList.remove("active");
    nav.querySelectorAll(".vernal-nav.active").forEach((el) => el.classList.remove("active"));
    if (runId) {
      if (typeof window.openRunFromId === "function") {
        window.openRunFromId(runId, { forceRuns: true });
        return;
      }
      location.hash = `#/runs/${encodeURIComponent(runId)}`;
      return;
    }
    if (issueId && typeof window.openIssueDetail === "function") {
      const nativeBtn = document.querySelector(`.nav[data-view="issues"]`);
      if (nativeBtn) nativeBtn.click();
      window.openIssueDetail(issueId);
      return;
    }
    const nativeBtn = document.querySelector(`.nav[data-view="${targetView}"]`);
    if (nativeBtn) nativeBtn.click();
  });

  let userChoseNative = false;
  document.addEventListener(
    "click",
    (e) => {
      if (
        e.target.closest(".nav[data-view], [data-jump], #railSettingsEntry, .management-back") &&
        !e.target.closest("[data-vernal-tab], #vernalStage")
      ) {
        userChoseNative = true;
        stage.classList.remove("active");
        nav.querySelectorAll(".vernal-nav.active").forEach((el) => el.classList.remove("active"));
      }
    },
    true
  );

  const switcher = document.getElementById("projectSwitcher");
  let lastSyncedId = switcher?.value || "";
  async function syncSelectedProject(projectId, projectName, notify = false, skipSelectApi = false) {
    if (!projectId) return;
    lastSyncedId = projectId;
    setActiveProjectId(projectId);
    if (boundProject !== projectId) {
      panels.hidden = true;
      guide.hidden = false;
      stage.querySelector("#drawer")?.classList.add("hidden");
      document.querySelectorAll("dialog[open]").forEach(d => { if (d.dataset.vernalDialog) d.close(); });
    }
    try {
      let res = { switched: true };
      if (!skipSelectApi) {
        res = await api("/vernal-api/project/select", {
          method: "POST",
          body: JSON.stringify({ project_id: projectId, project_name: projectName || projectId }),
        });
      }
      if (lastSyncedId !== projectId) return;
      boundProject = res.needs_binding ? "" : projectId;
      guide.hidden = !res.needs_binding;
      if (reloadProjectContext && !res.needs_binding) {
        await reloadProjectContext();
        if (lastSyncedId !== projectId) return;
        panels.hidden = false;
        if (notify && res.switched) toast(`已切换至项目「${projectName || projectId}」`);
      }
    } catch (err) {
      if (notify) toast(err.message);
    }
  }
  window.vernalSwitchProject = async (projectId, projectName, opts = {}) => {
    if (!projectId) return;
    if (switcher) {
      let opt = Array.from(switcher.options || []).find((o) => o.value === projectId);
      if (!opt) {
        opt = document.createElement("option");
        opt.value = projectId;
        opt.textContent = projectName || projectId;
        switcher.append(opt);
      }
      switcher.value = projectId;
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await syncSelectedProject(projectId, projectName, false, Boolean(opts.skipSelectApi));
  };
  switcher?.addEventListener("change", () => {
    if (switcher.value === lastSyncedId) return;
    const opt = switcher.selectedOptions?.[0];
    syncSelectedProject(switcher.value, opt?.textContent?.trim() || switcher.value, true);
  });

  const shouldDefaultVernalHome = () => {
    const rawHash = (location.hash || "").replace(/^#\/?/, "").trim();
    return !userChoseNative && (!rawHash || rawHash === "home" || rawHash === "collaboration");
  };
  if (pendingView) {
    activate(pendingView);
  } else if (shouldDefaultVernalHome()) {
    activate("overview");
  }

  // LoopForge 异步 boot 可能会在挂载后再次激活聊天页；首次未手动切换前保持默认停留在 Vernal 工作台
  const bootGuard = new MutationObserver(() => {
    if (userChoseNative) {
      bootGuard.disconnect();
      return;
    }
    if (shouldDefaultVernalHome() && !stage.classList.contains("active")) {
      activate("overview");
    }
  });
  bootGuard.observe(main, { subtree: true, attributes: true, attributeFilter: ["class"] });
  setTimeout(() => bootGuard.disconnect(), 6000);

  let bound = false;
  let binding = false;
  let retryTimer;
  async function bind(silent) {
    if (binding || !stage.isConnected) return;
    binding = true;
    clearTimeout(retryTimer);
    let needInitProject = false;
    try {
      const status = await api("/vernal-api/runtime/status");
      needInitProject = status.workspace_initialized === false;
      bound = Boolean(status.loopforge.authenticated && status.loopforge.project_id && status.loopforge.workflow_id);
      if (status.loopforge.reachable && !bound && !needInitProject) {
        const result = await api("/vernal-api/runtime/bind", { method: "POST" });
        bound = Boolean(result.bound);
        if (bound && reloadProjectContext && !needInitProject) await reloadProjectContext();
      }
      if ((bound || needInitProject) && switcher?.value) {
        const opt = switcher.selectedOptions?.[0];
        await syncSelectedProject(switcher.value, opt?.textContent?.trim() || switcher.value, false);
      }
    } catch (err) {
      if (!silent) toast(err.message);
    } finally {
      binding = false;
      if (!bound && !needInitProject && stage.isConnected) retryTimer = setTimeout(() => bind(true), 5000);
    }
    dot.classList.toggle("warn", !bound);
    pillText.textContent = bound ? "执行底座已就绪" : "等待登录认证";
  }

  if (switcher) {
    const swObs = new MutationObserver(() => {
      if (switcher.value && switcher.value !== lastSyncedId) {
        const opt = switcher.selectedOptions?.[0];
        syncSelectedProject(switcher.value, opt?.textContent?.trim() || switcher.value, false);
      }
    });
    swObs.observe(switcher, { childList: true, subtree: true, attributes: true });
  }

  pill.addEventListener("click", () => (bound ? activate("runtime") : bind(false)));
  await bind(true);
  if (!pendingView && shouldDefaultVernalHome()) {
    activate("overview");
  }
}

function attachObjectTabs(activate) {
  const issueTabs = document.getElementById("issueDetailTabs");
  const runDetail = document.getElementById("runDetail");
  function attach(host, title, resolve, render) {
    if (!host) return;
    const button = element("button", "button", title);
    button.type = "button";
    const dialog = document.createElement("dialog");
    dialog.dataset.vernalDialog = "1";
    const content = document.createElement("div");
    const close = element("button", "button", "关闭");
    close.addEventListener("click", () => dialog.close());
    dialog.append(content, close);
    document.body.append(dialog);
    button.addEventListener("click", async () => {
      const path = resolve();
      if (!path) return;
      content.replaceChildren();
      dialog.showModal();
      try {
        const rows = await api(path);
        if (!dialog.open || resolve() !== path) return dialog.close();
        if (!rows.length) content.textContent = "此对象尚无研发扩展记录";
        else render(content, rows);
      } catch (err) { content.textContent = err.message; }
    });
    host.append(button);
  }
  const issuePath = () => {
    const id = typeof state !== "undefined" && state.issueDetailData?.issue?.id;
    return id && "/vernal-api/extensions/issues/" + encodeURIComponent(id);
  };
  attach(issueTabs, "详设与验收约束", issuePath, (host, rows) => {
    for (const row of rows) host.append(element("pre", "", [row.task_id, row.design, ...row.acceptance].join("\n")));
  });
  attach(issueTabs, "人工 Diff 审查", issuePath, (host, rows) => {
    for (const row of rows) {
      if (!row.reviews.length) host.append(element("p", "", row.task_id + " 暂无审查"));
      for (const review of row.reviews) {
        const button = element("button", "button", row.task_id + " · 审查 #" + review.review_id);
        button.addEventListener("click", () => {
          host.closest("dialog").close();
          window.closeIssueDrawer?.();
          activate("inbox");
          document.dispatchEvent(new CustomEvent("vernal-open-review", {detail: review.review_id}));
        });
        host.append(button);
      }
    }
  });
  attach(runDetail?.parentElement, "五类质量证据", () => runDetail.dataset.runId &&
    "/vernal-api/extensions/runs/" + encodeURIComponent(runDetail.dataset.runId), (host, rows) => {
      for (const row of rows) host.append(element("p", "", row.gate + " · " + row.status));
    });
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
  observer.observe(document.body, { subtree: true, childList: true });
}
