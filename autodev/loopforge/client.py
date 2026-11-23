"""LoopForge REST API 客户端（基于 OpenAPI 184 接口，Bearer Token 认证，零侵入上游）。"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def use_environment_proxy(url: str) -> bool:
    """Connect directly to a local LoopForge or Gitea, even when macOS has a proxy."""
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ip_address(host).is_loopback
    except ValueError:
        return True


class LoopForgeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


class LoopForgeClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _req(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=use_environment_proxy(url)) as client:
                resp = await client.request(method, url, headers=self._headers(), json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        if resp.status_code == 204:
            return {"ok": True}
        try:
            data = resp.json()
        except ValueError:
            data = {"text": resp.text}
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else resp.text
            raise LoopForgeError(str(err or f"HTTP {resp.status_code}"), status_code=resp.status_code)
        return data

    async def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            data = await self._req("GET", "/api/health")
            return True, data if isinstance(data, dict) else {"status": "ok"}
        except LoopForgeError as exc:
            return False, {"error": str(exc)}

    async def gitea_health(self, gitea_url: str) -> tuple[bool, dict[str, Any]]:
        url = f"{gitea_url.rstrip('/')}/api/v1/version"
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=use_environment_proxy(url)) as client:
                resp = await client.get(url)
            if resp.status_code < 400:
                return True, resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"ok": True}
            # Gitea 未完成首次安装页时根路径返回 200，API 返回 404/302 也算服务存活
            return resp.status_code in (302, 307, 401, 403, 404), {"status_code": resp.status_code, "note": "已启动（待首次初始化）"}
        except httpx.HTTPError as exc:
            return False, {"error": str(exc)}

    async def list_projects(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/projects")
        return data if isinstance(data, list) else []

    async def create_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str = "",
        workflow_id: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": project_id, "name": name, "description": description}
        if workflow_id:
            body["workflowId"] = workflow_id
        return await self._req("POST", "/api/projects", json_body=body)

    async def list_repositories(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/repositories")
        return data if isinstance(data, list) else []

    async def list_runners(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/runners")
        return data if isinstance(data, list) else []

    async def set_default_runner(self, runner_id: str) -> dict[str, Any]:
        data = await self._req("PUT", "/api/runners/default", json_body={"runnerId": runner_id})
        return data if isinstance(data, dict) else {}

    async def auth_current_with_cookie(self, cookie_header: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/auth/current"
        headers = {"Accept": "application/json"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=use_environment_proxy(url)) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400:
            raise LoopForgeError(str((data or {}).get("error") or f"HTTP {resp.status_code}"), status_code=resp.status_code)
        return data if isinstance(data, dict) else {}

    async def create_token_with_cookie(
        self,
        cookie_header: str,
        *,
        name: str = "Vernal Runtime Bridge",
        expires_in_days: int = 365,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/auth/tokens"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.base_url,
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=use_environment_proxy(url)) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"name": name, "expiresInDays": expires_in_days},
                )
        except httpx.HTTPError as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"text": resp.text}
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else resp.text
            raise LoopForgeError(str(err or f"HTTP {resp.status_code}"), status_code=resp.status_code)
        return data if isinstance(data, dict) else {}

    async def list_workflows(self, project_id: str = "") -> list[dict[str, Any]]:
        params = {"projectId": project_id} if project_id else None
        data = await self._req("GET", "/api/workflows", params=params)
        return data if isinstance(data, list) else []

    async def put_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/workflows", json_body=workflow)
        return data if isinstance(data, dict) else {}

    async def publish_workflow(self, workflow_id: str) -> dict[str, Any]:
        data = await self._req("POST", f"/api/workflows/{quote(workflow_id, safe='')}/publish", json_body={})
        return data if isinstance(data, dict) else {}

    async def list_agents(self, project_id: str = "") -> list[dict[str, Any]]:
        params = {"projectId": project_id} if project_id else None
        data = await self._req("GET", "/api/agents", params=params)
        return data if isinstance(data, list) else []

    async def put_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/agents", json_body=agent)
        return data if isinstance(data, dict) else {}

    async def put_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/rules", json_body=rule)
        return data if isinstance(data, dict) else {"ok": True}

    async def list_skills(self, project_id: str = "") -> list[dict[str, Any]]:
        params = {"projectId": project_id} if project_id else None
        data = await self._req("GET", "/api/skills", params=params)
        return data if isinstance(data, list) else []

    async def put_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/skills", json_body=skill)
        return data if isinstance(data, dict) else {"ok": True}

    async def list_mcp_servers(self, project_id: str = "") -> list[dict[str, Any]]:
        params = {"projectId": project_id} if project_id else None
        data = await self._req("GET", "/api/mcp-servers", params=params)
        return data if isinstance(data, list) else []

    async def put_mcp_server(self, server: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/mcp-servers", json_body=server)
        return data if isinstance(data, dict) else {"ok": True}

    async def list_sandboxes(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/sandboxes")
        return data if isinstance(data, list) else []

    async def list_executors(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/executors")
        return data if isinstance(data, list) else []

    async def list_runs(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/runs")
        return data if isinstance(data, list) else []

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/api/runs/{run_id}")

    async def named_artifact(self, run: dict[str, Any], filename: str) -> str:
        """Read one declared SOP output from the run's artifact index."""
        records = list(run.get("artifacts") or [])
        for node in (run.get("nodes") or {}).values():
            if isinstance(node, dict):
                records.extend(node.get("artifacts") or [])
        for item in reversed(records):
            if not isinstance(item, dict):
                continue
            if not any(str(item.get(k, "")).split("/")[-1] == filename for k in ("name", "path", "filename", "id")):
                continue
            if isinstance(item.get("content"), str) and item["content"]:
                return item["content"]
            aid = item.get("id") or item.get("artifactId")
            if aid:
                result = await self._req("GET", f"/api/runs/{quote(run['id'], safe='')}/artifacts/{quote(aid, safe='')}")
                if isinstance(result, dict):
                    if "files" in result:
                        import json
                        return json.dumps(result)
                    for field in ("content", "text"):
                        if isinstance(result.get(field), str):
                            return result[field]
        raise LoopForgeError(f"Run {run.get('id', '')} 未提供声明的交付文件 {filename}")

    async def get_run_diff(self, run_id: str) -> Any:
        return await self._req("GET", f"/api/runs/{run_id}/workspace/diff")

    async def get_run_logs(self, run_id: str) -> Any:
        return await self._req("GET", f"/api/runs/{run_id}/logs")

    async def get_analytics(self) -> dict[str, Any]:
        data = await self._req("GET", "/api/analytics")
        return data if isinstance(data, dict) else {}

    async def create_issue(
        self,
        *,
        project_id: str,
        title: str,
        description: str,
        workflow_id: str = "",
        repository_id: str = "",
        base_branch: str = "",
        priority: str = "medium",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "projectId": project_id,
            "title": title,
            "description": description,
            "priority": priority,
        }
        if workflow_id:
            body["workflowId"] = workflow_id
        if repository_id:
            body["repositoryId"] = repository_id
        if base_branch:
            body["baseBranch"] = base_branch
        return await self._req("POST", "/api/issues", json_body=body)

    async def update_issue(
        self,
        *,
        issue_id: str,
        project_id: str,
        title: str,
        description: str,
        workflow_id: str = "",
        repository_id: str = "",
        base_branch: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": issue_id,
            "projectId": project_id,
            "title": title,
            "description": description,
        }
        if workflow_id:
            body["workflowId"] = workflow_id
        if repository_id:
            body["repositoryId"] = repository_id
        if base_branch:
            body["baseBranch"] = base_branch
        return await self._req("PUT", "/api/issues", json_body=body)

    async def dispatch_issue(self, issue_id: str, rerun: bool = False) -> dict[str, Any]:
        return await self._req("POST", f"/api/issues/{issue_id}/dispatch", json_body={"rerun": rerun})

    async def approve_run(self, run_id: str, comment: str = "", node_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"comment": comment}
        if node_id:
            body["nodeId"] = node_id
        return await self._req("POST", f"/api/runs/{run_id}/approve", json_body=body)

    async def reject_run(self, run_id: str, reason: str, target_node: str = "", node_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"reason": reason}
        if target_node:
            body["targetNodeId"] = target_node
        if node_id:
            body["nodeId"] = node_id
        return await self._req("POST", f"/api/runs/{run_id}/reject", json_body=body)

    async def retry_run(self, run_id: str, node_id: str = "") -> dict[str, Any]:
        body = {"nodeId": node_id} if node_id else {}
        return await self._req("POST", f"/api/runs/{run_id}/retry", json_body=body)

    async def pause_run(self, run_id: str) -> dict[str, Any]:
        return await self._req("POST", f"/api/runs/{run_id}/pause", json_body={})

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        return await self._req("POST", f"/api/runs/{run_id}/resume", json_body={})

    async def list_pending_approvals(self) -> list[dict[str, Any]]:
        """筛选 LoopForge 中正停在人工审批门禁（waiting）的运行项，聚合进统一收件箱。"""
        runs = await self.list_runs()
        pending: list[dict[str, Any]] = []
        for r in runs:
            if r.get("status") != "waiting":
                continue
            nodes = r.get("nodes") or {}
            wf_nodes = {n.get("id"): n for n in ((r.get("workflowSnapshot") or {}).get("nodes") or [])}
            waiting_nodes = [
                (nid, nr) for nid, nr in nodes.items() if isinstance(nr, dict) and nr.get("status") == "waiting"
            ]
            for nid, nr in waiting_nodes or [("", {})]:
                wn = wf_nodes.get(nid, {})
                pending.append(
                    {
                        "source": "loopforge",
                        "run_id": r.get("id", ""),
                        "issue_id": r.get("issueId", ""),
                        "workflow_id": r.get("workflowId", ""),
                        "workflow_name": (r.get("workflowSnapshot") or {}).get("name") or r.get("workflowId", ""),
                        "node_id": nid,
                        "node_name": wn.get("name") or nid or "人工门禁",
                        "reject_to": wn.get("rejectTo", ""),
                        "updated_at": nr.get("startedAt") or r.get("updatedAt") or "",
                    }
                )
        return pending
