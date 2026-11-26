import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev import public_pool
from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.cli_generic import DeepSeekHarness
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.server.app import create_app
from autodev.server.project_launcher import merge_runner_runtimes


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("VERNAL_POOL_DIR", str(root))
    monkeypatch.setenv("VERNAL_POOL_TOKEN", "public-account-token")
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf")
    public_pool.save({"deepseek_api_key": "real-test-key", "default_model": "coding", "reasoning_model": "planning"})
    (root / "image-ready").write_text("dsh version\nopencode version\n")
    return root


def test_pool_routes_readiness_and_models(pool):
    s = Settings(runtime_backend="loopforge", harness="auto")
    rt, info = merge_runner_runtimes({}, s, runners=[])
    assert info["effective_pool"] == "public_docker" and rt["dsh"]["ready"] and rt["opencode"]["ready"]
    local = [{"status": "online", "id": "mac", "runtimes": [{"provider": "codex", "binaryPath": "/host/codex"}]}]
    _, info = merge_runner_runtimes({}, s, runners=local)
    assert info["effective_pool"] == "local_runner" and info["pools"]["public_docker"]["available"]
    s.execution_pool = "public_docker"
    assert merge_runner_runtimes({}, s, runners=local)[0]["dsh"]["source"] == "public_docker"
    s.harness = "dsh"
    assert s.model_for("director") == "planning"
    assert DeepSeekHarness(s, "/bin/dsh").build_cmd("task", role="coder")[-3:] == ["--model", "coding", "task"]
    s.harness = "opencode"
    assert s.model_for("coder") == "coding"
    assert s.model_for("director") == "planning"
    s.execution_pool = "local_runner"
    assert not merge_runner_runtimes({}, s, runners=[])[0]["codex"]["ready"]


def test_pool_service_identity_and_ticket_only(pool, monkeypatch):
    # Simulate real LoopForge 0.4.0 behavior: Bearer tokens hit /api/auth/current and get guest (id=0),
    # while identity.json self-heals user_id via cookie login and confirms enrollment via /api/users.
    monkeypatch.delenv("VERNAL_POOL_TOKEN", raising=False)
    (pool / "identity.json").write_text(json.dumps({
        "login": "vernalpool17",
        "password": "pw-17",
        "token": "public-account-token",
    }))
    saved = {}
    routes = []
    pool_user_role = {"platformRole": ""}

    def upstream(req):
        token = req.headers.get("Authorization") or ""
        cookie = req.headers.get("Cookie") or ""
        routes.append((req.url.path, token, cookie))
        if req.url.path == "/api/runners":
            return httpx.Response(200, json=[])
        if req.url.path == "/api/projects":
            return httpx.Response(200, json=[{"id": "demo", "ownerId": 5, "members": [{"userId": 17, "role": "member"}]}])
        if req.url.path == "/api/auth/password" and req.method == "POST":
            body = json.loads(req.content)
            assert body == {"login": "vernalpool17", "password": "pw-17"}
            return httpx.Response(200, headers={"set-cookie": "session=pool17"}, json={"ok": True})
        if req.url.path == "/api/auth/current":
            if "session=pool17" in cookie:
                return httpx.Response(200, json={"user": {"id": 17, "loginName": "vernalpool17", "isGuest": False}})
            # Real LoopForge 0.4.0: Bearer API tokens without cookie always return guest id=0.
            return httpx.Response(200, json={"user": {"id": 0, "loginName": "guest", "isGuest": True}})
        if req.url.path == "/api/users":
            return httpx.Response(200, json=[
                {"id": 5, "loginName": "owner5", "platformRole": "admin"},
                {
                    "id": 17,
                    "loginName": "vernalpool17",
                    "platformRole": pool_user_role["platformRole"],
                    "agentCliCredentials": {
                        k: {"apiKeyConfigured": True, "baseUrl": v["baseUrl"], "protocol": v.get("protocol", "chat"), "models": v.get("models", [])}
                        for k, v in saved.items()
                    },
                },
            ])
        if req.url.path == "/api/users/17/credentials":
            assert token == "Bearer public-account-token" and b"real-test-key" not in req.content
            saved.update(json.loads(req.content)["agentCliCredentials"])
            return httpx.Response(200, json={"ok": True})
        assert False, req.url
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(upstream)))
    s = Settings(loopforge_url="http://lf", loopforge_project_id="demo", runtime_backend="loopforge", harness="auto")
    owner = LoopForgeClient("http://lf", "personal")
    route, shared = asyncio.run(public_pool.resolve(s, owner))
    assert route == "public_docker" and shared.token == "public-account-token"
    assert shared.issue_user_id == 17
    # Self-healed identity.json now persists user_id=17
    assert public_pool.identity()["user_id"] == 17
    assert saved["dsh"]["apiKey"] == public_pool.broker_key(17, "deepseek")
    assert saved["opencode"]["apiKey"] == public_pool.broker_key(17, "deepseek")
    assert saved["opencode"]["models"] == [{"id": "coding"}, {"id": "planning"}]
    assert not public_pool.valid_ticket("deepseek", saved["dsh"]["apiKey"])
    assert saved["dsh"]["baseUrl"].endswith("/pool-broker/17/deepseek")

    # Admin platformRole must be rejected fail-closed
    pool_user_role["platformRole"] = "admin"
    with pytest.raises(LoopForgeError, match="独立的普通执行账号"):
        asyncio.run(public_pool.resolve(s, owner))
    pool_user_role["platformRole"] = ""

    # Unbound external VERNAL_POOL_TOKEN without user_id must fail closed when /api/auth/current returns guest
    monkeypatch.setenv("VERNAL_POOL_TOKEN", "mystery-token")
    with pytest.raises(LoopForgeError, match="独立的普通执行账号"):
        asyncio.run(public_pool.resolve(s, owner))
    monkeypatch.delenv("VERNAL_POOL_TOKEN", raising=False)

    s.execution_pool = "local_runner"
    with pytest.raises(LoopForgeError, match="离线"):
        asyncio.run(public_pool.resolve(s, owner))
    s.execution_pool = "public_docker"
    s.loopforge_url = "https://another-server.invalid"
    with pytest.raises(LoopForgeError, match="当前部署"):
        asyncio.run(public_pool.resolve(s, owner))


