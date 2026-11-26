import { $, actorName, api, esc, toast } from "./api.js";

function ensureCouncilCodeIntelCard() {
  const councilView = document.getElementById("view-council");
  if (!councilView) return null;
  let host = document.getElementById("codeintel-trace-host");
  if (host) return host;
  host = document.createElement("section");
  host.id = "codeintel-trace-host";
  host.className = "panel";
  host.style.marginTop = "16px";
  host.innerHTML = `
    <div class="panel-head">
      <div>
        <h3 style="margin:0;font-size:15px">代码库理解与需求双向追溯矩阵（RTM）</h3>
        <p class="muted" style="margin:4px 0 0;font-size:12px">基于 Personalized PageRank Repo Map 生成代码百科、只读问答，并核验 SRS 需求到测试用例的闭环覆盖</p>
      </div>
      <div class="inline-form" style="gap:8px">
        <button type="button" class="btn btn-sm" id="btn-gen-wiki">生成代码百科与逆向架构</button>
        <a class="btn btn-sm" href="/vernal-api/trace/export?format=csv" target="_blank" rel="noopener">导出追溯 CSV</a>
        <a class="btn btn-sm" href="/vernal-api/trace/export?format=docx" target="_blank" rel="noopener">导出追溯 DOCX</a>
      </div>
    </div>
    <div class="inline-form" style="margin:10px 0;gap:8px">
      <input type="text" id="ask-codebase-input" style="flex:1" placeholder="向存量代码库提问（只读符号图 + 源码行定位），例如：订单状态机在哪里流转？" />
      <button type="button" class="btn btn-primary btn-sm" id="btn-ask-codebase">提问代码库</button>
    </div>
    <div id="ask-codebase-result" class="muted" style="font-size:12px;margin-bottom:10px" hidden></div>
    <div id="trace-matrix-box"></div>
  `;
  councilView.appendChild(host);

  $("btn-gen-wiki")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const res = await api("/vernal-api/wiki/generate", {
        method: "POST",
        body: JSON.stringify({ actor: actorName() }),
      });
      toast(`已生成 ${res.generated_docs?.length || 0} 篇代码百科与架构初稿`);
    } catch (err) {
      toast(`生成失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-ask-codebase")?.addEventListener("click", async (ev) => {
    const q = $("ask-codebase-input")?.value?.trim() || "";
    if (!q) return;
    const btn = ev.currentTarget;
    const box = $("ask-codebase-result");
    btn.disabled = true;
    try {
      const res = await api("/vernal-api/ask", {
        method: "POST",
        body: JSON.stringify({ question: q }),
      });
      if (box) {
        box.hidden = false;
        const cites = (res.citations || [])
          .slice(0, 5)
          .map((c) => `<li><code>${esc(c.file)}:${c.line}</code> — ${esc(c.snippet)}</li>`)
          .join("");
        box.innerHTML = `<div style="padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--surface-2)">
          <div><strong>定位文件：</strong>${esc((res.ranked_files || []).join("、") || "无")}</div>
          ${cites ? `<ul style="margin:6px 0 0;padding-left:18px">${cites}</ul>` : ""}
        </div>`;
      }
    } catch (err) {
      toast(`查询失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });
  return host;
}

async function refreshTraceMatrix() {
  ensureCouncilCodeIntelCard();
  const box = $("trace-matrix-box");
  if (!box) return;
  try {
    const data = await api("/vernal-api/trace/matrix");
    const s = data.summary || {};
    const rows = data.rows || [];
    if (!rows.length) {
      box.innerHTML = `<p class="muted" style="font-size:12px;margin:0">当前工作区暂无结构化 REQ 条目（生成或编辑 SRS.md 后将自动构建双向追溯矩阵）。</p>`;
      return;
    }
    const trs = rows
      .map((r) => {
        const broken = (r.broken_links || []).length > 0;
        const stCls = r.verify_status === "PASSED" ? "ok" : broken ? "warn" : "";
        const testsTxt = (r.tests || []).map((t) => `${t.file}::${t.test}`).join(", ") || "未覆盖";
        return `<tr>
          <td><code>${esc(r.req_id)}</code></td>
          <td><span class="tag">${esc(r.priority)}</span></td>
          <td>${esc(r.title)}</td>
          <td>${esc((r.designs || []).join(", ") || "-")}</td>
          <td>${esc((r.tasks || []).join(", ") || "-")}</td>
          <td><code>${esc(testsTxt)}</code></td>
          <td><span class="tag ${stCls}">${esc(r.verify_status)}</span></td>
        </tr>`;
      })
      .join("");
    box.innerHTML = `
      <div class="inline-form" style="margin-bottom:8px;gap:12px">
        <span class="tag ${s.p0_coverage_rate >= 1 ? "ok" : "warn"}">P0 测试通过率：${s.p0_passed}/${s.p0_total} (${Math.round((s.p0_coverage_rate || 0) * 100)}%)</span>
        <span class="muted" style="font-size:12px">总需求覆盖：${s.covered_requirements}/${s.total_requirements}</span>
      </div>
      <div style="overflow-x:auto">
        <table class="data-table" style="width:100%;font-size:12px;border-collapse:collapse">
          <thead>
            <tr style="text-align:left;border-bottom:1px solid var(--border)">
              <th>REQ</th><th>级别</th><th>标题</th><th>详细设计</th><th>任务</th><th>测试定位</th><th>验证结果</th>
            </tr>
          </thead>
          <tbody>${trs}</tbody>
        </table>
      </div>
    `;
  } catch {}
}

