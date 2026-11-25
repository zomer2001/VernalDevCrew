"""通用 Webhook / GitLab CI / Jenkins CI 外部流水线适配器（WP-3；外部实例待真实环境联调验证）。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from autodev.ci.providers.base import CIHandle, CIStatus
from autodev.loopforge.client import create_async_http_client


class WebhookCIProvider:
    name = "webhook"

    def __init__(self, webhook_url: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.webhook_url = webhook_url
        self.token = token
        self.transport = transport

    async def trigger(self, project: str, ref: str, sha: str, ctx: dict[str, Any]) -> CIHandle:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with create_async_http_client(
            self.webhook_url,
            transport=self.transport,
            timeout=15.0,
        ) as client:
            resp = await client.post(
                self.webhook_url,
                headers=headers,
                json={"project": project, "ref": ref, "commit_sha": sha, **ctx},
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        return CIHandle(
            provider="webhook",
            project=project,
            ref=ref,
            commit_sha=sha,
            external_id=str(data.get("id") or ""),
            url=str(data.get("url") or ""),
        )

    async def poll(self, handle: CIHandle) -> CIStatus:
        return CIStatus(status="RUNNING", url=handle.url)

    async def fetch_log_tail(self, handle: CIHandle, lines: int = 150) -> str:
        return ""


class GitLabCIProvider:
    """GitLab Pipelines API 适配器（MockTransport 已测，待真实 GitLab 实例环境验证）。"""

    name = "gitlab"

    def __init__(self, base_url: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
        return create_async_http_client(
            self.base_url,
            transport=self.transport,
            base_url=self.base_url,
            headers=headers,
            timeout=15.0,
        )

    async def trigger(self, project: str, ref: str, sha: str, ctx: dict[str, Any]) -> CIHandle:
        enc = quote(project, safe="")
        async with self._client() as client:
            resp = await client.post(f"/api/v4/projects/{enc}/pipeline", json={"ref": ref})
            resp.raise_for_status()
            data = resp.json()
        return CIHandle(
            provider="gitlab",
            project=project,
            ref=ref,
            commit_sha=sha,
            external_id=str(data.get("id") or ""),
            url=str(data.get("web_url") or ""),
        )

    async def poll(self, handle: CIHandle) -> CIStatus:
        if not handle.external_id:
            raise ValueError("GitLab CI 轮询缺少持久化 pipeline external_id")
        enc = quote(handle.project, safe="")
        async with self._client() as client:
            resp = await client.get(f"/api/v4/projects/{enc}/pipelines/{handle.external_id}")
            resp.raise_for_status()
            data = resp.json()
        st = str(data.get("status") or "running").lower()
        mapping = {
            "pending": "QUEUED",
            "created": "QUEUED",
            "running": "RUNNING",
            "success": "SUCCESS",
            "failed": "FAILURE",
            "canceled": "CANCELLED",
        }
        return CIStatus(status=mapping.get(st, "RUNNING"), url=str(data.get("web_url") or handle.url))  # type: ignore[arg-type]

    async def fetch_log_tail(self, handle: CIHandle, lines: int = 150) -> str:
        return ""


class JenkinsCIProvider:
    """Jenkins buildWithParameters 适配器（MockTransport 已测，待真实 Jenkins 实例环境验证）。"""

    name = "jenkins"

    def __init__(self, job_url: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.job_url = job_url.rstrip("/")
        self.token = token
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return create_async_http_client(
            self.job_url,
            transport=self.transport,
            headers=headers,
            timeout=15.0,
        )

    async def trigger(self, project: str, ref: str, sha: str, ctx: dict[str, Any]) -> CIHandle:
        async with self._client() as client:
            resp = await client.post(
                f"{self.job_url}/buildWithParameters",
                params={"PROJECT": project, "REF": ref, "SHA": sha},
            )
            resp.raise_for_status()
            queue_url = resp.headers.get("Location") or f"{self.job_url}/lastBuild"
        return CIHandle(
            provider="jenkins",
            project=project,
            ref=ref,
            commit_sha=sha,
            external_id=queue_url,
            url=queue_url,
        )

    async def poll(self, handle: CIHandle) -> CIStatus:
        target_base = (handle.external_id or handle.url).rstrip("/")
        api_url = f"{target_base}/api/json"
        async with self._client() as client:
            resp = await client.get(api_url)
            if resp.status_code == 404:
                return CIStatus(status="QUEUED", url=handle.url)
            resp.raise_for_status()
            data = resp.json()
        if data.get("building"):
            return CIStatus(status="RUNNING", url=str(data.get("url") or handle.url))
        res = str(data.get("result") or "").upper()
        if res == "SUCCESS":
            return CIStatus(status="SUCCESS", url=str(data.get("url") or handle.url))
        if res in ("FAILURE", "UNSTABLE"):
            return CIStatus(status="FAILURE", url=str(data.get("url") or handle.url))
        if res == "ABORTED":
            return CIStatus(status="CANCELLED", url=str(data.get("url") or handle.url))
        return CIStatus(status="QUEUED", url=handle.url)

    async def fetch_log_tail(self, handle: CIHandle, lines: int = 150) -> str:
        target_base = (handle.external_id or handle.url).rstrip("/")
        try:
            async with self._client() as client:
                resp = await client.get(f"{target_base}/consoleText")
                if resp.status_code == 200:
                    return "\n".join(resp.text.splitlines()[-lines:])
        except (httpx.HTTPError, RuntimeError):
            pass
        return ""
