import { $, actorName, api, esc, fmtElapsed, getActiveProjectId, projectQuerySuffix, root, setLoggedInUser, toast } from "./api.js";
import { bindCouncilActions, refreshCouncil } from "./council.js";
import { renderDAG } from "./dag.js";
import { handleLiveDrawerEvent, initDrawer, openReportModal, openTaskDrawer } from "./drawer.js";
import { initLauncher } from "./launcher.js";
import { renderDocsAndPlan, renderGoalBar, renderRoster, renderSquadColumns } from "./roster.js";
import { refreshInbox, refreshRuntimePanel, updateInboxBadges } from "./runtime.js";
import { pollOpenTaskStreams, renderStageDrilldown, resetDrilldownProject } from "./stage-drilldown.js";
import { initSteering, renderSteering } from "./steering.js";
import { createTimeline } from "./timeline.js";

let state = null;
let liveProgress = null;
let _liveProgressInFlight = false;
let slices = [];
let planDoc = null;
let lastEventId = 0;
let activeView = "overview";
let activeAuxPanel = "";
const timeline = createTimeline(openTaskDrawer);
let launcher = null;
let _refreshTimer = null;

function renderOverviewDrilldown() {
  renderStageDrilldown(state, liveProgress, {
    onRefresh: refresh,
    onOpenTask: openTaskDrawer,
    onSwitchView: switchView,
  });
}

async function refreshLiveProgress() {
  if (_liveProgressInFlight || !state || state.workspace_initialized === false) return;
  _liveProgressInFlight = true;
  try {
    liveProgress = await api("/vernal-api/runtime/live-progress");
    renderOverviewDrilldown();
  } catch {
    renderOverviewDrilldown();
  } finally {
    _liveProgressInFlight = false;
  }
}

$("vov-aux-bar")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-aux-panel]");
  if (!btn) return;
  const key = btn.dataset.auxPanel;
  activeAuxPanel = activeAuxPanel === key ? "" : key;
  $("vov-aux-bar")?.querySelectorAll("[data-aux-panel]").forEach((b) => {
    const on = b.dataset.auxPanel === activeAuxPanel;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  for (const name of ["dag", "roster", "timeline", "evo"]) {
    const panel = $(`vov-panel-${name}`);
    if (panel) panel.hidden = activeAuxPanel !== name;
  }
  if (activeAuxPanel === "evo" && $("evolution-rail-card")) {
    $("evolution-rail-card").hidden = false;
  }
});

function updateHarnessCompatHint() {
  const el = $("cfg-harness-compat-hint");
  if (!el || !state) return;
  const rs = state.runner_summary || {};
  const selectedPool = $("cfg-pool")?.value || state.settings?.execution_pool || "auto";
  const effectivePool = selectedPool === "auto" ? (rs.effective_pool || "local_runner") : selectedPool;
  const harness = $("cfg-harness")?.value || state.settings?.harness || "auto";
  const localRt = rs.pools?.local_runner?.runtimes || state.runtimes || {};
  const pubRt = rs.pools?.public_docker?.runtimes || {};
  const labels = { codex: "Codex", "claude-code": "Claude Code", codebuddy: "CodeBuddy", opencode: "OpenCode", dsh: "DeepSeek Harness (dsh)" };
  const installedOf = (map) => Object.keys(labels).filter((k) => map?.[k]?.installed).map((k) => labels[k]);

  el.classList.remove("vset-compat-warn");
  if (effectivePool === "public_docker") {
    if (harness !== "auto" && !["dsh", "opencode"].includes(harness)) {
      el.classList.add("vset-compat-warn");
      el.textContent = `⚠ 公用 Docker 沙箱仅内置 DeepSeek Harness (dsh) 与 OpenCode，不支持 ${labels[harness] || harness}；请改选 auto / dsh / opencode，或切换为「本机 Runner」。`;
      return;
    }
    el.textContent = "公用 Docker 沙箱内置 DeepSeek Harness (dsh) 与 OpenCode，自动选用时会优先匹配已配置 Key 的执行器。";
    return;
  }
  const localInstalled = installedOf(localRt);
  if (harness !== "auto" && localRt?.[harness] && !localRt[harness].installed) {
    el.classList.add("vset-compat-warn");
    const extra = harness === "dsh" ? "如需使用 dsh，请将上方执行池切换为「锁定公用 Docker 沙箱」，或改选 OpenCode / Codex。" : "请在本机安装对应 CLI 或改选其他已就绪执行器。";
    el.textContent = `⚠ 当前本机 Runner 未检测到 ${labels[harness] || harness}（本机已装：${localInstalled.join("、") || "无"}）。${extra}`;
    return;
  }
  el.textContent = localInstalled.length
    ? `当前生效池（本机 Runner）已检测：${localInstalled.join("、")}；选 auto 将自动挑选已就绪执行器。`
    : "自动选择时会优先使用当前选中执行池内已就绪的执行器。";
}

// Phase 1 可选挂载模块（在 WP-0 ~ WP-3 中动态注册或按需导入）
const viewHooks = {
  onOverviewRender: null,
  onCouncilOpen: null,
  onInboxOpen: null,
  onRuntimeOpen: null,
};
export function registerViewHooks(hooks) {
  for (const [k, fn] of Object.entries(hooks)) {
    const prev = viewHooks[k];
    viewHooks[k] = prev
      ? (...args) => {
          prev(...args);
          fn(...args);
        }
      : fn;
  }
}

if ($("actor-input")) {
  $("actor-input").value = localStorage.getItem("autodev_actor") || "owner";
  $("actor-input").addEventListener("input", () => {
    localStorage.setItem("autodev_actor", actorName());
    if (!state?.current_user) setLoggedInUser("");
  });
}

export function switchView(rawView) {
  const view = rawView === "docs" ? "council" : rawView;
  activeView = view;
  root().querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  root().querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  const stage = document.getElementById("vernalStage");
  if (stage) {
    for (const panel of stage.querySelectorAll(".v-subview")) {
      panel.classList.toggle("active", panel.id === `view-${view}`);
    }
    for (const tab of document.querySelectorAll("[data-vernal-tab]")) {
      tab.classList.toggle("active", tab.dataset.vernalTab === view);
    }
  }
  if (!state) return;
  if (view === "overview") {
    renderOverviewDrilldown();
    refreshLiveProgress();
  } else if (view === "council") {
    refreshCouncil(openTaskDrawer);
    refreshDocs();
    viewHooks.onCouncilOpen?.(state, refresh);
  } else if (view === "inbox") {
    refreshInbox(state, refresh, openTaskDrawer, openReportModal);
    viewHooks.onInboxOpen?.(state, refresh, openTaskDrawer);
  } else if (view === "runtime") {
    populateAgentConfigForm();
    refreshRuntimePanel(refresh);
    viewHooks.onRuntimeOpen?.(state, refresh);
    requestAnimationFrame(() => updateSettingsTabIndicator());
  }
}

