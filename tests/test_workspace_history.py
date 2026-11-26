import asyncio
import json

from fastapi.testclient import TestClient
from starlette.requests import Request

from autodev.engine import Engine, init_workspace
from autodev.server.app import create_app


def test_archive_pages_are_isolated_and_reach_first_event(tmp_path):
    base = tmp_path / "workspaces"
    current = Engine(init_workspace(base / "game", collaboration_mode="files"))
    archive = Engine(init_workspace(base / "old", collaboration_mode="files"))
    try:
        for i in range(127):
            archive.db.add_event("agent.command", f"step {i}", task_id="T-001")
        (base / "linked").symlink_to(archive.ws.root, target_is_directory=True)
        app = create_app(current)
        with TestClient(app, client=("127.0.0.1", 12345)) as web:
            assert web.get("/vernal-api/history/workspaces").json() == {
                "current": "game", "workspaces": ["game", "old"]}
            endpoint = "/vernal-api/history/events"
            params = {"workspace": "old", "limit": 60}
            page = web.get(endpoint, params=params).json()
            ids = []
            assert page["events"][0]["message"] == "step 126"
            while True:
                ids.extend(ev["event_id"] for ev in page["events"])
                if page["next_before"] is None:
                    break
                params["before"] = page["next_before"]
                page = web.get(endpoint, params=params).json()
            assert ids == list(range(archive.db.last_event_id(), 0, -1))
            assert web.get(endpoint, params={"workspace": "../old"}).status_code == 404
            assert len(web.get(endpoint, params={"workspace": "game"}).json()["events"]) == 1
        with TestClient(app, client=("192.0.2.2", 12345)) as web:
            assert web.get("/vernal-api/history/workspaces").status_code == 401
            assert web.get(endpoint, params={"workspace": "old"}).status_code == 401
    finally:
        archive.db.close()
        current.db.close()


def test_sse_replays_backlog_and_picks_up_another_writer(tmp_path):
    ws = init_workspace(tmp_path / "game", collaboration_mode="files")
    engine = Engine(ws)
    writer = Engine(ws)
    try:
        for i in range(515):
            writer.db.add_event("agent.command", str(i))
        endpoint = next(route.endpoint for route in create_app(engine).routes if route.path == "/vernal-api/events")

        async def check():
            req = Request({"type":"http", "headers":[(b"last-event-id", b"5")]})
            response = await endpoint(req, after=0)
            stream = response.body_iterator
            try:
                for expected in range(6, writer.db.last_event_id() + 1):
                    frame = await anext(stream)
                    assert json.loads(frame.split("data: ", 1)[1])["event_id"] == expected
                waiting = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                ev = writer.db.add_event("agent.file", "updated after connecting")
                frame = await asyncio.wait_for(waiting, timeout=6)
                if frame.startswith(":"):
                    frame = await asyncio.wait_for(anext(stream), timeout=2)
                assert f"id: {ev['event_id']}\n" in frame
            finally:
                await stream.aclose()

        asyncio.run(check())
    finally:
        writer.db.close()
        engine.db.close()
