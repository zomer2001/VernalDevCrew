import { $, actorName, api, setActiveProjectId, toast } from "./api.js";

const SKIP_DIRS = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", ".autodev", "dist", "build", ".next"]);
const TEXT_EXTS = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".json", ".md", ".txt", ".html", ".css", ".scss",
  ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".zsh", ".go", ".rs", ".java", ".kt", ".c",
  ".h", ".cpp", ".hpp", ".sql", ".xml", ".svg", ".env", ".gitignore", ".dockerfile", ".vue", ".svelte",
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
  let localSubMode = "host"; // host | browser
  let currentHostPath = "";
  let selectedHostPath = "";
  let dismissedProjects = new Set();
  let pendingScratchDocs = [];
  let pendingImportFiles = [];

  function setMode(mode) {
    activeMode = mode;
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
        method: "POST", body: JSON.stringify({native_runner: !!runnerOption?.checked})
      });
      $("link-command").textContent = `python -m autodev.cli link "${location.origin}" --code ${pair.code} --dir "."`;
    } catch (err) { toast(err.message); }
  });

  function setDisplayMode(kind) {
    // kind: "modal"（浮层居中弹窗，点击「＋ 新建 / 接管项目」打开）| "hero"（空白项目居中引导卡）
    panel.classList.toggle("is-modal", kind === "modal");
    panel.classList.toggle("is-hero", kind === "hero");
    const stage = document.getElementById("vernalStage");
    if (stage) stage.classList.toggle("vernal-hero-active", kind === "hero" && !panel.hidden);
  }

  function open(mode, kind = "modal") {
    if ($("agent-config-form")) $("agent-config-form").hidden = true;
    if ($("wish-form")) $("wish-form").hidden = true;
    panel.hidden = false;
    setDisplayMode(kind);
    if (mode) setMode(mode);
    else setMode(activeMode);
  }

  function close(projectKey = "") {
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
    if (statusEl) statusEl.textContent = "正在扫描宿主机目录…";
    try {
      const q = targetPath ? `?path=${encodeURIComponent(targetPath)}` : "";
      const data = await api(`/vernal-api/host/dirs${q}`);
      if (!data.available) {
        if (statusEl) statusEl.textContent = data.note || "宿主机目录未挂载，已自动切换为浏览器导入文件夹";
        if (listEl) listEl.innerHTML = `<div class="muted" style="padding:12px">未检测到挂载目录，可切换到上方「浏览器导入文件夹」。</div>`;
        return;
      }
      currentHostPath = data.current;
      if (!selectedHostPath) selectedHostPath = data.current;
      if (pathInput && !pathInput._edited) pathInput.value = selectedHostPath;
      const branchHint = data.current_is_git ? ` · Git 分支 ${data.current_git_branch || "HEAD"}` : "";
      if (statusEl) {
        statusEl.textContent = `当前浏览：${data.current}${branchHint}（已授权根目录：${data.display_root}；如需接管其他宿主机目录，请用 ./vernal.sh --mount ~/你的代码目录。超过 4 MiB 的源码自动使用 Git 分支交付）`;
      }
      const upBtn = $("btn-host-dir-up");
      if (upBtn) {
        upBtn.disabled = !data.parent;
        upBtn.dataset.parentPath = data.parent || "";
      }
      if (!listEl) return;
      if (!data.entries.length) {
        listEl.innerHTML = `<div class="muted" style="padding:10px">该目录下没有子文件夹，可直接点击「选定当前目录」原地接管。</div>`;
        return;
      }
      listEl.innerHTML = data.entries
        .map((ent) => {
          const isSel = selectedHostPath === ent.path;
          const badges = [
            ent.is_git ? `<span class="tag ok">Git${ent.git_branch ? " · " + esc(ent.git_branch) : ""}</span>` : "",
            ent.has_autodev ? `<span class="tag">已接管</span>` : "",
            ent.has_readme ? `<span class="tag">README</span>` : "",
          ].filter(Boolean).join("");
          return `
            <div class="host-dir-row ${isSel ? "selected" : ""}" data-dir-path="${esc(ent.path)}" data-dir-name="${esc(ent.name)}" data-dir-branch="${esc(ent.git_branch || "")}">
              <div class="host-dir-main">
                <span class="host-dir-icon">${ent.is_git ? "🌿" : "📁"}</span>
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
      selHint.textContent = `已选定：${fullPath}${branch ? "（Git 分支：" + branch + "）" : ""}`;
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
        // 尝试从 LoopForge 原生 API 直接读取已有仓库
        const raw = await fetch("/api/repositories").then((r) => (r.ok ? r.json() : [])).catch(() => []);
        populateRepoSelect(sel, Array.isArray(raw) ? raw : []);
      } else {
        populateRepoSelect(sel, repos);
      }
    } catch {
      // ignore when offline
    }
  }

  function populateRepoSelect(sel, repos) {
    sel.innerHTML = `<option value="">-- 可直接从 LoopForge / Gitea 已有仓库选取 --</option>` +
      repos
        .filter((r) => r && (r.cloneUrl || r.repoUrl || r.url))
        .map((r) => {
          const url = r.cloneUrl || r.repoUrl || r.url;
          const br = r.defaultBranch || "main";
          return `<option value="${esc(url)}" data-branch="${esc(br)}" data-name="${esc(r.name || "")}">${esc(r.name || url)}（${esc(br)}）</option>`;
        })
        .join("");
  }

  async function finishLaunch(res) {
    setActiveProjectId(res.project_id);
    dismissedProjects.add(res.project_id);
    panel._manualOpen = false;
    close(res.project_id);
    toast(
      `项目「${res.name}」已就绪`
      + (res.branch && res.collaboration_mode === "git" ? `（Git · ${res.branch}）` : "")
      + (res.delivery_branch_prefix ? ` · 交付分支 ${res.delivery_branch_prefix}<里程碑>` : "")
      + (res.council_started ? " · 已自动启动架构圆桌" : "")
    );
    if (typeof window.vernalSwitchProject === "function") {
      await window.vernalSwitchProject(res.project_id, res.name, { skipSelectApi: true });
    } else if (onProjectReady) {
      await onProjectReady(res);
    }
    if (res.needs_wish && $("wish-form")) {
      $("wish-form").hidden = false;
      $("wish-content")?.focus();
      toast("空目录已接入，请填写第一条需求");
    }
    if (res.council_started && onSwitchView) {
      onSwitchView("council");
    }
  }

  // Tab switching
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

  // Host dir controls
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

  // Scratch docs attachment
  $("launch-scratch-docs")?.addEventListener("change", async (e) => {
    pendingScratchDocs = [];
    const files = Array.from(e.target.files || []);
    for (const f of files.slice(0, 12)) {
      if (f.size > 512 * 1024) continue;
      const content = await f.text();
      pendingScratchDocs.push({ filename: f.name, content });
    }
    const hint = $("launch-scratch-docs-hint");
    if (hint) {
      hint.textContent = pendingScratchDocs.length
        ? `已附带 ${pendingScratchDocs.length} 份文档：${pendingScratchDocs.map((d) => d.filename).join("、")}`
        : "支持附带 .md / .txt 需求或架构文档";
    }
  });

  // Browser folder upload (webkitdirectory fallback)
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
        ? `已选中 ${pendingImportFiles.length} 个文本源码文件（已自动跳过 ${skipped} 个依赖/二进制文件）`
        : "未读取到可导入的文本源码文件";
    }
  });

  // Existing LoopForge repo quick-fill
  $("launch-git-existing")?.addEventListener("change", (e) => {
    const opt = e.target.selectedOptions?.[0];
    if (!opt || !opt.value) return;
    if ($("launch-git-url")) $("launch-git-url").value = opt.value;
    if ($("launch-git-branch") && opt.dataset.branch) $("launch-git-branch").value = opt.dataset.branch;
    if ($("launch-git-name") && !$("launch-git-name").value.trim() && opt.dataset.name) {
      $("launch-git-name").value = opt.dataset.name;
    }
  });

  // Submit: 1. From scratch
  $("form-launch-scratch")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("btn-submit-scratch");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          mode: "scratch",
          name: $("launch-scratch-name").value.trim(),
          wish: $("launch-scratch-wish").value.trim(),
          docs: pendingScratchDocs,
          collaboration_mode: $("launch-scratch-collab")?.value || "files",
          start_council: Boolean($("launch-scratch-autostart")?.checked),
          actor: actorName(),
        }),
      });
      pendingScratchDocs = [];
      await finishLaunch(res);
    } catch (err) {
      toast(`创建项目失败：${err.message}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // Submit: 2a. Local host folder in-place takeover
  $("form-launch-local-host")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const localPath = ($("launch-local-path")?.value || selectedHostPath || "").trim();
    if (!localPath) {
      toast("请先点选或填写本机项目目录");
      return;
    }
    const btn = $("btn-submit-local-host");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          mode: "local_folder",
          name: $("launch-local-name")?.value?.trim() || "",
          local_path: localPath,
          wish: $("launch-local-wish")?.value?.trim() || "",
          start_council: Boolean($("launch-local-autostart")?.checked),
          actor: actorName(),
        }),
      });
      await finishLaunch(res);
    } catch (err) {
      toast(`接入目录失败：${err.message}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // Submit: 2b. Browser folder upload fallback
  $("form-launch-local-browser")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingImportFiles.length) {
      toast("请先选择一个本地文件夹");
      return;
    }
    const btn = $("btn-submit-local-browser");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/vernal-api/project/import-folder", {
        method: "POST",
        body: JSON.stringify({
          name: $("launch-browser-name")?.value?.trim() || "导入项目",
          wish: $("launch-browser-wish")?.value?.trim() || "",
          files: pendingImportFiles,
          start_council: Boolean($("launch-browser-autostart")?.checked),
          actor: actorName(),
        }),
      });
      pendingImportFiles = [];
      await finishLaunch(res);
    } catch (err) {
      toast(`导入文件夹失败：${err.message}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // Submit: 3. Git repo + branch
  $("form-launch-git")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const gitUrl = $("launch-git-url")?.value?.trim() || "";
    if (!gitUrl) {
      toast("请填写 Git 仓库地址");
      return;
    }
    const btn = $("btn-submit-git");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/vernal-api/project/create", {
        method: "POST",
        body: JSON.stringify({
          mode: "git_repo",
          name: $("launch-git-name")?.value?.trim() || "",
          git_url: gitUrl,
          git_username: $("launch-git-user")?.value?.trim() || "",
          git_token: $("launch-git-token")?.value?.trim() || "",
          branch: $("launch-git-branch")?.value?.trim() || "",
          wish: $("launch-git-wish")?.value?.trim() || "",
          start_council: Boolean($("launch-git-autostart")?.checked),
          actor: actorName(),
        }),
      });
      await finishLaunch(res);
    } catch (err) {
      toast(`接入 Git 仓库失败：${err.message}`);
    } finally {
      if ($("launch-git-token")) $("launch-git-token").value = "";
      if (btn) btn.disabled = false;
    }
  });

  function syncAutoOpen(state) {
    if (!state) return;
    const pkey = state.settings?.loopforge_project_id || state.name || "default";
    window.__vernalCurrentProjectKey = pkey;
    const isFreshEmpty =
      state.status === "CREATED" &&
      !(state.tasks || []).length &&
      !(state.council_count > 0) &&
      !(state.slices_count > 0) &&
      !state.planning;
    if (isFreshEmpty && !dismissedProjects.has(pkey)) {
      if (panel.hidden) open(activeMode, "hero");
    } else if (!isFreshEmpty && !panel._manualOpen) {
      close();
    }
  }

  return {
    open(mode) {
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