let activeSettingsTab = "pools";
function updateSettingsTabIndicator() {
  const bar = $("vset-tabs");
  const ind = $("vset-tab-indicator");
  if (!bar || !ind) return;
  const activeBtn = bar.querySelector(`.vset-tab[data-vset-tab="${activeSettingsTab}"]`) || bar.querySelector(".vset-tab.active");
  if (!activeBtn || activeBtn.offsetWidth === 0) return;
  ind.style.width = `${activeBtn.offsetWidth}px`;
  ind.style.transform = `translateX(${activeBtn.offsetLeft}px)`;
  ind.style.opacity = "1";
}

function selectSettingsTab(tabKey) {
  activeSettingsTab = tabKey || "pools";
  const view = $("view-runtime");
  if (!view) return;
  view.querySelectorAll(".vset-tab").forEach((btn) => {
    const on = btn.dataset.vsetTab === activeSettingsTab;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  view.querySelectorAll(".vset-pane").forEach((pane) => {
    const on = pane.dataset.vsetPane === activeSettingsTab;
    pane.hidden = !on;
    pane.classList.toggle("active", on);
  });
  requestAnimationFrame(updateSettingsTabIndicator);
}

$("vset-tabs")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-vset-tab]");
  if (btn) selectSettingsTab(btn.dataset.vsetTab);
});
window.addEventListener("resize", () => {
  if (activeView === "runtime") updateSettingsTabIndicator();
});

function syncPoolModeButtons(mode) {
  const val = mode || "auto";
  if ($("cfg-pool")) $("cfg-pool").value = val;
  $("vset-pool-seg")?.querySelectorAll("[data-pool-mode]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.poolMode === val);
  });
  const eff = val === "auto" ? (state?.runner_summary?.effective_pool || "local_runner") : val;
  $("vset-card-local")?.classList.toggle("is-active", eff === "local_runner");
  $("vset-card-public")?.classList.toggle("is-active", eff === "public_docker");
  updateHarnessCompatHint();
}

$("vset-pool-seg")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-pool-mode]");
  if (!btn) return;
  syncPoolModeButtons(btn.dataset.poolMode);
  if ($("agent-config-form")) $("agent-config-form")._editing = true;
});

async function refreshDocs() {
  try {
    const [s, p] = await Promise.all([api("/vernal-api/slices"), api("/vernal-api/plan")]);
    slices = s;
    planDoc = p;
    renderDocsAndPlan(state, planDoc, slices, openTaskDrawer);
  } catch {}
}

export async function refresh() {
  const [nextState, inbox] = await Promise.all([api("/vernal-api/state"), api("/vernal-api/inbox")]);
  state = nextState;
  updateInboxBadges(inbox.count);
  renderAll();
  document.dispatchEvent(new CustomEvent("vernal-state", { detail: state }));
}

function scheduleRefresh(delayMs = 150) {
  if (_refreshTimer) return;
  _refreshTimer = setTimeout(() => {
    _refreshTimer = null;
    refresh().catch(() => {});
  }, delayMs);
}

