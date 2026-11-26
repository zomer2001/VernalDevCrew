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
        for (const rec of g.records || []) {
          if (rec.status === "PENDING") {
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

    const pendingBanner = pendingGates.length
      ? `<div class="inline-form" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
          <span class="tag warn">当前阻塞门禁</span>
          <strong>${esc(pendingGates[0].gate.name)}</strong>
          <span class="muted" style="font-size:12px">(${esc(pendingGates[0].rec.scope_type)}:${esc(pendingGates[0].rec.scope_id)})</span>
          <input id="quick-gate-comment" type="text" placeholder="签署评审意见（豁免必填）…" />
          <button type="button" class="btn btn-primary btn-sm" data-gate-decide="pass" data-gate-rec="${pendingGates[0].rec.record_id}" data-gate-round="${pendingGates[0].rec.round}">批准放行</button>
          <button type="button" class="btn btn-sm" data-gate-decide="waive" data-gate-rec="${pendingGates[0].rec.record_id}" data-gate-round="${pendingGates[0].rec.round}">记录豁免</button>
        </div>`
      : "";

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
