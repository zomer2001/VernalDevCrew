import { $, api, esc } from "./api.js";

const drawer = {
  open: false,
  taskId: null,
  tab: "trace",
  detail: null,
  selectedRunId: null,
  steps: [],
  stepIndexByItem: new Map(),
};

export function initDrawer() {
  $("drawer").querySelectorAll(".dtab").forEach((btn) => {
    btn.addEventListener("click", () => {
      drawer.tab = btn.dataset.dtab;
      $("drawer").querySelectorAll(".dtab").forEach((b) => b.classList.toggle("active", b === btn));
      renderDrawerTab();
    });
  });
  const close = () => {
    drawer.open = false;
    drawer.taskId = null;
    $("drawer").classList.add("hidden");
  };
  $("drawer-close")?.addEventListener("click", close);
  $("drawer")?.addEventListener("click", (e) => {
    if (e.target === $("drawer")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawer.open) close();
  });
}

export function openReportModal(title, content) {
  drawer.open = true;
  drawer.taskId = null;
  $("drawer-title").textContent = title;
  $("drawer-subtitle").textContent = "";
  $("drawer-live-badge").classList.add("hidden");
  $("drawer").querySelector(".drawer-tabs").classList.add("hidden");
  $("drawer-body").innerHTML = `<pre>${esc(content || "暂无内容")}</pre>`;
  $("drawer").classList.remove("hidden");
}

export async function openTaskDrawer(taskId, initialTab = null) {
  drawer.open = true;
  drawer.taskId = taskId;
  if (initialTab) drawer.tab = initialTab;
  drawer.selectedRunId = null;
  $("drawer").querySelector(".drawer-tabs").classList.remove("hidden");
  $("drawer").querySelectorAll(".dtab").forEach((b) => b.classList.toggle("active", b.dataset.dtab === drawer.tab));
  $("drawer").classList.remove("hidden");
  $("drawer-body").innerHTML = `<p class="muted">加载中…</p>`;
  await loadDetail();
  await renderDrawerTab();
}

async function loadDetail() {
  if (!drawer.taskId) return;
  drawer.detail = await api(`/vernal-api/tasks/${encodeURIComponent(drawer.taskId)}`);
  const t = drawer.detail.task;
  $("drawer-title").textContent = `${t.task_id} · ${t.title}`;
  const sub = $("drawer-subtitle");
  sub.innerHTML = `${esc(`状态 ${t.status} · 组 ${t.squad || t.module} · 角色 ${t.role}${t.branch_name ? ` · 分支 ${t.branch_name}` : ""}`)}${
    t.loopforge_run_id
      ? ` · <button type="button" class="btn btn-sm" id="btn-jump-native-run" style="padding:1px 7px;font-size:11px">原生运行 ${esc(t.loopforge_run_id.slice(0, 12))} ↗</button>`
      : t.loopforge_issue_id
        ? ` · <button type="button" class="btn btn-sm" id="btn-jump-native-issue" style="padding:1px 7px;font-size:11px">原生工单 ↗</button>`
        : ""
  }`;
  sub.querySelector("#btn-jump-native-run")?.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "runs", runId: t.loopforge_run_id } }));
  });
  sub.querySelector("#btn-jump-native-issue")?.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "issues", issueId: t.loopforge_issue_id } }));
  });
  const isLive = ["IN_PROGRESS", "VERIFYING", "REVIEWING", "RUNNING"].includes(t.status);
  $("drawer-live-badge").classList.toggle("hidden", !isLive);
}

function stepHtml(step) {
  const k = step.kind || "info";
  if (k === "command") {
    const running = step.phase === "started";
    const badge = running
      ? `<span class="status-pill RUNNING">执行中…</span>`
      : `<span class="status-pill ${step.exit_code === 0 ? "PASS" : "FAIL"}">exit ${step.exit_code ?? "-"}</span>`;
    const out = step.output
      ? `<details class="step-out"><summary>展开终端输出 (${step.output.length} 字符)</summary><pre>${esc(step.output)}</pre></details>`
      : "";
    return `<div class="trace-step kind-command">
      <div class="trace-step-head"><span>⌘ Shell 命令 / 工具调用</span>${badge}</div>
      <div class="cmd-line">${esc(step.command || "")}</div>
      ${out}
    </div>`;
  }
  if (k === "message" || k === "reasoning") {
    return `<div class="trace-step kind-message">
      <div class="trace-step-head"><span>${k === "reasoning" ? "🧠 推理" : "💬 智能体说明"}</span></div>
      <div style="white-space:pre-wrap">${esc(step.text || "")}</div>
    </div>`;
  }
  if (k === "file") {
    const files = (step.changes || []).map((c) => `${c.kind || "edit"}: ${c.path || ""}`).join("\n");
    return `<div class="trace-step kind-file">
      <div class="trace-step-head"><span>✎ 文件变更</span><span class="muted">${(step.changes || []).length} 个</span></div>
      ${files ? `<pre>${esc(files)}</pre>` : `<div class="muted">正在写入…</div>`}
    </div>`;
  }
  if (k === "warning") {
    return `<div class="trace-step kind-warning"><div class="trace-step-head"><span>⚠ 提示 / 重连</span></div><div>${esc(step.text || "")}</div></div>`;
  }
  if (k === "turn") {
    const u = step.usage;
    const uTxt = u ? ` · in ${u.input.toLocaleString()} / cache ${u.cached.toLocaleString()} / out ${u.output.toLocaleString()}${u.reasoning ? ` / think ${u.reasoning.toLocaleString()}` : ""}` : "";
    return `<div class="trace-step kind-turn"><div class="muted" style="font-size:12px">${esc(step.summary || step.phase || "")}${esc(uTxt)}</div></div>`;
  }
  return "";
}