function renderAll() {
  if (!state) return;
  window.__vernalLastState = state;
  setLoggedInUser(state.current_user || "");
  if ($("actor-fallback-wrap")) {
    $("actor-fallback-wrap").hidden = Boolean(state.current_user);
  }

  $("project-name").textContent = `${state.name}`;
  const st = $("project-status");
  st.textContent = state.planning ? "PLANNING" : state.status;
  st.className = `status-pill ${state.planning ? "RUNNING" : state.status}`;

  const srcPill = $("project-source-pill");
  if (srcPill) {
    const modeLabels = {
      local_folder: state.settings?.collaboration_mode === "git" ? "本机 Git 副本" : "本机目录",
      host_link: "远程目录桥接",
      git_repo: "Git 仓库",
      import_folder: "已导入目录",
      scratch: state.settings?.collaboration_mode === "git" ? "Git 分支模式" : "文件交接模式",
    };
    const baseTxt = modeLabels[state.source_mode] || "Git 分支模式";
    srcPill.textContent = state.git_branch ? `${baseTxt} · ${state.git_branch}` : baseTxt;
    srcPill.title = state.workspace_root || "";
    srcPill.classList.remove("hidden");
  }

  if ($("side-backend")) {
    $("side-backend").textContent = state.settings?.runtime_backend || "local";
    $("side-backend").className = `tag ${state.settings?.runtime_backend === "loopforge" ? "ok" : ""}`;
  }

  const rt = state.runtimes || {};
  const rs = state.runner_summary || {};
  if ($("pool-pill")) {
    $("pool-pill").textContent =
      rs.effective_pool === "public_docker"
        ? "公用 Docker"
        : rs.effective_pool === "local_runner"
          ? "本机 Runner"
          : "本地执行";
  }
  const pool = rs.pools?.public_docker;
  if ($("pool-status") && pool) {
    const modeLabel =
      (state.settings?.execution_pool || "auto") === "public_docker"
        ? "当前策略：锁定公用 Docker 沙箱"
        : (state.settings?.execution_pool || "auto") === "local_runner"
          ? "当前策略：锁定本机个人 Runner"
          : `当前策略：智能自动（当前生效：${rs.effective_pool === "public_docker" ? "公用 Docker 沙箱" : "本机 Runner"}）`;
    $("pool-status").textContent = modeLabel;
  }

  // 双执行池并列状态卡片（永远同时渲染本机 Runner 与公用 Docker 沙箱的真实能力）
  const localPoolRt = rs.pools?.local_runner?.runtimes || rt;
  const localOnline = Boolean(rs.pools?.local_runner?.online ?? (rs.online_count > 0 || state.settings?.runtime_backend === "local"));
  const localHosts = (rs.pools?.local_runner?.runner_names || rs.runner_names || []).filter(Boolean);
  if ($("vset-local-dot")) $("vset-local-dot").className = `vset-pool-dot ${localOnline ? "ok" : "warn"}`;
  if ($("vset-local-status")) {
    $("vset-local-status").textContent = localOnline ? "在线" : "离线";
    $("vset-local-status").className = `vset-state-pill ${localOnline ? "ok" : "warn"}`;
  }
  if ($("vset-local-hosts")) {
    $("vset-local-hosts").textContent = localHosts.length
      ? `在线执行机：${localHosts.join("、")}`
      : state.settings?.runtime_backend === "local"
        ? "本地调试执行模式"
        : "未检测到在线本机 Runner（运行 ./vernal.sh 或复制下方命令接入）";
  }
  const renderRtChip = ([k, label], sourceMap, allowCbClick = false) => {
    const info = sourceMap?.[k] || {};
    const cls = !info.installed ? "" : info.ready ? "ok" : "warn";
    const statusTxt = !info.installed
      ? "未检测"
      : info.ready
        ? info.needs_api_key
          ? "凭据就绪"
          : "免配置就绪"
        : info.api_key_configured === null
          ? "待核实"
          : "缺少凭据";
    const tip = esc(`${info.note || ""}${info.bin ? " (" + info.bin + ")" : ""}`);
    const clickAttr = allowCbClick && k === "codebuddy" ? ' data-open-cb-key="1" style="cursor:pointer"' : "";
    return `<span class="vset-rt-chip ${cls}" title="${tip}"${clickAttr}><i></i><span>${esc(label)}</span><em>${esc(statusTxt)}</em></span>`;
  };
  const localRtBox = $("vset-local-runtimes");
  if (localRtBox) {
    const localEntries = [
      ["codex", "Codex"],
      ["claude-code", "Claude Code"],
      ["codebuddy", "CodeBuddy"],
      ["opencode", "OpenCode"],
      ["dsh", "DeepSeek Harness"],
    ];
    localRtBox.innerHTML = localEntries.map((ent) => renderRtChip(ent, localPoolRt, true)).join("");
    localRtBox.querySelectorAll("[data-open-cb-key]").forEach((el) => {
      el.addEventListener("click", () => {
        const box = $("cb-key-box");
        if (box) {
          box._userToggled = true;
          box.hidden = !box.hidden;
        }
      });
    });
  }

  if (pool) {
    const installOk = Boolean(pool.install_ready ?? (pool.image_ready && pool.key_configured));
    const projReady = Boolean(pool.project_ready);
    const pubWarn = Boolean(installOk || pool.image_ready || pool.key_configured);
    if ($("vset-public-dot")) $("vset-public-dot").className = `vset-pool-dot ${projReady ? "ok" : pubWarn ? "warn" : ""}`;
    if ($("vset-public-status")) {
      const stLabel = projReady
        ? "当前项目可用"
        : !pool.image_ready
          ? "镜像待构建"
          : !pool.key_configured
            ? "待配置 Key"
            : !pool.identity_configured
              ? "账号待启用"
              : pool.project_authorized === false
                ? "待授权当前项目"
                : pool.identity_verified === false
                  ? "账号待核验"
                  : "底座已装 · 待核验";
      $("vset-public-status").textContent = stLabel;
      $("vset-public-status").className = `vset-state-pill ${projReady ? "ok" : "warn"}`;
    }
    if ($("vset-public-image")) {
      $("vset-public-image").textContent = `镜像 ${pool.sandbox_image || "autodev-runtime-sandbox:latest"} · ${pool.image_ready ? "已安装" : "待构建"}${pool.deepseek_key_configured ? ` · DeepSeek Key (${pool.deepseek_key_masked})` : ""}`;
    }
    const pubRtBox = $("vset-public-runtimes");
    if (pubRtBox) {
      const pubEntries = [
        ["dsh", "DeepSeek Harness (dsh)"],
        ["opencode", "OpenCode"],
      ];
      pubRtBox.innerHTML = pubEntries.map((ent) => renderRtChip(ent, pool.runtimes || {}, false)).join("");
    }
    const checksBox = $("vset-public-checks");
    if (checksBox) {
      const last = pool.last_sandbox_run;
      const smoke = pool.last_smoke;
      const lastTxt = smoke
        ? `试跑 ${smoke.harness}/${smoke.model || "-"} · ${smoke.ok ? "通过 ✓" : "失败"}${smoke.run_id ? ` (${smoke.run_id.slice(0, 12)})` : ""}`
        : last
          ? `${last.task_id} · ${last.state}（${String(last.updated_at || "").slice(0, 19).replace("T", " ")}）`
          : "尚无本项目公用沙箱执行记录（可点击下方「用当前项目试跑」验证）";
      const lastCls = smoke ? (smoke.ok ? "ok" : "err") : last ? (last.state === "FINISHED" ? "ok" : last.state === "FAILED" ? "err" : "warn") : "";
      const acctLabel = !pool.identity_configured
        ? "待签发公用执行账号"
        : pool.has_personal_runner
          ? `公用账号 ${pool.service_login || ""} 绑定了个人执行机（不可用）`
          : pool.project_authorized === true
            ? `公用账号 ${pool.service_login || ""}${pool.service_user_id ? ` (#${pool.service_user_id})` : ""} 已授权当前项目`
            : pool.project_authorized === false
              ? `公用账号 ${pool.service_login || ""}${pool.service_user_id ? ` (#${pool.service_user_id})` : ""} 尚未加入当前项目成员`
              : `公用账号 ${pool.service_login || "已配置"}（登录后自动核验项目授权）`;
      const acctCls = pool.project_authorized === true && pool.identity_verified !== false ? "ok" : pool.has_personal_runner ? "err" : "warn";
      checksBox.innerHTML = `
        <div class="vset-check-row ${installOk ? "ok" : "warn"}"><i></i><span><strong>镜像与额度：</strong>${installOk ? "沙箱镜像已安装，公用模型 Key 已配置" : !pool.image_ready ? "沙箱镜像尚未构建" : "尚未配置公用模型 Key"}</span></div>
        <div class="vset-check-row ${acctCls}"><i></i><span><strong>账号与项目授权：</strong>${esc(acctLabel)}</span></div>
        <div class="vset-check-row ${lastCls}"><i></i><span><strong>最近沙箱实测：</strong>${esc(lastTxt)}</span></div>
      `;
    }
    renderPublicSmokeResult(pool.last_smoke);
    const authText = $("vset-public-auth-text");
    const authBtn = $("btn-authorize-pool-project");
    if (authText) {
      if (!pool.identity_configured) {
        authText.textContent = "保存 DeepSeek / OpenAI API Key 后将自动签发公用执行账号";
      } else if (pool.project_authorized === true) {
        authText.textContent = "在上方切到「锁定公用 Docker 沙箱」并选 dsh 或 OpenCode，保存后即可启动沙箱执行 ✓";
      } else if (pool.project_authorized === false) {
        authText.textContent = "请先点击右侧「一键授权当前项目」，将公用账号加入本项目可写成员后再锁定沙箱执行";
      } else {
        authText.textContent = `公用账号：${pool.service_login || "已配置"}（使用前请确认已加入当前项目成员）`;
      }
    }
    if (authBtn) {
      authBtn.classList.toggle("hidden", !(pool.identity_configured && pool.project_authorized === false && pool.can_authorize));
    }
  }
  const personalDsh = $("vset-personal-dsh-status");
  if (personalDsh) {
    const ocOk = Boolean(rs.personal_cli?.opencode);
    const dshOk = Boolean(rs.personal_cli?.dsh);
    const localHasOc = Boolean(localPoolRt?.opencode?.installed);
    const localHasDsh = Boolean(localPoolRt?.dsh?.installed);
    if (ocOk && dshOk) {
      personalDsh.textContent = localOnline && localHasOc && !localHasDsh
        ? "聊天助手个人凭据已就绪（OpenCode ✓ · dsh ✓）— 本机已装 OpenCode，聊天页请选 OpenCode → deepseek-chat"
        : "聊天助手个人凭据已就绪（OpenCode ✓ · dsh ✓）";
    } else if (dshOk && !ocOk) {
      personalDsh.textContent = "已配置 dsh，尚未配置 OpenCode（若本机仅装 OpenCode，请点左侧同步按钮一键补齐）";
    } else if (ocOk && !dshOk) {
      personalDsh.textContent = "聊天助手 OpenCode 凭据已就绪 ✓（点左侧同步按钮可同时补齐 dsh）";
    } else {
      personalDsh.textContent = "聊天助手尚未配置 DeepSeek 凭据（点击左侧按钮可一键同步 OpenCode + dsh）";
    }
  }
  if (!$("agent-config-form")?._editing) {
    syncPoolModeButtons(state.settings?.execution_pool || "auto");
  }
  updateHarnessCompatHint();

  const renderPill = (elId, info, activeModel = "") => {
    const el = $(elId);
    if (!el) return;
    if (!info) {
      el.textContent = "未提供";
      el.className = "tag";
      el.title = "";
      return;
    }
    if (!info.installed) {
      el.textContent = info.runner_online === false ? "Runner 离线" : "未安装";
      el.className = info.runner_online === false ? "tag warn" : "tag";
    } else if (info.ready) {
      el.textContent = activeModel || "就绪";
      el.className = "tag ok";
    } else {
      el.textContent = info.api_key_configured === null ? "凭据待核实" : "缺少凭据";
      el.className = "tag warn";
    }
    el.title = `${info.note || ""}${info.bin ? " · " + info.bin : ""}`;
  };
  renderPill("codex-pill", rt.codex, state.settings?.effective_harness === "codex" ? state.settings?.model || "就绪" : "就绪");
  renderPill("codebuddy-pill", rt.codebuddy);
  renderPill("opencode-pill", rt.opencode);
  renderPill("dsh-pill", rt.dsh);

  const badgesEl = $("agent-runtime-badges");
  if (badgesEl) {
    const entries = [
      ["codex", "Codex"],
      ["codebuddy", "CodeBuddy"],
      ["claude-code", "Claude Code"],
      ["opencode", "OpenCode"],
      ["dsh", "DeepSeek Harness"],
    ];
    const sourceLabel =
      rs.effective_pool === "public_docker"
        ? "公用 Docker 沙箱："
        : rs.source === "loopforge_runner"
          ? `在线 Runner（${(rs.runner_names || []).join(", ")}）：`
          : state.settings?.runtime_backend === "loopforge"
            ? "Runner 离线（请执行 ./vernal.sh 启动）："
            : "本机环境扫描：";
    badgesEl.innerHTML =
      `<span class="muted" style="font-size:12px">${esc(sourceLabel)}</span>` +
      entries
        .map(([k, label]) => {
          const info = rt[k] || {};
          const cls = !info.installed ? "" : info.ready ? "ok" : "warn";
          const statusTxt = !info.installed
            ? "未检测"
            : info.ready
              ? info.needs_api_key
                ? "凭据就绪"
                : "免配置就绪"
              : info.api_key_configured === null
                ? "待核实"
                : "缺少凭据";
          const tip = esc(`${info.note || ""}${info.bin ? " (" + info.bin + ")" : ""}`);
          const clickAttr = k === "codebuddy" ? ' data-open-cb-key="1" style="cursor:pointer"' : "";
          return `<span class="tag ${cls}" title="${tip}"${clickAttr}>${label} · ${statusTxt}</span>`;
        })
        .join("");
    badgesEl.querySelectorAll("[data-open-cb-key]").forEach((el) => {
      el.addEventListener("click", () => {
        const box = $("cb-key-box");
        if (box) {
          box._userToggled = true;
          box.hidden = !box.hidden;
        }
      });
    });
  }

  const cbBox = $("cb-key-box");
  if (cbBox && state.settings?.runtime_backend === "loopforge") {
    const cbInfo = rt.codebuddy || {};
    const cbSelected = [
      state.settings.harness,
      state.settings.director_harness,
      state.settings.raw_coder_harness,
      state.settings.qa_harness,
    ].includes("codebuddy");
    if ((cbInfo.installed && !cbInfo.ready) || (cbSelected && !cbInfo.ready)) {
      cbBox.hidden = false;
    } else if (cbInfo.ready && !cbBox._userToggled) {
      cbBox.hidden = true;
    }
    if ($("cb-key-env") && rs.codebuddy_internet_environment !== undefined && !$("cb-key-env")._touched) {
      $("cb-key-env").value = rs.codebuddy_internet_environment || "";
    }
  }

  const effHint = $("cfg-effective-hint");
  if (effHint && state.settings) {
    effHint.textContent = `当前生效：流程=${state.settings.process_template || "legacy"} · 总监=${state.settings.effective_harness || "-"} · 编码=${state.settings.effective_coder_harness || "-"} · 审查=${state.settings.effective_qa_harness || "-"}`;
  }

  const u = state.usage_total || { input: 0, cached: 0, output: 0, reasoning: 0 };
  $("usage-pill").textContent = `Tokens ${(u.input + u.output).toLocaleString()} · 缓存 ${u.cached.toLocaleString()}`;

  // 顶栏单一主 CTA 阶段机：确保同一时刻只有一个 btn-primary
  const sliceCount = state.slices ?? state.slices_count ?? 0;
  const uninitialized = state.workspace_initialized === false;
  const needsPlan = !uninitialized && ["CREATED", "INGESTED", "SPECIFIED"].includes(state.status);
  const canPlanNow = needsPlan && !state.planning && (sliceCount > 0 || state.source_mode !== "scratch");
  const isPlanned = state.status === "PLANNED" && !state.planning && !state.scheduler_running;
  const canStart = !state.scheduler_running && ["APPROVED", "RUNNING", "STALLED"].includes(state.status);

  $("btn-launcher-toggle")?.classList.toggle("btn-primary", uninitialized);
  const btnPlan = $("btn-plan");
  if (btnPlan) {
    btnPlan.textContent = (state.milestones || []).length > 0 ? "重新立项" : "开始立项评审";
    btnPlan.classList.toggle("btn-primary", canPlanNow);
    btnPlan.classList.toggle("hidden", uninitialized || state.scheduler_running || isPlanned || state.status === "COMPLETED");
  }
  $("btn-approve-and-start")?.classList.toggle("hidden", !isPlanned);
  $("btn-approve")?.classList.toggle("hidden", !isPlanned);
  $("btn-start")?.classList.toggle("hidden", !canStart);
  $("btn-stop")?.classList.toggle("hidden", !state.scheduler_running);

  launcher?.syncAutoOpen(state);

  const modeSel = $("plan-mode");
  if (modeSel && !modeSel._touched) modeSel.value = state.settings?.planning_mode || "goal";
  if ($("plan-summary")) $("plan-summary").textContent = state.plan_summary || "尚未生成规划";

  // 左侧导航与收件箱角标同步
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
  if ($("collaboration-mode")) {
    $("collaboration-mode").value = state.settings?.collaboration_mode || "git";
    $("collaboration-mode").disabled =
      state.source_mode === "host_link" || state.planning || state.scheduler_running || (state.milestones || []).length > 0;
  }

  renderGoalBar(state);
  renderOverviewDrilldown();
  if (activeView === "overview") refreshLiveProgress();
  renderSteering(state, { onRefresh: refresh, onSelectTask: openTaskDrawer });
  renderDAG(state, openTaskDrawer);
  renderRoster(state, openTaskDrawer);
  renderSquadColumns(state, openTaskDrawer);
  viewHooks.onOverviewRender?.(state, refresh, switchView);
  if (activeView === "council") renderDocsAndPlan(state, planDoc, slices, openTaskDrawer);
  if (activeView === "inbox") {
    refreshInbox(state, refresh, openTaskDrawer, openReportModal);
    viewHooks.onInboxOpen?.(state, refresh, openTaskDrawer);
  }
  if (activeView === "runtime" && !$("agent-config-form")?._editing) {
    populateAgentConfigForm();
  }
}

root().querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

$("btn-council-trace")?.addEventListener("click", () => openTaskDrawer("_council_director", "trace"));
$("btn-planner-trace")?.addEventListener("click", () => openTaskDrawer("_planner", "trace"));
$("btn-refresh-inbox")?.addEventListener("click", () => {
  refreshInbox(state, refresh, openTaskDrawer, openReportModal);
  viewHooks.onInboxOpen?.(state, refresh, openTaskDrawer);
});
$("plan-mode")?.addEventListener("change", (e) => {
  e.target._touched = true;
});

$("btn-ingest")?.addEventListener("click", async () => {
  try {
    const r = await api("/vernal-api/ingest", { method: "POST" });
    toast(`已摄入 ${r.slices} 个资料切片`);
    await refresh();
    if (activeView === "council") refreshDocs();
  } catch (err) {
    toast(`摄入失败：${err.message}`);
  }
});

async function triggerPlan(extra = "") {
  if ((state?.milestones || []).length > 0) {
    if (!window.confirm("当前项目已生成研发蓝图，重新立项将覆盖未开始的拆解规划（如需局部变更建议使用「变更与评审」）。确认继续重新立项吗？")) {
      return;
    }
  }
  const mode = $("plan-mode")?.value || state?.settings?.planning_mode || "goal";
  try {
    await api("/vernal-api/plan", { method: "POST", body: JSON.stringify({ extra_instructions: extra, mode }) });
    toast(mode === "goal" ? "已启动多智能体架构评审与立项拆解" : "已启动后台蓝图拆解");
    await refresh();
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
    await refresh();
  } catch (err) {
    toast(`批准失败：${err.message}`);
  }
});

$("btn-approve-and-start")?.addEventListener("click", async () => {
  try {
    await api("/vernal-api/plan/approve", { method: "POST", body: JSON.stringify({ actor: actorName() }) });
    await api("/vernal-api/scheduler/start", { method: "POST" });
    toast("蓝图已批准并启动自动研发流水线");
    await refresh();
    switchView("overview");
  } catch (err) {
    toast(`启动失败：${err.message}`);
  }
});

$("btn-start")?.addEventListener("click", async () => {
  try {
    await api("/vernal-api/scheduler/start", { method: "POST" });
    toast("研发调度器已启动");
    await refresh();
  } catch (err) {
    toast(`启动失败：${err.message}`);
  }
});

$("btn-stop")?.addEventListener("click", async () => {
  if (!window.confirm("确认暂停当前研发调度器吗？正在执行的工位将在当前原子步骤结束后安全停机。")) return;
  await api("/vernal-api/scheduler/stop", { method: "POST" });
  toast("已发送暂停调度信号");
  await refresh();
});

// 仅做轻量文本计时跳秒，不再每秒重绘整张 SVG DAG
setInterval(() => {
  root().querySelectorAll(".elapsed[data-start]").forEach((el) => {
    el.textContent = fmtElapsed(el.dataset.start);
  });
}, 1000);

// 概览页自动轮询底层节点级实时进度（10 秒）与已展开任务的思考/命令流（4 秒）
setInterval(() => {
  if (activeView !== "overview" || !state || state.workspace_initialized === false) return;
  const hasWatchable =
    state.scheduler_running ||
    state.status === "RUNNING" ||
    (state.tasks || []).some((t) => ["IN_PROGRESS", "VERIFYING", "REVIEWING", "FUSED"].includes(t.status));
  if (hasWatchable) refreshLiveProgress();
}, 10000);

setInterval(() => {
  if (activeView !== "overview" || !state || state.workspace_initialized === false) return;
  pollOpenTaskStreams();
}, 4000);

let activeEventSource = null;

function connectSSE() {
  if (activeEventSource) activeEventSource.close();
  const projectId = getActiveProjectId();
  const es = new EventSource(`/vernal-api/events?after=${lastEventId}${projectQuerySuffix("&")}`);
  activeEventSource = es;
  const isCurrent = () => es === activeEventSource && projectId === getActiveProjectId();
  es.onopen = () => {
    if (!isCurrent()) return;
    timeline.setConnected(true);
    $("sse-indicator")?.classList.add("live");
    if ($("sse-text")) $("sse-text").textContent = "已连接";
  };
  es.onerror = () => {
    if (!isCurrent()) return;
    timeline.setConnected(false);
    $("sse-indicator")?.classList.remove("live");
    if ($("sse-text")) $("sse-text").textContent = "重连中";
  };
  es.onmessage = (e) => {
    if (!isCurrent()) return;
    try {
      const ev = JSON.parse(e.data);
      if (ev.event_id > lastEventId) {
        lastEventId = ev.event_id;
        timeline.append(ev);
        if (ev.event_type === "link.conflict") toast(ev.message);
        handleLiveDrawerEvent(ev);
        if (ev.task_id && state?.tasks) {
          const t = state.tasks.find((x) => x.task_id === ev.task_id);
          if (t) {
            t.latest_activity = {
              event_type: ev.event_type,
              actor: ev.actor,
              message: ev.message,
              created_at: ev.created_at,
            };
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
        if (!ev.event_type.startsWith("agent.")) {
          scheduleRefresh(150);
        }
      }
    } catch {}
  };
}

export async function reloadProjectContext() {
  resetDrilldownProject();
  liveProgress = null;
  await refresh();
  lastEventId = state.recent_events?.at(-1)?.event_id || 0;
  timeline.start(state);
  connectSSE();
  if (activeView === "council") {
    await refreshCouncil(openTaskDrawer);
    await refreshDocs();
    viewHooks.onCouncilOpen?.(state, refresh);
  } else if (activeView === "inbox") {
    await refreshInbox(state, refresh, openTaskDrawer, openReportModal);
    viewHooks.onInboxOpen?.(state, refresh, openTaskDrawer);
  } else if (activeView === "runtime") {
    populateAgentConfigForm();
    await refreshRuntimePanel(refresh);
  }
}

initDrawer();
bindCouncilActions(refresh, openTaskDrawer);
initSteering({ onRefresh: refresh, getState: () => state, onSelectTask: openTaskDrawer });
launcher = initLauncher({
  onProjectReady: async () => {
    await reloadProjectContext();
  },
  onSwitchView: (v) => switchView(v),
});
document.addEventListener("vernal-setup-workspace", e => launcher.open("local_folder", e.detail));
$("btn-launcher-toggle")?.addEventListener("click", () => {
  const panel = $("project-launcher");
  if (!panel) return;
  if (panel.hidden) launcher.open();
  else launcher.close();
});
$("cb-key-env")?.addEventListener("change", (e) => {
  e.target._touched = true;
});
$("btn-cb-key-save")?.addEventListener("click", async () => {
  const key = $("cb-key-input")?.value?.trim() || "";
  const env = $("cb-key-env")?.value || "";
  if (!key) {
    toast("请先粘贴 CodeBuddy API Key");
    return;
  }
  const btn = $("btn-cb-key-save");
  if (btn) btn.disabled = true;
  try {
    await api("/vernal-api/credentials/codebuddy", {
      method: "POST",
      body: JSON.stringify({ api_key: key, internet_environment: env }),
    });
    if ($("cb-key-input")) $("cb-key-input").value = "";
    toast("CodeBuddy API Key 已安全保存，Runner 已就绪");
    await refresh();
  } catch (err) {
    toast(`保存失败：${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
});
$("btn-cb-key-jump")?.addEventListener("click", () => {
  document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "settings" } }));
});

const nativeAssignments = [["cfg-native-executor", "native_executor_user_id"], ["cfg-native-approver", "native_approver_user_id"]];
for (const [id] of nativeAssignments) {
  $(id)?.addEventListener("focus", async () => {
    const select = $(id);
    try {
      const projects = await api("/api/projects");
      const project = projects.find(p => p.id === state.settings.loopforge_project_id);
      if (!project) return;
      const members = new Map([[project.ownerId, project.ownerName || project.ownerId]]);
      for (const member of project.members || []) {
        if (member.role !== "viewer") members.set(member.userId, member.loginName || member.userId);
      }
      const selected = select.value;
      select.replaceChildren(new Option("项目默认", ""));
      for (const [uid, name] of members) if (uid) select.add(new Option(String(name), String(uid)));
      select.value = selected;
    } catch (err) { toast(err.message); }
  });
}
$("copy-pool-account")?.addEventListener("click", async () => {
  try {
    const pool = await api("/vernal-api/runtime/public-pool");
    if (!pool.service_login) { toast("请先配置公用账号"); return; }
    await navigator.clipboard.writeText(pool.service_login);
    toast("已复制公用账号，请在项目成员页授予成员权限");
  } catch (err) { toast(err.message); }
});
$("enroll-company-model")?.addEventListener("click", async () => {
  try {
    await api("/vernal-api/runtime/public-pool/enroll", {method:"POST"});
    toast("当前执行账号已启用公司模型");
  } catch (err) { toast(err.message); }
});

function populateAgentConfigForm() {
  if (!state?.settings || !$("agent-config-form")) return;
  const s = state.settings;
  if ($("pool-project-members") && s.loopforge_project_id) {
    $("pool-project-members").href = "/#/projects/" + encodeURIComponent(s.loopforge_project_id) + "/members";
    $("pool-project-members").target = "_top";
  }
  if ($("cfg-process-template")) $("cfg-process-template").value = s.process_template || "legacy";
  if ($("cfg-review-policy")) $("cfg-review-policy").value = s.review_policy || "off";
  if ($("cfg-doc-profile")) $("cfg-doc-profile").value = s.doc_profile || "default";
  if ($("cfg-ci-provider")) $("cfg-ci-provider").value = s.ci_provider || "none";
  for (const [id, key] of nativeAssignments) {
    const select = $(id);
    if (!select) continue;
    const value = String(s[key] || "");
    if (value && !Array.from(select.options).some(o => o.value === value)) select.add(new Option(value,value));
    select.value = value;
  }
  if ($("cfg-pool")) syncPoolModeButtons(s.execution_pool || "auto");
  const publicPool = state.runner_summary?.pools?.public_docker || {};
  if ($("pool-deepseek-url")) $("pool-deepseek-url").value = publicPool.deepseek_base_url || "";
  if ($("pool-openai-url")) $("pool-openai-url").value = publicPool.openai_base_url || "";
  if ($("pool-model")) $("pool-model").value = publicPool.default_model || "";
  if ($("pool-reasoning")) $("pool-reasoning").value = publicPool.reasoning_model || "";
  if ($("cfg-harness")) $("cfg-harness").value = s.harness || "auto";
  if ($("cfg-model")) $("cfg-model").value = s.model || "";
  if ($("cfg-concurrency")) $("cfg-concurrency").value = String(s.max_concurrency || 2);
  if ($("cfg-director-harness")) $("cfg-director-harness").value = s.director_harness || "";
  if ($("cfg-coder-harness")) $("cfg-coder-harness").value = s.raw_coder_harness ?? "";
  if ($("cfg-qa-harness")) $("cfg-qa-harness").value = s.qa_harness || "";
  if ($("cfg-director-model")) $("cfg-director-model").value = s.director_model || "";
  if ($("cfg-coder-model")) $("cfg-coder-model").value = s.coder_model || "";
  if ($("cfg-qa-model")) $("cfg-qa-model").value = s.qa_model || "";
  updateHarnessCompatHint();
}

$("cfg-harness")?.addEventListener("change", () => updateHarnessCompatHint());
$("agent-config-form")?.addEventListener("input", () => {
  $("agent-config-form")._editing = true;
});
$("btn-agent-toggle")?.addEventListener("click", () => {
  switchView("runtime");
});
$("btn-agent-rescan")?.addEventListener("click", async () => {
  try {
    const res = await api("/vernal-api/agent/rescan", { method: "POST" });
    toast(`扫描完成，推荐执行器：${res.preferred_harness || "暂无可选执行器"}`);
    await refresh();
  } catch (err) {
    toast(`扫描失败：${err.message}`);
  }
});
function renderPublicSmokeResult(smoke) {
  const box = $("vset-public-smoke-result");
  if (!box) return;
  if (!smoke || !Array.isArray(smoke.steps) || !smoke.steps.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const runBtn = smoke.run_id
    ? `<button type="button" class="btn btn-xs" data-smoke-run="${esc(smoke.run_id)}">查看试跑 Run ${esc(smoke.run_id.slice(0, 12))} ↗</button>`
    : "";
  box.innerHTML = `
    <div class="vset-smoke-head">
      <strong>最近试跑（${esc(smoke.harness || "-")} / ${esc(smoke.model || "-")}）· ${smoke.ok ? "全部通过 ✓" : "未通过"}</strong>
      <div class="vset-inline-actions">${runBtn}<span class="muted" style="font-size:11px">${esc(String(smoke.tested_at || "").slice(0, 19).replace("T", " "))}</span></div>
    </div>
    <div class="vset-smoke-steps">${smoke.steps
      .map(
        (st) => `<div class="vset-check-row ${st.ok ? "ok" : "err"}"><i></i><span><strong>${st.ok ? "✓" : "✕"} ${esc(st.label)}：</strong>${esc(st.detail || "")}</span></div>`
      )
      .join("")}</div>
  `;
  box.querySelector("[data-smoke-run]")?.addEventListener("click", (e) => {
    document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "runs", runId: e.currentTarget.dataset.smokeRun } }));
  });
}

async function runPublicPoolSmoke(harness, btn) {
  const box = $("vset-public-smoke-result");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = `正在沙箱试跑 ${harness}…`;
  if (box) {
    box.hidden = false;
    box.innerHTML = `<div class="muted" style="font-size:12px">正在通过正式公用 Docker 沙箱链路试跑 ${esc(harness)}（校验授权 → 启动隔离容器 → 调用公用模型 → 核验回传产物，约需 15–45 秒）…</div>`;
  }
  try {
    const res = await api("/vernal-api/runtime/public-pool/smoke-test", {
      method: "POST",
      body: JSON.stringify({ harness }),
    });
    renderPublicSmokeResult(res);
    toast(res.ok ? `公用沙箱 ${harness} 试跑全部通过 ✓（Run ${res.run_id?.slice(0, 12) || ""}）` : `沙箱试跑失败：${res.error || "见逐项详情"}`);
    await refresh();
  } catch (err) {
    toast(`沙箱试跑失败：${err.message}`);
    if (box) {
      box.hidden = false;
      box.innerHTML = `<div class="vset-check-row err"><i></i><span>试跑请求失败：${esc(err.message)}</span></div>`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

$("btn-smoke-public-dsh")?.addEventListener("click", (e) => runPublicPoolSmoke("dsh", e.currentTarget));
$("btn-smoke-public-opencode")?.addEventListener("click", (e) => runPublicPoolSmoke("opencode", e.currentTarget));

$("btn-authorize-pool-project")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const res = await api("/vernal-api/runtime/public-pool/authorize-project", { method: "POST" });
    toast(res.already ? `公用账号 ${res.service_login} 已在当前项目成员中` : `已授权公用账号 ${res.service_login} 加入当前项目`);
    await refresh();
  } catch (err) {
    toast(`授权失败：${err.message}`);
  } finally {
    btn.disabled = false;
  }
});
$("btn-sync-personal-dsh")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const rawKey = $("pool-deepseek-key")?.value?.trim() || "";
    const baseUrl = $("pool-deepseek-url")?.value?.trim() || "";
    const models = [$("pool-model")?.value?.trim(), $("pool-reasoning")?.value?.trim()].filter(Boolean);
    const res = await api("/vernal-api/credentials/personal-cli", {
      method: "POST",
      body: JSON.stringify({
        provider: "both",
        api_key: rawKey,
        base_url: baseUrl,
        models,
        use_pool_key: !rawKey,
      }),
    });
    toast(`已为当前账号同步聊天助手凭据（OpenCode + dsh · 模型：${(res.models || []).join("、")}）`);
    await refresh();
  } catch (err) {
    toast(`同步失败：${err.message}`);
  } finally {
    btn.disabled = false;
  }
});
$("save-public-pool")?.addEventListener("click", async () => {
  const payload = {
    deepseek_base_url: $("pool-deepseek-url")?.value || "",
    openai_base_url: $("pool-openai-url")?.value || "",
    default_model: $("pool-model")?.value || "",
    reasoning_model: $("pool-reasoning")?.value || "",
  };
  if ($("pool-deepseek-key")?.value) payload.deepseek_api_key = $("pool-deepseek-key").value;
  if ($("pool-openai-key")?.value) payload.openai_api_key = $("pool-openai-key").value;
  try {
    const saved = await api("/vernal-api/runtime/public-pool", { method: "POST", body: JSON.stringify(payload) });
    if ($("pool-deepseek-key")) $("pool-deepseek-key").value = "";
    if ($("pool-openai-key")) $("pool-openai-key").value = "";
    await refresh();
    toast(saved.setup_message || (saved.service_login ? `公用额度池已保存（账号：${saved.service_login}）` : "公用额度池配置已保存"));
  } catch (err) {
    toast(err.message);
  }
});
$("copy-runner-command")?.addEventListener("click", async () => {
  try {
    const res = await api("/vernal-api/runner/connect", { method: "POST" });
    const field = $("runner-command");
    if (field) {
      field.hidden = false;
      field.value = res.command;
      field.select();
    }
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(res.command);
      toast("本机 Runner 接入命令已复制到剪贴板");
    } else {
      toast("请复制输入框中的命令并在终端执行");
    }
  } catch (err) {
    toast(err.message);
  }
});
$("agent-config-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitBtns = [$("btn-agent-save"), ...Array.from(root().querySelectorAll("[data-vset-save]"))].filter(Boolean);
  const syncAlert = $("vset-sync-alert");
  if (syncAlert) {
    syncAlert.hidden = true;
    syncAlert.innerHTML = "";
  }
  submitBtns.forEach((b) => (b.disabled = true));
  try {
    const res = await api("/vernal-api/agent/config", {
      method: "POST",
      body: JSON.stringify({
        process_template: $("cfg-process-template")?.value || "legacy",
        review_policy: $("cfg-review-policy")?.value || "off",
        doc_profile: $("cfg-doc-profile")?.value || "default",
        ci_provider: $("cfg-ci-provider")?.value || "none",
        execution_pool: $("cfg-pool")?.value || "auto",
        ...Object.fromEntries(nativeAssignments.filter(([id,key]) => Number($(id)?.value || 0) !== Number(state.settings[key] || 0))
          .map(([id,key]) => [key, Number($(id)?.value || 0)])),
        harness: $("cfg-harness")?.value || "auto",
        model: $("cfg-model")?.value || "",
        max_concurrency: Number($("cfg-concurrency")?.value || 2),
        director_harness: $("cfg-director-harness")?.value || "",
        coder_harness: $("cfg-coder-harness")?.value || "",
        qa_harness: $("cfg-qa-harness")?.value || "",
        director_model: $("cfg-director-model")?.value || "",
        coder_model: $("cfg-coder-model")?.value || "",
        qa_model: $("cfg-qa-model")?.value || "",
      }),
    });
    $("agent-config-form")._editing = false;
    const wfVer = res.workflow_version ? ` v${res.workflow_version}` : "";
    if (res.synced_loopforge === false && res.sync_error) {
      toast(`配置已保存，但底层工作流同步失败：${res.sync_error}`);
      if (syncAlert) {
        syncAlert.hidden = false;
        syncAlert.innerHTML = `<span><strong>配置已保存，但工作流同步失败：</strong>${esc(res.sync_error)}</span> <button type="button" class="btn btn-xs" id="btn-retry-wf-sync">重试同步工作流</button>`;
        $("btn-retry-wf-sync")?.addEventListener("click", () => $("agent-config-form")?.requestSubmit());
      }
    } else if (res.synced_loopforge) {
      toast(
        `配置已保存，工作流已同步（${res.workflow_id || "SOP"}${wfVer} · 总监=${res.effective.director_harness}, 编码=${res.effective.coder_harness}）`
      );
    } else {
      toast(`本地配置已保存（总监=${res.effective.director_harness}, 编码=${res.effective.coder_harness}, 审查=${res.effective.qa_harness}）`);
    }
    await refresh();
    if (activeView === "runtime") await refreshRuntimePanel(refresh);
  } catch (err) {
    toast(`保存失败：${err.message}`);
  } finally {
    submitBtns.forEach((b) => (b.disabled = false));
  }
});

$("wish-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("btn-wish-submit");
  if (submit) submit.disabled = true;
  try {
    const result = await api("/vernal-api/wish", {
      method: "POST",
      body: JSON.stringify({
        title: $("wish-title").value,
        content: $("wish-content").value,
      }),
    });
    toast("已保存需求并完成切片，共 " + result.slices + " 段");
    await refresh();
    $("wish-form").hidden = true;
  } catch (err) {
    toast(err.message);
  } finally {
    if (submit) submit.disabled = false;
  }
});

$("collaboration-mode")?.addEventListener("change", async (event) => {
  const mode = event.target.value;
  try {
    await api("/vernal-api/runtime/config", { method: "POST", body: JSON.stringify({ collaboration_mode: mode }) });
    await refresh();
  } catch (err) {
    event.target.value = state?.settings?.collaboration_mode || "git";
    toast(err.message);
  }
});

// 按需动态加载 Phase 1 扩展模块（若已存在则自动挂载到对应视图容器）
Promise.allSettled([
  import("./pipeline.js").then((m) => m.initPipelineModule?.({ registerViewHooks, refresh, switchView })),
  import("./docs-center.js").then((m) => m.initDocsCenterModule?.({ registerViewHooks, refresh })),
  import("./review.js").then((m) => m.initReviewModule?.({ registerViewHooks, refresh, openTaskDrawer })),
  import("./governance.js").then((m) => m.initGovernanceModule?.({ registerViewHooks, refresh })),
]).finally(() => {
  refresh()
    .then(() => {
      lastEventId = state.recent_events?.at(-1)?.event_id || 0;
      timeline.start(state);
      connectSSE();
      switchView(activeView);
    })
    .catch((err) => toast(err.message));
});
