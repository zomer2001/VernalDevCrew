import { $, actorName, api, setActiveProjectId, toast } from "./api.js";

const SKIP_DIRS = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", ".autodev", "dist", "build", ".next"]);
const TEXT_EXTS = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".json", ".md", ".txt", ".html", ".css", ".scss",
  ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".zsh", ".go", ".rs", ".java", ".kt", ".c",
  ".h", ".cpp", ".hpp", ".sql", ".xml", ".svg", ".env", ".gitignore", ".dockerfile", ".vue", ".svelte", ".proto",
]);

function isTextFile(name) {
  const lower = (name || "").toLowerCase();
  if (["makefile", "dockerfile", "readme", "license", "procfile"].includes(lower)) return true;
  const dot = lower.lastIndexOf(".");
  return dot >= 0 && TEXT_EXTS.has(lower.slice(dot));
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function initLauncher({ onProjectReady, onSwitchView }) {
  const panel = $("project-launcher");
  if (!panel) return { open: () => {}, close: () => {}, syncAutoOpen: () => {} };

  let activeMode = "scratch";
  let bindingProject = "";
  const bindingFields = () => bindingProject ? { project_id: bindingProject, bind_existing: true } : {};
  let localSubMode = "host";
  let currentHostPath = "";
  let selectedHostPath = "";
  let selectedRoute = { pool: "public_docker", harness: "dsh" };
  const dismissedProjects = new Set();
  let pendingScratchDocs = [];
  let pendingImportFiles = [];

  function updateRouteSummary() {
    const meta = $("launch-route-live-meta");
    if (!meta) return;
    const st = window.__vernalLastState || null;
    const rs = st?.runner_summary || {};
    const pub = rs.pools?.public_docker || {};
    const localHosts = (rs.pools?.local_runner?.runner_names || rs.runner_names || []).filter(Boolean);
    const pubModel = pub.default_model || "deepseek-flash";
    if (selectedRoute.pool === "public_docker") {
      const hName = selectedRoute.harness === "opencode" ? "OpenCode" : "DeepSeek Harness (dsh)";
      meta.textContent = `生效机器：公用 Docker 沙箱 · 执行器：${hName} · 模型：${pubModel}（公用池额度）`;
    } else if (selectedRoute.pool === "local_runner") {
      meta.textContent = `生效机器：${localHosts.length ? localHosts.join("、") : "本机 Runner"} · 执行器：Codex（本机登录态）`;
    } else {
      meta.textContent = `智能自动：优先本机（${localHosts.join("、") || "检测中"}），离线时回退公用沙箱（${pubModel}）`;
    }
  }

  function setRoute(pool, harness) {
    selectedRoute = { pool: pool || "auto", harness: harness || "auto" };
    $("launch-route-picker")?.querySelectorAll("[data-route-pool]").forEach((btn) => {
      const on = btn.dataset.routePool === selectedRoute.pool && btn.dataset.routeHarness === selectedRoute.harness;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-checked", on ? "true" : "false");
    });
    updateRouteSummary();
  }

  function setScratchCollab(val) {
    const mode = val === "git" ? "git" : "files";
    if ($("launch-scratch-collab")) $("launch-scratch-collab").value = mode;
    $("launch-scratch-collab-seg")?.querySelectorAll("[data-collab-val]").forEach((btn) => {
      const on = btn.dataset.collabVal === mode;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-checked", on ? "true" : "false");
    });
  }

  $("launch-route-picker")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-route-pool]");
    if (btn) setRoute(btn.dataset.routePool, btn.dataset.routeHarness);
  });
  $("launch-scratch-collab-seg")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-collab-val]");
    if (btn) setScratchCollab(btn.dataset.collabVal);
  });

  function setMode(mode) {
    activeMode = mode;
    clearLaunchError();
    panel.querySelectorAll("[data-launch-mode]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.launchMode === mode);
    });
    panel.querySelectorAll("[data-launch-pane]").forEach((pane) => {
      pane.hidden = pane.dataset.launchPane !== mode;
    });
    if (mode === "local_folder" && localSubMode === "host" && !currentHostPath) {
      loadHostDirs("");
    }
    if (mode === "git_repo") {
      loadExistingRepos();
    }
  }

  function setLocalSubMode(sub) {
    localSubMode = sub;
    panel.querySelectorAll("[data-local-sub]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.localSub === sub);
    });
    const hostPane = $("launch-local-host-pane");
    const browserPane = $("launch-local-browser-pane");
    if ($("launch-local-link-pane")) $("launch-local-link-pane").hidden = sub !== "link";
    if (hostPane) hostPane.hidden = sub !== "host";
    if (browserPane) browserPane.hidden = sub !== "browser";
    if (sub === "host" && !currentHostPath) loadHostDirs("");
  }

  const runnerOption = $("link-native-runner");
  if (runnerOption) runnerOption.checked = !/win/i.test(navigator.platform);
  $("link-create-code")?.addEventListener("click", async () => {
    try {
      const pair = await api("/vernal-api/link/create-code", {
        method: "POST",
        body: JSON.stringify({ native_runner: !!runnerOption?.checked }),
      });
      $("link-command").textContent = `vernal link "${location.origin}" --code ${pair.code} --dir "."`;
    } catch (err) {
      toast(err.message);
    }
  });

  function setDisplayMode(kind) {
    panel.classList.toggle("is-modal", kind === "modal");
    panel.classList.toggle("is-hero", kind === "hero");
    const stage = document.getElementById("vernalStage");
    if (stage) stage.classList.toggle("vernal-hero-active", kind === "hero" && !panel.hidden);
  }

  function open(mode, kind = "modal") {
    clearLaunchError();
    if ($("wish-form")) $("wish-form").hidden = true;
    panel.hidden = false;
    setDisplayMode(kind);
    updateRouteSummary();
    setMode(mode || activeMode);
  }

  function close(projectKey = "") {
    bindingProject = "";
    clearLaunchError();
    panel.hidden = true;
    panel.classList.remove("is-modal", "is-hero");
    const stage = document.getElementById("vernalStage");
    if (stage) stage.classList.remove("vernal-hero-active");
    if (projectKey) dismissedProjects.add(projectKey);
  }

  async function loadHostDirs(targetPath = "") {
    const listEl = $("host-dir-list");
    const statusEl = $("host-dir-status");
    const pathInput = $("launch-local-path");
    if (statusEl) statusEl.textContent = "正在读取授权目录…";
    try {
      const q = targetPath ? `?path=${encodeURIComponent(targetPath)}` : "";
      const data = await api(`/vernal-api/host/dirs${q}`);
      if (!data.available) {
        if (statusEl) statusEl.textContent = data.note || "未挂载宿主机目录，请切换到「浏览器上传文件夹」";
        if (listEl) listEl.innerHTML = `<div class="empty-hint">未检测到挂载目录，可切换到上方「浏览器上传文件夹」。</div>`;
        return;
      }
      currentHostPath = data.current;
      if (!selectedHostPath) selectedHostPath = data.current;
      if (pathInput && !pathInput._edited) pathInput.value = selectedHostPath;
      const branchHint = data.current_is_git ? ` · 分支 ${data.current_git_branch || "HEAD"}` : "";
      if (statusEl) {
        statusEl.textContent = `当前路径：${data.current}${branchHint}（授权根目录：${data.display_root}）`;
      }
      const upBtn = $("btn-host-dir-up");
      if (upBtn) {
        upBtn.disabled = !data.parent;
        upBtn.dataset.parentPath = data.parent || "";
      }
      if (!listEl) return;
      if (!data.entries.length) {
        listEl.innerHTML = `<div class="empty-hint">无子目录，可点击上方「选定当前」直接接入该目录。</div>`;
        return;
      }
      listEl.innerHTML = data.entries
        .map((ent) => {
          const isSel = selectedHostPath === ent.path;
          const badges = [
            ent.is_git ? `<span class="tag ok">Git${ent.git_branch ? " · " + esc(ent.git_branch) : ""}</span>` : `<span class="tag">目录</span>`,
            ent.has_autodev ? `<span class="tag">已接入</span>` : "",
            ent.has_readme ? `<span class="tag">README</span>` : "",
          ]
            .filter(Boolean)
            .join("");
          return `
            <div class="host-dir-row ${isSel ? "selected" : ""}" data-dir-path="${esc(ent.path)}" data-dir-name="${esc(ent.name)}" data-dir-branch="${esc(ent.git_branch || "")}">
              <div class="host-dir-main">
                <strong class="host-dir-name">${esc(ent.name)}</strong>
                <div class="host-dir-badges">${badges}</div>
              </div>
              <div class="host-dir-ops">
                <button type="button" class="btn btn-sm" data-dir-pick="${esc(ent.path)}">选定</button>
                <button type="button" class="btn btn-sm" data-dir-enter="${esc(ent.path)}">进入</button>
              </div>
            </div>`;
        })
        .join("");

      listEl.querySelectorAll("[data-dir-pick]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const row = btn.closest(".host-dir-row");
          pickHostPath(row.dataset.dirPath, row.dataset.dirName, row.dataset.dirBranch);
        });
      });
      listEl.querySelectorAll("[data-dir-enter]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          loadHostDirs(btn.dataset.dirEnter);
        });
      });
      listEl.querySelectorAll(".host-dir-row").forEach((row) => {
        row.addEventListener("click", () => {
          pickHostPath(row.dataset.dirPath, row.dataset.dirName, row.dataset.dirBranch);
        });
        row.addEventListener("dblclick", () => {
          loadHostDirs(row.dataset.dirPath);
        });
      });
    } catch (err) {
      if (statusEl) statusEl.textContent = `目录读取失败：${err.message}`;
    }
  }

  function pickHostPath(fullPath, folderName = "", branch = "") {
    selectedHostPath = fullPath;
    const pathInput = $("launch-local-path");
    if (pathInput) {
      pathInput.value = fullPath;
      pathInput._edited = false;
    }
    const nameInput = $("launch-local-name");
    if (nameInput && !nameInput._touched && folderName) {
      nameInput.value = folderName;
    }
    const selHint = $("launch-local-selected-hint");
    if (selHint) {
      selHint.textContent = `已选定：${fullPath}${branch ? "（分支：" + branch + "）" : ""}`;
    }
    panel.querySelectorAll(".host-dir-row").forEach((r) => {
      r.classList.toggle("selected", r.dataset.dirPath === fullPath);
    });
  }

  async function loadExistingRepos() {
    const sel = $("launch-git-existing");
    if (!sel) return;
    try {
      const st = await api("/vernal-api/runtime/status");
      const repos = st.repositories || [];
      if (!repos.length) {
        const raw = await fetch("/api/repositories")
          .then((r) => (r.ok ? r.json() : []))
          .catch(() => []);
        populateRepoSelect(sel, Array.isArray(raw) ? raw : []);
      } else {
        populateRepoSelect(sel, repos);
      }
    } catch {}
  }

  function populateRepoSelect(sel, repos) {
    sel.innerHTML =
      `<option value="">-- 从平台已登记仓库快速填充 --</option>` +
      repos
        .filter((r) => r && (r.cloneUrl || r.repoUrl || r.url))
        .map((r) => {
          const url = r.cloneUrl || r.repoUrl || r.url;
          const br = r.defaultBranch || "main";
          return `<option value="${esc(url)}" data-branch="${esc(br)}" data-name="${esc(r.name || "")}">${esc(r.name || url)}（${esc(br)}）</option>`;
        })
        .join("");
  }

  function clearLaunchError() {
    panel.querySelectorAll("[data-launch-error], [data-resume-binding]").forEach((el) => el.remove());
  }

  function launchError(error, label, submit) {
    const text = label + "：" + error.message;
    toast(text);
    clearLaunchError();
    const foot = submit?.closest(".launcher-foot");
    if (foot) {
      const box = document.createElement("div");
      box.className = "launcher-error";
      box.dataset.launchError = "1";
      box.setAttribute("role", "alert");
      box.textContent = text;
      foot.before(box);
    }
    const info = error.detail;
    if (info?.bind_state !== "PENDING_UPSTREAM" || !info.project_id || !submit) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm";
    button.dataset.resumeBinding = info.project_id;
    button.textContent = "重试绑定";
    submit.after(button);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const res = await api("/vernal-api/project/resume", {
          method: "POST", body: JSON.stringify({ project_id: info.project_id }),
        });
        await finishLaunch(res);
      } catch (err) { launchError(err, "重试绑定失败", submit); }
      finally { button.disabled = false; }
    });
  }

  async function finishLaunch(res) {
    setActiveProjectId(res.project_id);
    dismissedProjects.add(res.project_id);
    panel._manualOpen = false;
    close(res.project_id);
    toast(
      `项目「${res.name}」已就绪` +
        (res.branch && res.collaboration_mode === "git" ? `（Git · ${res.branch}）` : "") +
        (res.delivery_branch_prefix ? ` · 交付分支 ${res.delivery_branch_prefix}<里程碑>` : "") +
        (res.council_started ? " · 已启动架构评审" : "")
    );
    if (typeof window.vernalSwitchProject === "function") {
      await window.vernalSwitchProject(res.project_id, res.name, { skipSelectApi: true });
    } else if (onProjectReady) {
      await onProjectReady(res);
    }
    if (res.needs_wish && $("wish-form")) {
      $("wish-form").hidden = false;
      $("wish-content")?.focus();
      toast("空目录已接入，请录入初始需求规格");
    }
    if (res.council_started && onSwitchView) {
      onSwitchView("council");
    }
  }

  panel.querySelectorAll("[data-launch-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.launchMode));
  });
  panel.querySelectorAll("[data-local-sub]").forEach((btn) => {
    btn.addEventListener("click", () => setLocalSubMode(btn.dataset.localSub));
  });
  $("btn-launcher-close")?.addEventListener("click", () => {
    panel._manualOpen = false;
    close(window.__vernalCurrentProjectKey || "");
  });
  panel.addEventListener("click", (e) => {
    if (e.target === panel && panel.classList.contains("is-modal")) {
      panel._manualOpen = false;
      close(window.__vernalCurrentProjectKey || "");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden && panel.classList.contains("is-modal")) {
      panel._manualOpen = false;
      close(window.__vernalCurrentProjectKey || "");
    }
  });

  $("btn-host-dir-up")?.addEventListener("click", (e) => {
    const p = e.currentTarget.dataset.parentPath;
    if (p) loadHostDirs(p);
  });
  $("btn-host-dir-go")?.addEventListener("click", () => {
    const val = $("launch-local-path")?.value?.trim() || "";
    loadHostDirs(val);
  });
  $("btn-host-dir-pick-current")?.addEventListener("click", () => {
    if (!currentHostPath) return;
    const base = currentHostPath.split("/").filter(Boolean).pop() || "local-project";
    pickHostPath(currentHostPath, base, "");
  });
  $("launch-local-path")?.addEventListener("input", (e) => {
    e.target._edited = true;
    selectedHostPath = e.target.value.trim();
  });
  $("launch-local-name")?.addEventListener("input", (e) => {
    e.target._touched = true;
  });

  $("launch-scratch-docs")?.addEventListener("change", async (e) => {
    pendingScratchDocs = [];
    const files = Array.from(e.target.files || []);
    for (const f of files.slice(0, 12)) {
      if (f.size > 1024 * 1024) continue;
      if (/\.(pdf|docx)$/i.test(f.name)) continue;
      const content = await f.text();
      pendingScratchDocs.push({ filename: f.name, content });
    }
    const hint = $("launch-scratch-docs-hint");
    if (hint) {
      hint.textContent = pendingScratchDocs.length
        ? `已附加 ${pendingScratchDocs.length} 份文档：${pendingScratchDocs.map((d) => d.filename).join("、")}`
        : "支持附加 .md / .txt / .yaml / .proto 文本规格文档（PDF/DOCX 可放入工作区 inputs/ 摄入）";
    }
  });

  $("launch-browser-folder")?.addEventListener("change", async (e) => {
    pendingImportFiles = [];
    const files = Array.from(e.target.files || []);
    let rootName = "";
    let skipped = 0;
    for (const f of files) {
      const rel = f.webkitRelativePath || f.name;
      const parts = rel.split("/").filter(Boolean);
      if (!rootName && parts.length > 1) rootName = parts[0];
      const innerParts = parts.length > 1 ? parts.slice(1) : parts;
      if (innerParts.some((p) => SKIP_DIRS.has(p) || p.startsWith("."))) {
        skipped += 1;
        continue;
      }
      if (!isTextFile(f.name) || f.size > 400 * 1024) {
        skipped += 1;
        continue;
      }
      if (pendingImportFiles.length >= 400) break;
      const content = await f.text();
      pendingImportFiles.push({ path: innerParts.join("/"), content });
    }
    if (rootName && $("launch-browser-name") && !$("launch-browser-name").value.trim()) {
      $("launch-browser-name").value = rootName;
    }
    const hint = $("launch-browser-folder-hint");
    if (hint) {
      hint.textContent = pendingImportFiles.length
        ? `已读取 ${pendingImportFiles.length} 个源码文件（跳过 ${skipped} 个依赖或二进制文件）`
        : "未读取到有效文本源码文件";
    }
  });

  $("launch-git-existing")?.addEventListener("change", (e) => {
    const opt = e.target.selectedOptions?.[0];
    if (!opt || !opt.value) return;
    if ($("launch-git-url")) $("launch-git-url").value = opt.value;
    if ($("launch-git-branch") && opt.dataset.branch) $("launch-git-branch").value = opt.dataset.branch;
    if ($("launch-git-name") && !$("launch-git-name").value.trim() && opt.dataset.name) {
      $("launch-git-name").value = opt.dataset.name;
    }
  });

  $("form-launch-scratch")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("btn-submit-scratch");
    const origTxt = btn?.textContent || "创建项目并开始";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在初始化工作区与流水线…";
    }
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          ...bindingFields(),
          mode: "scratch",
          name: $("launch-scratch-name").value.trim(),
          wish: $("launch-scratch-wish").value.trim(),
          docs: pendingScratchDocs,
          collaboration_mode: $("launch-scratch-collab")?.value || "files",
          process_template: $("launch-scratch-template")?.value || "",
          execution_pool: selectedRoute.pool,
          harness: selectedRoute.harness,
          start_council: Boolean($("launch-scratch-autostart")?.checked),
          actor: actorName(),
        }),
      });
      pendingScratchDocs = [];
      await finishLaunch(res);
    } catch (err) {
      launchError(err, "创建项目失败", btn);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = origTxt;
      }
    }
  });

  $("form-launch-local-host")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const localPath = ($("launch-local-path")?.value || selectedHostPath || "").trim();
    if (!localPath) {
      toast("请先选择或填写本机项目目录");
      return;
    }
    const btn = $("btn-submit-local-host");
    const origTxt = btn?.textContent || "接入该目录";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在扫描代码库并建立隔离副本…";
    }
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          ...bindingFields(),
          mode: "local_folder",
          name: $("launch-local-name")?.value?.trim() || "",
          local_path: localPath,
          wish: $("launch-local-wish")?.value?.trim() || "",
          execution_pool: selectedRoute.pool,
          harness: selectedRoute.harness,
          start_council: Boolean($("launch-local-autostart")?.checked),
          actor: actorName(),
        }),
      });
      await finishLaunch(res);
    } catch (err) {
      launchError(err, "接入目录失败", btn);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = origTxt;
      }
    }
  });

  $("form-launch-local-browser")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingImportFiles.length) {
      toast("请先选择一个本地文件夹");
      return;
    }
    const btn = $("btn-submit-local-browser");
    const origTxt = btn?.textContent || "导入文件夹并创建";
    if (btn) {
      btn.disabled = true;
      btn.textContent = `正在上传 ${pendingImportFiles.length} 个源码文件…`;
    }
    try {
      const res = await api("/vernal-api/project/import-folder", {
        method: "POST",
        body: JSON.stringify({
          ...bindingFields(),
          name: $("launch-browser-name")?.value?.trim() || "导入项目",
          wish: $("launch-browser-wish")?.value?.trim() || "",
          files: pendingImportFiles,
          execution_pool: selectedRoute.pool,
          harness: selectedRoute.harness,
          start_council: Boolean($("launch-browser-autostart")?.checked),
          actor: actorName(),
        }),
      });
      pendingImportFiles = [];
      await finishLaunch(res);
    } catch (err) {
      launchError(err, "导入文件夹失败", btn);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = origTxt;
      }
    }
  });

  $("form-launch-git")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const gitUrl = $("launch-git-url")?.value?.trim() || "";
    if (!gitUrl) {
      toast("请填写 Git 仓库地址");
      return;
    }
    const btn = $("btn-submit-git");
    const origTxt = btn?.textContent || "克隆并接入仓库";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在克隆远端仓库并装配资产…";
    }
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          ...bindingFields(),
          mode: "git_repo",
          name: $("launch-git-name")?.value?.trim() || "",
          git_url: gitUrl,
          git_username: $("launch-git-user")?.value?.trim() || "",
          git_token: $("launch-git-token")?.value?.trim() || "",
          branch: $("launch-git-branch")?.value?.trim() || "",
          wish: $("launch-git-wish")?.value?.trim() || "",
          execution_pool: selectedRoute.pool,
          harness: selectedRoute.harness,
          start_council: Boolean($("launch-git-autostart")?.checked),
          actor: actorName(),
        }),
      });
      await finishLaunch(res);
    } catch (err) {
      launchError(err, "接入 Git 仓库失败", btn);
    } finally {
      if ($("launch-git-token")) $("launch-git-token").value = "";
      if (btn) {
        btn.disabled = false;
        btn.textContent = origTxt;
      }
    }
  });

  function syncAutoOpen(state) {
    if (!state) return;
    const pkey = state.settings?.loopforge_project_id || state.name || "default";
    window.__vernalCurrentProjectKey = pkey;
    const sliceCnt = state.slices ?? state.slices_count ?? 0;
    const isFreshEmpty =
      state.status === "CREATED" &&
      !(state.tasks || []).length &&
      !(state.council_count > 0) &&
      !(sliceCnt > 0) &&
      !state.planning;
    if (isFreshEmpty && !dismissedProjects.has(pkey)) {
      if (panel.hidden) open(activeMode, "hero");
    } else if (!isFreshEmpty && !panel._manualOpen) {
      close();
    }
  }

  return {
    open(mode, projectId = "") {
      bindingProject = projectId;
      panel._manualOpen = true;
      open(mode, "modal");
    },
    close(key) {
      panel._manualOpen = false;
      close(key);
    },
    syncAutoOpen,
  };
}
