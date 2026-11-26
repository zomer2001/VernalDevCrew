import { $, actorName, api, esc, toast } from "./api.js";
import { selectStage, setProcessData } from "./stage-drilldown.js";

const STATE_LABELS = {
  NOT_STARTED: "未开始",
  IN_PROGRESS: "进行中",
  WAITING_GATE: "待评审放行",
  PASSED: "已通过",
  FAILED: "未通过",
  SKIPPED: "模板跳过",
};

const CSS_STATUS = {
  NOT_STARTED: "",
  IN_PROGRESS: "ACTIVE",
  WAITING_GATE: "AWAITING_GATE",
  PASSED: "PASSED",
  FAILED: "FAILED",
  SKIPPED: "",
};

const BLOCKER_GUIDANCE = {
  TARGET_CHANGED: { text: "文档已有新版本，请审阅当前版本", actionLabel: "查看当前文档", actionView: "council" },
  TARGET_MISSING: { text: "审批目标文档尚未归档，请先生成或归档文档", actionLabel: "打开文档中心", actionView: "council" },
  FILE_MISSING_OR_DRIFTED: { text: "受影响文档缺失或存在未归档外部改动，请先修订或恢复", actionLabel: "进入文档修订/恢复", actionView: "council" },
  OPEN_QUESTIONS: { text: "还有影响交付的关键问题未回答", actionLabel: "前往回答问题", actionView: "council" },
  ASSESSMENT_PENDING: { text: "回答已保存，请重新评估整理规格后再审批", actionLabel: "前往需求面板", actionView: "council" },
  ASSESSMENT_RUNNING: { text: "回答已保存，正在重新整理规格", actionLabel: "", actionView: "" },
  CHECKLIST_RUNNING: { text: "规格质量检查正在进行，完成后可审阅批准", actionLabel: "", actionView: "" },
  MISSION_RUNNING: { text: "当前需求流程正在运行，完成后可审阅批准", actionLabel: "", actionView: "" },
  CHECKLIST_MISSING: { text: "当前版本尚未完成规格质量检查", actionLabel: "继续质量检查", actionView: "council" },
  CHECKLIST_STALE: { text: "质量检查基于旧版本规格，请重新检查", actionLabel: "重新执行检查", actionView: "council" },
  CHECKLIST_FINDINGS_MISSING: { text: "质量检查缺少结构化发现记录，请重新检查", actionLabel: "重新执行检查", actionView: "council" },
  CHECKLIST_CRITICAL_FINDINGS: { text: "规格质量检查存在严重问题，请先修订规格", actionLabel: "查看发现并修订", actionView: "council" },
  PREREQUISITES_UNVERIFIED: { text: "交付前置条件尚未提供核验依据", actionLabel: "补充核验依据", actionView: "council" },
  CONSTITUTION_MISSING: { text: "尚未建立项目宪章", actionLabel: "前往需求面板", actionView: "council" },
  PERMISSION_DENIED: { text: "材料已齐全，但当前账号缺少审批角色权限", actionLabel: "", actionView: "" },
};

export function describeGateState(rec) {
  const blockers = Array.isArray(rec.blockers) ? rec.blockers : [];
  const primary = blockers[0] || null;
  const contentCurrent = rec.content_current !== undefined ? Boolean(rec.content_current) : rec.status !== "STALE";
  const readyForDecision = rec.ready_for_decision !== undefined ? Boolean(rec.ready_for_decision) : rec.status === "PENDING";
  const canApprove = rec.can_approve !== undefined ? Boolean(rec.can_approve) : rec.status === "PENDING" && contentCurrent && readyForDecision;
  const canWaive = rec.can_waive !== undefined ? Boolean(rec.can_waive) : canApprove;
  if (primary) {
    const guide = BLOCKER_GUIDANCE[primary.code] || { text: primary.message || "尚不满足审批条件", actionLabel: "", actionView: "" };
    const featureNote = primary.feature_id ? ` [${primary.feature_id}]` : "";
    return {
      contentCurrent,
      readyForDecision,
      canApprove,
      canWaive,
      code: primary.code || "",
      detail: `${primary.message || guide.text}${featureNote && !(primary.message || "").includes(primary.feature_id) ? featureNote : ""}`,
      actionLabel: guide.actionLabel,
      actionView: guide.actionView,
    };
  }
  if (!contentCurrent || rec.status === "STALE") {
    return {
      contentCurrent: false,
      readyForDecision: false,
      canApprove: false,
      canWaive: false,
      code: "TARGET_CHANGED",
      detail: rec.stale_reason || "文档已有新版本，请审阅当前版本",
      actionLabel: "进入文档修订/恢复",
      actionView: "council",
    };
  }
  return {
    contentCurrent: true,
    readyForDecision: true,
    canApprove,
    canWaive,
    code: "READY",
    detail: "当前版本已满足审批前置条件，可审阅批准",
    actionLabel: "",
    actionView: "",
  };
}

