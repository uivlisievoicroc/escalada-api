import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import escalada.api.live as live
from escalada.auth.service import create_access_token
from escalada.main import app


@pytest.fixture(autouse=True)
def patch_run_migrations(monkeypatch):
    """Skip DB migrations during tests to avoid external dependencies."""

    async def _noop():
        return None

    monkeypatch.setattr("escalada.main.run_migrations", _noop)
    yield


@pytest.fixture(autouse=True)
def patch_persist_state(monkeypatch):
    """Avoid hitting the database when commands try to persist state."""

    async def _noop(box_id, state, action, payload):
        return "ok"

    monkeypatch.setattr(live, "_persist_state", _noop)
    yield


@pytest.fixture(autouse=True)
def patch_ensure_state(monkeypatch):
    """Avoid DB hydration during tests; ensure predictable state_map."""

    async def _fake(box_id: int):
        st = live._default_state()
        live.state_map[box_id] = st
        return st

    monkeypatch.setattr(live, "_ensure_state", _fake)
    yield


@pytest.fixture(autouse=True)
def reset_state_map():
    live.state_map.clear()
    yield
    live.state_map.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _token(role: str, boxes=None) -> str:
    return create_access_token(
        username=f"user-{role}", role=role, assigned_boxes=boxes or []
    )


def test_ws_requires_token(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws/1"):
            pass
    assert exc.value.code == 4401


def test_ws_forbidden_box(client: TestClient):
    token = _token("judge", boxes=[2])
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/ws/1?token={token}"):
            pass
    assert exc.value.code == 4403


def test_ws_admin_allowed(client: TestClient):
    token = _token("admin", boxes=[])
    with client.websocket_connect(f"/api/ws/3?token={token}") as ws:
        # Expect a snapshot message on connect
        msg = ws.receive_json()
        assert msg.get("type") == "STATE_SNAPSHOT"
        assert msg.get("boxId") == 3


def test_get_state_forbidden_box(client: TestClient):
    token = _token("judge", boxes=[2])
    res = client.get("/api/state/1", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403
    assert res.json()["detail"] == "forbidden_box"


def test_get_state_viewer_allowed(client: TestClient):
    token = _token("viewer", boxes=[1])
    res = client.get("/api/state/1", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["boxId"] == 1
    assert body["type"] == "STATE_SNAPSHOT"