def test_pool_config_persistence_admin_and_model_proxy(pool, tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / "workspace", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(collaboration_mode="files"))
    app = create_app(eng)
    with TestClient(app, client=("127.0.0.1", 10)) as local:
        url = "/vernal-api/runtime/public-pool"
        response = local.post(url, json={"deepseek_api_key": "second-test-key"})
        assert response.status_code == 200 and "second-test-key" not in response.text
        assert (pool / "settings.json").stat().st_mode & 0o777 == 0o600
        assert "second-test-key" not in local.get(url).text
        assert local.post(url, headers={"Origin": "https://other.invalid"}, json={}).status_code == 403
        assert local.post("/vernal-api/agent/config", json={"execution_pool": "public_docker"}).is_success
        assert ws.load_settings().execution_pool == "public_docker"
        token = public_pool.model_ticket("deepseek", int(time.time()) + 60)
        headers = {"Authorization": f"Bearer {token}"}
        route = "/vernal-api/pool-model/deepseek/v1/chat/completions"
        assert local.post(route, json={}).status_code == 401
        assert local.post(route, headers=headers, json={"model": "unauthorized"}).status_code == 422
        assert not public_pool.valid_ticket("openai", token)
        assert not public_pool.valid_ticket("deepseek", public_pool.model_ticket("deepseek", 1))
        real = httpx.AsyncClient
        def model(req):
            assert req.headers["authorization"] == "Bearer second-test-key"
            assert req.url.host == "api.deepseek.com"
            return httpx.Response(200, content=b"data: hello\n\n", headers={"content-type":"text/event-stream"})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(model)))
        assert local.post(route, headers=headers, json={"model": "coding"}).text == "data: hello\n\n"
    with TestClient(app, client=("192.0.2.10", 10)) as remote:
        assert remote.post(url, json={"deepseek_api_key": "illegal"}).status_code in (401, 403)
    eng.db.close()


