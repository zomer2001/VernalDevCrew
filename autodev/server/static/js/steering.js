import { $, actorName, api, esc, root, toast } from "./api.js";

const rejectDrafts = new Map();
let selectedCheckpointId = null;
let showDecisionsList = false;

const KIND_LABELS = {
  INITIAL: "立项共识",
  REVIEW: "阶段复盘",
  PIVOT: "方向转向",
  DIAGNOSE: "异常会诊",
};

const STATUS_LABELS = {
  RUNNING: "并发评审中",
  DISPATCHING: "派单中",
  DISPATCH_FAILED: "派单失败",
  SYNC_PENDING: "远端已通过，正在同步最新变更单产物…",
  APPLYING: "正在应用变更单",
  AWAITING_APPROVAL: "待拍板",
  APPLIED: "已生效",
  FAILED: "失败",
};

function jumpToNativeRun(runId, issueId = "") {
  document.dispatchEvent(
    new CustomEvent("vernal-jump-native", {
      detail: { view: runId ? "runs" : "issues", runId: runId || "", issueId: issueId || "" },
    })
  );
}

export function openSteeringModal(preset = {}) {
  const modal = $("steering-modal");
  if (!modal) return;
  modal.hidden = false;
  if (preset.kind && $("steering-kind")) $("steering-kind").value = preset.kind;
  if (preset.topic !== undefined && $("steering-topic")) $("steering-topic").value = preset.topic;
  if (preset.note !== undefined && $("steering-note")) $("steering-note").value = preset.note;
  setTimeout(() => ($("steering-topic")?.value ? $("steering-note") : $("steering-topic"))?.focus(), 30);
}

export function closeSteeringModal() {
  const modal = $("steering-modal");
  if (modal) modal.hidden = true;
}