async function renderDrawerTab() {
  if (!drawer.open || !drawer.taskId || !drawer.detail) return;
  const body = $("drawer-body");
  const runs = drawer.detail.runs || [];

  if (drawer.tab === "trace") {
    const traceRuns = runs.filter((r) => r.has_events || !["VERIFY"].includes(r.stage));
    if (drawer.selectedRunId === null && traceRuns.length) drawer.selectedRunId = traceRuns[traceRuns.length - 1].run_id;
    const q = drawer.selectedRunId !== null ? `?run_id=${encodeURIComponent(drawer.selectedRunId)}` : "";
    const data = await api(`/vernal-api/tasks/${encodeURIComponent(drawer.taskId)}/trace${q}`);
    drawer.steps = data.steps || [];
    drawer.stepIndexByItem = new Map();
    drawer.steps.forEach((s, i) => {
      if (s.item_id) drawer.stepIndexByItem.set(s.item_id, i);
    });
    const opts = traceRuns
      .map((r) => `<option value="${r.run_id}" ${r.run_id === drawer.selectedRunId ? "selected" : ""}>#${r.attempt_no} ${esc(r.stage)} (${esc(r.verdict || "RUNNING")})</option>`)
      .join("");
    const t = drawer.detail.task || {};
    const lfMeta = t.loopforge_run_id
      ? `<span class="tag ok">LoopForge Run ${esc(t.loopforge_run_id.slice(0, 12))}</span>
         <button id="drawer-jump-issues" class="btn btn-sm">跳至需求列表</button>
         <button id="drawer-jump-runs" class="btn btn-sm">跳至运行记录</button>`
      : "";
    body.innerHTML = `
      <div class="trace-toolbar">
        <div><span class="muted">轮次：</span> ${opts ? `<select id="trace-run-select">${opts}</select>` : t.loopforge_run_id ? lfMeta : `<span class="muted">暂无会话记录</span>`}</div>
        <div>${opts && lfMeta ? lfMeta : ""} <button id="trace-refresh" class="btn btn-sm">刷新</button></div>
      </div>
      <div id="trace-steps-box">${drawer.steps.length ? drawer.steps.map(stepHtml).join("") : `<p class="muted">等待步骤事件…</p>`}</div>
    `;
    $("drawer-jump-issues")?.addEventListener("click", () => {
      $("drawer").classList.add("hidden");
      document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "issues" } }));
    });
    $("drawer-jump-runs")?.addEventListener("click", () => {
      $("drawer").classList.add("hidden");
      document.dispatchEvent(new CustomEvent("vernal-jump-native", { detail: { view: "runs" } }));
    });
    $("trace-run-select")?.addEventListener("change", (e) => {
      drawer.selectedRunId = Number(e.target.value);
      renderDrawerTab();
    });
    $("trace-refresh")?.addEventListener("click", () => loadDetail().then(renderDrawerTab));
    return;
  }

  if (drawer.tab === "diff") {
    const d = await api(`/vernal-api/tasks/${encodeURIComponent(drawer.taskId)}/diff`);
    const lines = d.diff
      ? d.diff
          .split("\n")
          .map((ln) => {
            let cls = "";
            if (ln.startsWith("diff --git ")) cls = "diff-file";
            else if (ln.startsWith("@@")) cls = "diff-hunk";
            else if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "diff-add";
            else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "diff-del";
            return `<div class="diff-line ${cls}">${esc(ln)}</div>`;
          })
          .join("")
      : `<div class="muted">当前尚无代码改动（${esc(d.source)}）</div>`;
    body.innerHTML = `
      <div class="trace-toolbar">
        <span class="muted">来源：<code>${esc(d.source)}</code></span>
        <button id="diff-refresh" class="btn btn-sm">刷新实时 Diff</button>
      </div>
      ${d.diff_stat ? `<pre>${esc(d.diff_stat)}</pre>` : ""}
      <div class="diff-view">${lines}</div>
    `;
    $("diff-refresh")?.addEventListener("click", renderDrawerTab);
    return;
  }

  if (drawer.tab === "verify") {
    const t = drawer.detail.task;
    const blocks = runs
      .filter((r) => r.stage === "VERIFY" || r.stage === "REVIEW")
      .reverse()
      .map((r) => {
        if (r.stage === "VERIFY") {
          return `<div class="card" style="margin-bottom:10px">
            <div class="task-top"><strong>#${r.attempt_no} 本地确定性门禁</strong><span class="status-pill ${esc(r.verdict || "")}">${esc(r.verdict || "RUNNING")}</span></div>
            <pre>${esc(r.verify_log || r.summary_log || "暂无日志")}</pre>
          </div>`;
        }
        const rv = r.review_verdict;
        const tbl = rv?.criteria_check?.length
          ? `<table class="data-table"><thead><tr><th style="width:46px">结果</th><th>验收标准</th><th>核验证据</th></tr></thead><tbody>${rv.criteria_check
              .map((c) => `<tr><td>${c.met ? "✅" : "❌"}</td><td>${esc(c.criterion)}</td><td>${esc(c.evidence)}</td></tr>`)
              .join("")}</tbody></table>`
          : "";
        const issues = rv?.blocking_issues?.length
          ? `<h4>阻塞缺陷 (${rv.blocking_issues.length})</h4><ul>${rv.blocking_issues
              .map((i) => `<li><code>${esc(i.file)}:${i.line}</code> ${esc(i.problem)} → ${esc(i.fix)}</li>`)
              .join("")}</ul>`
          : "";
        return `<div class="card" style="margin-bottom:10px">
          <div class="task-top"><strong>#${r.attempt_no} 独立只读审查</strong><span class="status-pill ${esc(r.verdict || "")}">${esc(r.verdict || "RUNNING")}</span></div>
          <p>${esc(rv?.summary || r.summary_log || "")}</p>
          ${tbl}${issues}
        </div>`;
      })
      .join("");
    body.innerHTML = `
      <div class="adr-card" style="margin-bottom:10px">
        <div><strong>白名单：</strong><code>${esc((t.allowed_paths || []).join(", "))}</code></div>
        <div><strong>验证命令：</strong><code>${esc((t.verify_cmds || []).join(" && ") || "无")}</code></div>
      </div>
      ${blocks || `<p class="muted">尚未进入门禁或审查阶段。</p>`}
    `;
    return;
  }

  if (drawer.tab === "prompt") {
    if (!runs.length) {
      body.innerHTML = `<p class="muted">暂无调用记录。</p>`;
      return;
    }
    if (drawer.selectedRunId === null || !runs.some((r) => r.run_id === drawer.selectedRunId)) {
      drawer.selectedRunId = runs[runs.length - 1].run_id;
    }
    const art = await api(
      `/vernal-api/tasks/${encodeURIComponent(drawer.taskId)}/runs/${encodeURIComponent(drawer.selectedRunId)}/artifacts`
    );
    const opts = runs
      .map((r) => `<option value="${r.run_id}" ${r.run_id === drawer.selectedRunId ? "selected" : ""}>#${r.attempt_no} ${esc(r.stage)}</option>`)
      .join("");
    body.innerHTML = `
      <div class="trace-toolbar"><div><span class="muted">轮次：</span> <select id="art-run-select">${opts}</select></div></div>
      ${art.command ? `<h4>启动命令</h4><pre>${esc(art.command)}</pre>` : ""}
      ${art.prompt ? `<h4>提示词 (${art.prompt.length} 字)</h4><pre>${esc(art.prompt)}</pre>` : ""}
      ${art.last_message ? `<h4>最终输出</h4><pre>${esc(art.last_message)}</pre>` : ""}
      ${art.stderr ? `<h4>stderr</h4><pre>${esc(art.stderr)}</pre>` : ""}
    `;
    $("art-run-select")?.addEventListener("change", (e) => {
      drawer.selectedRunId = Number(e.target.value);
      renderDrawerTab();
    });
  }
}