export function renderPendingGateBanner(pendingGate) {
  const rec = pendingGate.rec;
  const info = describeGateState(rec);
  const tagText = !info.contentCurrent ? "版本已变更" : info.canApprove ? "待审阅批准" : "等待前置条件";
  const actionBtn = info.actionLabel && info.actionView
    ? `<button type="button" class="btn btn-sm" data-gate-nav="${esc(info.actionView)}">${esc(info.actionLabel)}</button>`
    : "";
  return `<div class="inline-form" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)" data-gate-blocker-code="${esc(info.code)}">
          <span class="tag warn">${esc(tagText)}</span>
          <strong>${esc(pendingGate.gate.name)}</strong>
          <span class="muted" style="font-size:12px">(${esc(rec.scope_type)}:${esc(rec.scope_id)})</span>
          <span class="muted">评审版本 v${rec.target_version || 0} · ${esc(info.detail)}</span>
          ${actionBtn}
          <input id="quick-gate-comment" type="text" placeholder="签署评审意见（豁免必填）…" />
          <button type="button" class="btn btn-primary btn-sm" ${info.canApprove ? "" : "disabled"} data-gate-decide="pass" data-gate-rec="${rec.record_id}" data-gate-round="${rec.round}" data-gate-sha="${esc(rec.target_sha || "")}">批准放行</button>
          <button type="button" class="btn btn-sm" ${info.canWaive ? "" : "disabled"} data-gate-decide="waive" data-gate-rec="${rec.record_id}" data-gate-round="${rec.round}" data-gate-sha="${esc(rec.target_sha || "")}">记录豁免</button>
        </div>`;
}

async function loadAndRenderPipeline(switchView, refresh) {
  const el = $("pipeline-stepper");
  if (!el) return;
  try {
    const data = await api("/vernal-api/process");
    const stages = data.stages || [];
    if (!stages.length) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    const gbar = $("goal-bar");
    if (gbar) {
      gbar.dataset.replacedByStepper = "1";
      gbar.hidden = true;
    }

    const pendingGates = [];
    for (const s of stages) {
      for (const g of s.gates || []) {
        const latest = new Map();
        for (const rec of g.records || []) {
          const key = `${rec.scope_type}:${rec.scope_id}`;
          if (!latest.has(key) || latest.get(key).round < rec.round) latest.set(key, rec);
        }
        for (const rec of latest.values()) {
          if (["PENDING", "STALE"].includes(rec.status) || (["PASSED", "WAIVED"].includes(rec.status) && rec.approval_valid === false)) {
            pendingGates.push({ stage: s, gate: g, rec });
          }
        }
      }
    }

    const stepsHtml = stages
      .map((s, idx) => {
        const cls = CSS_STATUS[s.status] || "";
        const badge = s.pending_count > 0 ? ` · ${s.pending_count} 待办` : "";
        return `<button type="button" class="pstep ${cls}" data-pstep-id="${esc(s.id)}">
          <span class="pstep-idx">S${idx + 1} · ${esc(STATE_LABELS[s.status] || s.status)}${esc(badge)}</span>
          <span class="pstep-name">${esc(s.name)}</span>
        </button>`;
      })
      .join("");

    const pendingBanner = pendingGates.length ? renderPendingGateBanner(pendingGates[0]) : "";

    el.innerHTML = `
      <div class="pipeline-stepper-head">
        <div>
          <strong style="font-size:13.5px">工程阶段导航 · ${esc(data.template?.name || "Legacy")}</strong>
          <span class="muted" style="font-size:12px;margin-left:8px">点击任意阶段 S1–S7 原地展开详情与实时执行流</span>
        </div>
        <span class="tag ${data.needs_you > 0 ? "warn" : "ok"}">${data.needs_you > 0 ? `${data.needs_you} 项待确认` : "流程正常"}</span>
      </div>
      <div class="pipeline-steps-row">${stepsHtml}</div>
      ${pendingBanner}
    `;

    el.querySelectorAll("[data-pstep-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectStage(btn.dataset.pstepId, { userInitiated: true });
      });
    });

    setProcessData(data);

    el.querySelectorAll("[data-gate-nav]").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchView?.(btn.dataset.gateNav);
      });
    });

    el.querySelectorAll("[data-gate-decide]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const decision = btn.dataset.gateDecide;
        const recId = Number(btn.dataset.gateRec);
        const expectedRound = Number(btn.dataset.gateRound);
        const comment = $("quick-gate-comment")?.value?.trim() || "";
        if (decision === "waive" && !comment) {
          toast("豁免门禁必须填写豁免原因");
          return;
        }
        btn.disabled = true;
        try {
          await api(`/vernal-api/gates/${recId}/decide`, {
            method: "POST",
            body: JSON.stringify({
              decision,
              comment,
              expected_round: expectedRound,
              expected_sha: btn.dataset.gateSha,
              actor: actorName(),
            }),
          });
          toast(decision === "pass" ? "阶段门禁已批准放行" : "阶段门禁已记录豁免");
          await refresh?.();
        } catch (err) {
          toast(`门禁决策失败：${err.message}`);
        } finally {
          btn.disabled = false;
        }
      });
    });
  } catch {
    el.hidden = true;
  }
}

export function initPipelineModule({ registerViewHooks, refresh, switchView }) {
  registerViewHooks({
    onOverviewRender: (_state) => {
      loadAndRenderPipeline(switchView, refresh);
    },
  });
}