export function initSteering({ onRefresh, getState, onSelectTask }) {
  $("btn-toggle-decisions")?.addEventListener("click", () => {
    showDecisionsList = !showDecisionsList;
    renderSteering(getState(), { onRefresh, onSelectTask });
  });
  $("btn-rail-diagnose")?.addEventListener("click", () =>
    openSteeringModal({ kind: "DIAGNOSE", topic: "会诊当前阻塞或集成失败问题并制定最小返工方案" }));
  $("btn-rail-start-review")?.addEventListener("click", () => openSteeringModal({ kind: "PIVOT" }));
  $("btn-steering-open")?.addEventListener("click", () => openSteeringModal());
  $("btn-steering-close")?.addEventListener("click", closeSteeringModal);
  $("steering-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "steering-modal") closeSteeringModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("steering-modal") && !$("steering-modal").hidden) {
      closeSteeringModal();
    }
  });

  root()
    .querySelectorAll("[data-steering-preset]")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        const kind = btn.dataset.steeringPreset || "REVIEW";
        const st = getState?.() || {};
        const fused = (st.tasks || []).filter((t) => t.status === "FUSED");
        const failedMs = (st.milestones || []).filter((m) => m.status === "FAILED");
        if ($("steering-kind")) $("steering-kind").value = kind;
        root()
          .querySelectorAll("[data-steering-preset]")
          .forEach((b) => b.classList.toggle("active", b === btn));
        if (kind === "DIAGNOSE" && !$("steering-topic").value.trim()) {
          const hint = failedMs[0]?.id
            ? `排查里程碑 ${failedMs[0].id} 集成报错并给出最小返工方案`
            : fused[0]?.task_id
              ? `会诊熔断任务 ${fused[0].task_id} 根因与契约调整`
              : "排查当前阻塞问题并评估返工范围";
          $("steering-topic").value = hint;
        } else if (kind === "PIVOT" && !$("steering-topic").value.trim()) {
          $("steering-topic").value = "保留已完成底座，调整后续需求与全局技术规矩";
        } else if (kind === "REVIEW" && !$("steering-topic").value.trim()) {
          $("steering-topic").value = "阶段性复盘当前已交付成果与下一波次优先级";
        }
      });
    });

  $("form-steering-start")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const topic = $("steering-topic")?.value.trim() || "";
    const human_note = $("steering-note")?.value.trim() || "";
    const kind = $("steering-kind")?.value || "REVIEW";
    if (!topic && !human_note) {
      toast("请填写讨论议题或具体诉求");
      return;
    }
    const submitBtn = $("btn-steering-submit");
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api("/vernal-api/steering/start", {
        method: "POST",
        body: JSON.stringify({ topic: topic || human_note.slice(0, 60), human_note, kind, actor: actorName() }),
      });
      selectedCheckpointId = res.checkpoint?.checkpoint_id || null;
      if ($("steering-topic")) $("steering-topic").value = "";
      if ($("steering-note")) $("steering-note").value = "";
      closeSteeringModal();
      toast(
        res.checkpoint?.loopforge_run_id
          ? `已发起 LoopForge 原生三专家并发评审（Run ${res.checkpoint.loopforge_run_id.slice(0, 8)}）`
          : "已生成评审变更单草案，请确认拍板"
      );
      if (onRefresh) await onRefresh();
    } catch (err) {
      toast(`发起评审失败：${err.message}`);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

function renderOpinionsGrid(opinions = {}) {
  const specs = [
    ["pm", "PM 产品视角"],
    ["arch", "契约架构视角"],
    ["qa", "QA 质量红线"],
  ];
  const items = specs
    .map(([key, label]) => {
      const op = opinions[key];
      if (!op || typeof op !== "object") return "";
      const changes = Array.isArray(op.change) && op.change.length ? ` · 建议变更：${op.change.slice(0, 2).join("；")}` : "";
      return `<div class="st-opinion-item">
        <span class="tag">${esc(label)}</span>
        <span>${esc(op.headline || "已提交意见")}${esc(changes)}</span>
      </div>`;
    })
    .filter(Boolean);
  return items.length ? `<div class="st-opinions">${items.join("")}</div>` : "";
}

export function renderSteering(state, { onRefresh, onSelectTask } = {}) {
  if (!state) return;
  const tasks = state.tasks || [];
  const milestones = state.milestones || [];
  const decisions = state.decisions || [];
  const activeDecisions = decisions.filter((d) => d.status === "ACTIVE");
  const checkpoints = state.checkpoints || [];
  const ruleVer = state.decisions_rule_version || 0;

  // 1. 更新弹窗顶部实时项目摘要
  const ctxBanner = $("steering-context-banner");
  if (ctxBanner) {
    const doneCnt = tasks.filter((t) => ["APPROVED", "MERGED"].includes(t.status)).length;
    const fusedCnt = tasks.filter((t) => t.status === "FUSED").length;
    ctxBanner.innerHTML =
      `<span>当前交付进度：<strong>${doneCnt} / ${tasks.length}</strong> 单已通过（不会因局部转向被推倒重来）</span>` +
      `<span>生效全局规矩：<strong>${activeDecisions.length} 条（Rule v${ruleVer}）</strong></span>` +
      (fusedCnt ? `<span class="tag warn">熔断待处理 ${fusedCnt} 单</span>` : "");
  }

  // 2. 待拍板转向变更单预览卡
  const approvalCard = $("steering-approval-card");
  const pendingCp = [...checkpoints].reverse().find((c) => c.status === "AWAITING_APPROVAL");
  const draftKey = pendingCp && JSON.stringify([state.project_key, pendingCp.checkpoint_id]);
  const renderKey = pendingCp && JSON.stringify([draftKey, pendingCp.plan_revision, pendingCp.status,
    Object.keys(pendingCp.opinions || {}).sort()]);
  if (approvalCard) {
    if (!pendingCp) {
      approvalCard.hidden = true;
      approvalCard.innerHTML = "";
    } else if (approvalCard.hidden || approvalCard.dataset.renderKey !== renderKey) {
      approvalCard.dataset.renderKey = renderKey;
      approvalCard.hidden = false;
      const plan = pendingCp.draft_plan || {};
      const upserts = plan.decisions_upsert || [];
      const revokes = plan.decisions_revoke || [];
      const reopens = plan.reopen_tasks || [];
      const cancels = plan.cancel_tasks || [];
      const newTasks = plan.new_tasks || [];
      const preservedCnt = tasks.filter(
        (t) => ["APPROVED", "MERGED"].includes(t.status) && !reopens.some((r) => r.task_id === t.task_id)
      ).length;

      approvalCard.innerHTML = `
        <div class="card-head">
          <div>
            <h3>⏸ 待拍板转向变更单 · 评审 #${pendingCp.round_no}「${esc(pendingCp.trigger_topic)}」</h3>
            <p class="card-desc">PM / 架构 / QA 三专家并发会审已收敛，批准后自动同步 LoopForge 全局 Rule 并精准调度受影响任务（你在原生「待我确认」里点通过同样自动生效）</p>
          </div>
          <div class="btn-group">
            ${pendingCp.loopforge_run_id ? `<button type="button" class="btn btn-sm" data-st-jump-run="${esc(pendingCp.loopforge_run_id)}">查看原生并发评审 Run</button>` : ""}
          </div>
        </div>
        ${renderOpinionsGrid(pendingCp.opinions)}
        <div class="st-plan-summary">${esc(plan.summary || "总监已生成转向变更单草案，请确认以下变更范围：")}</div>
        <div class="st-diff-grid">
          <div class="st-diff-col">
            <div class="st-diff-title">全局规矩变更（LoopForge Global Rule）</div>
            ${upserts.map((d) => `<div class="st-diff-row add"><code>+[${esc(d.decision_id)}]</code> <strong>${esc(d.title)}</strong>：${esc(d.rule_text)}</div>`).join("") || `<div class="muted" style="font-size:12px">无新增或修订规矩</div>`}
            ${revokes.map((d) => `<div class="st-diff-row del"><code>-[${esc(d.decision_id)}]</code> 废止：${esc(d.reason)}</div>`).join("")}
          </div>
          <div class="st-diff-col">
            <div class="st-diff-title">任务与 Issue 局部调度 <span class="tag ok">保留 ${preservedCnt} 单已通过成果</span></div>
            ${reopens.map((r) => `<div class="st-diff-row warn"><code>↺ ${esc(r.task_id)}</code> 局部退回重做：${esc(r.reason)}</div>`).join("")}
            ${newTasks.map((t) => `<div class="st-diff-row add"><code>＋ [${esc(t.milestone_id)}]</code> ${esc(t.title)}（${esc(t.squad)}）</div>`).join("")}
            ${cancels.map((tid) => `<div class="st-diff-row del"><code>✕ ${esc(tid)}</code> 取消并关闭对应 Issue</div>`).join("")}
            ${!reopens.length && !newTasks.length && !cancels.length ? `<div class="muted" style="font-size:12px">仅更新全局规矩，无需退回或增删现有任务</div>` : ""}
          </div>
        </div>
        <div class="st-approval-actions">
          <button type="button" id="btn-st-approve-now" class="btn btn-primary">✓ 批准并同步到 LoopForge</button>
          <input id="st-reject-reason-input" type="text" placeholder="若不满意，填写修改意见打回给研发总监重新收敛方案…" />
          <button type="button" id="btn-st-reject-now" class="btn">↺ 带意见打回重议</button>
        </div>
      `;

      const reasonInput = approvalCard.querySelector("#st-reject-reason-input");
      reasonInput.value = rejectDrafts.get(draftKey) || "";
      reasonInput.addEventListener("input", () => rejectDrafts.set(draftKey, reasonInput.value));
      approvalCard.querySelector("[data-st-jump-run]")?.addEventListener("click", (e) => {
        jumpToNativeRun(e.currentTarget.dataset.stJumpRun, pendingCp.loopforge_issue_id);
      });
      approvalCard.querySelector("#btn-st-approve-now")?.addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const result = await api(`/vernal-api/steering/${pendingCp.checkpoint_id}/approve`, {
            method: "POST",
            body: JSON.stringify({ actor: actorName(), expected_revision: pendingCp.plan_revision, comment: "Vernal 工作台确认拍板" }),
          });
          if (result.checkpoint.status === "APPLIED") {
            rejectDrafts.delete(draftKey);
            toast(result.checkpoint.last_error || "转向变更单已生效");
          } else {
            toast(STATUS_LABELS[result.checkpoint.status] || "正在确认审批状态");
          }
          if (onRefresh) await onRefresh();
        } catch (err) {
          toast(`批准失败：${err.message}`);
          btn.disabled = false;
        }
      });
      approvalCard.querySelector("#btn-st-reject-now")?.addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const reason = approvalCard.querySelector("#st-reject-reason-input")?.value.trim() || "";
        if (!reason) {
          toast("请先填写希望总监调整的具体意见");
          approvalCard.querySelector("#st-reject-reason-input")?.focus();
          return;
        }
        btn.disabled = true;
        try {
          await api(`/vernal-api/steering/${pendingCp.checkpoint_id}/reject`, {
            method: "POST",
            body: JSON.stringify({ actor: actorName(), reason }),
          });
          rejectDrafts.delete(draftKey);
          toast("已带意见打回 LoopForge 总监节点重新生成方案");
          if (onRefresh) await onRefresh();
        } catch (err) {
          toast(`打回失败：${err.message}`);
          btn.disabled = false;
        }
      });
    }
  }

  // 3. 项目演进主线（Checkpoint Rail）与全局规矩抽屉
  const railCard = $("evolution-rail-card");
  if (!railCard) return;
  const hasAny = checkpoints.length > 0 || milestones.length > 0 || decisions.length > 0;
  railCard.hidden = !hasAny;
  if (!hasAny) return;

  const hasTrouble = tasks.some((t) => t.status === "FUSED") || milestones.some((m) => m.status === "FAILED");
  const ruleBtn = $("btn-toggle-decisions");
  if (ruleBtn) ruleBtn.textContent = `生效规矩 ${activeDecisions.length} 条 · v${ruleVer}`;
  const diagBtn = $("btn-rail-diagnose");
  if (diagBtn) diagBtn.hidden = !hasTrouble;

  const railEl = $("evolution-rail");
  if (railEl) {
    const nodesHtml = [];
    checkpoints.forEach((cp, idx) => {
      const activeCls = selectedCheckpointId === cp.checkpoint_id ? "selected" : "";
      const stCls = cp.status === "APPLIED" ? "done" : cp.status === "AWAITING_APPROVAL" ? "awaiting" : ["RUNNING", "DISPATCHING", "SYNC_PENDING", "APPLYING"].includes(cp.status) ? "running" : "failed";
      const verBadge = cp.rule_version ? ` · v${cp.rule_version}` : "";
      nodesHtml.push(`
        <button type="button" class="evo-node checkpoint ${stCls} ${activeCls}" data-cp-id="${cp.checkpoint_id}">
          <span class="evo-kicker">${esc(KIND_LABELS[cp.kind] || "阶段评审")} #${cp.round_no}${esc(verBadge)}</span>
          <strong class="evo-title">${esc(cp.trigger_topic || "项目评审")}</strong>
          <span class="evo-sub">${esc(STATUS_LABELS[cp.status] || cp.status)}</span>
        </button>
      `);
      const waveMs = milestones[idx];
      if (waveMs) {
        const mTasks = tasks.filter((t) => t.milestone_id === waveMs.id);
        const mDone = mTasks.filter((t) => ["APPROVED", "MERGED"].includes(t.status)).length;
        const mCls = waveMs.status === "MERGED" ? "done" : waveMs.status === "FAILED" ? "failed" : mDone > 0 ? "running" : "";
        nodesHtml.push(`<span class="evo-arrow">→</span>
          <div class="evo-node wave ${mCls}">
            <span class="evo-kicker">执行波次 · ${esc(waveMs.id)}</span>
            <strong class="evo-title">${esc(waveMs.title)}</strong>
            <span class="evo-sub">${mDone}/${mTasks.length} 单通过 · ${esc(waveMs.status)}</span>
          </div>`);
      }
      if (idx < checkpoints.length - 1) nodesHtml.push(`<span class="evo-arrow">→</span>`);
    });
    for (let i = checkpoints.length; i < milestones.length; i++) {
      const waveMs = milestones[i];
      const mTasks = tasks.filter((t) => t.milestone_id === waveMs.id);
      const mDone = mTasks.filter((t) => ["APPROVED", "MERGED"].includes(t.status)).length;
      const mCls = waveMs.status === "MERGED" ? "done" : waveMs.status === "FAILED" ? "failed" : mDone > 0 ? "running" : "";
      if (nodesHtml.length) nodesHtml.push(`<span class="evo-arrow">→</span>`);
      nodesHtml.push(`
        <div class="evo-node wave ${mCls}">
          <span class="evo-kicker">执行波次 · ${esc(waveMs.id)}</span>
          <strong class="evo-title">${esc(waveMs.title)}</strong>
          <span class="evo-sub">${mDone}/${mTasks.length} 单通过 · ${esc(waveMs.status)}</span>
        </div>
      `);
    }
    railEl.innerHTML = nodesHtml.join("");
    railEl.querySelectorAll("[data-cp-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.dataset.cpId);
        selectedCheckpointId = selectedCheckpointId === id ? null : id;
        renderSteering(state, { onRefresh, onSelectTask });
      });
    });
  }

  const decDrawer = $("evolution-decisions-drawer");
  if (decDrawer) {
    decDrawer.hidden = !showDecisionsList;
    if (showDecisionsList) {
      decDrawer.innerHTML = decisions.length
        ? `<div class="st-dec-list">` +
          decisions
            .map(
              (d) => `<div class="st-dec-item ${d.status !== "ACTIVE" ? "inactive" : ""}">
                <div><code>[${esc(d.decision_id)}]</code> <strong>${esc(d.title)}</strong> <span class="tag ${d.status === "ACTIVE" ? "ok" : ""}">${esc(d.status)} · v${d.version}</span></div>
                <div class="st-dec-rule">${esc(d.rule_text)}</div>
              </div>`
            )
            .join("") +
          `</div>`
        : `<div class="muted" style="font-size:12px">当前暂无已登记的全局规矩（立项圆桌收敛或阶段评审拍板后自动写入并同步至 LoopForge 全局 Rule）</div>`;
    }
  }

  const detailEl = $("evolution-detail");
  if (detailEl) {
    const sel = checkpoints.find((c) => c.checkpoint_id === selectedCheckpointId);
    detailEl.hidden = !sel;
    if (sel) {
      const appSum = sel.applied_summary || {};
      detailEl.innerHTML = `
        <div class="evo-detail-head">
          <div>
            <strong>评审 #${sel.round_no} · ${esc(sel.trigger_topic)}</strong>
            <span class="tag" style="margin-left:8px">${esc(STATUS_LABELS[sel.status] || sel.status)}</span>
          </div>
          ${sel.loopforge_run_id ? `<button type="button" class="btn btn-sm" data-detail-run="${esc(sel.loopforge_run_id)}">打开 LoopForge 原生并发评审 Run</button>` : ""}
        </div>
        ${sel.human_note ? `<div class="muted" style="font-size:12px;margin-top:4px">负责人诉求：${esc(sel.human_note)}</div>` : ""}
        ${sel.last_error ? `<p>${esc(sel.last_error)}</p>` : ""}
        ${sel.status === "DISPATCH_FAILED" ? '<button type="button" class="btn" data-retry-review>重试发起并发评审</button>' : ""}
        ${renderOpinionsGrid(sel.opinions)}
        ${
          appSum.summary
            ? `<div class="evo-detail-summary">生效结果：${esc(appSum.summary)}（规矩 v${appSum.rule_version || sel.rule_version}，退回 ${(appSum.reopened_tasks || []).join(", ") || "0 单"}，新增 ${(appSum.added_tasks || []).join(", ") || "0 单"}）</div>`
            : sel.status === "RUNNING"
              ? `<div class="muted" style="font-size:12px;margin-top:6px">正在 LoopForge 中并发运行 PM / 架构 / QA 三专家节点并汇总变更单…</div>`
              : ""
        }
      `;
      detailEl.querySelector("[data-retry-review]")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api(`/vernal-api/steering/${sel.checkpoint_id}/retry`, { method: "POST", body: JSON.stringify({actor: actorName()}) });
          await onRefresh?.();
        } catch (err) { toast(err.message); button.disabled = false; }
      });
      detailEl.querySelector("[data-detail-run]")?.addEventListener("click", (e) => {
        jumpToNativeRun(e.currentTarget.dataset.detailRun, sel.loopforge_issue_id);
      });
    }
  }
}
