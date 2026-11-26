"""R2 regressions: distinguish service liveness from upstream schema readiness and preserve filters/isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.loopforge.client import LoopForgeClient
from autodev.server.app import create_app


def test_telemetry_schema_probe_and_proxy_preserve_filter_and_surface_mismatch(tmp_path: Path) -> None:
    seen_queries: list[str] = []

    def upstream(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if p == "/api/runs/run-demo-1/telemetry":
            seen_queries.append(req.url.query.decode("utf-8"))
            if "adapter=" in req.url.query.decode("utf-8"):
                return httpx.Response(
                    400,
                    json={"error": "Error 1054 (42S22): Unknown column 'adapter' in 'where clause'"},
                )
            return httpx.Response(200, json={"events": []})
        if p == "/api/runs":
            return httpx.Response(
                200,
                json=[
                    {"id": "run-demo-1", "projectId": "proj-a", "status": "succeeded"},
                    {"id": "run-other-2", "projectId": "proj-b", "status": "succeeded"},
                ],
            )
        if p in ("/api/projects", "/api/workflows", "/api/agents", "/api/sandboxes", "/api/executors", "/api/runners"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    ws = init_workspace(tmp_path / "r2", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://127.0.0.1:8090",
        loopforge_token="tok-r2",
        loopforge_project_id="proj-a",
    )
    eng = Engine(ws, settings=settings)
    transport = httpx.MockTransport(upstream)
    real_async = httpx.AsyncClient
    try:
        with patch(
            "autodev.loopforge.client.httpx.AsyncClient",
            lambda **kw: real_async(**{**kw, "transport": transport}),
        ):
            client_lf = LoopForgeClient(settings.loopforge_url, settings.loopforge_token)
            diag = asyncio.run(client_lf.check_telemetry_schema("run-demo-1"))
            assert diag["checked"] is True
            assert diag["compatible"] is False
            assert diag["code"] == "UPSTREAM_SCHEMA_COLUMN_MISSING"
            assert diag["table"] == "run_telemetry" and diag["column"] == "adapter"

            with TestClient(create_app(eng, proxy_transport=transport)) as web:
                # 1. Runtime status distinguishes liveness (reachable=True) from schema compatibility (schema_ready=False)
                # and only probes runs belonging to the active project (never leaks proj-b runs).
                status = web.get("/vernal-api/runtime/status").json()
                assert status["loopforge"]["reachable"] is True
                assert status["loopforge"]["schema_ready"] is False
                assert status["loopforge"]["schema_diagnostics"]["code"] == "UPSTREAM_SCHEMA_COLUMN_MISSING"
                assert [r["id"] for r in status["recent_runs"]] == ["run-demo-1"]

                # 2. Proxy MUST preserve the adapter query parameter and status code (never fake empty 200),
                # while exposing a structured diagnostic header.
                proxied = web.get("/api/runs/run-demo-1/telemetry?limit=100&adapter=live&after=0")
                assert proxied.status_code == 400
                assert "Unknown column 'adapter'" in proxied.text
                assert proxied.headers.get("x-vernal-upstream-diagnostic") == "upstream-schema-column-missing:run_telemetry.adapter"
                assert any("adapter=live" in q for q in seen_queries)
    finally:
        eng.db.close()
