import { $, actorName, api, esc, projectQuerySuffix, toast } from "./api.js";

let _openReviews = [];
let _selectedReviewId = null;
let _detail = null;
let _activeFileIdx = 0;
let _diffMode = "unified";

function renderUnifiedTable(fileObj, comments) {
  if (fileObj.too_large) {
    return `<div class="empty-hint">该文件变更超过 3000 行，请点击右上角「下载原始 Patch」离线审阅。</div>`;
  }
  const rows = [];
  for (const h of fileObj.hunks || []) {
    rows.push(`<tr class="diff-hunk"><td colspan="3">${esc(h.header)}</td></tr>`);
    for (const ln of h.lines || []) {
      const cls = ln.type === "add" ? "diff-add" : ln.type === "del" ? "diff-del" : "";
      const targetLine = ln.new_no ?? ln.old_no ?? 0;
      const prefix = ln.type === "add" ? "+" : ln.type === "del" ? "-" : " ";
      rows.push(`<tr class="${cls}">
        <td class="review-ln" data-add-cmt-line="${targetLine}" title="点击添加行内评论">${ln.old_no ?? ""}</td>
        <td class="review-ln" data-add-cmt-line="${targetLine}" title="点击添加行内评论">${ln.new_no ?? ""}</td>
        <td>${prefix}${esc(ln.text)}</td>
      </tr>`);
      const lineCmts = comments.filter((c) => c.file === fileObj.path && Number(c.line) === Number(targetLine));
      for (const c of lineCmts) {
        rows.push(`<tr><td colspan="3">
          <div class="review-cmt-card">
            <div class="task-top">
              <div>
                <span class="tag ${c.severity === "blocker" ? "warn" : ""}">${esc(c.severity)}</span>
                <strong>${esc(c.author)}</strong>
                <span class="muted">(${esc(c.source)})</span>
              </div>
              <button type="button" class="btn btn-xs" data-toggle-resolve="${c.comment_id}" data-resolved="${c.resolved ? "1" : "0"}">
                ${c.resolved ? "重新打开" : "标记已解决"}
              </button>
            </div>
            <div style="margin-top:4px;${c.resolved ? "text-decoration:line-through;opacity:0.65" : ""}">${esc(c.body)}</div>
          </div>
        </td></tr>`);
      }
    }
  }
  return `<table class="review-diff-table"><tbody>${rows.join("")}</tbody></table>`;
}

function renderSideBySideTable(fileObj) {
  if (fileObj.too_large) {
    return `<div class="empty-hint">该文件变更超过 3000 行，请下载原始 Patch 查看。</div>`;
  }
  const rows = [];
  for (const h of fileObj.hunks || []) {
    rows.push(`<tr class="diff-hunk"><td colspan="4">${esc(h.header)}</td></tr>`);
    for (const ln of h.lines || []) {
      if (ln.type === "del") {
        rows.push(`<tr>
          <td class="review-ln">${ln.old_no ?? ""}</td>
          <td class="diff-del">-${esc(ln.text)}</td>
          <td class="review-ln"></td>
          <td></td>
        </tr>`);
      } else if (ln.type === "add") {
        rows.push(`<tr>
          <td class="review-ln"></td>
          <td></td>
          <td class="review-ln" data-add-cmt-line="${ln.new_no ?? 0}">${ln.new_no ?? ""}</td>
          <td class="diff-add">+${esc(ln.text)}</td>
        </tr>`);
      } else {
        rows.push(`<tr>
          <td class="review-ln">${ln.old_no ?? ""}</td>
          <td> ${esc(ln.text)}</td>
          <td class="review-ln" data-add-cmt-line="${ln.new_no ?? 0}">${ln.new_no ?? ""}</td>
          <td> ${esc(ln.text)}</td>
        </tr>`);
      }
    }
  }
  return `<table class="review-diff-table"><tbody>${rows.join("")}</tbody></table>`;
}

