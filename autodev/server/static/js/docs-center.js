import { $, actorName, api, esc, projectQuerySuffix, toast } from "./api.js";

let _docs = [];
let _selectedPath = "";
let _editing = false;

async function renderDocsCenter(state, onRefresh) {
  const mount = $("docs-center-mount");
  if (!mount) return;
  try {
    _docs = await api("/vernal-api/docs");
  } catch {
    mount.innerHTML = "";
    return;
  }

  if (_docs.length && (!_selectedPath || !_docs.some((d) => d.path === _selectedPath))) {
    _selectedPath = _docs[0].path;
  }

  let detail = null;
  let versions = [];
  if (_selectedPath) {
    try {
      [detail, versions] = await Promise.all([
        api(`/vernal-api/docs/${_selectedPath}`),
        api(`/vernal-api/docs/${_selectedPath}/versions`),
      ]);
    } catch {
      detail = null;
    }
  }

  const treeHtml = _docs.length
    ? _docs
        .map(
          (d) =>
            `<button type="button" class="review-file-btn ${d.path === _selectedPath ? "active" : ""}" data-doc-pick="${esc(d.path)}">
              <span>[${esc(d.kind)}] ${esc(d.path.replace(/^docs\//, ""))}</span>
              <span class="muted">v${d.version} · ${esc(({APPROVED: "已批准", STALE: "已变更待重审", DRAFT: "草稿"})[d.status] || d.status)}</span>
            </button>`
        )
        .join("")
    : `<div class="muted" style="font-size:12px">尚无已归档规范文档，完成立项或点击「生成详细设计」后自动建档。</div>`;

  const activeMs = (state?.milestones || [])[0]?.milestone_id || "M1";
  if (detail?.readonly) _editing = false;

  mount.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>规范化研发文档中心（SRS · 架构说明 · 详细设计 · 测试报告）</h3>
          <p class="card-desc">文档先行驱动编码与审查，支持在线修订留痕、版本对比及一键导出 Word (.docx) 归档包</p>
        </div>
        <div class="btn-group">
          <button type="button" id="btn-gen-dd" class="btn btn-sm">生成 ${esc(activeMs)} 详细设计</button>
          <a class="btn btn-sm" href="/vernal-api/docs/export?format=zip${projectQuerySuffix("&")}" download>导出全套文档 (.docx/.md ZIP)</a>
        </div>
      </div>

      <div class="doc-tree-layout">
        <div class="review-file-list">${treeHtml}</div>
        <div class="stack">
          ${
            detail
              ? `
                <div class="inline-form" style="justify-content:space-between">
                  <div>
                    <strong>${esc(detail.path)}</strong>
                    <span class="tag" style="margin-left:6px">v${detail.version} · ${esc(detail.author)}</span>
                  </div>
                  <div class="btn-group">
                    ${detail.readonly ? '<span class="tag">只读汇总，请编辑功能规格</span>' : `<button type="button" id="btn-doc-edit-toggle" class="btn btn-xs">${_editing ? "取消编辑" : "在线修订"}</button>`}
                    ${
                      versions.length >= 2
                        ? `<button type="button" id="btn-doc-diff" class="btn btn-xs" data-v-from="${versions[1].version}" data-v-to="${versions[0].version}">对比 v${versions[1].version}..v${versions[0].version}</button>`
                        : ""
                    }
                    <a class="btn btn-xs" href="/vernal-api/docs/export?kinds=${encodeURIComponent(detail.kind)}&format=docx${projectQuerySuffix("&")}" download>导出 .docx</a>
                  </div>
                </div>
                ${
                  _editing
                    ? `
                      <textarea id="doc-editor-area" rows="14" style="width:100%;font-family:var(--mono)">${esc(detail.content)}</textarea>
                      <div class="inline-form">
                        <input id="doc-save-note" type="text" placeholder="版本修订说明（例如：补充库存超卖异常处理）…" />
                        <button type="button" id="btn-doc-save" class="btn btn-primary btn-sm">保存为新版本</button>
                      </div>
                    `
                    : `<pre style="max-height:420px;overflow-y:auto">${esc(detail.content)}</pre>`
                }
                <pre id="doc-version-diff-box" hidden></pre>
              `
              : `<div class="empty-hint">选择左侧文档查看或在线修订</div>`
          }
        </div>
      </div>
    </div>
  `;

  mount.querySelectorAll("[data-doc-pick]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _selectedPath = btn.dataset.docPick;
      _editing = false;
      renderDocsCenter(state, onRefresh);
    });
  });

  $("btn-doc-edit-toggle")?.addEventListener("click", () => {
    _editing = !_editing;
    renderDocsCenter(state, onRefresh);
  });

  $("btn-doc-save")?.addEventListener("click", async () => {
    const content = $("doc-editor-area")?.value || "";
    const note = $("doc-save-note")?.value?.trim() || "人工修订";
    try {
      await api(`/vernal-api/docs/${_selectedPath}`, {
        method: "PUT",
        body: JSON.stringify({ content, note, actor: actorName(), expected_sha: detail.content_sha }),
      });
      _editing = false;
      toast("已保存为新文档版本");
      await renderDocsCenter(state, onRefresh);
      await onRefresh?.();
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
  });

  $("btn-doc-diff")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const res = await api(`/vernal-api/docs/${_selectedPath}/diff?from_ver=${btn.dataset.vFrom}&to_ver=${btn.dataset.vTo}`);
    const box = $("doc-version-diff-box");
    if (box) {
      box.textContent = res.diff || "（两个版本无差异）";
      box.hidden = false;
    }
  });

  $("btn-gen-dd")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const res = await api("/vernal-api/docs/design/generate", {
        method: "POST",
        body: JSON.stringify({ milestone_id: activeMs }),
      });
      toast(`已生成详细设计：${(res.generated || []).join(", ")}`);
      await renderDocsCenter(state, onRefresh);
      await onRefresh?.();
    } catch (err) {
      toast(`生成详细设计失败：${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });
}

export function initDocsCenterModule({ registerViewHooks, refresh }) {
  registerViewHooks({
    onCouncilOpen: (state, onRefresh) => {
      renderDocsCenter(state, onRefresh || refresh);
    },
  });
}
