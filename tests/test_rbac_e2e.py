import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import escalada.api.live as live
from escalada.auth.service import create_access_token
from escalada.main import app


@pytest.fixture(autouse=True)
def patch_run_migrations(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr("escalada.main.run_migrations", _noop)
    yield


@pytest.fixture(autouse=True)
def patch_persist_state(monkeypatch):
    async def _noop(box_id, state, action, payload):
        return "ok"

    monkeypatch.setattr(live, "_persist_state", _noop)
    yield


@pytest.fixture(autouse=True)
def patch_ensure_state(monkeypatch):
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


@pytest.fixture(autouse=True)
def disable_validation(monkeypatch):
    old = live.VALIDATION_ENABLED
    monkeypatch.setattr(live, "VALIDATION_ENABLED", False)
    yield
    live.VALIDATION_ENABLED = old


@pytest.fixture
def client():
    return TestClient(app)


def _token(role: str, boxes=None) -> str:
    return create_access_token(
        username=f"user-{role}", role=role, assigned_boxes=boxes or []
    )


def test_cmd_requires_auth(client: TestClient):
    res = client.post("/api/cmd", json={"boxId": 1, "type": "INIT_ROUTE"})
    assert res.status_code == 401


def test_cmd_forbidden_box(client: TestClient):
    token = _token("judge", boxes=[2])
    res = client.post(
        "/api/cmd",
        headers={"Authorization": f"Bearer {token}"},
        json={"boxId": 1, "type": "INIT_ROUTE"},
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "forbidden_box"


def test_cmd_judge_allowed(client: TestClient):
    token = _token("judge", boxes=[1])
    payload = {"boxId": 1, "type": "INIT_ROUTE", "holdsCount": 5}
    res = client.post(
        "/api/cmd",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert 1 in live.state_map


def test_ws_judge_allowed(client: TestClient):
    token = _token("judge", boxes=[1])
    with client.websocket_connect(f"/api/ws/1?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "STATE_SNAPSHOT"
        assert msg["boxId"] == 1


def test_ws_judge_forbidden_box(client: TestClient):
    token = _token("judge", boxes=[2])
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/ws/1?token={token}") as ws:
            ws.receive_text()
    assert exc.value.code == 4403