export function handleLiveDrawerEvent(ev) {
  if (!drawer.open || !drawer.taskId || ev.task_id !== drawer.taskId) return;
  const et = ev.event_type || "";
  if (!et.startsWith("agent.")) {
    loadDetail().then(() => {
      if (drawer.tab === "verify" || drawer.tab === "diff") renderDrawerTab();
    });
    return;
  }
  if (drawer.tab === "diff" && et === "agent.file") {
    renderDrawerTab();
    return;
  }
  if (drawer.tab !== "trace") return;
  const p = ev.payload || {};
  const step = {
    item_id: p.item_id || "",
    kind: et.slice("agent.".length),
    phase: p.phase || "completed",
    command: p.command || "",
    exit_code: p.exit_code,
    output: p.output || "",
    changes: p.changes || [],
    text: p.text || ev.message || "",
    summary: ev.message || "",
    usage: p.usage || null,
  };
  if (step.item_id && drawer.stepIndexByItem.has(step.item_id)) {
    drawer.steps[drawer.stepIndexByItem.get(step.item_id)] = step;
  } else {
    if (step.item_id) drawer.stepIndexByItem.set(step.item_id, drawer.steps.length);
    drawer.steps.push(step);
  }
  const box = $("trace-steps-box");
  if (box) box.innerHTML = drawer.steps.map(stepHtml).join("");
}
