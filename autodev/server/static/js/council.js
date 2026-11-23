import { $, ROLE_NAMES, actorName, api, esc, toast } from "./api.js";

export async function refreshCouncil(onOpenTrace) {
  const data = await api("/vernal-api/council");
  renderCouncil(data, onOpenTrace);
  return data;
}

export function renderCouncil(data, onOpenTrace) {
  const stream = $("council-stream");
  const adrsEl = $("council-adrs");
  if (!stream || !adrsEl) return;

  const msgs = data?.messages || [];
  if (!msgs.length) {
    stream.innerHTML = `<div class="muted" style="padding:18px;text-align:center">尚无架构评审圆桌记录。点击右上角「发起新一轮圆桌评审」或「圆桌与立项」，PM、契约架构师、QA 总监与研发总监将在此展开 2 轮结构化讨论。</div>`;
    adrsEl.innerHTML = `<div class="muted">圆桌第 2 轮拍板后将在此生成结构化 ADR 架构决策纪要。</div>`;
    return;
  }

  stream.innerHTML = msgs
    .map((m) => {
      const b = m.body || {};
      const role = m.speaker_role || "director";
      const time = (m.created_at || "").slice(11, 19);
      const eng = m.harness ? `<span class="eng-badge ${m.harness === "opencode" ? "opencode" : "codex"}">${esc(m.harness)}/${esc(m.model || "default")}</span>` : "";
      const traceId = role === "human" ? "" : `_council_${role}`;

      if (m.kind === "human") {
        return `<div class="cmsg role-human">
          <div class="cmsg-head">
            <span class="cmsg-speaker">👤 负责人插话 · ${esc(m.speaker_name)}</span>
            <span class="muted">第 ${m.round_no} 轮 · ${esc(time)}</span>
          </div>
          <div>${esc(m.headline)}</div>
        </div>`;
      }

      if (m.kind === "consensus") {
        const debates = (b.resolved_debates || []).map((x) => `<li>${esc(x)}</li>`).join("");
        const risks = (b.open_risks || []).map((x) => `<li>${esc(x)}</li>`).join("");
        return `<div class="cmsg role-director">
          <div class="cmsg-head">
            <span class="cmsg-speaker">⚖️ ${esc(m.speaker_name)} ${eng}</span>
            <span>
              <span class="tag ok">第 ${m.round_no} 轮 · 最终裁决</span>
              <button class="btn btn-sm" data-council-trace="${esc(traceId)}">看推理过程</button>
            </span>
          </div>
          <div class="cmsg-headline">${esc(m.headline)}</div>
          ${b.contract_strategy ? `<p style="margin:0 0 8px;font-size:12.5px"><strong>契约策略：</strong>${esc(b.contract_strategy)}</p>` : ""}
          <div class="cmsg-grid">
            <div class="cmsg-box"><h5>分歧裁决 (${(b.resolved_debates || []).length})</h5><ul>${debates || "<li>无</li>"}</ul></div>
            <div class="cmsg-box"><h5>持续盯防风险 (${(b.open_risks || []).length})</h5><ul>${risks || "<li>无</li>"}</ul></div>
          </div>
        </div>`;
      }

      const props = (b.proposals || []).map((x) => `<li>${esc(x)}</li>`).join("");
      const concerns = (b.concerns || []).map((x) => `<li>${esc(x)}</li>`).join("");
      const qs = (b.questions || []).map((x) => `<li>${esc(x)}</li>`).join("");
      const sbs = (b.recommended_sandboxes || []).map((x) => `<code>${esc(x)}</code>`).join(" ");
      return `<div class="cmsg role-${esc(role)}">
        <div class="cmsg-head">
          <span class="cmsg-speaker">${esc(m.speaker_name)} <span class="muted" style="font-weight:400">(${esc(ROLE_NAMES[role] || role)})</span> ${eng}</span>
          <span>
            <span class="tag">第 ${m.round_no} 轮 · 独立提案</span>
            <button class="btn btn-sm" data-council-trace="${esc(traceId)}">看推理过程</button>
          </span>
        </div>
        <div class="cmsg-headline">${esc(m.headline)}</div>
        <div class="cmsg-grid">
          <div class="cmsg-box"><h5>核心提案</h5><ul>${props || "<li>无</li>"}</ul></div>
          <div class="cmsg-box"><h5>风险与挑刺</h5><ul>${concerns || "<li>无</li>"}</ul></div>
        </div>
        ${qs ? `<div class="cmsg-box" style="margin-top:8px"><h5>抛给圆桌的分歧问题</h5><ul>${qs}</ul></div>` : ""}
        ${sbs ? `<div class="muted" style="margin-top:6px;font-size:12px">建议执行环境 / 沙箱：${sbs}</div>` : ""}
      </div>`;
    })
    .join("");

  stream.querySelectorAll("[data-council-trace]").forEach((btn) => {
    btn.addEventListener("click", () => onOpenTrace(btn.dataset.councilTrace, "trace"));
  });

  const adrs = data?.consensus?.adrs || [];
  if (!adrs.length) {
    adrsEl.innerHTML = `<div class="muted">等待研发总监第 2 轮拍板生成 ADR…</div>`;
  } else {
    adrsEl.innerHTML = adrs
      .map(
        (a) => `<div class="adr-card">
          <div class="adr-id">${esc(a.adr_id)}</div>
          <div style="font-weight:700;margin:2px 0 4px">${esc(a.title)}</div>
          <div style="font-size:12.5px"><strong>裁决：</strong>${esc(a.decision)}</div>
          <div class="muted" style="font-size:12px;margin-top:3px">理由：${esc(a.rationale)}</div>
          ${a.affected_squads?.length ? `<div style="margin-top:5px">${a.affected_squads.map((s) => `<span class="tag">${esc(s)}</span>`).join(" ")}</div>` : ""}
        </div>`
      )
      .join("");
  }
}

export function bindCouncilActions(onRefresh, onOpenTrace) {
  $("btn-run-council")?.addEventListener("click", async () => {
    try {
      await api("/vernal-api/council/run", { method: "POST", body: JSON.stringify({ extra_instructions: "" }) });
      toast("多智能体架构评审圆桌已启动，可在工位席或本页实时观看");
      onRefresh();
    } catch (err) {
      toast(`启动圆桌失败：${err.message}`);
    }
  });

  $("btn-council-interject")?.addEventListener("click", async () => {
    const input = $("council-note");
    const note = input?.value.trim() || "";
    if (!note) return toast("请先输入你的补充要求");
    try {
      await api("/vernal-api/council/interject", {
        method: "POST",
        body: JSON.stringify({ actor: actorName(), note }),
      });
      input.value = "";
      toast("插话已提交，研发总监正在重新裁决 ADR");
      await refreshCouncil(onOpenTrace);
      onRefresh();
    } catch (err) {
      toast(`提交失败：${err.message}`);
    }
  });
}