async function refreshGovernancePanel() {
  const mount = $("governance-panel-mount") || $("runtime-panel")?.parentElement;
  if (!mount) return;
  let govCard = $("governance-metrics-card");
  if (!govCard) {
    govCard = document.createElement("div");
    govCard.id = "governance-metrics-card";
    govCard.className = "vset-stack";
    mount.appendChild(govCard);
  }
  try {
    const [m, auditVer, members] = await Promise.all([
      api("/vernal-api/metrics"),
      api("/vernal-api/audit/verify"),
      api("/vernal-api/rbac/members"),
    ]);
    const memberTags = (members || [])
      .map((mb) => `<span class="vset-rt-chip ok"><i></i><span>${esc(mb.user_id)}</span><em>${esc(mb.role)}</em></span>`)
      .join("");
    const passRate = Math.round((m.tasks?.first_pass_ai_review_rate || 0) * 100);
    const ciRate = Math.round((m.ci?.pass_rate || 0) * 100);
    govCard.innerHTML = `
      <section class="vset-card">
        <div class="vset-card-head">
          <div>
            <h3>研发效能度量</h3>
            <p>基于当前项目任务执行、AI 独立审查与 CI 流水线的真实统计</p>
          </div>
          <span class="vset-scope-badge">当前项目 · 只读观测</span>
        </div>
        <div class="vset-kpi-grid">
          <div class="vset-kpi">
            <span>完成任务</span>
            <strong>${m.tasks?.completed || 0} / ${m.tasks?.total || 0}</strong>
            <small>平均返工 ${m.tasks?.avg_rework_rounds || 0} 轮</small>
          </div>
          <div class="vset-kpi">
            <span>AI 一次通过率</span>
            <strong>${passRate}%</strong>
            <small>独立审查门禁首轮通过</small>
          </div>
          <div class="vset-kpi">
            <span>CI 流水线通过率</span>
            <strong>${ciRate}%</strong>
            <small>里程碑集成构建统计</small>
          </div>
          <div class="vset-kpi">
            <span>累计 Token</span>
            <strong>${Number(m.tokens?.total || 0).toLocaleString()}</strong>
            <small>估算成本约 ¥${m.tokens?.estimated_cost_cny || 0}</small>
          </div>
        </div>
      </section>

      <section class="vset-card">
        <div class="vset-card-head">
          <div>
            <h3>审计防篡改与项目研发角色</h3>
            <p>关键审批、模型网关调用与交付事件均写入 SHA-256 哈希链审计日志</p>
          </div>
          <span class="vset-state-pill ${auditVer.valid ? "ok" : "warn"}">${auditVer.valid ? `哈希链完整 (${auditVer.checked_count} 条)` : `检测到篡改 (#${auditVer.tampered_id})`}</span>
        </div>
        <div class="vset-rows">
          <div class="vset-row">
            <div class="vset-row-meta">
              <strong>审计日志归档导出</strong>
              <small>导出当前可访问范围内的不可篡改审计记录（CSV 格式）</small>
            </div>
            <div class="vset-inline-actions">
              <a class="btn btn-sm" href="/vernal-api/audit?format=csv" target="_blank" rel="noopener">导出审计日志 CSV ↗</a>
            </div>
          </div>
          <div class="vset-row">
            <div class="vset-row-meta">
              <strong>项目研发成员角色 (RBAC)</strong>
              <small>叠加在原生项目成员之上，约束架构评审、审批签字与里程碑合入权限</small>
            </div>
            <div class="vset-runtime-list">
              ${memberTags || '<span class="vset-caption">未启用严格 RBAC（单人默认开放模式）</span>'}
            </div>
          </div>
        </div>
      </section>
    `;
  } catch {}
}

export function initGovernanceModule({ registerViewHooks }) {
  registerViewHooks({
    onCouncilOpen: () => {
      refreshTraceMatrix();
    },
    onRuntimeOpen: () => {
      refreshGovernancePanel();
    },
  });
}