def test_service_bootstrap_after_first_human_login(tmp_path, monkeypatch):
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.delenv("VERNAL_POOL_TOKEN", raising=False)
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf")
    registered = []
    def server(req):
        path = req.url.path
        if path == "/api/auth/current":
            return httpx.Response(200, json={"user":{"id": 1}})
        if path == "/api/auth/password":
            if not registered:
                return httpx.Response(401)
            assert json.loads(req.content) == registered[0]
            return httpx.Response(200, headers={"set-cookie":"session=dedicated"})
        if path == "/api/auth/register":
            registered.append(json.loads(req.content))
            return httpx.Response(201)
        assert path == "/api/auth/tokens" and req.headers["cookie"] == "session=dedicated"
        return httpx.Response(201, json={"token": "pool-secret", "metadata": {"userId": 29}})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**kw, transport=httpx.MockTransport(server)))
    client = LoopForgeClient("http://lf", "owner")
    async def provision_twice():
        await asyncio.gather(public_pool.provision(client), public_pool.provision(client))
    asyncio.run(provision_twice())
    assert len(registered) == 1 and public_pool.service_token() == "pool-secret"
    assert public_pool.identity().get("user_id") == 29
    assert "pool-secret" not in json.dumps(public_pool.status())


def test_admin_public_pool_write_from_team_browser(pool, tmp_path, monkeypatch):
    eng = Engine(init_workspace(tmp_path / "workspace", collaboration_mode="files"), settings=Settings())
    async def current(_self, cookie):
        return {"user":{"id":3, "loginName":"operator", "isAdmin":False}}
    monkeypatch.setattr(LoopForgeClient, "auth_current_with_cookie", current)
    with TestClient(create_app(eng), client=("192.0.2.5", 1)) as client:
        client.cookies.set("session", "login")
        path = "/vernal-api/runtime/public-pool"
        assert client.post(path, json={"default_model": "new"}).status_code == 403
        monkeypatch.setenv("VERNAL_POOL_ADMINS", "operator")
        assert client.post(path, json={"default_model":"new"}).is_success
    eng.db.close()


def test_unconfirmed_issue_owner_never_dispatches(monkeypatch):
    attempts = []
    def handler(req):
        attempts.append(req.method)
        return httpx.Response(200, json={"issue":{"responsibleUserId": 42}})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    client = LoopForgeClient("http://lf", "shared")
    client.issue_user_id = 18
    with pytest.raises(LoopForgeError, match="责任人"):
        asyncio.run(client.dispatch_issue("issue1"))
    assert attempts == ["GET"]


def test_two_project_members_use_their_own_execution_identity(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    settings = Settings(runtime_backend="loopforge", loopforge_url="http://lf",
                        loopforge_project_id="demo", loopforge_token="owner-stable-key")
    eng = Engine(init_workspace(tmp_path / "workspace", collaboration_mode="files"), settings=settings)
    from autodev.governance import upsert_member
    for member in ("alice", "bob"):
        upsert_member(eng.db, member, "architect", project="demo")
    calls = []
    async def cookie_user(self, cookie):
        return {"user": {"id": 1 if "alice" in cookie else 2, "login": "alice" if "alice" in cookie else "bob"}}
    async def owner(self):
        return {"user": {"id":1, "login":"alice"}}
    async def token(self, cookie, **kwargs):
        assert "bob" in cookie
        return {"token":"bob-personal-token"}
    async def assets(client, project, settings):
        calls.append((client.token, settings.effective_loopforge_token(), settings.project_signing_key()))
        return {}
    monkeypatch.setattr(LoopForgeClient, "auth_current_with_cookie", cookie_user)
    monkeypatch.setattr(LoopForgeClient, "auth_current", owner)
    monkeypatch.setattr(LoopForgeClient, "create_token_with_cookie", token)
    monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", AsyncMock(return_value=[{"id":"demo"}]))
    monkeypatch.setattr("autodev.server.app.ensure_vernal_loopforge_assets", assets)
    proxy = httpx.MockTransport(lambda req: pytest.fail(f"Unexpected proxy request {req.url.path}"))
    with TestClient(create_app(eng, proxy_transport=proxy), client=("192.0.2.9", 2)) as client:
        for who in ("alice", "bob", "alice"):
            client.cookies.set("session", who)
            assert client.post("/vernal-api/agent/config", json={"execution_pool":"local_runner"}).is_success
    signing = settings.project_signing_key()
    assert len(bytes.fromhex(signing)) == 32 and signing != "owner-stable-key"
    assert calls == [(f"session={who}", f"session={who}", signing) for who in ("alice", "bob", "alice")]
    eng.db.close()
