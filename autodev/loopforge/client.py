"""LoopForge REST API 客户端（基于 OpenAPI 184 接口，Bearer Token 认证，零侵入上游）。"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def use_environment_proxy(url: str) -> bool:
    """内网/回环/容器服务直连绕过宿主机代理，公网外部地址严格遵循环境代理合约。"""
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    if not host or "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return False
    except ValueError:
        pass
    return True


def create_async_http_client(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """统一构造 httpx.AsyncClient：保留内外网代理合约；要求 SOCKS 代理却缺少 socksio 时给出清晰可操作报错。"""
    trust_env = False if transport is not None else use_environment_proxy(url)
    if transport is not None:
        kwargs["transport"] = transport
    try:
        return httpx.AsyncClient(trust_env=trust_env, **kwargs)
    except ImportError as exc:
        if "socksio" in str(exc).lower():
            raise RuntimeError(
                f"当前环境配置了 SOCKS 代理访问外部地址 ({url})，但缺少 socksio 依赖；"
                "请执行 `pip install 'httpx[socks]'`（或 `pip install socksio`），或在离线测试中显式注入 transport"
            ) from exc
        raise


class LoopForgeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


def run_project_id(run: dict) -> str:
    context = run.get("executionContext") or {}
    stamped, flat = context.get("projectId"), run.get("projectId")
    if stamped and flat and stamped != flat:
        return ""
    return str(stamped or flat or "")


class SessionCookie(str):
    """Verified browser credential. Never serialize as a service credential."""


class LoopForgeClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 15.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token if isinstance(token, SessionCookie) else token.strip()
        self.timeout = timeout
        self.transport = transport
        self.approval_user_id = None
        self.approval_workflow = ""
        self.issue_user_id: int | str | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if isinstance(self.token, SessionCookie):
            h["Cookie"] = str(self.token)
            h["Origin"] = self.base_url
        elif self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _req(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with create_async_http_client(url, timeout=self.timeout, transport=self.transport) as client:
                resp = await client.request(method, url, headers=self._headers(), json=json_body, params=params)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        if method == "POST" and path.endswith("/retry") and resp.status_code == 202:
            raise LoopForgeError("原生工作区仍在恢复，重试尚未确认", 409)
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

    async def check_telemetry_schema(self, run_id: str) -> dict[str, Any]:
        """Read-only probe distinguishing LoopForge liveness from run_telemetry schema compatibility."""
        rid = (run_id or "").strip()
        if not rid:
            return {"checked": False, "compatible": None, "code": "", "reason": "暂无历史运行记录可供数据结构检查"}
        endpoint = f"/api/runs/{quote(rid, safe='')}/telemetry"
        try:
            await self._req("GET", endpoint, params={"limit": "1", "adapter": "live"})
            return {"checked": True, "compatible": True, "status_code": 200, "endpoint": endpoint, "code": "", "reason": ""}
        except LoopForgeError as exc:
            msg = str(exc)
            low = msg.lower()
            if "1054" in low and "adapter" in low:
                return {
                    "checked": True,
                    "compatible": False,
                    "status_code": exc.status_code,
                    "endpoint": endpoint,
                    "code": "UPSTREAM_SCHEMA_COLUMN_MISSING",
                    "table": "run_telemetry",
                    "column": "adapter",
                    "reason": msg,
                }
            return {
                "checked": True,
                "compatible": False,
                "status_code": exc.status_code,
                "endpoint": endpoint,
                "code": "UPSTREAM_TELEMETRY_ERROR",
                "reason": msg,
            }

    async def gitea_health(self, gitea_url: str) -> tuple[bool, dict[str, Any]]:
        url = f"{gitea_url.rstrip('/')}/api/v1/version"
        try:
            async with create_async_http_client(url, timeout=4.0) as client:
                resp = await client.get(url)
            if resp.status_code < 400:
                return True, resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"ok": True}
            # Gitea 未完成首次安装页时根路径返回 200，API 返回 404/302 也算服务存活
            return resp.status_code in (302, 307, 401, 403, 404), {"status_code": resp.status_code, "note": "已启动（待首次初始化）"}
        except (httpx.HTTPError, RuntimeError) as exc:
            return False, {"error": str(exc)}

    async def list_projects(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/projects")
        return data if isinstance(data, list) else []

    async def list_users(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/users")
        return data if isinstance(data, list) else []

    async def create_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str = "",
        workflow_id: str = "",
        default_repository_id: str = "",
        members: list[dict] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": project_id, "name": name, "description": description}
        if members:
            body["members"] = members
        if workflow_id:
            body["workflowId"] = workflow_id
        if default_repository_id:
            body["defaultRepositoryId"] = default_repository_id
        return await self._req("POST", "/api/projects", json_body=body)

    async def put_project(self, project: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/projects", json_body=project)
        return data if isinstance(data, dict) else {}

    async def list_repositories(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/repositories")
        return data if isinstance(data, list) else []

    async def put_repository(self, repo: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("PUT", "/api/repositories", json_body=repo)
        return data if isinstance(data, dict) else {}

    async def auth_current(self) -> dict[str, Any]:
        data = await self._req("GET", "/api/auth/current")
        return data if isinstance(data, dict) else {}

    async def save_user_codebuddy_key(
        self,
        user_id: int | str,
        api_key: str,
        internet_env: str = "",
        *,
        cookie_header: str = "",
    ) -> dict[str, Any]:
        path = f"/api/users/{quote(str(user_id), safe='')}/credentials"
        body: dict[str, Any] = {
            "codeBuddyApiKey": api_key.strip(),
            "codeBuddyInternetEnvironment": internet_env.strip(),
        }
        if cookie_header:
            url = f"{self.base_url}{path}"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "Cookie": cookie_header,
            }
            try:
                async with create_async_http_client(url, timeout=self.timeout) as client:
                    resp = await client.put(url, headers=headers, json=body)
            except (httpx.HTTPError, RuntimeError) as exc:
                raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400:
                raise LoopForgeError(str((data or {}).get("error") or f"HTTP {resp.status_code}"), status_code=resp.status_code)
            return data if isinstance(data, dict) else {"ok": True}
        data = await self._req("PUT", path, json_body=body)
        return data if isinstance(data, dict) else {"ok": True}

    async def list_runners(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/runners")
        return data if isinstance(data, list) else []

    async def list_runners_with_cookie(self, cookie: str) -> list[dict[str, Any]]:
        try:
            async with create_async_http_client(self.base_url, timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/api/runners", headers={"Cookie": cookie})
                response.raise_for_status()
        except (httpx.HTTPError, RuntimeError) as exc:
            raise LoopForgeError(f"无法读取当前登录账号的执行机：{exc}") from exc
        rows = response.json()
        return rows if isinstance(rows, list) else []

    async def set_default_runner(self, runner_id: str) -> dict[str, Any]:
        data = await self._req("PUT", "/api/runners/default", json_body={"runnerId": runner_id})
        return data if isinstance(data, dict) else {}

    async def create_runner_bind_code(self) -> dict[str, Any]:
        data = await self._req("POST", "/api/runners/bind-codes", json_body={})
        return data if isinstance(data, dict) else {}

    async def auth_current_with_cookie(self, cookie_header: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/auth/current"
        headers = {"Accept": "application/json"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with create_async_http_client(url, timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400:
            raise LoopForgeError(str((data or {}).get("error") or f"HTTP {resp.status_code}"), status_code=resp.status_code)
        return data if isinstance(data, dict) else {}

    async def list_projects_with_cookie(self, cookie_header: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/projects"
        headers = {"Accept": "application/json"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with create_async_http_client(url, timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise LoopForgeError(f"无法连接 LoopForge ({url}): {exc}") from exc
        if resp.status_code >= 400:
            raise LoopForgeError(f"HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

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
            async with create_async_http_client(url, timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"name": name, "expiresInDays": expires_in_days},
                )
        except (httpx.HTTPError, RuntimeError) as exc:
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

    async def project_run(self, run_id: str, project_id: str) -> dict[str, Any]:
        run = await self.get_run(run_id)
        context = run.get("executionContext") or {}
        owner = run_project_id(run)
        if not owner and not context.get("projectId") and not run.get("projectId") and context.get("issueId"):
            detail = await self.issue_detail(context["issueId"])
            issue = detail.get("issue", {})
            owner = issue.get("projectId") if issue.get("id") == context["issueId"] else ""
        if not project_id or owner != project_id or run.get("id") != run_id:
            raise LoopForgeError("运行不属于当前项目", 403)
        return run

    async def named_artifact(self, run: dict[str, Any], filename: str, *, node_id: str = "") -> str:
        """Read one declared SOP output from the run's artifact index."""
        records = [a for a in run.get("artifacts") or []
                   if not node_id or isinstance(a, dict) and a.get("nodeId") == node_id]
        nodes = run.get("nodes") or {}
        for current_id, node in nodes.items():
            if node_id and current_id != node_id:
                continue
            if isinstance(node, dict):
                records.extend(node.get("artifacts") or [])
        for item in reversed(records):
            if not isinstance(item, dict):
                continue
            if not any(str(item.get(k, "")).split("/")[-1] == filename for k in ("name", "path", "filename", "id")):
                continue
            if node_id:
                node = nodes.get(node_id) or {}
                if item.get("nodeId", node_id) != node_id:
                    continue
                if node.get("attempt") is not None and item.get("attempt") != node["attempt"]:
                    continue
                if node.get("reworkCount") or (node.get("startedAt") and item.get("createdAt")):
                    try:
                        if datetime.fromisoformat(item["createdAt"]) < datetime.fromisoformat(node["startedAt"]):
                            continue
                    except (KeyError, TypeError, ValueError):
                        continue
            if isinstance(item.get("content"), str) and item["content"]:
                return item["content"]
            aid = item.get("id") or item.get("artifactId")
            if aid:
                result = await self._req("GET", f"/api/runs/{quote(run['id'], safe='')}/artifacts/{quote(aid, safe='')}")
                if isinstance(result, dict):
                    if set(result.keys()) <= {"content", "text", "id", "name", "mediaType"}:
                        for field in ("content", "text"):
                            if isinstance(result.get(field), str):
                                return result[field]
                    import json

                    return json.dumps(result, ensure_ascii=False)
                if isinstance(result, str) and result:
                    return result
        raise LoopForgeError(f"Run {run.get('id', '')} 未提供声明的交付文件 {filename}")

    async def get_run_diff(self, run_id: str) -> Any:
        return await self._req("GET", f"/api/runs/{run_id}/workspace/diff")

    async def get_run_logs(self, run_id: str) -> Any:
        return await self._req("GET", f"/api/runs/{run_id}/logs")

    async def get_analytics(self, project_id: str = "") -> dict[str, Any]:
        data = await self._req("GET", "/api/analytics", params={"projectId": project_id} if project_id else None)
        return data if isinstance(data, dict) else {}

    def issue_bindings(self, workflow_id):
        steps = [{"id": "development", "workflowId": workflow_id, "order": 1, "assigneeUserId": int(self.issue_user_id)}]
        if self.approval_user_id and self.approval_workflow and workflow_id != self.approval_workflow:
            steps.append({"id": "approval", "workflowId": self.approval_workflow, "order": 2,
                          "assigneeUserId": int(self.approval_user_id), "dependsOn": ["development"],
                          "handoff": ["review-report.md"]})
        return steps

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
        if self.issue_user_id is not None:
            body["assigneeUserId"] = self.issue_user_id
            if workflow_id:
                body["sopBindings"] = self.issue_bindings(workflow_id)
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
        status: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": issue_id,
            "projectId": project_id,
            "title": title,
            "description": description,
        }
        if self.issue_user_id is not None:
            body["assigneeUserId"] = self.issue_user_id
            if workflow_id:
                body["sopBindings"] = self.issue_bindings(workflow_id)
        if workflow_id:
            body["workflowId"] = workflow_id
        if repository_id:
            body["repositoryId"] = repository_id
        if base_branch:
            body["baseBranch"] = base_branch
        if status:
            body["status"] = status
        return await self._req("PUT", "/api/issues", json_body=body)

    async def issue_detail(self, issue_id: str) -> dict[str, Any]:
        data = await self._req("GET", f"/api/issues/{quote(issue_id, safe='')}")
        if not isinstance(data, dict):
            raise LoopForgeError("LoopForge 返回无效的工单详情")
        return data

    async def dispatch_issue(self, issue_id: str, rerun: bool = False, *, workflow_id: str = "") -> dict[str, Any]:
        if self.issue_user_id is not None:
            # LoopForge selects provider credentials by issue responsibility, with project-owner fallback.
            recorded = await self.issue_detail(issue_id)
            issue = recorded.get("issue", recorded) if isinstance(recorded, dict) else {}
            expected = self.approval_user_id if workflow_id and workflow_id == self.approval_workflow else self.issue_user_id
            binding = next((b for b in issue.get("sopBindings", []) if b.get("workflowId") == (workflow_id or issue.get("workflowId"))), {})
            if str(binding.get("assigneeUserId") or issue.get("assigneeUserId", "")) != str(expected):
                raise LoopForgeError("LoopForge 未确认工单的公用账号责任人，已停止派单")
        return await self._req("POST", f"/api/issues/{issue_id}/dispatch", json_body={"rerun": rerun, **({"workflowId": workflow_id} if workflow_id else {})})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/api/tasks/{quote(task_id, safe='')}")

    async def list_issue_tasks(self, issue_id: str) -> list[dict[str, Any]]:
        rows = await self._req("GET", "/api/tasks")
        if not isinstance(rows, list):
            raise LoopForgeError("原生 Task 列表格式无效", 502)
        return [r for r in rows if isinstance(r, dict) and r.get("issueId") == issue_id]

    async def list_issue_runs(self, issue_id: str) -> list[dict[str, Any]]:
        runs = await self._req("GET", f"/api/issues/{quote(issue_id, safe='')}/runs")
        if not isinstance(runs, list):
            raise LoopForgeError("原生运行历史格式无效", 502)
        return runs

    async def reopen_issue(self, issue_id: str) -> dict[str, Any]:
        return await self._req("POST", f"/api/issues/{issue_id}/reopen", json_body={})

    async def approve_run(self, run_id: str, comment: str = "", node_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"context": comment}
        if node_id:
            body["nodeId"] = node_id
        return await self._req("POST", f"/api/runs/{run_id}/approve", json_body=body)

    async def reject_run(self, run_id: str, reason: str, target_node: str = "", node_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"reason": reason}
        if target_node:
            body["targetId"] = target_node
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

    async def list_pending_approvals(self, project_id: str) -> list[dict[str, Any]]:
        """筛选 LoopForge 中正停在人工审批门禁（waiting / waiting_approval）的运行项，聚合进统一收件箱。"""
        if not project_id:
            return []
        runs = await self.list_runs()
        pending: list[dict[str, Any]] = []
        for r in runs:
            if run_project_id(r) != project_id:
                continue
            if r.get("status") not in ("waiting", "waiting_approval"):
                continue
            nodes = r.get("nodes") or {}
            wf_nodes = {n.get("id"): n for n in ((r.get("workflowSnapshot") or {}).get("nodes") or [])}
            waiting_nodes = [
                (nid, nr)
                for nid, nr in nodes.items()
                if isinstance(nr, dict) and nr.get("status") in ("waiting", "waiting_approval")
            ]
            for nid, nr in waiting_nodes or [("", {})]:
                wn = wf_nodes.get(nid, {})
                pending.append(
                    {
                        "source": "loopforge",
                        "run_id": r.get("id", ""),
                        "issue_id": (r.get("executionContext") or {}).get("issueId") or r.get("issueId", ""),
                        "workflow_id": r.get("workflowId", ""),
                        "workflow_name": (r.get("workflowSnapshot") or {}).get("name") or r.get("workflowId", ""),
                        "node_id": nid,
                        "node_name": wn.get("name") or nid or "人工门禁",
                        "reject_to": wn.get("rejectTo", ""),
                        "updated_at": nr.get("startedAt") or r.get("updatedAt") or "",
                    }
                )
        return pending
