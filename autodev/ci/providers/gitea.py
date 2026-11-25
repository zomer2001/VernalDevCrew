"""Gitea Actions CI 适配器（WP-3：隔离分支/提交/非破坏性 workflow 发布与状态对账）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

import httpx

from autodev import gitops
from autodev.ci.providers.base import CIHandle, CIStateLiteral, CIStatus
from autodev.loopforge.client import create_async_http_client

_GITEA_STATE_MAP: dict[str, CIStateLiteral] = {
    "pending": "RUNNING",
    "running": "RUNNING",
    "waiting": "QUEUED",
    "success": "SUCCESS",
    "failure": "FAILURE",
    "error": "FAILURE",
    "cancelled": "CANCELLED",
}

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class GiteaCIProvider:
    name = "gitea"

    def __init__(
        self,
        base_url: str,
        *,
        owner: str = "vernal-local",
        token: str = "",
        auth: tuple[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.token = token
        self.auth = auth
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return create_async_http_client(
            self.base_url,
            transport=self.transport,
            base_url=self.base_url,
            headers=headers,
            auth=self.auth,
            timeout=15.0,
        )

    async def trigger(self, project: str, ref: str, sha: str, ctx: dict[str, Any]) -> CIHandle:
        clean_sha = (sha or "").strip()
        if not _GIT_SHA_RE.fullmatch(clean_sha):
            raise ValueError(f"Gitea CI 触发要求真实的 40 位 Git Commit SHA，拒绝非提交摘要：{clean_sha[:24]}")

        repo_dir_raw = str(ctx.get("repo_dir") or "").strip()
        published_remote = ""
        if repo_dir_raw:
            repo_dir = Path(repo_dir_raw)
            resolved = gitops.git(["rev-parse", f"refs/heads/{ref}"], repo_dir).stdout.strip()
            if resolved != clean_sha:
                raise ValueError(f"隔离 CI 分支 refs/heads/{ref} 头提交 ({resolved[:12]}) 与预期 SHA ({clean_sha[:12]}) 不一致")
            wf_check = gitops.git(["cat-file", "-e", f"{clean_sha}:.gitea/workflows/vernal-ci.yml"], repo_dir, check=False)
            if wf_check.returncode != 0:
                raise ValueError(f"隔离 CI 提交 {clean_sha[:12]} 缺少已渲染的 .gitea/workflows/vernal-ci.yml")

            if ctx.get("gitea_mirror"):
                from autodev import local_git_bridge

                await local_git_bridge.ensure_private_repo(project)
                await asyncio.to_thread(local_git_bridge.deliver, project, repo_dir, ref, clean_sha, "")
                published_remote = "gitea_mirror"
            elif ctx.get("private_remote"):
                from autodev import local_git_bridge

                await asyncio.to_thread(
                    local_git_bridge.publish_private_remote,
                    repo_dir,
                    str(ctx["private_remote"]),
                    ref,
                    clean_sha,
                    "",
                )
                published_remote = "private_remote"
            elif ctx.get("git_remote"):
                git_remote = str(ctx["git_remote"]).strip()
                await asyncio.to_thread(
                    gitops.git,
                    ["push", "--force", "--", git_remote, f"{clean_sha}:refs/heads/{ref}"],
                    repo_dir,
                )
                published_remote = git_remote

        url = f"{self.base_url}/{self.owner}/{project}/actions"
        if self.base_url:
            try:
                async with self._client() as client:
                    await client.post(
                        f"/api/v1/repos/{self.owner}/{project}/statuses/{clean_sha}",
                        json={
                            "state": "pending",
                            "context": "vernal-ci / verify",
                            "description": f"Triggered on {ref}",
                            "target_url": url,
                        },
                    )
            except (httpx.HTTPError, RuntimeError):
                pass

        extra_out = {**ctx, "published_remote": published_remote}
        return CIHandle(
            provider="gitea",
            project=project,
            ref=ref,
            commit_sha=clean_sha,
            external_id=f"{ref}@{clean_sha[:12]}",
            url=url,
            extra=extra_out,
        )

    async def poll(self, handle: CIHandle) -> CIStatus:
        path = f"/api/v1/repos/{self.owner}/{handle.project}/commits/{handle.commit_sha}/status"
        async with self._client() as client:
            resp = await client.get(path)
            if resp.status_code == 404:
                return CIStatus(status="QUEUED", url=handle.url)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        raw_state = str(data.get("state") or "pending").lower()
        statuses = data.get("statuses") or []
        target_url = handle.url
        failed_ctx = ""
        for item in statuses:
            if isinstance(item, dict):
                if item.get("target_url"):
                    target_url = str(item["target_url"])
                if str(item.get("status") or "").lower() in ("failure", "error"):
                    failed_ctx = str(item.get("context") or item.get("description") or "")
        if not statuses and raw_state == "pending":
            return CIStatus(status="QUEUED", url=target_url)
        mapped = _GITEA_STATE_MAP.get(raw_state, "RUNNING")
        return CIStatus(
            status=mapped,
            url=target_url,
            summary={"raw_state": raw_state, "failed_step": failed_ctx, "checks": len(statuses)},
        )

    async def fetch_log_tail(self, handle: CIHandle, lines: int = 150) -> str:
        path = f"/api/v1/repos/{self.owner}/{handle.project}/commits/{handle.commit_sha}/logs"
        try:
            async with self._client() as client:
                resp = await client.get(path)
                if resp.status_code == 200 and resp.text.strip():
                    tail = resp.text.splitlines()[-lines:]
                    return "\n".join(tail)
        except (httpx.HTTPError, RuntimeError):
            pass
        return ""