async function renderWorkbench(refresh, requestedId) {
  const mount = $("review-workbench-mount");
  if (!mount) return;
  try {
    _openReviews = await api("/vernal-api/reviews?status=OPEN");
    if (requestedId) {
      const selected = await api(`/vernal-api/reviews/${requestedId}`);
      if (!_openReviews.some(r => r.review_id === requestedId)) _openReviews.push(selected.review);
      _selectedReviewId = requestedId;
    }
  } catch {
    mount.innerHTML = "";
    return;
  }

  if (!_openReviews.length) {
    mount.innerHTML = "";
    _selectedReviewId = null;
    _detail = null;
    return;
  }

  if (!_selectedReviewId || !_openReviews.some((r) => r.review_id === _selectedReviewId)) {
    _selectedReviewId = _openReviews[0].review_id;
    _activeFileIdx = 0;
  }

  _detail = await api(`/vernal-api/reviews/${_selectedReviewId}`);
  const rev = _detail.review;
  const files = _detail.diff?.files || [];
  const comments = _detail.comments || [];
  if (_activeFileIdx >= files.length) _activeFileIdx = 0;
  const curFile = files[_activeFileIdx] || null;
  const unresolvedBlockers = comments.filter((c) => !c.resolved && c.severity === "blocker");

  const selectorTabs = _openReviews
    .map(
      (r) =>
        `<button type="button" class="btn btn-sm ${r.review_id === rev.review_id ? "btn-primary" : ""}" data-pick-rev="${r.review_id}">
          ${esc(r.scope_id)} · 第 ${r.round} 轮
        </button>`
    )
    .join("");

  const fileListHtml = files.length
    ? files
        .map((f, idx) => {
          const fCmts = comments.filter((c) => c.file === f.path && !c.resolved).length;
          return `<button type="button" class="review-file-btn ${idx === _activeFileIdx ? "active" : ""}" data-rev-file-idx="${idx}">
            <span>${esc(f.path)}</span>
            <span class="muted">+${f.additions}/-${f.deletions}${fCmts ? ` · ${fCmts}` : ""}</span>
          </button>`;
        })
        .join("")
    : `<div class="muted" style="font-size:12px">暂无代码文件变更</div>`;

  const checklistEntries = Object.entries(rev.checklist || {});
  const checklistHtml = checklistEntries.length
    ? checklistEntries
        .map(
          ([label, checked], i) =>
            `<label class="check-inline" style="font-size:12px">
              <input type="checkbox" data-rev-check="${i}" data-label="${esc(label)}" ${checked ? "checked" : ""} />
              <span>${esc(label)}</span>
            </label>`
        )
        .join("")
    : `<span class="muted" style="font-size:12px">无附加检查项</span>`;

  mount.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>人工代码审查工作台（Human-in-the-Loop Code Review）</h3>
          <p class="card-desc">审阅候选变更 Diff、核对验收标准与质量扫描，可逐行添加评论或打回智能体自动返工（快捷键 j/k 切换文件）</p>
        </div>
        <div class="btn-group">
          ${selectorTabs}
          <a class="btn btn-sm" href="/vernal-api/reviews/${rev.review_id}/patch${projectQuerySuffix("?")}" download>下载原始 Patch</a>
        </div>
      </div>

      <pre style="margin-bottom:12px">${esc(rev.ai_summary_md || "")}</pre>

      <div class="review-workbench-grid">
        <div class="stack">
          <div class="review-file-list">${fileListHtml}</div>
          <div class="adr-card">
            <strong style="font-size:12px;display:block;margin-bottom:6px">工程审查清单</strong>
            <div class="stack" style="gap:6px">${checklistHtml}</div>
          </div>
        </div>

        <div class="stack">
          <div class="inline-form" style="justify-content:space-between">
            <strong>${esc(curFile ? curFile.path : "文件预览")}</strong>
            <div class="btn-group">
              <button type="button" class="btn btn-xs ${_diffMode === "unified" ? "btn-primary" : ""}" data-diff-mode="unified">统一视图</button>
              <button type="button" class="btn btn-xs ${_diffMode === "split" ? "btn-primary" : ""}" data-diff-mode="split">并排对比</button>
            </div>
          </div>

          <div style="overflow-x:auto">
            ${curFile ? (_diffMode === "split" ? renderSideBySideTable(curFile) : renderUnifiedTable(curFile, comments)) : `<div class="empty-hint">无差异内容</div>`}
          </div>

          <div id="rev-inline-composer" class="adr-card" hidden>
            <div class="inline-form">
              <span class="tag" id="rev-cmt-target-tag">行号 1</span>
              <select id="rev-cmt-severity">
                <option value="blocker">blocker（阻断合入）</option>
                <option value="major" selected>major（重要问题）</option>
                <option value="minor">minor（改进建议）</option>
                <option value="nit">nit（细节风格）</option>
              </select>
              <input id="rev-cmt-body" type="text" placeholder="输入行内审查意见（打回时自动汇总发给编码智能体）…" />
              <button type="button" id="btn-rev-cmt-save" class="btn btn-primary btn-sm">提交评论</button>
            </div>
          </div>

          <div class="adr-card">
            <div class="inline-form">
              <input id="rev-decision-note" type="text" placeholder="审查总评意见…" />
              ${
                unresolvedBlockers.length
                  ? `<input id="rev-override-reason" type="text" placeholder="存在 ${unresolvedBlockers.length} 条未解 Blocker，强制放行需填豁免理由…" />`
                  : ""
              }
              <button type="button" id="btn-rev-approve" class="btn btn-primary btn-sm" ${
                unresolvedBlockers.length ? 'title="存在未解决的 Blocker 评论，请先解决或填写强制放行理由"' : ""
              }>通过审查</button>
              <button type="button" id="btn-rev-changes" class="btn btn-danger btn-sm">要求修改（自动返工）</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  mount.querySelectorAll("[data-pick-rev]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _selectedReviewId = Number(btn.dataset.pickRev);
      _activeFileIdx = 0;
      renderWorkbench(refresh);
    });
  });

  mount.querySelectorAll("[data-rev-file-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _activeFileIdx = Number(btn.dataset.revFileIdx);
      renderWorkbench(refresh);
    });
  });

  mount.querySelectorAll("[data-diff-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _diffMode = btn.dataset.diffMode;
      renderWorkbench(refresh);
    });
  });

  let pendingLine = 1;
  mount.querySelectorAll("[data-add-cmt-line]").forEach((cell) => {
    cell.addEventListener("click", () => {
      pendingLine = Number(cell.dataset.addCmtLine || 1);
      const box = $("rev-inline-composer");
      const tag = $("rev-cmt-target-tag");
      if (box && tag && curFile) {
        tag.textContent = `${curFile.path}:${pendingLine}`;
        box.hidden = false;
        $("rev-cmt-body")?.focus();
      }
    });
  });

  $("btn-rev-cmt-save")?.addEventListener("click", async () => {
    const body = $("rev-cmt-body")?.value?.trim() || "";
    const severity = $("rev-cmt-severity")?.value || "major";
    if (!body || !curFile) {
      toast("请填写评论内容");
      return;
    }
    try {
      await api(`/vernal-api/reviews/${rev.review_id}/comments`, {
        method: "POST",
        body: JSON.stringify({
          file: curFile.path,
          line: pendingLine,
          severity,
          body,
          actor: actorName(),
        }),
      });
      toast("行内评论已添加");
      await renderWorkbench(refresh);
    } catch (err) {
      toast(err.message);
    }
  });

  mount.querySelectorAll("[data-toggle-resolve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cid = Number(btn.dataset.toggleResolve);
      const nextResolved = btn.dataset.resolved !== "1";
      try {
        await api(`/vernal-api/reviews/comments/${cid}`, {
          method: "PATCH",
          body: JSON.stringify({ resolved: nextResolved }),
        });
        await renderWorkbench(refresh);
      } catch (err) {
        toast(err.message);
      }
    });
  });

  const collectChecklist = () => {
    const out = {};
    mount.querySelectorAll("[data-rev-check]").forEach((chk) => {
      out[chk.dataset.label] = chk.checked;
    });
    return out;
  };

  const submitDecision = async (decision) => {
    const note = $("rev-decision-note")?.value?.trim() || "";
    const overrideReason = $("rev-override-reason")?.value?.trim() || "";
    try {
      await api(`/vernal-api/reviews/${rev.review_id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          note,
          expected_head: rev.head_ref,
          override_reason: overrideReason,
          checklist: collectChecklist(),
          actor: actorName(),
        }),
      });
      toast(decision === "approve" ? "审查已通过，后续依赖已自动解锁" : "已打回并生成结构化返工单");
      await refresh?.();
      await renderWorkbench(refresh);
    } catch (err) {
      toast(`提交失败：${err.message}`);
    }
  };

  $("btn-rev-approve")?.addEventListener("click", () => submitDecision("approve"));
  $("btn-rev-changes")?.addEventListener("click", () => submitDecision("request_changes"));
}

async function renderCIRunsAndDeployments(refresh) {
  const ciMount = $("ci-runs-mount");
  if (!ciMount) return;
  try {
    const [runs, deps] = await Promise.all([api("/vernal-api/ci/runs"), api("/vernal-api/deployments")]);
    if (!runs.length && !deps.length) {
      ciMount.innerHTML = "";
      return;
    }
    const runRows = runs
      .slice(0, 10)
      .map(
        (r) => `<tr>
          <td><code>${esc(r.milestone_id)}</code> · 第 ${r.round} 轮</td>
          <td>${esc(r.provider)}</td>
          <td><span class="status-pill ${esc(r.status === "SUCCESS" ? "PASSED" : r.status === "FAILURE" || r.status === "TIMEOUT" ? "FAILED" : "RUNNING")}">${esc(r.status)}</span></td>
          <td><code>${esc((r.commit_sha || "").slice(0, 10))}</code></td>
          <td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">查看流水线</a>` : "-"}</td>
        </tr>`
      )
      .join("");
    const depRows = deps
      .slice(0, 8)
      .map(
        (d) => `<div class="delivery-row"><code>vernal-${esc(d.milestone_id)}-v${d.version_no}</code> → ${esc(d.target)} · <strong>${esc(d.status)}</strong> (${esc(d.actor)})</div>`
      )
      .join("");
    ciMount.innerHTML = `<div class="card">
      <div class="card-head">
        <div>
          <h3>持续集成 (CI) 与版本部署记录</h3>
          <p class="card-desc">里程碑候选集成分支自动执行测试与质量门禁，失败时自动派发缺陷修复单</p>
        </div>
      </div>
      ${runs.length ? `<table class="data-table"><thead><tr><th>里程碑 / 轮次</th><th>引擎</th><th>状态</th><th>基线指纹</th><th>外部链接</th></tr></thead><tbody>${runRows}</tbody></table>` : ""}
      ${deps.length ? `<div style="margin-top:10px">${depRows}</div>` : ""}
    </div>`;
  } catch {
    ciMount.innerHTML = "";
  }
}

export function initReviewModule({ registerViewHooks, refresh }) {
  document.addEventListener("vernal-open-review", async event => {
    await renderWorkbench(refresh, Number(event.detail));
    $("review-workbench-mount")?.scrollIntoView();
  });
  document.addEventListener("keydown", (ev) => {
    if (!_detail || !["j", "k"].includes(ev.key)) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName || "")) return;
    const files = _detail.diff?.files || [];
    if (!files.length) return;
    if (ev.key === "j") _activeFileIdx = Math.min(files.length - 1, _activeFileIdx + 1);
    if (ev.key === "k") _activeFileIdx = Math.max(0, _activeFileIdx - 1);
    renderWorkbench(refresh);
  });

  registerViewHooks({
    onInboxOpen: (_state, onRefresh) => {
      renderWorkbench(onRefresh || refresh);
      renderCIRunsAndDeployments(onRefresh || refresh);
    },
  });
}
